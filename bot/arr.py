"""Sonarr and Radarr. Standard library only; the HTTP call is injectable for the tests.

- which downloads belonged to a title (torrent hashes, for /löschen)
- what is going on right now (for /status): episodes there, missing and still to come, the downloads with
  their state, and whether a search is running
- /folgen: the shows and their episodes; monitor single episodes and search them
- after watching: delete single episode files and stop monitoring them, so they aren't fetched again
- /verfolgen: add a show without its old episodes, follow it from an episode on, stop again; /kalender
"""
from __future__ import annotations

import json
import logging
import time
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
        return self._request("GET", path, query)

    def _request(self, method: str, path: str, query: dict[str, Any] | None = None, body: Any = None) -> Any:
        url = f"{self.base_url}/api/v3{path}" + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"X-Api-Key": self.api_key, "Accept": "application/json", "User-Agent": USER_AGENT}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        try:
            status, data = self._fetch(method, url, headers, data)
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

    def series(self) -> list[dict]:
        """Every show in Sonarr, with statistics (files, size) and images."""
        return [s for s in self._get("/series") or [] if isinstance(s, dict) and isinstance(s.get("id"), int)]

    def episodes(self, series_id: int) -> list[dict]:
        return [e for e in self._get("/episode", {"seriesId": series_id}) or [] if isinstance(e, dict)]

    def want_episodes(self, series: dict, episode_ids: list[int], search_ids: list[int]) -> None:
        """/folgen: monitors these episodes (and the show, or Sonarr ignores them) and searches those that are out."""
        if not series.get("monitored"):
            self._request("PUT", f"/series/{series['id']}", body={**series, "monitored": True})
        if episode_ids:
            self._request("PUT", "/episode/monitor", body={"episodeIds": episode_ids, "monitored": True})
        if search_ids:
            self._request("POST", "/command", body={"name": "EpisodeSearch", "episodeIds": search_ids})

    def find_episodes(self, tmdb_id: int | None, keys: list[tuple[int, int]]) -> tuple[dict | None, list[dict]]:
        """The show with this TMDB id and its episodes (season, episode) in keys."""
        show = next((s for s in self.series() if tmdb_id and s.get("tmdbId") == tmdb_id), None)
        if show is None:
            return None, []
        wanted = {tuple(k) for k in keys}
        return show, [e for e in self.episodes(show["id"]) if (e.get("seasonNumber"), e.get("episodeNumber")) in wanted]

    def episode_hashes(self, series_id: int, episode_ids: list[int]) -> list[str]:
        """Torrent hashes these episodes were downloaded with (Sonarr's history)."""
        wanted = set(episode_ids)
        history = self._get("/history/series", {"seriesId": series_id}) or []
        return sorted({str(h["downloadId"]).lower() for h in history
                       if isinstance(h, dict) and h.get("episodeId") in wanted and h.get("downloadId")})

    def queue_hashes(self) -> set[str]:
        """Torrents Sonarr/Radarr are still downloading or importing: these must not be removed."""
        data = self._get("/queue", {"pageSize": 500})
        records = data.get("records") if isinstance(data, dict) else None
        return {str(r["downloadId"]).lower() for r in records or [] if isinstance(r, dict) and r.get("downloadId")}

    def drop_episodes(self, episodes: list[dict]) -> int:
        """Stops monitoring these episodes (so Sonarr doesn't fetch them again) and deletes their files.
        Returns the size of the deleted files in bytes."""
        if not episodes:
            return 0
        self._request("PUT", "/episode/monitor", body={"episodeIds": [e["id"] for e in episodes], "monitored": False})
        freed = 0
        for file_id in sorted({e["episodeFileId"] for e in episodes if isinstance(e.get("episodeFileId"), int) and e["episodeFileId"]}):
            info = self._get(f"/episodefile/{file_id}")
            freed += info.get("size") or 0 if isinstance(info, dict) else 0
            self._request("DELETE", f"/episodefile/{file_id}")
        return freed

    def lookup_tvdb(self, tvdb_id: int) -> dict | None:
        """A show as Sonarr would add it, found by its TVDB id."""
        found = self._get("/series/lookup", {"term": f"tvdb:{tvdb_id}"})
        return next((s for s in found or [] if isinstance(s, dict) and s.get("tvdbId") == tvdb_id), None)

    def add_series(self, lookup: dict, profile_id: int, root: str, anime: bool, new_seasons: bool) -> dict:
        """Adds a show without monitoring or searching any episode. Sonarr still monitored all episodes once
        (19.11., Dexter), so follow() with only_these=True unmonitors everything that isn't wanted afterwards."""
        show = {**lookup, "qualityProfileId": profile_id, "rootFolderPath": root, "monitored": True, "seasonFolder": True,
                "seriesType": "anime" if anime else "standard", "monitorNewItems": "all" if new_seasons else "none",
                "seasons": [{**s, "monitored": False} for s in lookup.get("seasons") or [] if isinstance(s, dict)],
                "addOptions": {"monitor": "none", "searchForMissingEpisodes": False, "searchForCutoffUnmetEpisodes": False}}
        added = self._request("POST", "/series", body=show)
        if not isinstance(added, dict) or not isinstance(added.get("id"), int):
            raise ArrError("Sonarr hat die Serie nicht angenommen.")
        return added

    def wait_for_episodes(self, series_id: int, tries: int = 20, delay: float = 3.0, sleep=time.sleep) -> list[dict]:
        """Right after adding, Sonarr loads the episode list in the background."""
        for attempt in range(tries):
            episodes = self.episodes(series_id)
            if any(isinstance(e.get("seasonNumber"), int) and e["seasonNumber"] > 0 for e in episodes):
                return episodes
            if attempt < tries - 1:
                sleep(delay)
        return []

    def follow(self, series: dict, episodes: list[dict], start: tuple[int, int] | None, now: datetime,
               new_seasons: bool, only_these: bool = False) -> tuple[list[dict], list[dict], list[dict]]:
        """/verfolgen: monitors the episodes from start on (None: only those still to come), searches those that
        are out but missing, and new seasons if wanted. only_these (a show just added): every other episode is
        unmonitored. Returns (searched now, still to come, already there)."""
        wanted = []
        for e in episodes:
            season, number = e.get("seasonNumber"), e.get("episodeNumber")
            if not isinstance(season, int) or season <= 0 or not isinstance(number, int) or not isinstance(e.get("id"), int):
                continue
            aired = parse_time(e.get("airDateUtc"))
            if (start is not None and (season, number) >= start) or (start is None and (aired is None or aired > now)):
                wanted.append(e)
        ids = {e["id"] for e in wanted}
        if only_these:
            others = [e["id"] for e in episodes if isinstance(e.get("id"), int) and e["id"] not in ids and e.get("monitored")]
            if others:
                self._request("PUT", "/episode/monitor", body={"episodeIds": others, "monitored": False})
        changes: dict[str, Any] = {}
        if not series.get("monitored"):
            changes["monitored"] = True
        if (series.get("monitorNewItems") == "all") != new_seasons:
            changes["monitorNewItems"] = "all" if new_seasons else "none"
        if changes:
            self._request("PUT", f"/series/{series['id']}", body={**series, **changes})
        if ids:
            self._request("PUT", "/episode/monitor", body={"episodeIds": sorted(ids), "monitored": True})
        get = [e for e in wanted if not e.get("hasFile") and (t := parse_time(e.get("airDateUtc"))) is not None and t <= now]
        later = [e for e in wanted if not e.get("hasFile") and e not in get]
        have = [e for e in wanted if e.get("hasFile")]
        if get:
            self._request("POST", "/command", body={"name": "EpisodeSearch", "episodeIds": [e["id"] for e in get]})
        return get, later, have

    def unfollow(self, series: dict, now: datetime) -> int:
        """Stops fetching what is still to come (and new seasons); files that are there stay."""
        future = [e["id"] for e in self.episodes(series["id"])
                  if isinstance(e.get("id"), int) and e.get("monitored") and not e.get("hasFile")
                  and ((t := parse_time(e.get("airDateUtc"))) is None or t > now)]
        if future:
            self._request("PUT", "/episode/monitor", body={"episodeIds": future, "monitored": False})
        if series.get("monitorNewItems") != "none":
            self._request("PUT", f"/series/{series['id']}", body={**series, "monitorNewItems": "none"})
        return len(future)

    def calendar(self, start: datetime, end: datetime) -> list[dict]:
        """Monitored episodes (Sonarr) or movies (Radarr) coming out between start and end."""
        query = {"start": start.isoformat(), "end": end.isoformat(), "unmonitored": "false"}
        if self.kind == "tv":
            query["includeSeries"] = "true"
        return [i for i in self._get("/calendar", query) or [] if isinstance(i, dict)]

    def queued_ids(self) -> set[int]:
        """Episode ids (Sonarr) or movie ids (Radarr) that are downloading or importing."""
        data = self._get("/queue", {"pageSize": 500})
        key = "episodeId" if self.kind == "tv" else "movieId"
        records = data.get("records") if isinstance(data, dict) else None
        return {r[key] for r in records or [] if isinstance(r, dict) and isinstance(r.get(key), int)}

    def queued_titles(self) -> dict[int, str]:
        """TMDB id → 'Name (Jahr)' of everything in the download queue (for /status, also without a request)."""
        key = "series" if self.kind == "tv" else "movie"
        data = self._get("/queue", {"pageSize": 200, "includeSeries" if self.kind == "tv" else "includeMovie": "true"})
        found: dict[int, str] = {}
        for record in (data or {}).get("records") or [] if isinstance(data, dict) else []:
            item = record.get(key) if isinstance(record, dict) else None
            if isinstance(item, dict) and isinstance(item.get("tmdbId"), int) and item["tmdbId"] > 0:
                year = item.get("year")
                found[item["tmdbId"]] = f"{item.get('title', '?')} ({year})" if year else str(item.get("title", "?"))
        return found

    def manages(self, tmdb_id: int) -> bool:
        """Whether Sonarr/Radarr know the title. Files that were in the library before them aren't theirs to delete."""
        return bool(self._ids(tmdb_id))

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


