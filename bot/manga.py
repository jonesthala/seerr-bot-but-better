"""Manga: search packs through Prowlarr, pick volumes or chapters from the torrent's file list, load just those with
qBittorrent into the folder a reader like Kavita watches, show what is there, add more volumes later, delete.

The torrent files come from Nyaa (every manga pack Prowlarr finds there or on its mirrors has a Nyaa id), so the file
list is known before anything is loaded. Standard library only; the HTTP calls are injectable for the tests.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .seerr import USER_AGENT
from .torrents import QbitClient, RawFetch, TorrentError, raw_fetch

log = logging.getLogger("seerrbot.manga")

COMIC_EXTENSIONS = (".cbz", ".cbr", ".cb7", ".cbt", ".zip", ".rar", ".7z", ".pdf", ".epub")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
MAX_RESULTS = 15
MAX_PARTS = 2000  # a torrent with more files than this is not a manga pack
MAX_TORRENT_BYTES = 4_000_000
PREPARED_SECONDS = 3600  # a torrent file fetched for the picker is kept this long
SEARCH_TIMEOUT = 60.0  # Prowlarr asks every indexer and waits for the slow ones
BOOKS = "7000"  # Prowlarr's category for books, Nyaa's literature lands there
KINDS = ("band", "kapitel", "ordner", "datei")  # order in the list

_NYAA_VIEW = re.compile(r"^https?://(?:www\.)?nyaa\.[a-z.]+/view/(\d{1,9})(?:[/?#]|$)", re.IGNORECASE)
_HASH = re.compile(r"^[0-9a-f]{40}$")
_VOLUME = re.compile(r"(?:^|[\s_\-\[(])(?:v|vol\.?|volume|band|tome)\s?(\d{1,4}(?:\.\d)?)(?:\s?-\s?v?(\d{1,4}))?(?=$|[\s_\-\])])",
                     re.IGNORECASE)
_CHAPTER = re.compile(r"(?:^|[\s_\-\[(])(?:c|ch\.?|chap\.?|chapter|kapitel)\s?(\d{1,4}(?:\.\d{1,2})?)(?:\s?-\s?c?(\d{1,4}))?"
                      r"(?=$|[\s_\-\])])", re.IGNORECASE)
_BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}")
_LAST_NUMBER = re.compile(r"(?:^|[\s_#-])(\d{1,4}(?:\.\d{1,2})?)(?:\s?-\s?(\d{1,4}))?\s*$")


class MangaError(RuntimeError):
    """Shown to the user as it is."""


# --- torrent files (bencode) -------------------------------------------------------------------------
def _decode(data: bytes, pos: int, spans: dict[str, tuple[int, int]], depth: int = 0) -> tuple[Any, int]:
    if depth > 64:
        raise ValueError("too deep")
    lead = data[pos:pos + 1]
    if lead == b"i":
        end = data.index(b"e", pos)
        return int(data[pos + 1:end]), end + 1
    if lead == b"l":
        pos += 1
        items = []
        while data[pos:pos + 1] != b"e":
            item, pos = _decode(data, pos, spans, depth + 1)
            items.append(item)
        return items, pos + 1
    if lead == b"d":
        pos += 1
        result: dict[bytes, Any] = {}
        while data[pos:pos + 1] != b"e":
            key, pos = _decode(data, pos, spans, depth + 1)
            if not isinstance(key, bytes):
                raise ValueError("dictionary key is not a string")
            start = pos
            result[key], pos = _decode(data, pos, spans, depth + 1)
            if depth == 0 and key == b"info":
                spans["info"] = (start, pos)
        return result, pos + 1
    if lead.isdigit():
        colon = data.index(b":", pos)
        length = int(data[pos:colon])
        start = colon + 1
        if length < 0 or start + length > len(data):
            raise ValueError("string runs past the end")
        return data[start:start + length], start + length
    raise ValueError("not bencode")


def _text(value: Any) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else ""


@dataclass(frozen=True)
class TorrentFile:
    info_hash: str  # v1 info hash, lower case, what qBittorrent calls the hash
    name: str
    files: tuple[tuple[str, int], ...]  # (path inside the torrent, size) in torrent order, without padding files
    data: bytes


def read_torrent(data: bytes) -> TorrentFile:
    """Name, files and info hash of a .torrent file; raises MangaError if it isn't one."""
    spans: dict[str, tuple[int, int]] = {}
    try:
        meta, end = _decode(data, 0, spans)
    except (ValueError, IndexError) as exc:
        raise MangaError("Die Torrent-Datei ist kaputt.") from exc
    info = meta.get(b"info") if isinstance(meta, dict) else None
    if not isinstance(info, dict) or "info" not in spans or data[end:].strip():
        raise MangaError("Die Torrent-Datei ist kaputt.")
    start, stop = spans["info"]
    name = _text(info.get(b"name.utf-8") or info.get(b"name")) or "?"
    files: list[tuple[str, int]] = []
    if isinstance(info.get(b"files"), list):
        for entry in info[b"files"]:
            if not isinstance(entry, dict):
                continue
            parts = entry.get(b"path.utf-8") or entry.get(b"path")
            if not isinstance(parts, list) or not parts:
                continue
            path = "/".join(_text(p) for p in parts)
            if b"p" in (entry.get(b"attr") or b"") or path.split("/")[-1].startswith(".pad"):
                continue  # BEP 47 padding, qBittorrent doesn't list it
            size = entry.get(b"length")
            files.append((path, size if isinstance(size, int) and size >= 0 else 0))
    elif isinstance(info.get(b"length"), int):
        files.append((name, info[b"length"]))
    if not files:
        raise MangaError("Die Torrent-Datei enthält keine Dateien.")
    return TorrentFile(hashlib.sha1(data[start:stop]).hexdigest(), name, tuple(files), data)  # noqa: S324 (BitTorrent)


