"""Logic without Discord or network: Seerr answers as plain objects, status texts, seasons.

Only the standard library is used here, so this part can be tested anywhere.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

POSTER_BASE = "https://image.tmdb.org/t/p/w342"
MAX_CHOICES = 5  # search results offered per search
OVERVIEW_CHARS = 350
GB = 1024 ** 3
SPACE_MARGIN = 2 * GB  # "enough space again" only this far above the minimum, so it doesn't flap

# Availability of a movie, show or season in Seerr (MediaInfo.status)
UNKNOWN, PENDING, PROCESSING, PARTIAL, AVAILABLE, DELETED = 1, 2, 3, 4, 5, 6
# Status of a request (MediaRequest.status)
REQUEST_PENDING, REQUEST_APPROVED, REQUEST_DECLINED = 1, 2, 3

_YEAR_RE = re.compile(r"^(\d{4})")


class SeerrError(Exception):
    """An error whose message can be shown to Discord users as it is."""


@dataclass(frozen=True)
class Season:
    number: int
    episodes: int
    status: int = UNKNOWN

    @property
    def requestable(self) -> bool:
        """Specials (season 0) are left out; seasons that exist or are on their way too."""
        return self.number > 0 and self.status in (UNKNOWN, DELETED)


@dataclass(frozen=True)
class Title:
    kind: str  # "movie" or "tv"
    tmdb_id: int
    name: str
    year: str
    overview: str
    poster: str | None
    status: int
    seasons: tuple[Season, ...] = ()
    media_id: int | None = None  # Seerr's own id, exists once something was requested or found

    @property
    def in_library(self) -> bool:
        """Something of it is in Jellyfin, so it can be deleted."""
        return self.media_id is not None and self.status in (AVAILABLE, PARTIAL)

    @property
    def label(self) -> str:
        return f"{self.name} ({self.year})" if self.year else self.name

    @property
    def kind_text(self) -> str:
        return "Film" if self.kind == "movie" else "Serie"

    def requestable_seasons(self) -> list[int]:
        return [season.number for season in self.seasons if season.requestable]

    @property
    def requestable(self) -> bool:
        if self.kind == "tv" and self.seasons:
            return bool(self.requestable_seasons())
        return self.status in (UNKNOWN, DELETED, PARTIAL) if self.kind == "tv" else self.status in (UNKNOWN, DELETED)

    def status_text(self) -> str:
        if self.kind == "tv" and self.status == PARTIAL and self.seasons:
            missing = self.requestable_seasons()
            if missing:
                return f"🟡 Teilweise da, es fehlen noch Staffel {format_numbers(missing)}"
        return STATUS_TEXT.get(self.status, STATUS_TEXT[UNKNOWN])

    def seasons_text(self) -> str:
        """'Staffel 1–5 · fehlen: 4–5' for the embed; '' for movies or unknown seasons."""
        numbers = [season.number for season in self.seasons if season.number > 0]
        if not numbers:
            return ""
        text = f"Staffel {format_numbers(numbers)}"
        missing = self.requestable_seasons()
        if missing and missing != numbers:
            text += f" · noch nicht da: {format_numbers(missing)}"
        return text


STATUS_TEXT = {
    UNKNOWN: "➕ Noch nicht da, kann angefragt werden",
    DELETED: "➕ Noch nicht da, kann angefragt werden",
    PENDING: "⏳ Schon angefragt, wartet auf Freigabe",
    PROCESSING: "⬇️ Schon angefragt, wird gesucht oder geladen",
    PARTIAL: "🟡 Teilweise da",
    AVAILABLE: "✅ Schon da, viel Spass!",
}


def format_numbers(numbers: list[int]) -> str:
    """[1, 2, 3, 5] → '1–3, 5'"""
    parts: list[str] = []
    values = sorted(set(numbers))
    start = prev = None
    for value in values + [None]:
        if start is None:
            start = prev = value
            continue
        if value is not None and value == prev + 1:
            prev = value
            continue
        parts.append(str(start) if start == prev else f"{start}–{prev}")
        start = prev = value
    return ", ".join(parts)


def _year(date: Any) -> str:
    match = _YEAR_RE.match(date) if isinstance(date, str) else None
    return match.group(1) if match else ""


def _media_status(item: dict) -> int:
    info = item.get("mediaInfo") if isinstance(item.get("mediaInfo"), dict) else {}
    status = info.get("status")
    return status if isinstance(status, int) and UNKNOWN <= status <= DELETED else UNKNOWN


def _seasons(item: dict) -> tuple[Season, ...]:
    info = item.get("mediaInfo") if isinstance(item.get("mediaInfo"), dict) else {}
    known = {
        entry.get("seasonNumber"): entry.get("status")
        for entry in info.get("seasons") or []
        if isinstance(entry, dict)
    }
    seasons = []
    for entry in item.get("seasons") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("seasonNumber"), int):
            continue
        number = entry["seasonNumber"]
        status = known.get(number)
        seasons.append(Season(
            number=number,
            episodes=entry.get("episodeCount") if isinstance(entry.get("episodeCount"), int) else 0,
            status=status if isinstance(status, int) else UNKNOWN,
        ))
    return tuple(sorted(seasons, key=lambda season: season.number))


def title_from(item: Any, kind: str | None = None) -> Title | None:
    """A movie or show from a Seerr search result or details answer; None for anything else."""
    if not isinstance(item, dict):
        return None
    kind = item.get("mediaType") or kind
    if kind not in ("movie", "tv") or not isinstance(item.get("id"), int):
        return None
    if kind == "movie":
        name = item.get("title") or item.get("originalTitle")
        date = item.get("releaseDate")
    else:
        name = item.get("name") or item.get("originalName")
        date = item.get("firstAirDate")
    if not isinstance(name, str) or not name.strip():
        return None
    poster = item.get("posterPath")
    overview = item.get("overview") if isinstance(item.get("overview"), str) else ""
    info = item.get("mediaInfo") if isinstance(item.get("mediaInfo"), dict) else {}
    return Title(
        kind=kind,
        tmdb_id=item["id"],
        name=name.strip(),
        year=_year(date),
        overview=shorten(overview.strip(), OVERVIEW_CHARS),
        poster=POSTER_BASE + poster if isinstance(poster, str) and poster.startswith("/") else None,
        status=_media_status(item),
        seasons=_seasons(item) if kind == "tv" else (),
        media_id=info.get("id") if isinstance(info.get("id"), int) else None,
    )


def search_results(data: Any, kind: str | None, library_only: bool = False) -> list[Title]:
    """Movies or shows (kind None: both) from a /search answer, in Seerr's order, at most MAX_CHOICES.

    library_only keeps only what is (partly) in Jellyfin, for deleting."""
    results = data.get("results") if isinstance(data, dict) else None
    titles: list[Title] = []
    for item in results if isinstance(results, list) else []:
        if isinstance(item, dict) and (kind is None or item.get("mediaType") == kind):
            title = title_from(item)
            if title is not None and (title.in_library or not library_only):
                titles.append(title)
        if len(titles) == MAX_CHOICES:
            break
    return titles


# --- free space ---------------------------------------------------------------
def size_text(value: int) -> str:
    gigabytes = value / GB
    return f"{gigabytes:.0f} GB" if gigabytes >= 10 else f"{gigabytes:.1f} GB"


def pick_server(servers: Any) -> dict | None:
    """The Radarr/Sonarr server Seerr sends normal (not 4K) requests to."""
    usable = [s for s in servers if isinstance(s, dict) and isinstance(s.get("id"), int) and not s.get("is4k")] \
        if isinstance(servers, list) else []
    return next((s for s in usable if s.get("isDefault")), usable[0] if usable else None)


def root_folder_free(details: Any, directory: Any) -> int | None:
    """Free bytes of the folder new downloads go to; the smallest of all folders if it isn't listed."""
    folders = details.get("rootFolders") if isinstance(details, dict) else None
    sizes = {
        str(f.get("path", "")).rstrip("/"): f["freeSpace"]
        for f in folders if isinstance(f, dict) and isinstance(f.get("freeSpace"), (int, float))
    } if isinstance(folders, list) else {}
    if not sizes:
        return None
    wanted = str(directory or "").rstrip("/")
    return int(sizes[wanted]) if wanted in sizes else int(min(sizes.values()))