# --- cards for /status (one embed per title) ------------------------------------------------------
SHOWN_ACTIVE = 5  # downloads with their own bar per title
MOVING = ("downloading", "importing", "queued")


def emoji_bar(percent: int, width: int = 10, fill: str = "🟦") -> str:
    filled = max(0, min(width, round(percent / 100 * width)))
    return fill * filled + "⬜" * (width - filled)


def _group(downloads: list[Download]) -> str:
    """'S23E13–E17' for paused or stuck downloads that are single episodes in a row, else their labels."""
    labels = [d.what for d in downloads if d.what]
    singles = [lbl for lbl in labels if len(lbl) == 6 and lbl[0] == "S" and lbl[3] == "E"]
    if singles and len(singles) == len(labels) and len({lbl[:3] for lbl in singles}) == 1:
        numbers = sorted(int(lbl[4:]) for lbl in singles)
        return f"{singles[0][:3]} Folge {format_numbers(numbers)}"
    return ", ".join(labels) or f"{len(downloads)} Downloads"


@dataclass(frozen=True)
class Card:
    """Everything /status shows for one title, as text pieces for an embed."""
    headline: str  # big bar with percent, GB and time left
    counts: str  # "📦 0 / 17 da · ⬇️ 12 laden · ⏸️ 5 pausiert"
    active: list[str]  # downloads with their own bar
    other: list[str]  # paused, stuck, blocked, grouped
    missing: str  # what Sonarr/Radarr still look for
    color: str  # "blue", "orange", "green", "gold"
    speed: int  # bytes per second of this title's downloads


