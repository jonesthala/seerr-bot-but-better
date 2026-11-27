"""Admin panel in the browser: sessions, login limits, the Jellyfin login and the data the pages show.

No aiohttp or discord here, so all of it can be tested anywhere; the web server itself is in web.py.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .arr import MOVING, STATE_ORDER, Progress
from .core import Title, eta_text, format_numbers, parse_time
from .extras import episode_label, episode_state, season_episodes, season_numbers, WEEKDAYS_SHORT
from .seerr import http_fetch
from .store import Follow, Friend, WatchQuestion

SESSION_COOKIE = "panel_session"
SESSION_DAYS = 30
LOGIN_TRIES = 5  # failed logins per address ...
LOGIN_WINDOW = 15 * 60  # ... within this many seconds, then it has to wait
KEY_FILE = "panel.key"
AUTH_HEADER = 'MediaBrowser Client="seerr bot panel", Device="Admin-Panel", DeviceId="seerrbot-panel", Version="1.0"'
QBIT_NO_ETA = 8640000  # qBittorrent's "infinite"

Fetch = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, Any]]


class PanelError(Exception):
    """A message for the panel as it is (wrong input, nothing found, ...)."""


# --- sessions -------------------------------------------------------------------------------
def load_key(directory: str | None) -> bytes:
    """The secret that signs the login cookies. Made once and kept in DATA_DIR/panel.key (only the bot can read
    it), so a restart doesn't log anyone out. Deleting the file logs everyone out."""
    if not directory:
        return secrets.token_bytes(32)
    path = Path(directory) / KEY_FILE
    try:
        key = bytes.fromhex(path.read_text(encoding="ascii").strip())
        if len(key) >= 32:
            return key
    except (OSError, ValueError):
        pass
    key = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as file:
        file.write(key.hex())
    return key


def _mac(key: bytes, payload: str) -> str:
    return hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()


def sign_session(name: str, key: bytes, now: float, days: int = SESSION_DAYS) -> str:
    raw = json.dumps({"u": name, "e": int(now + days * 86400)}, separators=(",", ":")).encode("utf-8")
    payload = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{payload}.{_mac(key, payload)}"


def read_session(value: str | None, key: bytes, now: float) -> str | None:
    """The name in a valid, unexpired cookie; None for anything else."""
    if not value or value.count(".") != 1:
        return None
    payload, mac = value.split(".")
    try:
        if not hmac.compare_digest(mac, _mac(key, payload)):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, UnicodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("u"), str) or not isinstance(data.get("e"), int):
        return None
    return data["u"] if data["e"] > now else None


class LoginLimiter:
    """At most LOGIN_TRIES failed logins per address within LOGIN_WINDOW seconds."""

    def __init__(self, tries: int = LOGIN_TRIES, window: int = LOGIN_WINDOW) -> None:
        self.tries, self.window = tries, window
        self.failures: dict[str, list[float]] = {}

    def wait(self, address: str, now: float) -> int:
        """Seconds until this address may try again; 0 if it may now."""
        recent = [t for t in self.failures.get(address, []) if now - t < self.window]
        self.failures[address] = recent
        if len(recent) < self.tries:
            return 0
        return max(1, int(recent[0] + self.window - now))

    def failed(self, address: str, now: float) -> None:
        self.failures.setdefault(address, []).append(now)

    def succeeded(self, address: str) -> None:
        self.failures.pop(address, None)


def jellyfin_login(base_url: str, name: str, password: str, fetch: Fetch = http_fetch) -> tuple[str | None, str]:
    """Checks name and password with Jellyfin. (the user's name if they are a Jellyfin administrator, problem).
    The session Jellyfin opens for this is closed again right away; the password is not kept anywhere."""
    base = base_url.rstrip("/")
    headers = {"Authorization": AUTH_HEADER, "Content-Type": "application/json", "Accept": "application/json"}
    body = json.dumps({"Username": name, "Pw": password}).encode("utf-8")
    try:
        status, data = fetch("POST", f"{base}/Users/AuthenticateByName", headers, body, 15.0)
    except (OSError, ValueError):
        return None, "Jellyfin ist gerade nicht erreichbar. Versuch es in ein paar Minuten nochmals."
    if status in (400, 401, 403, 404):
        return None, "Benutzername oder Passwort stimmt nicht."
    if status != 200 or not isinstance(data, dict):
        return None, "Jellyfin hat die Anmeldung nicht angenommen. Versuch es nochmals."
    token = data.get("AccessToken")
    if isinstance(token, str) and token:
        try:
            fetch("POST", f"{base}/Sessions/Logout", {"Authorization": f'{AUTH_HEADER}, Token="{token}"'}, None, 10.0)
        except (OSError, ValueError):
            pass
    user = data.get("User") if isinstance(data.get("User"), dict) else {}
    policy = user.get("Policy") if isinstance(user.get("Policy"), dict) else {}
    if policy.get("IsAdministrator") is not True:
        return None, "Ins Admin-Panel dürfen nur Jellyfin-Administratoren."
    return str(user.get("Name") or name), ""


