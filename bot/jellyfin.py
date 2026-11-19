"""Jellyfin accounts for friends: create, new password, block. Standard library only.

Passwords are made when the friend asks for them and are never stored or logged.
The HTTP call is injectable, so the tests run without a network or a Jellyfin server.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .core import format_numbers, parse_time
from .seerr import USER_AGENT, Fetch, http_fetch

log = logging.getLogger("seerrbot.jellyfin")

NAME_RE = re.compile(r"[A-Za-z0-9._-]{3,20}")
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/l/I
PASSWORD_LENGTH = 14
UNREACHABLE = "Jellyfin ist gerade nicht erreichbar. Versuch es in ein paar Minuten nochmals."
TAILSCALE_ADMIN = "https://login.tailscale.com/admin/machines"


@dataclass(frozen=True)
class Usage:
    """One movie or show in the library: how much space it takes and when anyone last watched it."""
    name: str
    kind: str  # "movie" or "tv"
    size: int  # bytes
    files: int
    tmdb_id: int | None
    last_played: datetime | None  # None: nobody started it yet
    viewers: int  # how many Jellyfin users watched (part of) it
    hours: float = 0.0  # running time of all its files

    @property
    def gb_per_hour(self) -> float | None:
        return self.size / 1024 ** 3 / self.hours if self.hours >= 0.25 and self.size else None


def _tmdb(item: dict) -> int | None:
    value = (item.get("ProviderIds") or {}).get("Tmdb")
    return int(value) if isinstance(value, str) and value.isdigit() else None


def _label(item: dict) -> str:
    year = item.get("ProductionYear")
    return f"{item.get('Name', '?')} ({year})" if year else str(item.get("Name", "?"))


def library_usage(items: list[dict], series: list[dict], watched: dict[str, list[dict]]) -> list[Usage]:
    """Movies and shows by size, largest first. items: movies and episodes with MediaSources; series: the shows;
    watched: per Jellyfin user, the items with their UserData."""
    shows = {s.get("Id"): s for s in series if isinstance(s, dict)}
    groups: dict[str, dict] = {}
    owner: dict[str, str] = {}  # item id → group id
    for item in items:
        if not isinstance(item, dict) or not item.get("Id"):
            continue
        episode = item.get("Type") == "Episode" and item.get("SeriesId")
        key = item["SeriesId"] if episode else item["Id"]
        source = shows.get(key, {"Name": item.get("SeriesName"), "Id": key}) if episode else item
        group = groups.setdefault(key, {"name": _label(source), "kind": "tv" if episode else "movie",
                                        "tmdb": _tmdb(source), "size": 0, "files": 0, "last": None, "viewers": set(),
                                        "hours": 0.0})
        sources = [m for m in item.get("MediaSources") or [] if isinstance(m, dict)]
        group["size"] += sum(m.get("Size") or 0 for m in sources if isinstance(m.get("Size"), int))
        group["files"] += 1
        ticks = item.get("RunTimeTicks")
        group["hours"] += ticks / 1e7 / 3600 if isinstance(ticks, int) and ticks > 0 else 0.0
        owner[item["Id"]] = key
    for user_id, seen in watched.items():
        for item in seen:
            if not isinstance(item, dict):
                continue
            data = item.get("UserData") or {}
            group = groups.get(owner.get(item.get("Id"), ""))
            if group is None or not (data.get("Played") or data.get("PlaybackPositionTicks")):
                continue
            group["viewers"].add(user_id)
            moment = parse_time(data.get("LastPlayedDate"))
            if moment and (group["last"] is None or moment > group["last"]):
                group["last"] = moment
    found = [Usage(g["name"], g["kind"], g["size"], g["files"], g["tmdb"], g["last"], len(g["viewers"]), g["hours"])
             for g in groups.values()]
    return sorted(found, key=lambda u: u.size, reverse=True)


class JellyfinError(Exception):
    """An error whose message can be shown to Discord users as it is."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def new_password() -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(PASSWORD_LENGTH))