# --- volumes and chapters from file names ------------------------------------------------------------
@dataclass(frozen=True)
class Part:
    """One thing to pick: a volume, a chapter, a folder of images or some other file."""
    kind: str  # "band", "kapitel", "ordner", "datei"
    number: float | None
    last: float | None  # 'v01-02' in one file: 1 and 2
    label: str
    paths: tuple[str, ...]  # paths inside the torrent
    size: int
    group: str = ""  # a pack with several series in their own folders: the series, also in front of the label


def _number(text: str) -> float:
    return float(text)


def _show(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:g}"


def classify(path: str) -> tuple[str, float | None, float | None]:
    """('band', 10, None) for 'Tokyo Ghoul v10 (2016) (Digital).cbz', ('kapitel', 187, None) for
    'Chainsaw Man 187 (2025).cbz', ('datei', None, None) when the name says nothing."""
    base = path.rsplit("/", 1)[-1]
    stem = base.rsplit(".", 1)[0] if "." in base else base
    for kind, pattern in (("band", _VOLUME), ("kapitel", _CHAPTER)):
        found = pattern.search(stem)
        if found:
            last = _number(found.group(2)) if found.group(2) else None
            return kind, _number(found.group(1)), last
    plain = _BRACKETS.sub(" ", stem).strip()
    found = _LAST_NUMBER.search(plain)
    if found and plain[:found.start()].strip():  # a title in front of the number
        last = _number(found.group(2)) if found.group(2) else None
        return "kapitel", _number(found.group(1)), last
    return "datei", None, None


def _label(kind: str, number: float | None, last: float | None, path: str) -> str:
    if kind in ("band", "kapitel") and number is not None:
        word = "Band" if kind == "band" else "Kapitel"
        return f"{word} {_show(number)}" + (f"–{_show(last)}" if last is not None and last > number else "")
    return path.rsplit("/", 1)[-1] if kind == "datei" else f"Ordner {path}"