# --- what the pages show ------------------------------------------------------------------------
def title_data(title: Title) -> dict[str, Any]:
    return {
        "kind": title.kind,
        "tmdb_id": title.tmdb_id,
        "name": title.name,
        "year": title.year,
        "label": title.label,
        "overview": title.overview,
        "poster": title.poster,
        "status": title.status,
        "status_text": title.status_text(),
        "in_library": title.in_library,
        "requestable": title.requestable,
        "runtime": title.runtime,
        "seasons": [{"number": s.number, "episodes": s.episodes, "status": s.status, "requestable": s.requestable}
                    for s in title.seasons if s.number > 0],
        "requestable_seasons": title.requestable_seasons(),
    }


def progress_data(kind: str, progress: Progress, live: dict[str, dict], now: datetime) -> dict[str, Any]:
    """One title's downloads as numbers: overall percent, GB, speed, time left, and every download."""
    downloads = sorted(progress.downloads, key=lambda d: (STATE_ORDER.get(d.state, 9), -d.percent))
    moving = [d for d in downloads if d.state in MOVING]
    if kind == "tv" and progress.total:
        done = progress.have + sum(d.percent / 100 * max(d.episodes, 1) for d in moving)
        percent = round(100 * done / progress.total)
    elif progress.have:
        percent = 100
    else:
        percent = moving[0].percent if moving else 0
    etas = [d.eta for d in moving if d.eta]
    rows = []
    for d in downloads:
        info = live.get(d.hash) or {}
        speed = info.get("dlspeed") if isinstance(info.get("dlspeed"), int) else 0
        rows.append({"what": d.what, "percent": d.percent, "size": d.size, "left": d.left, "state": d.state,
                     "message": d.message, "speed": speed, "episodes": d.episodes,
                     "eta_text": eta_text(d.eta, now) if d.state == "downloading" else ""})
    return {
        "percent": max(0, min(100, percent)),
        "size": sum(d.size for d in moving),
        "loaded": sum(d.size - d.left for d in moving),
        "speed": sum(r["speed"] for r in rows if r["state"] in MOVING),
        "eta_text": eta_text(max(etas), now) if etas else "",
        "have": progress.have,
        "total": progress.total,
        "upcoming": progress.upcoming,
        "searching": progress.searching,
        "missing": [{"season": s, "text": format_numbers(e), "count": len(e)} for s, e in sorted(progress.missing.items())],
        "downloads": rows,
    }


_TORRENT_STATES = {
    "downloading": ("downloading", "forcedDL", "metaDL", "forcedMetaDL"),
    "stalled": ("stalledDL",),
    "paused": ("pausedDL", "stoppedDL"),
    "queued": ("queuedDL", "checkingDL", "allocating", "checkingResumeData", "moving"),
    "seeding": ("uploading", "stalledUP", "forcedUP", "queuedUP", "checkingUP"),
    "done": ("pausedUP", "stoppedUP"),
    "error": ("error", "missingFiles"),
}
TORRENT_GROUP = {state: group for group, states in _TORRENT_STATES.items() for state in states}


def torrent_data(t: dict) -> dict[str, Any]:
    """A qBittorrent torrent for the downloads page."""
    def number(key: str) -> int:
        value = t.get(key)
        return int(value) if isinstance(value, (int, float)) else 0

    progress = t.get("progress") if isinstance(t.get("progress"), (int, float)) else 0
    eta = number("eta")
    state = str(t.get("state") or "")
    return {
        "hash": str(t.get("hash") or ""),
        "name": str(t.get("name") or "?"),
        "percent": round(100 * progress, 1),
        "size": number("size") or number("total_size"),
        "left": number("amount_left"),
        "speed": number("dlspeed"),
        "upspeed": number("upspeed"),
        "eta": eta if 0 < eta < QBIT_NO_ETA else None,
        "state": TORRENT_GROUP.get(state, "other"),
        "raw_state": state,
        "category": str(t.get("category") or ""),
        "seeds": number("num_seeds"),
        "added": number("added_on"),
    }


def episode_rows(episodes: list[dict], loading: set[int], now: datetime, zone: Any, anime: bool) -> list[dict[str, Any]]:
    """Every episode of a show for the picker: label with title, state, and whether it can still be fetched."""
    rows = []
    for season in season_numbers(episodes):
        for e in season_episodes(episodes, season):
            when = parse_time(e.get("airDateUtc"))
            if e.get("hasFile"):
                state = "have"
            elif e["id"] in loading:
                state = "loading"
            elif when is None or when > now:
                state = "upcoming"
            else:
                state = "missing"
            rows.append({"id": e["id"], "season": season, "episode": e.get("episodeNumber") or 0,
                         "label": episode_label(e, anime), "state": state,
                         "state_text": episode_state(e, loading, now, zone), "monitored": bool(e.get("monitored"))})
    return rows


