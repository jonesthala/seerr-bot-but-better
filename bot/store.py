"""What the bot remembers between restarts, in one small JSON file. Standard library only.

- who requested what through Discord (for /status, the "it's there" message and the limit per person)
- the approval messages it posted, so it can update them
- the week of the last weekly digest
- who asked for a Jellyfin account (/jellyfin-zugang) and how it was decided; never a password

Without a writable data folder the bot keeps this in memory only and logs why.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("seerrbot.store")

FILE_NAME = "state.json"
KEEP_DAYS = 90  # finished requests are forgotten after this; longer than any sensible limit window
KEEP_APPROVALS = 200  # closed approval messages to remember


@dataclass
class Tracked:
    request_id: int
    kind: str
    tmdb_id: int
    label: str
    user_id: int
    user_name: str
    created: float
    notified: bool = False  # the requester was told it's there, declined or failed


@dataclass
class Approval:
    request_id: int
    channel_id: int
    message_id: int
    text: str
    closed: bool = False


FRIEND_PENDING, FRIEND_APPROVED, FRIEND_DECLINED, FRIEND_REVOKED = "pending", "approved", "declined", "revoked"


@dataclass
class Friend:
    user_id: int  # Discord
    user_name: str  # Discord display name when asking
    jellyfin_name: str
    created: float
    state: str = FRIEND_PENDING
    jellyfin_id: str = ""  # set once the account exists, kept when access is taken away
    channel_id: int = 0  # the approval message
    message_id: int = 0
    card_text: str = ""
    decided: float = 0.0


def _load_items(cls: type, items: Any) -> list:
    names = {f.name for f in fields(cls)}
    loaded = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            try:
                loaded.append(cls(**{key: value for key, value in item.items() if key in names}))
            except TypeError:
                log.warning("Skipping a broken %s entry", cls.__name__)
    return loaded


class Store:
    def __init__(self, directory: str | os.PathLike | None, now: Callable[[], float] = time.time) -> None:
        self.path = Path(directory) / FILE_NAME if directory else None
        self._now = now
        self.requests: dict[int, Tracked] = {}
        self.approvals: dict[int, Approval] = {}
        self.friends: dict[int, Friend] = {}
        self.digest = ""
        self._load()

    # --- file ------------------------------------------------------------------
    def _load(self) -> None:
        if self.path is None:
            log.warning("No data folder: requests are only remembered until the bot restarts")
            return
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            log.error("Could not read %s, starting empty: %s", self.path, exc)
            return
        if not isinstance(data, dict):
            return
        self.requests = {t.request_id: t for t in _load_items(Tracked, data.get("requests"))}
        self.approvals = {a.request_id: a for a in _load_items(Approval, data.get("approvals"))}
        self.friends = {f.user_id: f for f in _load_items(Friend, data.get("friends"))}
        self.digest = str(data.get("digest") or "")

    def save(self) -> None:
        if self.path is None:
            return
        self._prune()
        data = {
            "requests": [asdict(t) for t in self.requests.values()],
            "approvals": [asdict(a) for a in self.approvals.values()],
            "friends": [asdict(f) for f in self.friends.values()],
            "digest": self.digest,
        }
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            tmp = None
        except OSError as exc:
            log.error("Could not save %s: %s", self.path, exc)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _prune(self) -> None:
        cutoff = self._now() - KEEP_DAYS * 86400
        self.requests = {k: t for k, t in self.requests.items() if not (t.notified and t.created < cutoff)}
        closed = sorted(k for k, a in self.approvals.items() if a.closed)
        for key in closed[:-KEEP_APPROVALS] if len(closed) > KEEP_APPROVALS else []:
            del self.approvals[key]

    # --- requests ------------------------------------------------------------
    def add_request(self, request_id: int, kind: str, tmdb_id: int, label: str, user_id: int, user_name: str) -> None:
        self.requests[request_id] = Tracked(request_id, kind, tmdb_id, label, user_id, user_name, self._now())
        self.save()

    def request(self, request_id: int | None) -> Tracked | None:
        return self.requests.get(request_id) if request_id is not None else None

    def waiting(self) -> list[Tracked]:
        """Requests whose requester hasn't been told the outcome yet, oldest first."""
        return sorted((t for t in self.requests.values() if not t.notified), key=lambda t: t.created)

    def mark_notified(self, request_id: int) -> None:
        tracked = self.requests.get(request_id)
        if tracked is not None and not tracked.notified:
            tracked.notified = True
            self.save()

    def request_times(self, user_id: int) -> list[float]:
        return [t.created for t in self.requests.values() if t.user_id == user_id]

    # --- approval messages ------------------------------------------------------
    def approval(self, request_id: int) -> Approval | None:
        return self.approvals.get(request_id)

    def add_approval(self, request_id: int, channel_id: int, message_id: int, text: str) -> None:
        self.approvals[request_id] = Approval(request_id, channel_id, message_id, text)
        self.save()

    def open_approvals(self) -> list[Approval]:
        return [a for a in self.approvals.values() if not a.closed]

    def close_approval(self, request_id: int) -> None:
        approval = self.approvals.get(request_id)
        if approval is not None and not approval.closed:
            approval.closed = True
            self.save()

    # --- Jellyfin accounts for friends -------------------------------------------------
    def friend(self, user_id: int) -> Friend | None:
        return self.friends.get(user_id)

    def all_friends(self) -> list[Friend]:
        return sorted(self.friends.values(), key=lambda f: f.created)

    def save_friend(self, friend: Friend) -> None:
        self.friends[friend.user_id] = friend
        self.save()

    def name_taken(self, name: str, user_id: int) -> bool:
        """Another person already asked for this Jellyfin name (case doesn't matter)."""
        wanted = name.casefold()
        return any(f.jellyfin_name.casefold() == wanted and f.user_id != user_id
                   and f.state != FRIEND_DECLINED for f in self.friends.values())

    # --- weekly digest -----------------------------------------------------------
    def set_digest(self, key: str) -> None:
        self.digest = key
        self.save()