def parts_from_files(files: list[tuple[str, int]] | tuple[tuple[str, int], ...]) -> list[Part]:
    """What can be picked from a torrent: comic files one by one, folders of images as a whole, nothing else.
    If there are neither, every file on its own."""
    comics = [(p, s) for p, s in files if p.lower().endswith(COMIC_EXTENSIONS)]
    parents = {p.rsplit("/", 1)[0] if "/" in p else "" for p, _ in comics}
    parts: list[Part] = []
    for path, size in comics:
        kind, number, last = classify(path)
        label = _label(kind, number, last, path)
        group = ""
        if len(parents) > 1:  # e.g. 'Tokyo Ghoul (digital)/…' and 'Tokyo Ghoul - re (digital)/…' in one pack
            group = short_name(path.rsplit("/", 1)[0].rsplit("/", 1)[-1]) if "/" in path else "Sonstiges"
            label = f"{group}: {label}"
        parts.append(Part(kind, number, last, label, (path,), size, group))
    folders: dict[str, list[tuple[str, int]]] = {}
    for path, size in files:
        if path.lower().endswith(IMAGE_EXTENSIONS) and "/" in path:
            folders.setdefault(path.rsplit("/", 1)[0], []).append((path, size))
    if not comics:
        for folder, inside in folders.items():
            if len(inside) < 3:
                continue  # a cover or two, not a chapter
            kind, number, last = classify(folder.rsplit("/", 1)[-1])
            kind = kind if kind != "datei" else "ordner"
            label = _label(kind, number, last, folder) if kind != "ordner" else f"Ordner {folder.rsplit('/', 1)[-1]}"
            parts.append(Part(kind, number, last, label, tuple(p for p, _ in inside), sum(s for _, s in inside)))
    if not parts:
        parts = [Part("datei", None, None, p.rsplit("/", 1)[-1], (p,), s) for p, s in files]
    parts.sort(key=lambda p: (p.group.casefold(), KINDS.index(p.kind), p.number if p.number is not None else 0,
                              p.paths[0].casefold()))
    return parts[:MAX_PARTS]


def _ranges(numbers: list[float]) -> str:
    """[1, 2, 3, 5, 7.5] → '1–3, 5, 7.5'"""
    out: list[str] = []
    start = prev = None
    for value in sorted(set(numbers)) + [None]:
        if start is None:
            start = prev = value
            continue
        if value is not None and value == int(value) and prev == int(prev) and value == prev + 1:
            prev = value
            continue
        out.append(_show(start) if start == prev else f"{_show(start)}–{_show(prev)}")
        start = prev = value
    return ", ".join(out)


def selection_label(parts: list[Part]) -> str:
    """'Band 10–14' or 'Band 1–3, Kapitel 187–190 und 2 weitere Dateien' for the chosen parts; with several series
    in one pack 'Tokyo Ghoul: Band 1–14; Tokyo Ghoul - re: Band 1–3'."""
    if not parts:
        return "nichts"
    groups: dict[str, list[Part]] = {}
    for p in parts:
        groups.setdefault(p.group, []).append(p)
    if len(groups) > 1 or next(iter(groups)):
        return "; ".join(f"{group}: {_group_label(members)}" if group else _group_label(members)
                         for group, members in groups.items())
    return _group_label(parts)


def _group_label(parts: list[Part]) -> str:
    pieces = []
    for kind, word in (("band", "Band"), ("kapitel", "Kapitel")):
        numbers: list[float] = []
        for p in parts:
            if p.kind == kind and p.number is not None:
                numbers.append(p.number)
                if p.last is not None and p.last > p.number and p.last == int(p.last) and p.number == int(p.number):
                    numbers.extend(float(n) for n in range(int(p.number) + 1, int(p.last) + 1))
        if numbers:
            pieces.append(f"{word} {_ranges(numbers)}")
    others = [p for p in parts if p.kind in ("ordner", "datei") or p.number is None]
    if others:
        if len(others) == 1 and not pieces:
            only = others[0]
            pieces.append(only.label[len(only.group) + 2:] if only.group else only.label)
        else:
            what = "Ordner" if all(p.kind == "ordner" for p in others) else ("Datei" if len(others) == 1 else "Dateien")
            pieces.append(f"{len(others)} {'weitere ' if pieces else ''}{what}")
    return pieces[0] if len(pieces) == 1 else ", ".join(pieces[:-1]) + " und " + pieces[-1]


_SELECT_ALL = {"alle", "alles", "all", "*", "ganz"}
_PREFIX = re.compile(r"^(?:(bände|baende|band|volume|vol|bd|b|v)|(kapitel|chapter|chap|kap|ch|c|k))(?=[\s.\d])\.?\s*",
                     re.IGNORECASE)
_RANGE = re.compile(r"^(\d{1,4}(?:\.\d{1,2})?)(?:\s*[-–]\s*(\d{1,4}(?:\.\d{1,2})?))?$")