def season_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seasons: dict[int, dict[str, Any]] = {}
    for row in rows:
        info = seasons.setdefault(row["season"], {"number": row["season"], "total": 0, "have": 0, "open": 0})
        info["total"] += 1
        info["have"] += row["state"] == "have"
        info["open"] += row["state"] in ("missing", "upcoming")
    return list(seasons.values())


def calendar_days(episodes: list[dict], movies: list[dict], loading: set[int], loading_movies: set[int],
                  start: datetime, end: datetime, now: datetime, zone: Any) -> list[dict[str, Any]]:
    """Like /kalender, by day: monitored episodes with time and state, movies on their digital release day."""
    entries: list[tuple[datetime, dict[str, Any]]] = []
    for e in episodes:
        when = parse_time(e.get("airDateUtc"))
        show = e.get("series") if isinstance(e.get("series"), dict) else {}
        if when is None or not start <= when < end or not isinstance(e.get("seasonNumber"), int):
            continue
        what = f"S{e['seasonNumber']:02d}E{e.get('episodeNumber', 0):02d}"
        if isinstance(e.get("absoluteEpisodeNumber"), int) and show.get("seriesType") == "anime":
            what += f" · Folge {e['absoluteEpisodeNumber']}"
        state = ("have" if e.get("hasFile") else "loading" if e.get("id") in loading
                 else "searching" if when <= now else "waiting")
        entries.append((when, {"kind": "tv", "title": str(show.get("title") or "?"), "what": what,
                               "episode_title": str(e.get("title") or "") if str(e.get("title") or "").upper() != "TBA" else "",
                               "time": when.astimezone(zone).strftime("%H:%M"), "state": state}))
    for m in movies:
        when = parse_time(m.get("digitalRelease"))
        if when is None or not start <= when < end:
            continue
        state = "have" if m.get("hasFile") else "loading" if m.get("id") in loading_movies else "waiting"
        year = f" ({m['year']})" if m.get("year") else ""
        entries.append((when, {"kind": "movie", "title": f"{m.get('title', '?')}{year}", "what": "erscheint digital",
                               "episode_title": "", "time": None, "state": state}))
    days: list[dict[str, Any]] = []
    for when, item in sorted(entries, key=lambda x: x[0]):
        local = when.astimezone(zone)
        key = local.date().isoformat()
        if not days or days[-1]["date"] != key:
            today = now.astimezone(zone).date()
            name = ("Heute" if local.date() == today else "Morgen" if local.date() == today + timedelta(days=1)
                    else WEEKDAYS_SHORT[local.weekday()])
            days.append({"date": key, "label": f"{name} {local:%d.%m.}", "items": []})
        days[-1]["items"].append(item)
    return days


def watched_data(q: WatchQuestion) -> dict[str, Any]:
    return {"id": q.qid, "kind": q.kind, "label": q.label, "episodes": [f"S{s:02d}E{e:02d}" for s, e in q.episodes],
            "viewers": q.viewers, "size": q.size, "created": q.created, "text": q.text}


def follow_request_data(f: Follow) -> dict[str, Any]:
    return {"id": f.fid, "label": f.label, "user": f.user_name, "start": f.start_text, "new_seasons": f.new_seasons,
            "created": f.created}


def friend_data(f: Friend) -> dict[str, Any]:
    return {"id": f.user_id, "user": f.user_name, "jellyfin_name": f.jellyfin_name, "created": f.created,
            "existing": bool(f.jellyfin_id), "web": f.from_web, "renew": f.renew}


def follow_data(f: Follow, upcoming: dict | None, zone: Any) -> dict[str, Any]:
    nxt = None
    if upcoming is not None:
        when = parse_time(upcoming.get("airDateUtc"))
        if when is not None:
            local = when.astimezone(zone)
            nxt = {"code": f"S{upcoming.get('seasonNumber', 0):02d}E{upcoming.get('episodeNumber', 0):02d}",
                   "title": str(upcoming.get("title") or ""),
                   "when": f"{WEEKDAYS_SHORT[local.weekday()]} {local:%d.%m.%Y, %H:%M}"}
    return {"id": f.fid, "tmdb_id": f.tmdb_id, "label": f.label, "start": f.start_text, "user": f.user_name,
            "new_seasons": f.new_seasons, "next": nxt}


def number_list(value: Any, limit: int = 2000) -> list[int]:
    """A list of positive whole numbers from a JSON body (episode ids, seasons); anything else is an error."""
    if not isinstance(value, list) or len(value) > limit or not all(type(v) is int and v >= 0 for v in value):
        raise PanelError("Die Auswahl ist ungültig.")
    return value
