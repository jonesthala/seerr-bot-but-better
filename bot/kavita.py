"""Kavita, the manga reader: have it read a finished manga's folder right away and check that the files are in it.

Kavita's own folder watching waits 10 minutes before it scans. With this, the bot tells Kavita to scan as soon as
qBittorrent is done, looks a few seconds later whether Kavita has the files, and only then sends the message, with a
link to the series. Needs an auth key (API key) of a Kavita admin; Kavita 0.8.x and 0.9.x.
Standard library only; the HTTP calls are injectable for the tests.
"""
from __future__ import annotations

import json
import logging
import posixpath
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

from .manga import COMIC_EXTENSIONS, MangaError, MangaService
from .seerr import USER_AGENT
from .torrents import RawFetch, TorrentError, raw_fetch

log = logging.getLogger("seerrbot.kavita")

PLUGIN_NAME = "seerr-bot"  # how the bot shows up in Kavita's log when it logs in with the key
TOKEN_SECONDS = 30 * 60  # a login token is reused this long (Kavita's last longer)
MAX_CHECKED = 20  # files looked up per manga: all of a small pack, else the first and the last ten
SCAN_WAIT = 20  # seconds between asking Kavita to scan and the first look
GIVE_UP_SECONDS = 15 * 60  # then the message goes out anyway (Kavita's own watcher would have run by then)

__all__ = ["GIVE_UP_SECONDS", "SCAN_WAIT", "Finished", "KavitaClient", "KavitaError", "ReaderSync", "Seen",
           "series_link"]


class KavitaError(Exception):
    """Kavita can't be reached or refuses; never stops the message that a manga is loaded."""


def _norm(path: Any) -> str:
    return str(path or "").replace("\\", "/").rstrip("/")


def _query(path: str) -> str:
    """Search text for a file: its name without extension. Kavita drops ':' and '%' from searches, so then the
    longest piece between them (Kavita matches file paths with LIKE %text%)."""
    stem = posixpath.splitext(posixpath.basename(path))[0]
    return max(re.split(r"[:%]", stem), key=len).strip()


def _sample(paths: list[str]) -> list[str]:
    if len(paths) <= MAX_CHECKED:
        return paths
    half = MAX_CHECKED // 2
    return paths[:half] + paths[-half:]


def series_link(reader_url: str, ids: tuple[int, int] | None) -> str:
    """The series page in Kavita, or the start page if the series is unknown."""
    base = reader_url.rstrip("/")
    if base and ids:
        return f"{base}/library/{ids[0]}/series/{ids[1]}"
    return base


