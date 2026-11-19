"""Logic without Discord or network: Seerr answers as plain objects, status texts, seasons.

Only the standard library is used here, so this part can be tested anywhere.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

POSTER_BASE = "https://image.tmdb.org/t/p/w342"
MAX_CHOICES = 5  # search results offered per search
OVERVIEW_CHARS = 350
GB = 1024 ** 3
SPACE_MARGIN = 2 * GB  # "enough space again" only this far above the minimum, so it doesn't flap

# Availability of a movie, show or season in Seerr (MediaInfo.status)
# Seerr's MediaStatus (3.5.1): 6 is "blocklisted", 7 "deleted" (removed from the library, may be requested again)
UNKNOWN, PENDING, PROCESSING, PARTIAL, AVAILABLE, BLOCKLISTED, DELETED = 1, 2, 3, 4, 5, 6, 7
# Status of a request (MediaRequest.status)
REQUEST_PENDING, REQUEST_APPROVED, REQUEST_DECLINED, REQUEST_FAILED, REQUEST_COMPLETED = 1, 2, 3, 4, 5

# Seerr's issue types, as offered by /problem
ISSUE_TYPES = {1: "Bild", 2: "Ton", 3: "Untertitel", 4: "Anderes"}

_YEAR_RE = re.compile(r"^(\d{4})")


class SeerrError(Exception):
    """An error whose message can be shown to Discord users as it is."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status  # HTTP status from Seerr, None if Seerr wasn't reached


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
    runtime: int | None = None  # minutes: the movie, or one episode of a show; only in details answers

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
    DELETED: "🗑️ Wurde gelöscht, kann wieder angefragt werden",
    BLOCKLISTED: "🚫 Gesperrt, kann nicht angefragt werden",
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
        runtime=_runtime(item, kind),
    )


def _runtime(item: dict, kind: str) -> int | None:
    if kind == "movie":
        value = item.get("runtime")
        return value if isinstance(value, int) and value > 0 else None
    values = [v for v in item.get("episodeRunTime") or [] if isinstance(v, int) and v > 0]
    return sorted(values)[len(values) // 2] if values else None


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


def parse_time(value: Any) -> datetime | None:
    """An ISO time from Seerr ('2026-11-18T10:00:00.000Z') as an aware datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def eta_text(eta: datetime | None, now: datetime) -> str:
    """'noch ca. 20 Min.' for a download that should be done at eta; '' if unknown."""
    if eta is None:
        return ""
    minutes = (eta - now).total_seconds() / 60
    if minutes < 1:
        return "gleich fertig"
    if minutes < 60:
        return f"noch ca. {round(minutes)} Min."
    hours = minutes / 60
    if hours < 24:
        return f"noch ca. {max(1, round(hours))} Std."
    days = round(hours / 24)
    return f"noch ca. {days} Tag" if days == 1 else f"noch ca. {days} Tage"


@dataclass(frozen=True)
class OpenRequest:
    kind: str
    tmdb_id: int
    request_status: int
    media_status: int
    requested_by: str
    progress: int | None  # percent downloaded, None if nothing is downloading
    request_id: int | None = None
    eta: datetime | None = None  # when the download should be done

    def line(self, name: str, who: str | None = None, now: datetime | None = None) -> str:
        """One line for /status; who replaces Seerr's requester (the Discord name the bot remembered)."""
        who = who or self.requested_by
        return f"**{name}** · {self.state(now)}" + (f" · von {who}" if who else "")

    def state(self, now: datetime | None = None) -> str:
        """What Seerr knows: waiting for approval, downloading (with progress), partly there or being searched."""
        if self.request_status == REQUEST_PENDING:
            state = "⏳ wartet auf Freigabe"
        elif self.progress is not None:
            state = f"⬇️ wird geladen, {self.progress} %"
            rest = eta_text(self.eta, now or datetime.now(timezone.utc))
            if rest:
                state += f", {rest}"
            if self.media_status == PARTIAL:
                state = "🟡 teilweise da · " + state
        elif self.media_status == PARTIAL:
            state = "🟡 teilweise da, der Rest wird gesucht"
        else:
            state = "🔎 wird gesucht"
        return state


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


def _eta(media: dict) -> datetime | None:
    """When all running downloads should be done (the latest estimate)."""
    times = [parse_time(d.get("estimatedCompletionTime")) for d in media.get("downloadStatus") or [] if isinstance(d, dict)]
    known = [moment for moment in times if moment is not None]
    return max(known) if known else None


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
            request_id=item.get("id") if isinstance(item.get("id"), int) else None,
            eta=_eta(media),
        ))
    return found


