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
        # /abo: Discord user → Jellyfin series ids; per series its name and the episodes already announced
        self.abos: dict[int, list[str]] = {}
        self.abo_names: dict[str, str] = {}
        self.abo_known: dict[str, list[str]] = {}
        # automatic abos for show requests whose show is not in Jellyfin yet: TMDB id → (Discord users, since when)
        self.pending_abos: dict[int, tuple[list[int], float]] = {}
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
        abos = data.get("abos") if isinstance(data.get("abos"), dict) else {}
        users = abos.get("users") if isinstance(abos.get("users"), dict) else {}
        self.abos = {int(k): [str(s) for s in v] for k, v in users.items() if str(k).isdigit() and isinstance(v, list)}
        names, known = abos.get("names"), abos.get("known")
        self.abo_names = {str(k): str(v) for k, v in names.items()} if isinstance(names, dict) else {}
        self.abo_known = {str(k): [str(e) for e in v] for k, v in known.items() if isinstance(v, list)} \
            if isinstance(known, dict) else {}
        pending = abos.get("pending") if isinstance(abos.get("pending"), dict) else {}
        self.pending_abos = {int(k): ([int(u) for u in v.get("users", []) if str(u).isdigit()], float(v.get("since") or 0))
                             for k, v in pending.items() if str(k).isdigit() and isinstance(v, dict)}
        self.digest = str(data.get("digest") or "")

    def save(self) -> None:
        if self.path is None:
            return
        self._prune()
        data = {
            "requests": [asdict(t) for t in self.requests.values()],
            "approvals": [asdict(a) for a in self.approvals.values()],
            "friends": [asdict(f) for f in self.friends.values()],
            "abos": {"users": {str(k): v for k, v in self.abos.items()}, "names": self.abo_names, "known": self.abo_known,
                     "pending": {str(k): {"users": users, "since": since} for k, (users, since) in self.pending_abos.items()}},
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

    # --- /abo: new episodes of a show --------------------------------------------------
    def toggle_abo(self, user_id: int, series_id: str, name: str, known: list[str]) -> bool:
        """Subscribes, or unsubscribes if already subscribed. Returns True if subscribed now.
        known: the episodes there right now, so they aren't announced as new."""
        mine = self.abos.setdefault(user_id, [])
        if series_id in mine:
            mine.remove(series_id)
            if not mine:
                del self.abos[user_id]
            if not self.subscribers(series_id):  # nobody follows it any more
                self.abo_names.pop(series_id, None)
                self.abo_known.pop(series_id, None)
            self.save()
            return False
        mine.append(series_id)
        self.abo_names[series_id] = name
        if series_id not in self.abo_known:
            self.abo_known[series_id] = sorted(known)
        self.save()
        return True

    def subscribe(self, user_id: int, series_id: str, name: str, known: list[str]) -> None:
        """Like toggle_abo, but never unsubscribes (for the automatic abo after a request)."""
        if series_id not in self.abos.get(user_id, []):
            self.toggle_abo(user_id, series_id, name, known)

    def add_pending_abo(self, tmdb_id: int, user_id: int) -> None:
        users, since = self.pending_abos.get(tmdb_id, ([], self._now()))
        if user_id not in users:
            users.append(user_id)
        self.pending_abos[tmdb_id] = (users, since)
        self.save()

    def take_pending_abos(self, tmdb_id: int) -> list[int]:
        users, _ = self.pending_abos.pop(tmdb_id, ([], 0.0))
        self.save()
        return users

    def drop_old_pending_abos(self, days: int = 30) -> None:
        """A request that was declined never shows up in Jellyfin; forget it after a while."""
        cutoff = self._now() - days * 86400
        old = [k for k, (_, since) in self.pending_abos.items() if since < cutoff]
        for key in old:
            del self.pending_abos[key]
        if old:
            self.save()

    def user_abos(self, user_id: int) -> list[str]:
        return list(self.abos.get(user_id, []))

    def subscribers(self, series_id: str) -> list[int]:
        return sorted(user for user, shows in self.abos.items() if series_id in shows)

    def subscribed_series(self) -> list[str]:
        return sorted({s for shows in self.abos.values() for s in shows})

    def new_episodes(self, series_id: str, present: list[str]) -> list[str]:
        """Episodes not announced yet; remembers them as announced."""
        known = set(self.abo_known.get(series_id, []))
        new = sorted(set(present) - known)
        if new:
            self.abo_known[series_id] = sorted(known | set(new))
            self.save()
        return new

    # --- weekly digest -----------------------------------------------------------
    def set_digest(self, key: str) -> None:
        self.digest = key
        self.save()