def parse_selection(text: str, parts: list[Part]) -> list[int]:
    """Indexes into parts for '10-14', 'Band 3, 5', 'Kapitel 100-120', 'v1-3, k187' or 'alle'.
    Without 'Band'/'Kapitel' it means volumes if there are any, else chapters. Raises ValueError with a hint."""
    raw = text.strip()
    if not raw:
        raise ValueError("Bitte angeben, welche Bände oder Kapitel, z.B. 10-14, Kapitel 100-120 oder alle.")
    if raw.casefold() in _SELECT_ALL:
        return list(range(len(parts)))
    default = "band" if any(p.kind == "band" for p in parts) else "kapitel"
    chosen: list[int] = []
    for token in re.split(r"[,;]|\bund\b|\s+(?=[a-zA-Zä]+\.?\s*\d)", raw):
        token = token.strip()
        if not token:
            continue
        kind = default
        prefix = _PREFIX.match(token)
        if prefix:
            kind = "band" if prefix.group(1) else "kapitel"
            token = token[prefix.end():]
        found = _RANGE.match(token.strip())
        if not found:
            raise ValueError(f"«{text.strip()[:60]}» verstehe ich nicht. Beispiele: 10-14, Band 3, Kapitel 100-120, alle.")
        low = float(found.group(1))
        high = float(found.group(2)) if found.group(2) else low
        if high < low:
            low, high = high, low
        hits = [i for i, p in enumerate(parts) if p.kind == kind and p.number is not None
                and p.number <= high and (p.last if p.last is not None else p.number) >= low]
        if not hits:
            word = "Band" if kind == "band" else "Kapitel"
            raise ValueError(f"{word} {_show(low)}" + (f"–{_show(high)}" if high != low else "") + " gibt es in diesem Paket nicht.")
        chosen += [i for i in hits if i not in chosen]
    return sorted(chosen)


def overview_label(parts: list[Part]) -> str:
    """What a pack contains, e.g. 'Band 1–20, Kapitel 187–230'."""
    return selection_label(parts)


# --- search results ------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Release:
    nyaa_id: int
    title: str
    size: int
    seeders: int
    leechers: int
    info_hash: str  # may be empty: then it is known once the torrent file is read
    published: str


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and value >= 0 else 0


def releases_from(results: Any) -> list[Release]:
    """Prowlarr's search results as Nyaa packs: one per torrent (indexers overlap), most seeders first."""
    found: dict[str, Release] = {}
    for r in results if isinstance(results, list) else []:
        if not isinstance(r, dict):
            continue
        nyaa = None
        for key in ("infoUrl", "commentUrl", "guid"):
            match = _NYAA_VIEW.match(str(r.get(key) or ""))
            if match:
                nyaa = int(match.group(1))
                break
        if nyaa is None:
            continue  # only a magnet: the file list isn't known before loading
        info_hash = str(r.get("infoHash") or "").lower()
        release = Release(nyaa, str(r.get("title") or "?")[:300], _int(r.get("size")), _int(r.get("seeders")),
                          _int(r.get("leechers")), info_hash if _HASH.match(info_hash) else "",
                          str(r.get("publishDate") or "")[:10])
        key = release.info_hash or f"nyaa:{nyaa}"
        if key not in found or release.seeders > found[key].seeders:
            found[key] = release
    return sorted(found.values(), key=lambda r: (-r.seeders, -r.size))[:MAX_RESULTS]


def _search_fetch(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, dict[str, str], bytes]:
    return raw_fetch(method, url, headers, body, timeout=SEARCH_TIMEOUT)