def suggest_name(display_name: str) -> str:
    """A Jellyfin user name from a Discord name: allowed characters only, 3 to 20 long."""
    name = re.sub(r"[^A-Za-z0-9._-]", "", display_name.replace(" ", "."))[:20].strip("._-")
    return name if len(name) >= 3 else ""


def check_name(name: str) -> str | None:
    """Why this user name is not allowed, or None if it is fine."""
    if not NAME_RE.fullmatch(name):
        return "Der Benutzername braucht 3 bis 20 Zeichen: Buchstaben, Zahlen, Punkt, Strich oder Unterstrich."
    return None


def friend_policy(policy: dict, folder_ids: list[str], bitrate: int) -> dict:
    """The policy for a friend, built on Jellyfin's own (it requires fields we don't touch)."""
    return {
        **policy,
        "IsAdministrator": False,
        "IsDisabled": False,
        "IsHidden": True,  # not listed on the login page
        "EnableAllFolders": False,
        "EnabledFolders": list(folder_ids),
        "EnableContentDeletion": False,
        "EnableContentDeletionFromFolders": [],
        "EnableRemoteAccess": True,
        "RemoteClientBitrateLimit": bitrate,
    }


def _error_text(status: int) -> str:
    if status == 401:
        return "Jellyfin lehnt den API-Schlüssel ab. Ein Admin muss `JELLYFIN_API_KEY` prüfen."
    if status == 403:
        return "Jellyfin erlaubt das nicht. Der API-Schlüssel braucht Admin-Rechte."
    if status == 404:
        return "Das Jellyfin-Konto gibt es nicht (mehr)."
    return f"Jellyfin meldet einen Fehler (HTTP {status}). Ein Admin findet die Details im Log."


