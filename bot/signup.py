"""Friends without Discord: requests from the web form (github.com/jonesthala/jellyfin-zugang, e.g. on Vercel).

The bot fetches open requests from the form's site (outbound only, the homelab stays closed), shows Approve/Decline
like for /jellyfin-zugang, creates the Jellyfin account itself and then asks the site to mail the guide with a
one-time link to the password, or a short no. A mail that can't go out is tried again on the next check.
Web friends are kept with the Discord ones in the store, with a negative id instead of a Discord user id.
Standard library only; the HTTP calls are injectable for the tests.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable

from .jellyfin import NAME_RE, suggest_name
from .seerr import USER_AGENT
from .store import FRIEND_APPROVED, FRIEND_DECLINED, FRIEND_PENDING, Friend
from .torrents import RawFetch, raw_fetch

log = logging.getLogger("seerrbot.signup")

DECLINED_WAIT_DAYS = 7  # after a "no", new requests from the same address are dropped quietly for this long
WEB_ICON = "🌐"

__all__ = ["SignupClient", "SignupError", "WebRequest", "arrival", "free_name", "mask_email", "wished_name"]


class SignupError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class WebRequest:
    id: str
    name: str
    email: str
    username: str  # wished Jellyfin user name, may be empty
    created: float  # seconds


class SignupClient:
    def __init__(self, base_url: str, token: str, fetch: RawFetch = raw_fetch) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._fetch = fetch

    def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json", "User-Agent": USER_AGENT}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        try:
            status, _, raw = self._fetch(method, f"{self.base_url}{path}", headers, data)
        except Exception as exc:  # noqa: BLE001 - timeouts, DNS, refused
            log.warning("Signup site %s failed: %s", path, exc)
            raise SignupError("Die Webseite für Zugangsanfragen ist nicht erreichbar.") from exc
        try:
            answer = json.loads(raw) if raw.strip() else {}
        except ValueError:
            answer = {}
        if status == 401:
            raise SignupError("Die Webseite lehnt den Bot ab: `SIGNUP_TOKEN` passt nicht zu `BOT_TOKEN` dort.", 401)
        if not 200 <= status < 300:
            reason = str(answer.get("fehler") or "") if isinstance(answer, dict) else ""
            raise SignupError(f"Die Webseite meldet HTTP {status}" + (f": {reason}" if reason else "."), status)
        return answer

    def pending(self) -> list[WebRequest]:
        data = self._call("GET", "/api/bot/anfragen")
        found = data.get("anfragen") if isinstance(data, dict) else None
        requests = []
        for item in found if isinstance(found, list) else []:
            if not isinstance(item, dict) or not item.get("id") or not item.get("email"):
                continue
            requests.append(WebRequest(str(item["id"]), str(item.get("name") or "")[:60], str(item["email"]).lower(),
                                       str(item.get("benutzer") or ""), float(item.get("zeit") or 0) / 1000))
        return requests

    def approve(self, request_id: str, username: str, password: str, server_url: str, share_url: str,
                new: bool) -> None:
        """The site mails the guide with a one-time link to the password, then forgets the request."""
        self._call("POST", "/api/bot/freigeben", {"id": request_id, "benutzer": username, "passwort": password,
                                                   "server": server_url, "tailscale": share_url, "neu": new})

    def decline(self, request_id: str, notify: bool) -> None:
        self._call("POST", "/api/bot/ablehnen", {"id": request_id, "mail": notify})


def _ascii(text: str) -> str:
    """'Jürg Zoë' → 'Juerg Zoe', so names with accents still give a user name."""
    for umlaut, plain_text in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss")):
        text = text.replace(umlaut, plain_text)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def wished_name(request: WebRequest) -> str:
    """The user name to start from: the wished one, else one made from the name or the e-mail."""
    if request.username and NAME_RE.fullmatch(request.username):
        return request.username
    return suggest_name(_ascii(request.name)) or suggest_name(request.email.split("@", 1)[0]) or "freund"


def free_name(wanted: str, taken: Callable[[str], bool]) -> str:
    """`wanted`, or with a number at the end if that is taken (max.muster → max.muster2)."""
    if not taken(wanted):
        return wanted
    for number in range(2, 100):
        name = f"{wanted[:20 - len(str(number))]}{number}"
        if not taken(name):
            return name
    raise SignupError("Für diese Anfrage findet der Bot keinen freien Benutzernamen.")


def mask_email(email: str) -> str:
    """'m***@example.com' for the approval card, which other admins may see."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def arrival(existing: Friend | None, now: float) -> str:
    """What to do with a new web request, given what the bot knows about this e-mail address:
    'new', 'reopen' (had an account), 'renew' (has access, wants new login data), 'replace' (already waiting:
    take the newer request), or 'drop' (declined less than a week ago)."""
    if existing is None:
        return "new"
    if existing.state == FRIEND_PENDING:
        return "replace"
    if existing.state == FRIEND_APPROVED:
        return "renew"
    if existing.state == FRIEND_DECLINED and now - existing.decided < DECLINED_WAIT_DAYS * 86400:
        return "drop"
    return "reopen" if existing.jellyfin_id else "new"


def card_text(friend: Friend) -> str:
    """The approval card for a web request (Discord markdown; the name is escaped by the caller)."""
    who = f"🔑 **{friend.user_name}** {WEB_ICON} ({mask_email(friend.email)}, über die Webseite)"
    if friend.renew:
        return f"{who} möchte neue Zugangsdaten für **{friend.jellyfin_name}**. Der Zugang besteht schon."
    again = " Das Konto gab es schon, es wird wieder geöffnet." if friend.jellyfin_id else ""
    return f"{who} möchte Zugang zu Jellyfin als **{friend.jellyfin_name}**.{again}"


_UNSAFE = re.compile(r"[*_`~|>\\]")


def plain(text: str) -> str:
    """A name from the web form without Discord markdown characters."""
    return _UNSAFE.sub("", text).strip() or "?"
