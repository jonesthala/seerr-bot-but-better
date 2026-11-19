"""Removes the torrents of a deleted movie or show from qBittorrent, files included, so the space is really freed.

Which torrents: the download ids (torrent hashes) Sonarr/Radarr recorded in their history for that title.
They have to be read *before* deleting, because Sonarr/Radarr drop the history together with the title.
Standard library only; the HTTP calls are injectable for the tests.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from .arr import ArrClient, ArrError
from .seerr import MAX_BYTES, USER_AGENT

log = logging.getLogger("seerrbot.torrents")

# (method, url, headers, body) → (HTTP status, lower-case response headers, raw body)
RawFetch = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]

TorrentError = ArrError  # one error type for Sonarr/Radarr and qBittorrent, shown to Discord users as it is
__all__ = ["ArrClient", "QbitClient", "TorrentCleaner", "TorrentError", "raw_fetch"]


def raw_fetch(method: str, url: str, headers: dict[str, str], body: bytes | None,
              timeout: float = 15.0) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (URL from settings)
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read(MAX_BYTES)
    except urllib.error.HTTPError as error:
        with error:
            return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read(MAX_BYTES)


class QbitClient:
    """qBittorrent's WebUI API (works with 4.x): login with user and password, then cookie SID."""

    def __init__(self, base_url: str, username: str, password: str, fetch: RawFetch = raw_fetch) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._fetch = fetch
        self._sid: str | None = None

    def _send(self, method: str, path: str, form: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        headers = {"Referer": self.base_url, "Origin": self.base_url, "User-Agent": USER_AGENT}
        if self._sid:
            headers["Cookie"] = f"SID={self._sid}"
        body = None
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body = urllib.parse.urlencode(form).encode()
        try:
            return self._fetch(method, f"{self.base_url}/api/v2{path}", headers, body)
        except Exception as exc:  # noqa: BLE001 - timeouts, refused
            log.warning("qBittorrent %s failed: %s", path, exc)
            raise TorrentError("qBittorrent ist nicht erreichbar.") from exc

    def _login(self) -> None:
        self._sid = None
        status, headers, body = self._send("POST", "/auth/login", {"username": self.username, "password": self.password})
        if status == 403:
            raise TorrentError("qBittorrent hat den Bot nach zu vielen Fehlversuchen kurz gesperrt. Später nochmals.")
        cookie = headers.get("set-cookie", "")
        sid = next((part.split("=", 1)[1] for part in cookie.split(";") if part.strip().startswith("SID=")), None)
        if status != 200 or body.strip() != b"Ok." or not sid:
            raise TorrentError("qBittorrent lehnt Benutzer oder Passwort ab. Ein Admin muss "
                               "`QBITTORRENT_USERNAME` und `QBITTORRENT_PASSWORD` prüfen.")
        self._sid = sid.strip()

    def _call(self, method: str, path: str, form: dict[str, str] | None = None) -> bytes:
        if self._sid is None:
            self._login()
        status, _, body = self._send(method, path, form)
        if status == 403:  # session expired
            self._login()
            status, _, body = self._send(method, path, form)
        if not 200 <= status < 300:
            raise TorrentError(f"qBittorrent meldet einen Fehler (HTTP {status}).")
        return body

    @staticmethod
    def _torrent_list(body: bytes) -> list[dict]:
        try:
            data = json.loads(body)
        except ValueError:
            return []
        return [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []

    def torrents(self, hashes: list[str]) -> list[dict]:
        if not hashes:
            return []
        return self._torrent_list(self._call("GET", "/torrents/info?" + urllib.parse.urlencode({"hashes": "|".join(hashes)})))

    def all_torrents(self) -> list[dict]:
        return self._torrent_list(self._call("GET", "/torrents/info"))

    def _switch(self, hashes: list[str], old: str, new: str) -> None:
        if not hashes:
            return
        form = {"hashes": "|".join(hashes)}
        try:
            self._call("POST", f"/torrents/{old}", form)
        except TorrentError as error:
            if "HTTP 404" not in str(error):
                raise
            self._call("POST", f"/torrents/{new}", form)  # qBittorrent 5 calls them stop and start

    def pause(self, hashes: list[str]) -> None:
        self._switch(hashes, "pause", "stop")

    def resume(self, hashes: list[str]) -> None:
        self._switch(hashes, "resume", "start")

    def delete(self, hashes: list[str]) -> None:
        """Removes the torrents and their files."""
        self._call("POST", "/torrents/delete", {"hashes": "|".join(hashes), "deleteFiles": "true"})


class TorrentCleaner:
    """Finds the torrents of a title before it is deleted and removes them afterwards."""

    def __init__(self, qbit: QbitClient, arrs: dict[str, ArrClient]) -> None:
        self.qbit = qbit
        self.arrs = arrs  # "tv" → Sonarr, "movie" → Radarr

    def prepare(self, kind: str, tmdb_id: int) -> list[dict]:
        """Before deleting: the title's torrents that qBittorrent still has. Raises TorrentError if Sonarr/Radarr
        or qBittorrent can't be asked, so nothing gets deleted half."""
        arr = self.arrs.get(kind)
        hashes = arr.download_ids(tmdb_id) if arr is not None else []
        return self.qbit.torrents(hashes)

    def remove(self, torrents: list[dict]) -> tuple[int, int]:
        """After deleting: removes them with their files; returns (how many, their size in bytes)."""
        hashes = [str(t["hash"]) for t in torrents if t.get("hash")]
        if not hashes:
            return 0, 0
        self.qbit.delete(hashes)
        return len(hashes), sum(t.get("size") or 0 for t in torrents if isinstance(t.get("size"), int))
