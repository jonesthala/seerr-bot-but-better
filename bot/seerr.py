"""Talks to the Seerr API (search, details, requests). Standard library only.

The HTTP call is injectable, so the tests run without a network or a Seerr server.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .core import (
    AVAILABLE,
    GB,
    ISSUE_TYPES,
    PARTIAL,
    NewMedia,
    OpenRequest,
    Requested,
    RequestState,
    SeerrError,
    Title,
    new_media,
    no_space_text,
    open_requests,
    pending_requests,
    pick_profiles,
    pick_server,
    request_state,
    root_folder_free,
    search_results,
    seasons_done,
    title_from,
    trending_results,
)

log = logging.getLogger("seerrbot.seerr")

USER_AGENT = "seerr-discord-bot/1.0"
MAX_BYTES = 5_000_000
MAX_QUERY = 100

# (method, url, headers, body) → (HTTP status, parsed JSON or None)
Fetch = Callable[[str, str, dict[str, str], bytes | None], tuple[int, Any]]

UNREACHABLE = "Seerr ist gerade nicht erreichbar. Versuch es in ein paar Minuten nochmals."


def http_fetch(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float = 15.0) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (URL from settings)
            status, raw = response.status, response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        with error:
            status, raw = error.code, error.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("response too large")
    try:
        return status, json.loads(raw) if raw.strip() else None
    except ValueError:
        return status, None


def _error_text(status: int, path: str) -> str:
    if status == 401:
        return "Seerr lehnt den API-Key ab. Ein Admin muss `SEERR_API_KEY` prüfen."
    if status == 403:
        if path == "/request":
            return "Seerr hat die Anfrage abgelehnt (keine Berechtigung oder Anfrage-Kontingent aufgebraucht)."
        return "Seerr erlaubt das nicht."
    if status == 404:
        return "Das gibt es in Seerr nicht (mehr)."
    if status == 409:
        return "Das ist schon angefragt."
    return f"Seerr meldet einen Fehler (HTTP {status}). Ein Admin findet die Details im Log."


class SeerrClient:
    def __init__(self, base_url: str, api_key: str, language: str = "de", user_id: int | None = None,
                 min_free: int = 15 * GB, fetch: Fetch = http_fetch) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.language = language
        self.user_id = user_id
        self.min_free = min_free
        self._fetch = fetch
        self._names: dict[tuple[str, int], str] = {}

    def _call(self, method: str, path: str, query: dict[str, Any] | None = None, body: dict | None = None,
              as_user: int | None = None) -> Any:
        url = f"{self.base_url}/api/v1{path}"
        if query:
            # Seerr wants reserved characters like ! ' ( ) percent-encoded, so quote with safe="".
            url += "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote, safe="")
        headers = {"X-Api-Key": self.api_key, "Accept": "application/json", "User-Agent": USER_AGENT}
        if as_user:
            # Act as that Seerr user instead of the key's owner, so its permissions apply.
            headers["X-API-User"] = str(as_user)
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        try:
            status, answer = self._fetch(method, url, headers, data)
        except Exception as exc:  # noqa: BLE001 - timeouts, DNS, refused, broken JSON
            log.warning("%s %s failed: %s", method, path, exc)
            raise SeerrError(UNREACHABLE) from exc
        if 200 <= status < 300:
            return answer
        detail = answer.get("message") if isinstance(answer, dict) else answer
        log.warning("%s %s → HTTP %s %s", method, path, status, detail)
        raise SeerrError(_error_text(status, path), status)

    # --- reading -----------------------------------------------------------
    def search(self, query: str, kind: str | None, library_only: bool = False) -> list[Title]:
        text = " ".join(query.split())[:MAX_QUERY]
        if not text:
            raise SeerrError("Gib einen Titel ein, zum Beispiel `/film Sintel`.")
        data = self._call("GET", "/search", {"query": text, "page": 1, "language": self.language})
        titles = search_results(data, kind, library_only)
        for title in titles:
            self._names[(title.kind, title.tmdb_id)] = title.label
        return titles

    def details(self, kind: str, tmdb_id: int) -> Title:
        data = self._call("GET", f"/{kind}/{int(tmdb_id)}", {"language": self.language})
        title = title_from(data, kind)
        if title is None:
            raise SeerrError("Seerr hat unerwartet geantwortet. Ein Admin findet die Details im Log.")
        self._names[(title.kind, title.tmdb_id)] = title.label
        return title

    def name_of(self, kind: str, tmdb_id: int) -> str:
        key = (kind, tmdb_id)
        if key not in self._names:
            try:
                self.details(kind, tmdb_id)
            except SeerrError:
                return f"{'Film' if kind == 'movie' else 'Serie'} #{tmdb_id}"
        return self._names[key]

    def open_requests(self, limit: int = 10) -> list[tuple[OpenRequest, str]]:
        """Requests waiting for approval, then those being searched or downloaded, newest first."""
        found: list[OpenRequest] = []
        for which in ("pending", "processing"):
            data = self._call("GET", "/request", {"take": limit, "skip": 0, "filter": which, "sort": "added"})
            found.extend(open_requests(data))
        seen: set[tuple[str, int]] = set()
        unique = []
        for item in found:
            if (item.kind, item.tmdb_id) not in seen:
                seen.add((item.kind, item.tmdb_id))
                unique.append(item)
        return [(item, self.name_of(item.kind, item.tmdb_id)) for item in unique[:limit]]

    def free_bytes(self, kind: str | None = None) -> int | None:
        """Free space where new movies (kind "movie"), shows ("tv") or both (None) go.

        Asks Seerr for the root folders of its Radarr/Sonarr servers. None if that is unknown,
        so a hiccup never blocks requests."""
        services = {"movie": ["radarr"], "tv": ["sonarr"]}.get(kind or "", ["radarr", "sonarr"])
        values = []
        for service in services:
            try:
                server = pick_server(self._call("GET", f"/service/{service}"))
                if server is None:
                    continue
                details = self._call("GET", f"/service/{service}/{server['id']}")
            except SeerrError:
                continue
            free = root_folder_free(details, server.get("activeDirectory"))
            if free is not None:
                values.append(free)
        return min(values) if values else None

    def request_state(self, request_id: int) -> RequestState | None:
        """One request as Seerr sees it now; None if it doesn't exist any more."""
        try:
            data = self._call("GET", f"/request/{int(request_id)}")
        except SeerrError as error:
            if error.status == 404:
                return None
            raise
        return request_state(data)

    def is_done(self, state: RequestState) -> bool:
        """Everything that was requested is in the library (for shows: all requested seasons)."""
        if state.media_status == AVAILABLE:
            return True
        if state.kind == "tv" and state.media_status == PARTIAL and state.seasons:
            return seasons_done(self.details("tv", state.tmdb_id), state.seasons)
        return False

    def pending_requests(self, limit: int = 20) -> list[RequestState]:
        """Requests waiting for approval, newest first."""
        data = self._call("GET", "/request", {"take": limit, "skip": 0, "filter": "pending", "sort": "added"})
        return pending_requests(data)

    def trending(self, kind: str | None = None, limit: int = 10) -> list[Title]:
        data = self._call("GET", "/discover/trending", {"page": 1, "language": self.language})
        titles = trending_results(data, kind, limit)
        for title in titles:
            self._names[(title.kind, title.tmdb_id)] = title.label
        return titles

    def recently_added(self, limit: int = 10) -> list[NewMedia]:
        """What came into the library last, newest first."""
        data = self._call("GET", "/media", {"take": limit, "skip": 0, "filter": "available", "sort": "mediaAdded"})
        return new_media(data)

    def profiles(self, kind: str, names: list[str]) -> list[tuple[int, str]]:
        """The quality profiles called names on the default Radarr ("movie") or Sonarr ("tv") server."""
        if not names:
            return []
        service = "radarr" if kind == "movie" else "sonarr"
        server = pick_server(self._call("GET", f"/service/{service}"))
        if server is None:
            return []
        found = pick_profiles(self._call("GET", f"/service/{service}/{server['id']}"), names)
        missing = {n.strip().lower() for n in names} - {name.lower() for _, name in found}
        if missing:
            log.warning("Quality profiles not found in %s: %s", service, ", ".join(sorted(missing)))
        return found

    # --- writing -----------------------------------------------------------
    def request(self, title: Title, seasons: list[int] | None = None, profile_id: int | None = None) -> Requested:
        """Creates a request; returns its status (1 = waits for approval, 2 = approved) and id."""
        if not title.requestable:
            raise SeerrError(f"**{title.label}**: {title.status_text()}")
        free = self.free_bytes(title.kind)
        if free is not None and free < self.min_free:
            raise SeerrError(no_space_text(free, self.min_free))
        body: dict[str, Any] = {"mediaType": title.kind, "mediaId": title.tmdb_id}
        if title.kind == "tv":
            wanted = sorted(set(seasons or title.requestable_seasons()))
            if not wanted:
                raise SeerrError(f"Von **{title.label}** gibt es keine Staffel, die noch fehlt.")
            body["seasons"] = wanted
        if profile_id is not None:
            body["profileId"] = profile_id
        # Seerr decides about auto-approval by who *makes* the request. With "userId" in the body the key's
        # owner (an admin) makes it, so it would always be approved at once. Acting as the user instead makes
        # Seerr apply that user's permissions: without auto-approve the request waits for an admin.
        data = self._call("POST", "/request", body=body, as_user=self.user_id)
        status = data.get("status") if isinstance(data, dict) else None
        request_id = data.get("id") if isinstance(data, dict) else None
        return Requested(status if isinstance(status, int) else 1, request_id if isinstance(request_id, int) else None)

    def set_request_status(self, request_id: int, action: str) -> None:
        """Approves ("approve") or declines ("decline") a request."""
        if action not in ("approve", "decline"):
            raise ValueError(action)
        self._call("POST", f"/request/{int(request_id)}/{action}")

    def report_issue(self, title: Title, issue_type: int, message: str,
                     season: int | None = None, episode: int | None = None) -> int | None:
        """Opens an issue in Seerr (1 picture, 2 sound, 3 subtitles, 4 other); returns its id."""
        if title.media_id is None or not title.in_library:
            raise SeerrError(f"**{title.label}** ist nicht in der Bibliothek, dazu kann man kein Problem melden.")
        if issue_type not in ISSUE_TYPES:
            raise ValueError(issue_type)
        body: dict[str, Any] = {"issueType": issue_type, "message": message.strip(), "mediaId": title.media_id}
        if title.kind == "tv":
            body["problemSeason"] = season or 0
            body["problemEpisode"] = (episode or 0) if season else 0  # an episode only makes sense with its season
        data = self._call("POST", "/issue", body=body)
        issue_id = data.get("id") if isinstance(data, dict) else None
        return issue_id if isinstance(issue_id, int) else None

    def delete(self, title: Title) -> None:
        """Removes a movie or show (all seasons) from Radarr/Sonarr with its files, then resets it in Seerr."""
        if not title.in_library or title.media_id is None:
            raise SeerrError(f"**{title.label}** ist nicht in der Bibliothek, da gibt es nichts zu löschen.")
        self._call("DELETE", f"/media/{title.media_id}/file")
        try:
            self._call("DELETE", f"/media/{title.media_id}")  # so it can be requested again later
        except SeerrError:
            log.warning("Files of %s are gone, but Seerr kept its entry", title.label)