class MangaSource:
    """Prowlarr for the search, Nyaa for the torrent files."""

    def __init__(self, prowlarr_url: str, api_key: str, nyaa_url: str = "https://nyaa.si",
                 fetch: RawFetch = _search_fetch) -> None:
        self.prowlarr_url = prowlarr_url.rstrip("/")
        self.api_key = api_key
        self.nyaa_url = nyaa_url.rstrip("/")
        self._fetch = fetch

    def search(self, query: str) -> list[Release]:
        text = " ".join(query.split())[:100]
        if len(text) < 2:
            raise MangaError("Bitte mindestens zwei Buchstaben eingeben.")
        params = urllib.parse.urlencode([("query", text), ("type", "search"), ("categories", BOOKS), ("limit", "100")])
        try:
            status, _, body = self._fetch("GET", f"{self.prowlarr_url}/api/v1/search?{params}",
                                          {"X-Api-Key": self.api_key, "Accept": "application/json",
                                           "User-Agent": USER_AGENT}, None)
        except Exception as exc:  # noqa: BLE001 - timeouts, refused
            log.warning("Prowlarr search failed: %s", exc)
            raise MangaError("Prowlarr ist nicht erreichbar oder zu langsam. Später nochmals versuchen.") from exc
        if status == 401:
            raise MangaError("Prowlarr lehnt den Bot ab: `PROWLARR_API_KEY` stimmt nicht.")
        if not 200 <= status < 300:
            raise MangaError(f"Prowlarr meldet einen Fehler (HTTP {status}).")
        try:
            return releases_from(json.loads(body))
        except ValueError as exc:
            raise MangaError("Prowlarr hat keine gültige Antwort geschickt.") from exc

    def torrent(self, nyaa_id: int) -> TorrentFile:
        try:
            status, headers, body = self._fetch("GET", f"{self.nyaa_url}/download/{int(nyaa_id)}.torrent",
                                                {"User-Agent": USER_AGENT}, None)
        except Exception as exc:  # noqa: BLE001
            log.warning("Nyaa torrent %s failed: %s", nyaa_id, exc)
            raise MangaError("Nyaa ist gerade nicht erreichbar.") from exc
        if status == 404:
            raise MangaError("Diesen Torrent gibt es auf Nyaa nicht mehr.")
        if not 200 <= status < 300 or len(body) > MAX_TORRENT_BYTES:
            raise MangaError(f"Nyaa liefert die Torrent-Datei nicht (HTTP {status}).")
        return read_torrent(body)


# --- what is loaded ---------------------------------------------------------------------------------------
def _relative(name: str, torrent_name: str) -> str:
    """qBittorrent's file name without the torrent's own folder in front."""
    prefix = torrent_name + "/"
    return name[len(prefix):] if torrent_name and name.startswith(prefix) else name


def qbit_parts(torrent: dict, files: list[dict]) -> tuple[list[Part], dict[str, dict]]:
    """Parts of a torrent in qBittorrent, and its files by path (with index, priority, progress)."""
    name = str(torrent.get("name") or "")
    multi = len(files) > 1 or any("/" in str(f.get("name") or "") for f in files)
    by_path: dict[str, dict] = {}
    for position, f in enumerate(files):
        if not isinstance(f, dict):
            continue
        path = str(f.get("name") or "")
        if multi:
            path = _relative(path, name) if path.startswith(name + "/") else path.split("/", 1)[-1] if "/" in path else path
        entry = dict(f)
        entry.setdefault("index", position)
        by_path[path] = entry
    parts = parts_from_files([(p, _int(f.get("size"))) for p, f in by_path.items()])
    return parts, by_path


def part_state(part: Part, by_path: dict[str, dict]) -> tuple[bool, float]:
    """(chosen, progress 0..1) of a part from qBittorrent's files."""
    entries = [by_path[p] for p in part.paths if p in by_path]
    if not entries:
        return False, 0.0
    chosen = all(_int(e.get("priority")) > 0 for e in entries)
    size = sum(_int(e.get("size")) for e in entries) or 1
    done = sum(_int(e.get("size")) * float(e.get("progress") or 0) for e in entries)
    return chosen, min(1.0, done / size)


DONE_STATES = {"uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP", "forcedUP", "checkingUP"}


def item_data(torrent: dict, files: list[dict]) -> dict[str, Any]:
    """A manga in qBittorrent for the list: what is chosen, how far, how big."""
    parts, by_path = qbit_parts(torrent, files)
    chosen = [p for p in parts if part_state(p, by_path)[0]]
    wanted = sum(p.size for p in chosen)
    loaded = sum(p.size * part_state(p, by_path)[1] for p in chosen)
    state = str(torrent.get("state") or "")
    progress = loaded / wanted if wanted else 0.0
    done = progress >= 0.999 or state in DONE_STATES
    return {
        "hash": str(torrent.get("hash") or "").lower(),
        "name": str(torrent.get("name") or "?"),
        "chosen": selection_label(chosen),
        "all": selection_label(parts),
        "count": len(chosen),
        "total": len(parts),
        "size": wanted,
        "percent": 100 if done else round(100 * progress, 1),
        "done": done,
        "state": state,
        "speed": _int(torrent.get("dlspeed")),
        "eta": _int(torrent.get("eta")) if not done and _int(torrent.get("eta")) < 8640000 else 0,
        "added": _int(torrent.get("added_on")),
        "seeds": _int(torrent.get("num_seeds")),
    }