# --- single requests: approval and "it's there" -----------------------------------
@dataclass(frozen=True)
class Requested:
    """What POST /request answered."""
    status: int
    request_id: int | None


@dataclass(frozen=True)
class RequestState:
    request_id: int
    kind: str
    tmdb_id: int
    status: int  # request status (REQUEST_*)
    media_status: int  # availability (AVAILABLE etc.)
    media_url: str | None  # link to it in Jellyfin/Plex, once Seerr knows one
    seasons: tuple[int, ...]  # requested seasons of a show
    requested_by: str


def request_state(item: Any) -> RequestState | None:
    """A request from GET /request or GET /request/{id}; None for anything unusable."""
    if not isinstance(item, dict) or not isinstance(item.get("id"), int) or not isinstance(item.get("media"), dict):
        return None
    media = item["media"]
    kind = item.get("type") or media.get("mediaType")
    if kind not in ("movie", "tv") or not isinstance(media.get("tmdbId"), int):
        return None
    status = item.get("status") if isinstance(item.get("status"), int) else REQUEST_PENDING
    media_status = media.get("status") if isinstance(media.get("status"), int) else UNKNOWN
    url = media.get("mediaUrl")
    seasons = sorted({
        entry["seasonNumber"] for entry in item.get("seasons") or []
        if isinstance(entry, dict) and isinstance(entry.get("seasonNumber"), int)
    })
    return RequestState(
        request_id=item["id"],
        kind=kind,
        tmdb_id=media["tmdbId"],
        status=status,
        media_status=media_status,
        media_url=url if isinstance(url, str) and url.startswith(("http://", "https://")) else None,
        seasons=tuple(seasons),
        requested_by=_display_name(item.get("requestedBy")),
    )


def pending_requests(data: Any) -> list[RequestState]:
    results = data.get("results") if isinstance(data, dict) else None
    states = [request_state(item) for item in results] if isinstance(results, list) else []
    return [state for state in states if state is not None and state.status == REQUEST_PENDING]


def seasons_done(title: Title, wanted: tuple[int, ...]) -> bool:
    """All wanted seasons of a show are in the library."""
    status = {season.number: season.status for season in title.seasons}
    return bool(wanted) and all(status.get(number) == AVAILABLE for number in wanted)


def what_text(name: str, kind: str, seasons: tuple[int, ...] | list[int]) -> str:
    """'**Loki (2021)**, Staffel 1–2'"""
    text = f"**{name}**"
    if kind == "tv" and seasons:
        text += f", Staffel {format_numbers(list(seasons))}"
    return text


def approval_text(name: str, kind: str, seasons: tuple[int, ...], who: str) -> str:
    by = f" von {who}" if who else ""
    return f"📥 Neue Anfrage{by}: {what_text(name, kind, seasons)}. Wartet auf Freigabe."


def done_text(mention: str, name: str) -> str:
    return f"🍿 {mention} **{name}** ist jetzt da. Viel Spass!"


def declined_text(mention: str, name: str) -> str:
    return f"❌ {mention} Deine Anfrage für **{name}** wurde abgelehnt."


def failed_text(mention: str, name: str) -> str:
    return f"⚠️ {mention} **{name}** konnte nicht an Radarr/Sonarr übergeben werden. Ein Admin schaut es sich an."


# --- limit per person ------------------------------------------------------------------
def next_allowed(times: list[float], limit: int, days: int, now: float) -> float | None:
    """None if one more request is allowed now; otherwise when the next one is (epoch seconds)."""
    if limit <= 0:
        return None
    window = days * 86400
    recent = sorted(moment for moment in times if now - moment < window)
    if len(recent) < limit:
        return None
    return recent[len(recent) - limit] + window


