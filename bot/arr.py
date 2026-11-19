"""Sonarr and Radarr, read only. Standard library only; the HTTP call is injectable for the tests.

- which downloads belonged to a title (torrent hashes, for /löschen)
- what is going on right now (for /status): episodes there, missing and still to come, the downloads with
  their state, and whether a search is running
"""
from __future__ import annotations

import logging
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .core import eta_text, format_numbers, parse_time, shorten
from .seerr import USER_AGENT, Fetch, http_fetch

log = logging.getLogger("seerrbot.arr")

SEARCHES = {"EpisodeSearch", "SeasonSearch", "SeriesSearch", "MissingEpisodeSearch", "CutoffUnmetEpisodeSearch",
            "MoviesSearch", "MissingMoviesSearch", "CutoffUnmetMoviesSearch"}
SHOWN_DOWNLOADS = 4  # per title in /status
# order in /status: what is moving first, paused last
STATE_ORDER = {"downloading": 0, "importing": 1, "queued": 2, "blocked": 3, "warning": 4, "stalled": 5, "failed": 6, "paused": 7}


class ArrError(Exception):
    """An error whose message can be shown to Discord users as it is."""


@dataclass(frozen=True)
class Download:
    what: str  # "S23E13", "S23E13–E15", "" for a movie
    size: int
    left: int
    eta: datetime | None
    state: str  # downloading, queued, paused, stalled, importing, blocked, failed, warning
    message: str = ""
    hash: str = ""  # torrent hash, lower case
    episodes: int = 0

    @property
    def percent(self) -> int:
        return max(0, min(100, round(100 * (self.size - self.left) / self.size))) if self.size > 0 else 0


@dataclass(frozen=True)
class Progress:
    have: int  # episodes there (aired, monitored), or 1 for a movie that is there
    total: int  # aired monitored episodes, or 1 for a movie
    missing: dict[int, list[int]]  # season → episodes neither there nor downloading
    upcoming: int  # monitored episodes that have not aired yet
    downloads: tuple[Download, ...]
    searching: bool


def _state(record: dict) -> tuple[str, str]:
    status = str(record.get("status") or "").lower()
    tracked = str(record.get("trackedDownloadState") or "").lower()
    health = str(record.get("trackedDownloadStatus") or "").lower()
    messages = [str(m) for s in record.get("statusMessages") or [] if isinstance(s, dict) for m in s.get("messages") or []]
    if record.get("errorMessage"):
        messages.append(str(record["errorMessage"]))
    text = messages[0] if messages else ""
    if tracked in ("importpending", "importblocked") and health in ("warning", "error"):
        return "blocked", text
    if tracked in ("importpending", "importing") or status == "completed":
        return "importing", ""
    if status == "failed" or tracked in ("failed", "failedpending"):
        return "failed", text
    if status == "warning":
        lower = text.lower()
        return ("stalled", text) if not text or "stalled" in lower or "connection" in lower else ("warning", text)
    if status in ("queued", "delay"):
        return "queued", ""
    if status == "paused":
        return "paused", ""
    return "downloading", ""


def _episode_label(numbers: list[tuple[int, int]]) -> str:
    numbers = sorted(set(numbers))
    if not numbers:
        return ""
    seasons = {s for s, _ in numbers}
    if len(numbers) == 1:
        return f"S{numbers[0][0]:02d}E{numbers[0][1]:02d}"
    if len(seasons) == 1:
        episodes = [e for _, e in numbers]
        if episodes == list(range(episodes[0], episodes[-1] + 1)):
            return f"S{numbers[0][0]:02d}E{episodes[0]:02d}–E{episodes[-1]:02d}"
        return f"Staffel {numbers[0][0]}, {len(numbers)} Folgen"
    return f"{len(numbers)} Folgen"


def downloads_from_queue(records: list[dict]) -> list[Download]:
    """One entry per download (a season pack is one download with several episodes)."""
    groups: dict[str, list[dict]] = {}
    for record in records:
        key = str(record.get("downloadId") or record.get("id"))
        groups.setdefault(key, []).append(record)
    found = []
    for key, group in groups.items():
        first = group[0]
        numbers = [(r["episode"].get("seasonNumber"), r["episode"].get("episodeNumber")) for r in group
                   if isinstance(r.get("episode"), dict) and isinstance(r["episode"].get("seasonNumber"), int)
                   and isinstance(r["episode"].get("episodeNumber"), int)]
        state, message = _state(first)
        size = first.get("size") if isinstance(first.get("size"), (int, float)) else 0
        left = first.get("sizeleft") if isinstance(first.get("sizeleft"), (int, float)) else 0
        found.append(Download(_episode_label(numbers), int(size), int(left), parse_time(first.get("estimatedCompletionTime")),
                              state, message, str(first.get("downloadId") or "").lower(), len(numbers)))
    return found