def no_space_text(free: int, minimum: int) -> str:
    return (
        f"💾 Kein Platz mehr: nur noch {size_text(free)} frei, für neue Anfragen braucht es mindestens "
        f"{size_text(minimum)}. Ein Admin kann mit `/löschen` alte Filme oder Serien entfernen."
    )


class SpaceWatch:
    """Decides when the bot should post about free space: once when it gets low, once when it is fine again."""

    def __init__(self, minimum: int) -> None:
        self.minimum = minimum
        self.low: bool | None = None  # None until the first reading

    def update(self, free: int | None) -> str | None:
        if free is None:
            return None
        low = free < self.minimum or (bool(self.low) and free < self.minimum + SPACE_MARGIN)
        previous, self.low = self.low, low
        if low and previous is not True:
            return (
                f"💾 Der Speicher ist fast voll: nur noch {size_text(free)} frei. Neue Anfragen sind gesperrt, "
                f"solange weniger als {size_text(self.minimum)} frei sind. "
                "Ein Admin kann mit `/löschen` Platz schaffen."
            )
        if not low and previous is True:
            return f"💾 Wieder genug Platz: {size_text(free)} frei. Anfragen gehen wieder."
        return None


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(",.;:–- ")
    return cut + "…"


@dataclass(frozen=True)
class OpenRequest:
    kind: str
    tmdb_id: int
    request_status: int
    media_status: int
    requested_by: str
    progress: int | None  # percent downloaded, None if nothing is downloading

    def line(self, name: str) -> str:
        if self.request_status == REQUEST_PENDING:
            state = "⏳ wartet auf Freigabe"
        elif self.progress is not None:
            state = f"⬇️ wird geladen, {self.progress} %"
        else:
            state = "🔎 wird gesucht"
        who = f" · von {self.requested_by}" if self.requested_by else ""
        return f"**{name}** · {state}{who}"