def limit_text(limit: int, days: int, until: datetime) -> str:
    what = "eine Anfrage" if limit == 1 else f"{limit} Anfragen"
    return (
        f"🚦 Pro Person gehen {what} in {days} Tagen, und die hast du schon gestellt. "
        f"Die nächste geht wieder ab {until:%d.%m. um %H:%M}."
    )


# --- new in the library and the weekly digest ---------------------------------------------
@dataclass(frozen=True)
class NewMedia:
    kind: str
    tmdb_id: int
    added: datetime | None
    url: str | None
    partial: tuple[int, ...] | None = None  # shows only partly there: the seasons that are (partly) there


def new_media(data: Any) -> list[NewMedia]:
    """Items of a GET /media answer, in Seerr's order."""
    results = data.get("results") if isinstance(data, dict) else None
    items = []
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict) or item.get("mediaType") not in ("movie", "tv") \
                or not isinstance(item.get("tmdbId"), int):
            continue
        url = item.get("mediaUrl")
        partial = None
        if item.get("status") == PARTIAL:
            seasons = item.get("seasons") if isinstance(item.get("seasons"), list) else []
            partial = tuple(sorted(s["seasonNumber"] for s in seasons if isinstance(s, dict)
                                   and isinstance(s.get("seasonNumber"), int) and s["seasonNumber"] > 0
                                   and s.get("status") in (PARTIAL, AVAILABLE)))
        items.append(NewMedia(
            kind=item["mediaType"],
            tmdb_id=item["tmdbId"],
            added=parse_time(item.get("mediaAddedAt")),
            url=url if isinstance(url, str) and url.startswith(("http://", "https://")) else None,
            partial=partial,
        ))
    return items


def new_line(item: NewMedia, name: str, zone: tzinfo) -> str:
    icon = "🎬" if item.kind == "movie" else "📺"
    safe = name.replace("[", "(").replace("]", ")")
    shown = f"[{safe}]({item.url})" if item.url else f"**{safe}**"
    date = f" · {item.added.astimezone(zone):%d.%m.}" if item.added else ""
    part = ""
    if item.partial is not None:
        seasons = item.partial
        which = (f" (Staffel {', '.join(map(str, seasons))})" if 0 < len(seasons) <= 3
                 else f" ({len(seasons)} Staffeln)" if seasons else "")
        part = f" · teilweise da{which}"
    return f"{icon} {shown}{date}{part}"


_WEEKDAYS = {
    "mo": 0, "mon": 0, "di": 1, "tue": 1, "mi": 2, "wed": 2, "do": 3, "thu": 3,
    "fr": 4, "fri": 4, "sa": 5, "sat": 5, "so": 6, "sun": 6,
}
_DIGEST_RE = re.compile(r"^([a-z]{2,3})\s+(\d{1,2}):(\d{2})$")


def parse_digest(spec: str) -> tuple[int, int, int] | None:
    """'sun 18:00' or 'so 18:00' → (6, 18, 0); '' → None (off). Raises ValueError for anything else."""
    text = spec.strip().lower()
    if not text:
        return None
    match = _DIGEST_RE.match(text)
    if not match or match.group(1) not in _WEEKDAYS:
        raise ValueError(spec)
    hour, minute = int(match.group(2)), int(match.group(3))
    if hour > 23 or minute > 59:
        raise ValueError(spec)
    return _WEEKDAYS[match.group(1)], hour, minute


def digest_key(now: datetime, schedule: tuple[int, int, int]) -> str | None:
    """This week's key ('2026-W47') once the scheduled time has passed this week, else None."""
    if (now.weekday(), now.hour, now.minute) < schedule:
        return None
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def added_since(items: list[NewMedia], since: datetime) -> list[NewMedia]:
    return [item for item in items if item.added is not None and item.added >= since]