class ArrClient:
    """Sonarr (kind "tv") or Radarr (kind "movie")."""

    def __init__(self, base_url: str, api_key: str, kind: str, fetch: Fetch = http_fetch) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.kind = kind
        self.name = "Sonarr" if kind == "tv" else "Radarr"
        self._fetch = fetch

    def _get(self, path: str, query: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/api/v3{path}" + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"X-Api-Key": self.api_key, "Accept": "application/json", "User-Agent": USER_AGENT}
        try:
            status, data = self._fetch("GET", url, headers, None)
        except Exception as exc:  # noqa: BLE001 - timeouts, refused
            log.warning("%s %s failed: %s", self.name, path, exc)
            raise ArrError(f"{self.name} ist nicht erreichbar.") from exc
        if status == 401:
            raise ArrError(f"{self.name} lehnt den API-Key ab. Ein Admin muss ihn prüfen.")
        if not 200 <= status < 300:
            raise ArrError(f"{self.name} meldet einen Fehler (HTTP {status}).")
        return data

    def _ids(self, tmdb_id: int) -> list[int]:
        if self.kind == "movie":
            found = self._get("/movie", {"tmdbId": tmdb_id})
            return [m["id"] for m in found or [] if isinstance(m, dict) and m.get("tmdbId") == tmdb_id]
        found = self._get("/series")
        return [s["id"] for s in found or [] if isinstance(s, dict) and s.get("tmdbId") == tmdb_id]

    def download_ids(self, tmdb_id: int) -> list[str]:
        """Torrent hashes of everything ever downloaded for this title (lower case, without duplicates)."""
        path, key = ("/history/movie", "movieId") if self.kind == "movie" else ("/history/series", "seriesId")
        history = [h for i in self._ids(tmdb_id) for h in self._get(path, {key: i}) or []]
        return sorted({str(h["downloadId"]).lower() for h in history if isinstance(h, dict) and h.get("downloadId")})

    def progress(self, tmdb_ids: list[int], now: datetime | None = None) -> dict[int, Progress]:
        """What is going on for these titles; titles not (yet) in Sonarr/Radarr are left out."""
        now = now or datetime.now(timezone.utc)
        queue_data = self._get("/queue", {"pageSize": 200, "includeEpisode": "true"} if self.kind == "tv" else {"pageSize": 200})
        queue = [r for r in (queue_data or {}).get("records") or [] if isinstance(r, dict)]
        running = [c for c in self._get("/command") or [] if isinstance(c, dict) and c.get("name") in SEARCHES
                   and str(c.get("status")).lower() in ("queued", "started")]
        if self.kind == "movie":
            return self._movie_progress(tmdb_ids, queue, running)
        series = {s.get("tmdbId"): s for s in self._get("/series") or [] if isinstance(s, dict)}
        result = {}
        for tmdb_id in tmdb_ids:
            show = series.get(tmdb_id)
            if show is None:
                continue
            episodes = [e for e in self._get("/episode", {"seriesId": show["id"]}) or [] if isinstance(e, dict)]
            mine = [r for r in queue if r.get("seriesId") == show["id"]]
            loading = {r.get("episodeId") for r in mine}
            wanted = [e for e in episodes if e.get("monitored") and (e.get("seasonNumber") or 0) > 0]
            aired = [e for e in wanted if (t := parse_time(e.get("airDateUtc"))) is not None and t <= now]
            missing: dict[int, list[int]] = {}
            for e in aired:
                if not e.get("hasFile") and e.get("id") not in loading:
                    missing.setdefault(e["seasonNumber"], []).append(e["episodeNumber"])
            ids = {e.get("id") for e in episodes}
            searching = any(c.get("name") == "MissingEpisodeSearch" or (c.get("body") or {}).get("seriesId") == show["id"]
                            or ids & set((c.get("body") or {}).get("episodeIds") or []) for c in running)
            result[tmdb_id] = Progress(sum(1 for e in aired if e.get("hasFile")), len(aired),
                                       {s: sorted(v) for s, v in missing.items()}, len(wanted) - len(aired),
                                       tuple(downloads_from_queue(mine)), searching)
        return result

    def _movie_progress(self, tmdb_ids: list[int], queue: list[dict], running: list[dict]) -> dict[int, Progress]:
        result = {}
        for tmdb_id in tmdb_ids:
            found = [m for m in self._get("/movie", {"tmdbId": tmdb_id}) or [] if isinstance(m, dict) and m.get("tmdbId") == tmdb_id]
            if not found:
                continue
            movie = found[0]
            mine = [r for r in queue if r.get("movieId") == movie["id"]]
            searching = any(c.get("name") == "MissingMoviesSearch" or movie["id"] in ((c.get("body") or {}).get("movieIds") or [])
                            for c in running)
            have = 1 if movie.get("hasFile") else 0
            result[tmdb_id] = Progress(have, 1, {}, 0, tuple(downloads_from_queue(mine)), searching)
        return result


# --- text for /status ------------------------------------------------------------------
def bar(percent: int, width: int = 10) -> str:
    filled = max(0, min(width, round(percent / 100 * width)))
    return "▓" * filled + "░" * (width - filled)


def speed_text(per_second: int) -> str:
    if per_second >= 1_000_000:
        return f"{per_second / 1_000_000:.1f} MB/s"
    return f"{max(per_second, 0) // 1000} KB/s"


STATE_TEXT = {
    "queued": "⏳ wartet in qBittorrent",
    "paused": "⏸️ pausiert",
    "importing": "📥 fertig geladen, wird in die Bibliothek übernommen",
    "stalled": "⏸️ hängt: keine Quellen (wird nach 2 Std. ohne Verbindung ersetzt)",
    "blocked": "⚠️ Übernahme blockiert",
    "failed": "❌ fehlgeschlagen",
    "warning": "⚠️ Problem",
}


def download_line(download: Download, live: dict | None, now: datetime) -> str:
    """One download: progress bar, speed, time left and sources while loading, else what is wrong."""
    label = f"{download.what} " if download.what else ""
    if download.state != "downloading":
        text = STATE_TEXT.get(download.state, "⚠️ Problem")
        emoji, _, rest = text.partition(" ")
        text = f"{emoji} {label}{rest}"
        if download.message and download.state in ("blocked", "failed", "warning"):
            text += f": {shorten(download.message, 120)}"
        return text
    text = f"⬇️ {label}`{bar(download.percent)}` {download.percent} %"
    if live and isinstance(live.get("dlspeed"), int):
        text += f" · {speed_text(live['dlspeed'])}"
    rest = eta_text(download.eta, now)
    if rest:
        text += f" · {rest}"
    if live and isinstance(live.get("num_seeds"), int):
        text += f" · {live['num_seeds']} Quellen"
    return text


def progress_lines(kind: str, progress: Progress, live: dict[str, dict], now: datetime) -> list[str]:
    """The lines under a title in /status."""
    lines = []
    ordered = sorted(progress.downloads, key=lambda d: (STATE_ORDER.get(d.state, 9), -d.percent))
    shown = ordered[:SHOWN_DOWNLOADS]
    if kind == "tv":
        def count(*states: str) -> int:
            return sum(max(d.episodes, 1) for d in progress.downloads if d.state in states)
        loading = count("downloading", "queued", "importing")
        paused, stuck = count("paused"), count("stalled", "blocked", "failed", "warning")
        missing = sum(len(v) for v in progress.missing.values())
        parts = [f"📦 {progress.have} von {progress.total} Folgen da"]
        if loading:
            parts.append(f"⬇️ {loading} laden")
        if paused:
            parts.append(f"⏸️ {paused} pausiert")
        if stuck:
            parts.append(f"⚠️ {stuck} hängen")
        if missing:
            parts.append(f"🔎 {missing} fehlen")
        if progress.upcoming:
            parts.append(f"📅 {progress.upcoming} kommen noch")
        lines.append(" · ".join(parts))
    lines += [download_line(d, live.get(d.hash), now) for d in shown]
    if len(progress.downloads) > len(shown):
        lines.append(f"… und {len(progress.downloads) - len(shown)} weitere Downloads")
    searching = "sucht gerade" if progress.searching else "wartet auf ein passendes Release"
    if kind == "tv" and progress.missing:
        which = "; ".join(f"Staffel {s}: Folge {format_numbers(e)}" for s, e in sorted(progress.missing.items()))
        lines.append(f"🔎 Fehlen noch: {which} · Sonarr {searching}")
    elif kind == "movie" and not progress.have and not progress.downloads:
        lines.append(f"🔎 Radarr {searching}")
    elif kind == "movie" and progress.have:
        lines.append("✅ Ist da, taucht gleich in Jellyfin auf")
    return lines