def card(kind: str, progress: Progress, live: dict[str, dict], now: datetime) -> Card:
    downloads = sorted(progress.downloads, key=lambda d: (STATE_ORDER.get(d.state, 9), -d.percent))
    moving = [d for d in downloads if d.state in MOVING]
    others = [d for d in downloads if d.state not in MOVING]
    if kind == "tv" and progress.total:
        done = progress.have + sum(d.percent / 100 * max(d.episodes, 1) for d in moving)
        percent = round(100 * done / progress.total)
    elif progress.have:
        percent = 100
    else:
        percent = moving[0].percent if moving else 0
    size = sum(d.size for d in moving)
    loaded = sum(d.size - d.left for d in moving)
    speed = sum(live.get(d.hash, {}).get("dlspeed") or 0 for d in moving)
    etas = [d.eta for d in moving if d.eta]
    headline = f"{emoji_bar(percent, fill='🟩' if percent >= 100 else '🟦')}  **{percent} %**"
    if size:
        headline += f"\n{loaded / 1e9:.1f} von {size / 1e9:.1f} GB"
        if speed:
            headline += f" · {speed_text(speed)}"
        rest = eta_text(max(etas), now) if etas else ""
        if rest:
            headline += f" · fertig {rest}"
    counts = ""
    if kind == "tv":
        def count(states: tuple[str, ...]) -> int:
            return sum(max(d.episodes, 1) for d in downloads if d.state in states)
        parts = [f"📦 **{progress.have}** von {progress.total} da"]
        for emoji, label, states in (("⬇️", "laden", MOVING), ("⏸️", "pausiert", ("paused",)),
                                     ("⚠️", "hängen", ("stalled", "blocked", "failed", "warning"))):
            if count(states):
                parts.append(f"{emoji} {count(states)} {label}")
        if progress.upcoming:
            parts.append(f"📅 {progress.upcoming} kommen noch")
        counts = " · ".join(parts)
    active = []
    for d in moving[:SHOWN_ACTIVE]:
        if d.state != "downloading":
            active.append(download_line(d, None, now))
            continue
        line = f"`{d.what or 'Film'}` {emoji_bar(d.percent, 8)} **{d.percent} %**"
        info = live.get(d.hash) or {}
        extra = [speed_text(info["dlspeed"])] if isinstance(info.get("dlspeed"), int) else []
        rest = eta_text(d.eta, now)
        if rest:
            extra.append(rest)
        active.append(line + (f"\n{' · '.join(extra)}" if extra else ""))
    if len(moving) > SHOWN_ACTIVE:
        active.append(f"… und {len(moving) - SHOWN_ACTIVE} weitere")
    other = []
    for state, text in (("paused", "⏸️ **Pausiert:**"), ("stalled", "⏸️ **Hängt, keine Quellen:**"),
                        ("queued", "⏳ **Wartet:**")):
        group = [d for d in others if d.state == state]
        if group:
            other.append(f"{text} {_group(group)}")
    for d in others:
        if d.state in ("blocked", "failed", "warning"):
            other.append(download_line(d, None, now))
    missing = ""
    searching = "sucht gerade" if progress.searching else "wartet auf ein passendes Release"
    if kind == "tv" and progress.missing:
        which = "; ".join(f"Staffel {s}: Folge {format_numbers(e)}" for s, e in sorted(progress.missing.items()))
        missing = f"🔎 {which}\nSonarr {searching}"
    elif kind == "movie" and not progress.have and not downloads:
        missing = f"🔎 Radarr {searching}"
    if percent >= 100:
        color = "green"
    elif moving:
        color = "blue"
    elif others:
        color = "orange"
    else:
        color = "gold"
    return Card(headline, counts, active, other, missing, color, speed)