# --- trending and quality profiles -------------------------------------------------------
def trending_results(data: Any, kind: str | None, limit: int = 10) -> list[Title]:
    """Movies and shows from GET /discover/trending (people are skipped)."""
    results = data.get("results") if isinstance(data, dict) else None
    titles: list[Title] = []
    for item in results if isinstance(results, list) else []:
        if isinstance(item, dict) and (kind is None or item.get("mediaType") == kind):
            title = title_from(item)
            if title is not None:
                titles.append(title)
        if len(titles) == limit:
            break
    return titles


def pick_profiles(details: Any, wanted: list[str]) -> list[tuple[int, str]]:
    """The quality profiles named in wanted (any case), in that order, as (id, name)."""
    profiles = details.get("profiles") if isinstance(details, dict) else None
    by_name = {
        str(p["name"]).strip().lower(): (p["id"], str(p["name"]).strip())
        for p in profiles if isinstance(p, dict) and isinstance(p.get("id"), int) and isinstance(p.get("name"), str)
    } if isinstance(profiles, list) else {}
    return [by_name[name.strip().lower()] for name in wanted if name.strip().lower() in by_name]


def deleted_text(title: Title, removed: tuple[int, int] | None = None, problem: str | None = None) -> str:
    """After /löschen. removed: (torrents, bytes) taken out of qBittorrent, None if the bot can't do that."""
    what = "Der Film" if title.kind == "movie" else "Die Serie mit allen Staffeln"
    if problem:
        return f"🗑️ {what} ist gelöscht, auch die Dateien. ⚠️ Die Torrents sind noch in qBittorrent: {problem}"
    if removed is None:
        return (f"🗑️ {what} ist gelöscht, auch die Dateien. Wird der Download in qBittorrent noch geteilt, "
                "wird der Platz erst frei, wenn der Torrent dort entfernt ist.")
    count, size = removed
    if not count:
        return f"🗑️ {what} ist gelöscht, auch die Dateien. In qBittorrent lief dazu kein Torrent mehr."
    torrents = "1 Torrent" if count == 1 else f"{count} Torrents"
    return (f"🗑️ {what} ist gelöscht, mit allen Dateien und {torrents} in qBittorrent. "
            f"Frei geworden: etwa {size_text(size)}.")


def unmanaged_text(title: Title, app: str, folder: str | None) -> str:
    """/löschen for a title that Sonarr/Radarr don't know: its files were in the library before, so Seerr can't
    have them deleted. Nothing is deleted, and the admin learns which folder to remove by hand."""
    what = "den Film" if title.kind == "movie" else "die Serie"
    where = f"den Ordner `{folder}`" if folder else "die Dateien"
    return (f"⚠️ Nichts gelöscht: {app} kennt {what} nicht, die Dateien lagen schon vorher in der Bibliothek. "
            f"Darum kann der Bot sie nicht löschen. Ein Admin muss {where} von Hand löschen, "
            "danach verschwindet der Titel beim nächsten Scan aus Jellyfin.")


def request_done_text(kind: str, request_status: int, notify: bool = False) -> str:
    """The answer under a request; notify: the bot will say here when it's there."""
    what = "Der Film" if kind == "movie" else "Die Serie"
    then = " Ich sage dir hier Bescheid, sobald alles da ist." if notify else " Sobald alles da ist, taucht es in Jellyfin auf."
    if request_status == REQUEST_PENDING:
        return f"⏳ Angefragt! {what} wartet noch auf die Freigabe durch einen Admin.{then}"
    return f"✅ Angefragt! {what} wird jetzt gesucht und geladen.{then}"


# --- /hilfe ---------------------------------------------------------------------
# Who may use a command: everyone in the channel, those who may request (role), or admins only.
EVERYONE, REQUESTERS, ADMINS = "everyone", "requesters", "admins"
HELP = {  # command → (who, example)
    "film": (REQUESTERS, "/film Sintel"),
    "serie": (REQUESTERS, "/serie Pioneer One"),
    "trending": (REQUESTERS, "/trending"),
    "neu": (EVERYONE, "/neu"),
    "status": (EVERYONE, "/status"),
    "problem": (REQUESTERS, "/problem Sintel Ton «kein Ton ab Minute 20»"),
    "jellyfin-zugang": (REQUESTERS, "/jellyfin-zugang"),
    "abo": (REQUESTERS, "/abo South Park"),
    "hilfe": (EVERYONE, "/hilfe"),
    "löschen": (ADMINS, "/löschen Sintel"),
    "jellyfin-liste": (ADMINS, "/jellyfin-liste"),
    "jellyfin-entziehen": (ADMINS, "/jellyfin-entziehen @person"),
    "speicher": (ADMINS, "/speicher"),
}