def part_rows(parts: list[Part], by_path: dict[str, dict] | None = None) -> list[dict[str, Any]]:
    """Parts for a picker: key (position), label, size, and if already in qBittorrent whether chosen and how far."""
    rows = []
    for index, p in enumerate(parts):
        chosen, progress = part_state(p, by_path) if by_path is not None else (False, 0.0)
        rows.append({"key": index, "label": p.label, "kind": p.kind, "number": p.number, "last": p.last, "size": p.size,
                     "have": chosen, "percent": round(100 * progress)})
    return rows


@dataclass
class Prepared:
    torrent: TorrentFile
    title: str
    parts: list[Part]
    loaded: float = field(default_factory=time.monotonic)


class MangaService:
    """Everything the slash command and the panel do with mangas; blocking, so called in a thread."""

    def __init__(self, source: MangaSource, qbit: QbitClient, category: str = "manga", folder: str = "",
                 reader_url: str = "", sleep: Callable[[float], None] = time.sleep) -> None:
        self.source = source
        self.qbit = qbit
        self.category = category
        self.folder = folder  # where qBittorrent saves the category, if the bot has to create it
        self.reader_url = reader_url
        self._sleep = sleep
        self._prepared: dict[str, Prepared] = {}  # picker id → fetched torrent
        self._lock = threading.Lock()

    # --- search and pick ---
    def search(self, query: str) -> list[Release]:
        return self.source.search(query)

    def prepare(self, release: Release) -> tuple[str, Prepared]:
        """Fetches the torrent file of a search result; returns a picker id for add() and the parts to pick from."""
        torrent = self.source.torrent(release.nyaa_id)
        prepared = Prepared(torrent, release.title, parts_from_files(torrent.files))
        key = uuid.uuid4().hex
        with self._lock:
            now = time.monotonic()
            self._prepared = {k: v for k, v in self._prepared.items() if now - v.loaded < PREPARED_SECONDS}
            self._prepared[key] = prepared
        return key, prepared

    def prepared(self, key: str) -> Prepared:
        with self._lock:
            found = self._prepared.get(key)
        if found is None:
            raise MangaError("Diese Auswahl ist abgelaufen. Bitte nochmals suchen.")
        return found

    def existing(self, info_hash: str) -> dict | None:
        found = self.qbit.torrents([info_hash]) if _HASH.match(info_hash) else []
        return found[0] if found else None

    # --- what is there ---
    def items(self) -> list[dict[str, Any]]:
        torrents = self.qbit.torrents_in(self.category)
        rows = []
        for t in torrents:
            if not t.get("hash"):
                continue
            rows.append(item_data(t, self.qbit.files(str(t["hash"]))))
        rows.sort(key=lambda r: (r["done"], r["name"].casefold()))
        return rows

    def manga(self, info_hash: str) -> tuple[dict, list[Part], dict[str, dict]]:
        """A manga in qBittorrent (only in the manga category) with its parts."""
        torrent = self.existing(info_hash.lower())
        if torrent is None or str(torrent.get("category") or "") != self.category:
            raise MangaError("Diesen Manga gibt es in qBittorrent nicht (mehr).")
        parts, by_path = qbit_parts(torrent, self.qbit.files(info_hash))
        return torrent, parts, by_path

    # --- load, add, delete ---
    def _ensure_category(self) -> None:
        if self.category in self.qbit.categories():
            return
        if not self.folder:
            raise MangaError(f"In qBittorrent fehlt die Kategorie `{self.category}`. Bitte dort anlegen "
                             "(Speicherort: der Ordner, den Kavita liest) oder `MANGA_DIR` setzen.")
        self.qbit.create_category(self.category, self.folder)

    def add(self, key: str, chosen: list[int]) -> tuple[str, str, int]:
        """Loads the chosen parts of a prepared torrent. Returns (info hash, what, size in bytes)."""
        prepared = self.prepared(key)
        picked = [prepared.parts[i] for i in sorted(set(chosen)) if 0 <= i < len(prepared.parts)]
        if not picked:
            raise MangaError("Ausgewählt ist nichts.")
        info_hash = prepared.torrent.info_hash
        if self.existing(info_hash) is not None:
            return self.extend(info_hash, [p.paths for p in picked])
        self._ensure_category()
        self.qbit.add_torrent(prepared.torrent.data, f"{info_hash}.torrent", self.category)
        files: list[dict] = []
        for _ in range(40):  # qBittorrent adds it in the background
            torrent = self.existing(info_hash)
            if torrent is not None:
                files = self.qbit.files(info_hash)
                if files:
                    break
            self._sleep(0.5)
        if not files:
            raise MangaError("qBittorrent hat den Torrent nicht übernommen. Steht er unter Downloads?")
        indexes = {str(f.get("name") or ""): _int(f.get("index", position)) for position, f in enumerate(files)
                   if isinstance(f, dict)}
        keep: set[int] = set()
        for path in (path for p in picked for path in p.paths):
            hits = [i for name, i in indexes.items() if name == path or name.endswith("/" + path)]
            if len(hits) != 1:
                self.qbit.delete([info_hash])
                raise MangaError("Die Dateien in qBittorrent passen nicht zur Torrent-Datei. Nichts geladen.")
            keep.add(hits[0])
        skip = [str(i) for i in indexes.values() if i not in keep]
        if skip:
            self.qbit.file_priority(info_hash, skip, 0)
        self.qbit.resume([info_hash])
        size = sum(p.size for p in picked)
        log.info("Manga: %s, %s (%d bytes)", prepared.torrent.name, selection_label(picked), size)
        return info_hash, selection_label(picked), size

    def extend(self, info_hash: str, chosen: list) -> tuple[str, str, int]:
        """Adds parts to a manga that is already there: by position in its part list, or by paths."""
        torrent, parts, by_path = self.manga(info_hash)
        if chosen and all(isinstance(c, int) for c in chosen):
            picked = [parts[i] for i in sorted(set(chosen)) if 0 <= i < len(parts)]
        else:
            wanted = {path for c in chosen for path in c}
            picked = [p for p in parts if any(path in wanted or any(path.endswith("/" + w) or w.endswith("/" + path)
                                                                    for w in wanted) for path in p.paths)]
        new = [p for p in picked if not part_state(p, by_path)[0]]
        if not new:
            raise MangaError("Das ist schon alles da oder wird geladen.")
        ids = [str(_int(by_path[path].get("index"))) for p in new for path in p.paths if path in by_path]
        self.qbit.file_priority(info_hash, ids, 1)
        self.qbit.resume([info_hash])
        size = sum(p.size for p in new)
        log.info("Manga: %s, more: %s (%d bytes)", torrent.get("name"), selection_label(new), size)
        return info_hash.lower(), selection_label(new), size

    def delete(self, info_hash: str) -> tuple[str, int]:
        """Removes a manga with its files. Returns (its name, the size of what was chosen)."""
        torrent, parts, by_path = self.manga(info_hash)
        size = sum(p.size for p in parts if part_state(p, by_path)[0])
        self.qbit.delete([info_hash])
        log.info("Manga deleted: %s", torrent.get("name"))
        return str(torrent.get("name") or "?"), size

    def finished(self, hashes: list[str]) -> list[tuple[str, str]]:
        """Of these, the ones whose chosen files are all there: (hash, name). Gone ones count as finished too,
        with an empty name, so they are not asked about forever."""
        if not hashes:
            return []
        present = {str(t.get("hash")).lower(): t for t in self.qbit.torrents(hashes)}
        done = []
        for h in hashes:
            t = present.get(h.lower())
            if t is None:
                done.append((h, ""))
            elif item_data(t, self.qbit.files(h))["done"]:
                done.append((h, str(t.get("name") or "?")))
        return done


def short_name(name: str) -> str:
    """'Tokyo Ghoul (2015-2017) (Digital) (DigitalMangaFan)' → 'Tokyo Ghoul'"""
    plain = _BRACKETS.sub(" ", name)
    plain = re.sub(r"\s+(?:v\d{1,4}|\d{2,4})(?:\s*-\s*v?\d{1,4})?(?:\s*\+.*)?\s*$", "", " ".join(plain.split()))
    return plain.strip(" -_") or name.strip()


__all__ = ["MangaError", "MangaService", "MangaSource", "Part", "Release", "TorrentError", "TorrentFile", "classify",
           "item_data", "overview_label", "parse_selection", "part_rows", "parts_from_files", "qbit_parts",
           "read_torrent", "releases_from", "selection_label", "short_name"]