class KavitaClient:
    def __init__(self, base_url: str, api_key: str, fetch: RawFetch = raw_fetch,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._fetch = fetch
        self._clock = clock
        self._token: tuple[float, str] | None = None  # (when logged in, token)

    def _send(self, method: str, path: str, body: dict | None = None, token: str = "") -> tuple[int, bytes]:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            status, _, raw = self._fetch(method, self.base_url + path, headers, data)
        except Exception as exc:  # noqa: BLE001 - timeouts, refused
            log.warning("Kavita %s failed: %s", path.split("?", 1)[0], exc)  # never the query: it may hold the key
            raise KavitaError("Kavita ist nicht erreichbar.") from exc
        return status, raw

    def scan(self, folder: str) -> None:
        """Asks Kavita to scan this folder (as Kavita sees it) now; Kavita does it in the background."""
        status, raw = self._send("POST", "/api/library/scan-folder",
                                 {"apiKey": self.api_key, "folderPath": folder, "abortOnNoSeriesMatch": False})
        if status == 401:
            raise KavitaError("Kavita lehnt den Bot ab: `KAVITA_API_KEY` stimmt nicht.")
        if status == 400:
            raise KavitaError("Kavita will den Ordner nicht scannen (gehört der Schlüssel einem Admin?): "
                              + raw[:200].decode("utf-8", "replace"))
        if not 200 <= status < 300:
            raise KavitaError(f"Kavita meldet beim Scannen HTTP {status}.")

    def _login(self) -> str:
        query = urllib.parse.urlencode({"apiKey": self.api_key, "pluginName": PLUGIN_NAME})
        status, raw = self._send("POST", f"/api/plugin/authenticate?{query}")
        if status in (401, 403):
            raise KavitaError("Kavita lehnt den Bot ab: `KAVITA_API_KEY` stimmt nicht.")
        if not 200 <= status < 300:
            raise KavitaError(f"Kavita meldet beim Anmelden HTTP {status}.")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise KavitaError("Kavita hat keine gültige Antwort geschickt.") from exc
        token = str((data.get("token") or data.get("Token") or "") if isinstance(data, dict) else "")
        if not token:
            raise KavitaError("Kavita hat kein Login-Token geschickt.")
        self._token = (self._clock(), token)
        return token

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        url = f"{path}?{urllib.parse.urlencode(params)}"
        for attempt in range(2):
            token = self._token[1] if self._token and self._clock() - self._token[0] < TOKEN_SECONDS else self._login()
            status, raw = self._send("GET", url, token=token)
            if status == 401 and attempt == 0:
                self._token = None  # expired: log in once more
                continue
            if status == 404:
                return None
            if not 200 <= status < 300:
                raise KavitaError(f"Kavita meldet HTTP {status}.")
            try:
                return json.loads(raw) if raw.strip() else None
            except ValueError as exc:
                raise KavitaError("Kavita hat keine gültige Antwort geschickt.") from exc
        raise KavitaError("Kavita lehnt das Login-Token ab.")

    def _file(self, path: str) -> dict | None:
        """Kavita's entry for this file, if it has read it."""
        text = _query(path)
        if not text:
            return None
        found = self._get("/api/search/search", {"queryString": text, "includeChapterAndFiles": "true"})
        files = (found.get("files") or found.get("Files") or []) if isinstance(found, dict) else []
        wanted = _norm(path)
        return next((f for f in files if isinstance(f, dict) and _norm(f.get("filePath") or f.get("FilePath")) == wanted),
                    None)

    def find(self, paths: list[str]) -> tuple[bool, tuple[int, int] | None]:
        """Whether Kavita has all these files (paths as Kavita sees them; a sample of big packs), and
        (library id, series id) of their series. Stops at the first missing one."""
        first = None
        for path in _sample(paths):
            entry = self._file(path)
            if entry is None:
                return False, None
            first = first or entry
        if first is None:
            return True, None  # nothing to look up (only images): the scan was asked for, that has to do
        series = self._get("/api/search/series-for-mangafile", {"mangaFileId": first.get("id") or first.get("Id")})
        if isinstance(series, dict):
            library, series_id = series.get("libraryId"), series.get("id")
            if isinstance(library, int) and isinstance(series_id, int):
                return True, (library, series_id)
        return True, None


@dataclass
class Finished:
    """A manga whose download is complete."""
    info_hash: str
    name: str  # "" if it is gone from qBittorrent
    paths: list[str] = field(default_factory=list)  # its chosen comic files as Kavita sees them
    scanned: bool = False  # Kavita was asked to scan it


@dataclass
class Seen:
    info_hash: str
    found: bool  # Kavita has the files
    series: tuple[int, int] | None = None  # (library id, series id) for the link


class ReaderSync:
    """The blocking part of 'tell when a manga is readable' (called in a thread); the bot keeps who waits."""

    def __init__(self, manga: MangaService, kavita: KavitaClient | None, library_dir: str = "/manga") -> None:
        self.manga = manga
        self.kavita = kavita  # None: tell as soon as qBittorrent is done, like before
        self.library_dir = library_dir.rstrip("/") or "/"

    def reader_paths(self, info_hash: str) -> tuple[str, list[str]]:
        """Where Kavita sees a manga: the folder to scan and its chosen comic files."""
        torrent = self.manga.existing(info_hash.lower())
        if torrent is None:
            raise MangaError("Diesen Manga gibt es in qBittorrent nicht (mehr).")
        files = [f for f in self.manga.qbit.files(info_hash) if isinstance(f, dict) and f.get("name")]
        names = [_norm(f["name"]) for f in files]
        root = self.library_dir.rstrip("/")
        paths = [f"{root}/{n}" for n, f in zip(names, files)
                 if int(f.get("priority") or 0) > 0 and n.lower().endswith(COMIC_EXTENSIONS)]
        # A pack with its own folder: scan just that. Single files lie in the library folder itself.
        tops = {n.split("/", 1)[0] for n in names if "/" in n}
        own_folder = len(tops) == 1 and all("/" in n for n in names)
        folder = f"{root}/{next(iter(tops))}" if own_folder else (root or "/")
        return folder, paths

    def start(self, hashes: list[str]) -> list[Finished]:
        """The ones of these that are done; for each, Kavita is asked to scan it (if Kavita is set up)."""
        done = []
        for info_hash, name in self.manga.finished(hashes):
            item = Finished(info_hash, name)
            if name and self.kavita is not None:
                try:
                    folder, item.paths = self.reader_paths(info_hash)
                    self.kavita.scan(folder)
                    item.scanned = True
                    log.info("Kavita scans %s", folder)
                except (MangaError, KavitaError, TorrentError) as exc:
                    log.warning("Kavita scan for %s failed: %s", name, exc)
            done.append(item)
        return done

    def check(self, waiting: dict[str, list[str]]) -> list[Seen]:
        """For scanned mangas (hash → paths): does Kavita have them yet? Leaves out the ones it couldn't ask."""
        if self.kavita is None:
            return []
        seen = []
        for info_hash, paths in waiting.items():
            try:
                found, series = self.kavita.find(paths)
            except KavitaError as exc:
                log.warning("Kavita check failed: %s", exc)
                continue
            seen.append(Seen(info_hash, found, series))
        return seen