class JellyfinClient:
    def __init__(self, base_url: str, api_key: str, libraries: tuple[str, ...] = (), bitrate: int = 4_000_000,
                 fetch: Fetch = http_fetch) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.libraries = libraries
        self.bitrate = bitrate
        self._fetch = fetch

    def _call(self, method: str, path: str, query: dict[str, str] | None = None, body: Any = None) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {
            "Authorization": f'MediaBrowser Token="{self.api_key}"',
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        try:
            status, answer = self._fetch(method, url, headers, data)
        except Exception as exc:  # noqa: BLE001 - timeouts, DNS, refused
            log.warning("%s %s failed: %s", method, path, exc)
            raise JellyfinError(UNREACHABLE) from exc
        if 200 <= status < 300:
            return answer
        log.warning("%s %s → HTTP %s", method, path, status)  # never the body: it could echo a password
        raise JellyfinError(_error_text(status), status)

    # --- reading -----------------------------------------------------------
    def users(self) -> list[dict]:
        data = self._call("GET", "/Users")
        return [u for u in data if isinstance(u, dict)] if isinstance(data, list) else []

    def find_user(self, name: str) -> dict | None:
        wanted = name.casefold()
        return next((u for u in self.users() if str(u.get("Name", "")).casefold() == wanted), None)

    def usage(self) -> list[Usage]:
        """What takes the most space, and who watched it (for /speicher)."""
        def items(**query: str) -> list[dict]:
            data = self._call("GET", "/Items", {"Recursive": "true", **query})
            found = data.get("Items") if isinstance(data, dict) else None
            return [i for i in found if isinstance(i, dict)] if isinstance(found, list) else []

        files = items(IncludeItemTypes="Movie,Episode", Fields="MediaSources,ProviderIds")
        series = items(IncludeItemTypes="Series", Fields="ProviderIds")
        watched = {str(u.get("Id")): items(userId=str(u.get("Id")), IncludeItemTypes="Movie,Episode", Fields="UserData")
                   for u in self.users() if u.get("Id")}
        return library_usage(files, series, watched)

    def size_rates(self) -> list[float]:
        """GB per hour of running time of each movie/show in the library (for the size estimate)."""
        data = self._call("GET", "/Items", {"Recursive": "true", "IncludeItemTypes": "Movie,Episode",
                                           "Fields": "MediaSources"})
        found = data.get("Items") if isinstance(data, dict) else None
        files = [i for i in found or [] if isinstance(i, dict)]
        return [rate for u in library_usage(files, [], {}) if (rate := u.gb_per_hour) is not None]

    def series_list(self) -> list[tuple[str, str]]:
        """(Jellyfin id, 'Name (Jahr)') of every show in the library, by name (for /abo)."""
        data = self._call("GET", "/Items", {"Recursive": "true", "IncludeItemTypes": "Series", "SortBy": "SortName"})
        found = data.get("Items") if isinstance(data, dict) else None
        return [(str(s["Id"]), _label(s)) for s in found or [] if isinstance(s, dict) and s.get("Id")]

    def episodes(self, series_id: str) -> dict[str, str]:
        """'S01E05' → episode title, for every episode of a show that is in the library."""
        data = self._call("GET", "/Items", {"Recursive": "true", "IncludeItemTypes": "Episode", "ParentId": series_id})
        found = data.get("Items") if isinstance(data, dict) else None
        keys = {}
        for item in found or []:
            key = episode_key(item) if isinstance(item, dict) else None
            if key:
                keys[key] = str(item.get("Name") or "")
        return keys

    def folder_ids(self) -> list[str]:
        """Ids of the libraries friends may see (JELLYFIN_FRIEND_LIBRARIES); all if none are named."""
        data = self._call("GET", "/Library/VirtualFolders")
        folders = {str(f.get("Name", "")).casefold(): f.get("ItemId") for f in data if isinstance(f, dict)} \
            if isinstance(data, list) else {}
        if not self.libraries:
            return [i for i in folders.values() if i]
        missing = [name for name in self.libraries if not folders.get(name.casefold())]
        if missing:
            raise JellyfinError(f"Diese Jellyfin-Bibliotheken gibt es nicht: {', '.join(missing)}. "
                                "Ein Admin muss `JELLYFIN_FRIEND_LIBRARIES` prüfen.")
        return [folders[name.casefold()] for name in self.libraries]

    # --- changing ------------------------------------------------------------
    def create_friend(self, name: str) -> str:
        """Creates the account with friend rights and a password nobody knows; returns its id."""
        if self.find_user(name) is not None:
            raise JellyfinError(f"Den Jellyfin-Benutzer **{name}** gibt es schon.", 409)
        folders = self.folder_ids()
        user = self._call("POST", "/Users/New", body={"Name": name, "Password": new_password()})
        if not isinstance(user, dict) or not user.get("Id"):
            raise JellyfinError("Jellyfin hat unerwartet geantwortet. Ein Admin findet die Details im Log.")
        try:
            self._call("POST", f"/Users/{user['Id']}/Policy",
                       body=friend_policy(user.get("Policy") or {}, folders, self.bitrate))
        except JellyfinError:
            # Without the friend policy the account would see everything: remove it again.
            try:
                self._call("DELETE", f"/Users/{user['Id']}")
            except JellyfinError:
                log.error("Could not remove the half-made Jellyfin user %s, please delete it by hand", name)
            raise
        return str(user["Id"])

    def set_access(self, user_id: str, allowed: bool) -> None:
        """Blocks the account, or opens it again with friend rights."""
        user = self._call("GET", f"/Users/{user_id}")
        policy = user.get("Policy") if isinstance(user, dict) else None
        if not isinstance(policy, dict):
            raise JellyfinError("Jellyfin hat unerwartet geantwortet. Ein Admin findet die Details im Log.")
        policy = friend_policy(policy, self.folder_ids(), self.bitrate) if allowed else {**policy, "IsDisabled": True}
        self._call("POST", f"/Users/{user_id}/Policy", body=policy)

    def reset_password(self, user_id: str) -> str:
        """Sets a new random password and returns it (only to be shown to the friend)."""
        password = new_password()
        self._call("POST", "/Users/Password", {"userId": user_id}, body={"NewPw": password, "ResetPassword": False})
        return password


def episode_key(item: dict) -> str | None:
    """'S01E05' for an episode, None if Jellyfin doesn't know its numbers."""
    season, episode = item.get("ParentIndexNumber"), item.get("IndexNumber")
    if not isinstance(season, int) or not isinstance(episode, int):
        return None
    return f"S{season:02d}E{episode:02d}"


def new_episodes_text(show: str, new: dict[str, str]) -> str:
    """The DM for subscribers: one episode with its title, or several grouped by season."""
    if len(new) == 1:
        key, title = next(iter(new.items()))
        return f"📺 Neue Folge von **{show}**: {key}" + (f" «{title}»" if title else "") + " ist jetzt in Jellyfin."
    seasons: dict[int, list[int]] = {}
    for key in new:
        season, episode = re.fullmatch(r"S(\d+)E(\d+)", key).groups()  # type: ignore[union-attr]
        seasons.setdefault(int(season), []).append(int(episode))
    parts = [f"Staffel {s}: Folge {format_numbers(numbers)}" for s, numbers in sorted(seasons.items())]
    return f"📺 **{len(new)} neue Folgen** von **{show}** sind jetzt in Jellyfin ({'; '.join(parts)})."


def usage_line(rank: int, usage: Usage, size: str, who: str | None, zone: Any) -> str:
    """'3. **Loki (2021)** · 22 GB · 6 Dateien · 👀 zuletzt 15.11.2026, 2 Personen · von alex' for /speicher."""
    files = "1 Datei" if usage.files == 1 else f"{usage.files} Dateien"
    if usage.last_played is None:
        seen = "💤 noch nie geschaut"
    else:
        people = "1 Person" if usage.viewers == 1 else f"{usage.viewers} Personen"
        seen = f"👀 zuletzt {usage.last_played.astimezone(zone).strftime('%d.%m.%Y')}, {people}"
    return f"{rank}. **{usage.name}** · {size} · {files} · {seen}" + (f" · von {who}" if who else "")


def welcome_text(name: str, password: str, server_url: str, share_url: str) -> str:
    """Everything a friend needs, shown only to them."""
    return (
        "🎬 **Dein Zugang zu Jellyfin**\n\n"
        "**1. Tailscale installieren** (privater Tunnel, du kommst damit nur an Jellyfin)\n"
        "iPhone/Android: App «Tailscale», Windows/Mac: <https://tailscale.com/download>\n"
        "App öffnen und mit Google, Microsoft oder Apple anmelden.\n\n"
        f"**2. Einladung annehmen:** {share_url}\n"
        "Mit demselben Konto anmelden wie in der App, dann «Accept invite».\n\n"
        "**3. Jellyfin-App installieren**\n"
        "iPhone: «Jellyfin Mobile» oder «Swiftfin», Android: «Jellyfin», TV: «Jellyfin for Android TV», "
        "PC: im Browser\n\n"
        "**4. Verbinden** (Tailscale muss eingeschaltet sein)\n"
        f"Server: `{server_url}`\nBenutzer: `{name}`\nPasswort: `{password}`\n\n"
        "📺 **Am Fernseher** musst du kein Passwort tippen: In der TV-App «Quick Connect» wählen, dann am Handy "
        "in der Jellyfin-App unter deinem Profil → «Quick Connect» den angezeigten Code eingeben.\n"
        "🍿 **Zusammen schauen:** Mit SyncPlay (Gruppen-Symbol oben in der App) schaut ihr denselben Film "
        "gleichzeitig, jeder bei sich. Wer pausiert, pausiert für alle.\n\n"
        "Speichere das Passwort, diese Nachricht verschwindet. Ein neuer Klick auf den Knopf setzt ein neues "
        "Passwort, das alte gilt dann nicht mehr. Bitte gib Link und Login nicht weiter. Viel Spass! 🍿"
    )