def help_text(commands: list[tuple[str, str]], admin: bool, may_request: bool) -> str:
    """The commands this person may use, from (name, description) of the registered commands."""
    allowed = {EVERYONE} | ({REQUESTERS} if may_request or admin else set()) | ({ADMINS} if admin else set())
    mine, admins = [], []
    for name, description in commands:
        who, example = HELP.get(name, (EVERYONE, f"/{name}"))
        if who not in allowed:
            continue
        line = f"`{example}`: {description}"
        (admins if who == ADMINS else mine).append(line)
    text = "\n".join(mine) or "Für dich gibt es hier keine Befehle."
    if admins:
        text += "\n\n**Nur für Admins**\n" + "\n".join(admins)
    return text


# --- size estimate before requesting ----------------------------------------------------------
EPISODE_MINUTES_GUESS = 45  # when TMDB knows no episode length


@dataclass(frozen=True)
class SizeRate:
    """GB per hour of running time: typical value and a range, measured in the own library."""
    typical: float
    low: float
    high: float
    titles: int = 0  # how many titles of the library it is based on; 0 = rule of thumb


RULE_OF_THUMB = SizeRate(1.8, 0.9, 3.5)


def rate_from_library(rates: list[float]) -> SizeRate:
    """From the GB per hour of each movie/show in the library; the rule of thumb below three titles."""
    rates = sorted(r for r in rates if r > 0)
    if len(rates) < 3:
        return RULE_OF_THUMB
    typical = rates[len(rates) // 2] if len(rates) % 2 else (rates[len(rates) // 2 - 1] + rates[len(rates) // 2]) / 2
    return SizeRate(typical, min(rates[0], 0.6 * typical), max(rates[-1], 1.6 * typical), len(rates))


def space_text(title: Title, seasons: list[int] | None, rate: SizeRate, free: int | None, min_free: int) -> str | None:
    """'≈ 18 GB (9–29 GB) …' for the request window; None if there is nothing to estimate."""
    if title.kind == "movie":
        if not title.runtime:
            return None
        hours, what, guessed = title.runtime / 60, f"{title.runtime} Min.", False
    else:
        wanted = set(seasons or [])
        episodes = sum(s.episodes for s in title.seasons if s.number in wanted)
        if not episodes:
            return None
        minutes = title.runtime or EPISODE_MINUTES_GUESS
        hours, guessed = episodes * minutes / 60, title.runtime is None
        what = f"{episodes} Folgen à {minutes} Min." + (" (Länge unbekannt, geschätzt)" if guessed else "")
    typical, low, high = (int(hours * r * GB) for r in (rate.typical, rate.low, rate.high))
    text = f"≈ **{size_text(typical)}** ({size_text(low)}–{size_text(high)}) für {what}"
    if free is not None:
        text += f"\nFrei: {size_text(free)}"
        if free - typical < min_free <= free - low:
            text += (f"\n⚠️ Könnte knapp werden: Danach wären etwa {size_text(max(free - typical, 0))} frei, "
                     f"unter {size_text(min_free)} nehme ich keine Anfragen mehr an.")
        elif free - low < min_free:
            text += ("\n❌ Reicht vermutlich nicht. Erst Platz schaffen, zum Beispiel mit `/löschen`, oder weniger "
                     "Staffeln wählen.")
    basis = (f"Schätzung aus deiner Bibliothek ({rate.typical:.1f} GB pro Stunde)" if rate.titles
             else "Faustregel, die Bibliothek ist noch zu klein für eine eigene Schätzung")
    return text + f"\n-# {basis}. Die echte Grösse hängt vom gefundenen Release ab."