def _display_name(user: Any) -> str:
    """A user's visible name. Never the e-mail address."""
    if not isinstance(user, dict):
        return ""
    for key in ("displayName", "username", "jellyfinUsername", "plexUsername"):
        value = user.get(key)
        if isinstance(value, str) and value.strip() and "@" not in value:
            return value.strip()
    return ""


def _progress(media: dict) -> int | None:
    downloads = [d for d in media.get("downloadStatus") or [] if isinstance(d, dict)]
    total = sum(d.get("size") or 0 for d in downloads if isinstance(d.get("size"), (int, float)))
    left = sum(d.get("sizeLeft") or 0 for d in downloads if isinstance(d.get("sizeLeft"), (int, float)))
    if total <= 0:
        return None
    return max(0, min(100, round(100 * (total - left) / total)))


def open_requests(data: Any) -> list[OpenRequest]:
    """Requests from a GET /request answer that are not available yet."""
    results = data.get("results") if isinstance(data, dict) else None
    found: list[OpenRequest] = []
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("media"), dict):
            continue
        media = item["media"]
        kind = item.get("type") or media.get("mediaType")
        if kind not in ("movie", "tv") or not isinstance(media.get("tmdbId"), int):
            continue
        status = media.get("status") if isinstance(media.get("status"), int) else UNKNOWN
        request_status = item.get("status") if isinstance(item.get("status"), int) else REQUEST_PENDING
        if status == AVAILABLE or request_status == REQUEST_DECLINED:
            continue
        found.append(OpenRequest(
            kind=kind,
            tmdb_id=media["tmdbId"],
            request_status=request_status,
            media_status=status,
            requested_by=_display_name(item.get("requestedBy")),
            progress=_progress(media),
        ))
    return found


def deleted_text(title: Title) -> str:
    what = "Der Film" if title.kind == "movie" else "Die Serie mit allen Staffeln"
    return (
        f"🗑️ {what} ist gelöscht, auch die Dateien. Wird der Download in qBittorrent noch geteilt, "
        "wird der Platz erst frei, wenn der Torrent dort entfernt ist."
    )


def request_done_text(kind: str, request_status: int) -> str:
    what = "Der Film" if kind == "movie" else "Die Serie"
    if request_status == REQUEST_APPROVED:
        return f"✅ Angefragt! {what} wird jetzt gesucht und geladen. Sobald alles da ist, taucht es in Jellyfin auf."
    return f"⏳ Angefragt! {what} wartet noch auf die Freigabe in Seerr."
