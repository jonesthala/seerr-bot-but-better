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
from typing import Any

from .seerr import USER_AGENT, Fetch, http_fetch

log = logging.getLogger("seerrbot.jellyfin")

NAME_RE = re.compile(r"[A-Za-z0-9._-]{3,20}")
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/l/I
PASSWORD_LENGTH = 14
UNREACHABLE = "Jellyfin ist gerade nicht erreichbar. Versuch es in ein paar Minuten nochmals."
TAILSCALE_ADMIN = "https://login.tailscale.com/admin/machines"


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
