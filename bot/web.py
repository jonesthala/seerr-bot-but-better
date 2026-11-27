"""Admin panel in the browser, served by the bot itself when PANEL_PORT is set.

The page (panel_static/) and a JSON API behind it that uses the bot's own logic, the same as the slash commands and
buttons. Discord stays in step: an approval clicked here closes the buttons there, the requester still gets a message.
Login with a Jellyfin administrator account. Plain HTTP, so only for the home network and Tailscale.
"""
from __future__ import annotations

import asyncio
import logging
import math
import mimetypes
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord
from aiohttp import ClientError, ClientSession, ClientTimeout, web

from .arr import ArrError
from .core import GB, REQUEST_PENDING, SeerrError, Title, declined_text, size_text
from .discord_bot import approved_text, decide_friend_request, escape, login_view
from .extras import BRAKE_MARGIN, first_season_to_pick, parse_start, per_episode_estimate, plan_episodes, plan_lines
from .jellyfin import JellyfinError
from .manga import MangaError, Release, overview_label, part_rows, short_name
from .notes import NotesError, read_notes
from .panel import (
    SESSION_COOKIE,
    SESSION_DAYS,
    LoginLimiter,
    PanelError,
    calendar_days,
    episode_rows,
    follow_data,
    follow_request_data,
    friend_data,
    jellyfin_login,
    load_key,
    number_list,
    progress_data,
    read_session,
    season_rows,
    sign_session,
    title_data,
    torrent_data,
    watched_data,
)
from .store import FRIEND_APPROVED, FRIEND_PENDING, Follow

if TYPE_CHECKING:
    from .discord_bot import SeerrBot

log = logging.getLogger("seerrbot.panel")

STATIC = Path(__file__).with_name("panel_static")
STATIC_FILES = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css", "/icon.svg": "icon.svg",
                "/manifest.webmanifest": "manifest.webmanifest", "/icon-180.png": "icon-180.png",
                "/icon-192.png": "icon-192.png", "/icon-512.png": "icon-512.png"}
mimetypes.add_type("application/manifest+json", ".webmanifest")  # "add to home screen" as an app
MAX_BODY = 64 * 1024
PANEL = "Admin-Panel"
ADDED_MINUTES = 30  # a show added to Sonarr only to look at its episodes goes again after this
MC_TIMEOUT = ClientTimeout(total=900)  # a world backup can take minutes
CSP = ("default-src 'self'; img-src 'self' https: data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
_HASH_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_MC_PATH_RE = re.compile(r"^[a-z]+(/[a-z]+)?$")
TORRENT_ORDER = {"downloading": 0, "stalled": 1, "queued": 2, "paused": 3, "error": 4, "seeding": 5, "done": 6, "other": 7}


# --- small helpers for JSON bodies --------------------------------------------------------------
async def _body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError:
        raise PanelError("Die Anfrage ist ungültig.") from None
    if not isinstance(data, dict):
        raise PanelError("Die Anfrage ist ungültig.")
    return data


def _whole(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if type(value) is not int or value < 0:
        raise PanelError("Die Anfrage ist ungültig.")
    return value


def _choice(data: dict[str, Any], key: str, options: tuple[str, ...]) -> str:
    value = data.get(key)
    if value not in options:
        raise PanelError("Die Anfrage ist ungültig.")
    return str(value)


def _text(data: dict[str, Any], key: str, limit: int = 200) -> str:
    value = data.get(key)
    return value.strip()[:limit] if isinstance(value, str) else ""


def _kind(value: Any) -> str:
    if value not in ("movie", "tv"):
        raise PanelError("Die Anfrage ist ungültig.")
    return str(value)


def _secure(response: web.StreamResponse) -> None:
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers.setdefault("Cache-Control", "no-store")


class Panel:
    def __init__(self, bot: "SeerrBot") -> None:
        self.bot = bot
        self.settings = bot.settings
        self.key = load_key(bot.settings.data_dir or None)
        self.limiter = LoginLimiter()
        self.added: dict[int, float] = {}  # Sonarr shows added only for the episode picker → when
        self.mc: ClientSession | None = None
        self.runner: web.AppRunner | None = None
        self._sweeper: asyncio.Task | None = None

    @property
    def owner(self) -> int | None:
        """The Discord account the panel acts for: abos, 'it's there' messages."""
        return self.settings.panel_user_id

    # --- app --------------------------------------------------------------------------------
    def app(self) -> web.Application:
        @web.middleware
        async def guard(request: web.Request, handler):
            path = request.path
            if path.startswith("/api/") and path != "/api/login":
                user = read_session(request.cookies.get(SESSION_COOKIE), self.key, time.time())
                if user is None:
                    response = web.json_response({"error": "Bitte zuerst anmelden."}, status=401)
                    _secure(response)
                    return response
                request["user"] = user
            if request.method not in ("GET", "HEAD"):
                if request.headers.get("X-Panel") != "1":  # a form or page elsewhere can't set this header
                    response = web.json_response({"error": "Die Anfrage ist ungültig."}, status=403)
                    _secure(response)
                    return response
                if path != "/api/login":
                    log.info("Panel: %s %s by %s", request.method, path, request.get("user"))
            try:
                response = await handler(request)
            except (PanelError, SeerrError, ArrError, JellyfinError, NotesError, MangaError) as error:
                response = web.json_response({"error": str(error)}, status=400)
            except web.HTTPException as error:
                _secure(error)
                raise
            except Exception:  # noqa: BLE001 - the page gets a short message, the details go to the log
                log.exception("Panel call %s %s failed", request.method, path)
                response = web.json_response({"error": "Da ist etwas schiefgelaufen. Die Details stehen im Bot-Log."},
                                             status=500)
            _secure(response)
            return response

        app = web.Application(middlewares=[guard], client_max_size=MAX_BODY)
        app.add_routes([web.get(path, self.static) for path in STATIC_FILES])
        app.add_routes([
            web.post("/api/login", self.login),
            web.post("/api/logout", self.logout),
            web.get("/api/session", self.session),
            web.get("/api/overview", self.overview),
            web.get("/api/inbox", self.inbox),
            web.post("/api/inbox/approval", self.decide_approval),
            web.post("/api/inbox/watched", self.decide_watched),
            web.post("/api/inbox/follow", self.decide_follow),
            web.post("/api/inbox/friend", self.decide_friend),
            web.get("/api/downloads", self.downloads),
            web.post("/api/downloads/{action:pause|resume}", self.torrent_action),
            web.get("/api/search", self.search),
            web.get("/api/title", self.title),
            web.post("/api/request", self.make_request),
            web.post("/api/delete", self.delete),
            web.post("/api/episodes/open", self.episodes_open),
            web.post("/api/episodes/fetch", self.episodes_fetch),
            web.post("/api/episodes/close", self.episodes_close),
            web.get("/api/follows", self.follows),
            web.post("/api/follows/add", self.follow_add),
            web.post("/api/follows/stop", self.follow_stop),
            web.get("/api/calendar", self.calendar),
            web.get("/api/todos", self.todos),
            web.get("/api/manga", self.manga_list),
            web.get("/api/manga/search", self.manga_search),
            web.post("/api/manga/open", self.manga_open),
            web.post("/api/manga/load", self.manga_load),
            web.post("/api/manga/delete", self.manga_delete),
            web.route("*", "/api/mc/{tail:.+}", self.mc_proxy),
        ])
        return app

    async def start(self) -> None:
        self.mc = ClientSession(timeout=MC_TIMEOUT)
        self.runner = web.AppRunner(self.app(), access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "0.0.0.0", self.settings.panel_port).start()
        self._sweeper = asyncio.create_task(self._sweep())
        log.info("Admin panel listening on port %s", self.settings.panel_port)

    async def _sweep(self) -> None:
        """Takes shows out of Sonarr again that were added only to look at their episodes, and nothing was fetched."""
        while True:
            await asyncio.sleep(300)
            for series_id, when in list(self.added.items()):
                if time.time() - when > ADDED_MINUTES * 60:
                    self.added.pop(series_id, None)
                    try:
                        await asyncio.to_thread(self.bot.arrs["tv"].remove_series, series_id)
                    except Exception:  # noqa: BLE001
                        log.warning("Could not remove show %s from Sonarr again", series_id)

    async def static(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC / STATIC_FILES[request.path], headers={"Cache-Control": "no-cache"})

    # --- login ------------------------------------------------------------------------------
    async def login(self, request: web.Request) -> web.Response:
        address = request.remote or "?"
        now = time.time()
        wait = self.limiter.wait(address, now)
        if wait:
            minutes = math.ceil(wait / 60)
            return web.json_response({"error": f"Zu viele Fehlversuche. Versuch es in {minutes} "
                                               f"{'Minute' if minutes == 1 else 'Minuten'} nochmals."}, status=429)
        data = await _body(request)
        name = _text(data, "name", 100)
        password = data.get("password") if isinstance(data.get("password"), str) else ""
        if not name or not password:
            raise PanelError("Bitte Benutzername und Passwort eingeben.")
        user, problem = await asyncio.to_thread(jellyfin_login, self.settings.jellyfin_api_url, name, password[:256])
        if user is None:
            if "erreichbar" not in problem:
                self.limiter.failed(address, now)
            log.warning("Panel login refused for %r from %s: %s", name, address, problem)
            return web.json_response({"error": problem}, status=401)
        self.limiter.succeeded(address)
        log.info("Panel login: %s from %s", user, address)
        response = web.json_response({"user": user})
        response.set_cookie(SESSION_COOKIE, sign_session(user, self.key, now), max_age=SESSION_DAYS * 86400,
                            httponly=True, samesite="Strict", path="/")
        return response

    async def logout(self, request: web.Request) -> web.Response:
        response = web.json_response({"ok": True})
        response.del_cookie(SESSION_COOKIE, path="/")
        return response

    async def session(self, request: web.Request) -> web.Response:
        bot = self.bot
        return web.json_response({
            "user": request["user"],
            "mc": bool(self.settings.panel_mc_url),
            "tv": "tv" in bot.arrs,
            "movie": "movie" in bot.arrs,
            "qbit": bot.qbit is not None,
            "jellyfin": bot.jellyfin is not None,
            "discord_owner": self.owner is not None,
            "todos": bool(self.settings.panel_todo_dir),
            "manga": bot.manga is not None,
            "manga_reader": self.settings.manga_reader_url,
        })

    # --- overview ---------------------------------------------------------------------------
    def brake_info(self) -> dict[str, Any]:
        limit = self.settings.brake_free_gb * GB
        return {"enabled": self.bot.qbit is not None and limit > 0, "active": self.bot.store.brake_active,
                "limit": limit, "resume_at": limit + BRAKE_MARGIN}

    async def inbox_counts(self) -> dict[str, int]:
        store = self.bot.store
        try:
            approvals = len(await asyncio.to_thread(self.bot.seerr.pending_requests))
        except SeerrError:
            approvals = 0
        return {
            "approvals": approvals,
            "watched": sum(1 for q in store.watch_questions.values() if q.state == "open"),
            "follows": sum(1 for f in store.follows.values() if f.state == "pending"),
            "friends": sum(1 for f in store.all_friends() if f.state == FRIEND_PENDING),
        }

    async def overview(self, request: web.Request) -> web.Response:
        """Like /status: every open request and every download without one, with numbers instead of text."""
        bot = self.bot
        items = await asyncio.to_thread(bot.seerr.open_requests, 20)
        free = await asyncio.to_thread(bot.seerr.free_bytes)
        now = datetime.now(timezone.utc)
        requested = {(i.kind, i.tmdb_id) for i, _ in items}
        extra: list[tuple[str, int, str]] = []
        details: dict[tuple[str, int], Any] = {}
        problems: list[str] = []
        for kind, arr in bot.arrs.items():
            try:
                queued = await asyncio.to_thread(arr.queued_titles)
                extra += [(kind, tmdb_id, name) for tmdb_id, name in queued.items() if (kind, tmdb_id) not in requested]
                ids = [i.tmdb_id for i, _ in items if i.kind == kind and i.request_status != REQUEST_PENDING]
                ids += [tmdb_id for k, tmdb_id, _ in extra if k == kind]
                if ids:
                    found = await asyncio.to_thread(arr.progress, ids, now)
                    details.update({(kind, tmdb_id): progress for tmdb_id, progress in found.items()})
            except ArrError as error:
                problems.append(str(error))
        live: dict[str, dict] = {}
        hashes = [d.hash for p in details.values() for d in p.downloads if d.hash and d.state == "downloading"]
        if bot.qbit is not None and hashes:
            try:
                live = {str(t.get("hash")).lower(): t for t in await asyncio.to_thread(bot.qbit.torrents, hashes)}
            except ArrError:
                pass  # without speed
        cards = []
        for item, name in items:
            tracked = bot.store.request(item.request_id)
            progress = details.get((item.kind, item.tmdb_id))
            cards.append({
                "kind": item.kind, "tmdb_id": item.tmdb_id, "name": name, "poster": await bot.poster(item.kind, item.tmdb_id),
                "who": tracked.user_name if tracked else item.requested_by, "request": True,
                "pending": item.request_status == REQUEST_PENDING, "state_text": item.state(now),
                "progress": progress_data(item.kind, progress, live, now) if progress else None,
            })
        for kind, tmdb_id, name in extra:
            progress = details.get((kind, tmdb_id))
            if progress is None:
                continue
            cards.append({
                "kind": kind, "tmdb_id": tmdb_id, "name": name, "poster": await bot.poster(kind, tmdb_id), "who": "",
                "request": False, "pending": False, "state_text": "", "progress": progress_data(kind, progress, live, now),
            })
        active = sum(1 for c in cards if c["progress"] for d in c["progress"]["downloads"] if d["state"] == "downloading")
        todos = None
        if self.settings.panel_todo_dir:
            try:
                found = await asyncio.to_thread(self._notes)
                todos = {key: sum(n[key] for n in found["notes"]) for key in ("open", "important", "ideas")}
            except NotesError:
                todos = None
        return web.json_response({
            "todos": todos,
            "free": free, "min_free": self.settings.min_free_gb * GB, "brake": self.brake_info(), "cards": cards,
            "speed": sum(c["progress"]["speed"] for c in cards if c["progress"]), "active": active,
            "problems": problems, "inbox": await self.inbox_counts(),
        })

    # --- inbox: everything waiting for an admin ------------------------------------------------
    async def inbox(self, request: web.Request) -> web.Response:
        bot, store = self.bot, self.bot.store
        approvals = []
        for state in await asyncio.to_thread(bot.seerr.pending_requests):
            tracked = store.request(state.request_id)
            approvals.append({
                "id": state.request_id, "kind": state.kind, "tmdb_id": state.tmdb_id,
                "name": await asyncio.to_thread(bot.seerr.name_of, state.kind, state.tmdb_id),
                "seasons": list(state.seasons), "who": tracked.user_name if tracked else state.requested_by,
                "poster": await bot.poster(state.kind, state.tmdb_id),
            })
        watched = [watched_data(q) for q in sorted(store.watch_questions.values(), key=lambda q: q.created)
                   if q.state == "open"]
        follows = [follow_request_data(f) for f in store.follows.values() if f.state == "pending"]
        friends = [friend_data(f) for f in store.all_friends() if f.state == FRIEND_PENDING]
        return web.json_response({"approvals": approvals, "watched": watched, "follows": follows, "friends": friends})

    async def _edit(self, channel_id: int, message_id: int, text: str) -> None:
        """Updates a bot message in Discord and takes its buttons away; quietly skipped if that fails."""
        if not channel_id or not message_id:
            return
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            await channel.get_partial_message(message_id).edit(content=text[:2000], view=None)  # type: ignore[union-attr]
        except (discord.HTTPException, AttributeError):
            log.info("Could not update Discord message %s", message_id)

    async def decide_approval(self, request: web.Request) -> web.Response:
        bot = self.bot
        data = await _body(request)
        request_id, action = _whole(data, "id"), _choice(data, "action", ("approve", "decline"))
        who = escape(request["user"])
        try:
            await asyncio.to_thread(bot.seerr.set_request_status, request_id, action)
            note = (f"✅ Freigegeben von {who} ({PANEL})." if action == "approve"
                    else f"❌ Abgelehnt von {who} ({PANEL}).")
            message = "Freigegeben, wird jetzt gesucht." if action == "approve" else "Abgelehnt."
        except SeerrError as error:
            if error.status != 404:
                raise
            note, action, message = "🗑️ Die Anfrage gibt es nicht mehr.", "gone", "Die Anfrage gibt es nicht mehr."
        approval = bot.store.approval(request_id)
        if approval is not None:
            await bot.close_approval_message(approval, note)
        tracked = bot.store.request(request_id)
        if action == "decline" and tracked is not None and not tracked.notified:
            try:
                await bot.post(declined_text(f"<@{tracked.user_id}>", escape(tracked.label)))
            except discord.HTTPException:
                log.warning("Could not tell %s about the declined request", tracked.user_id)
            bot.store.mark_notified(request_id)
        return web.json_response({"message": message})

    async def decide_watched(self, request: web.Request) -> web.Response:
        bot = self.bot
        data = await _body(request)
        qid, action = _whole(data, "id"), _choice(data, "action", ("delete", "keep"))
        who = escape(request["user"])
        question = bot.store.watch_questions.get(qid)
        if question is None or question.state != "open":
            raise PanelError("Diese Frage ist schon beantwortet.")
        async with bot._watch_lock:
            if question.state != "open":
                raise PanelError("Diese Frage ist schon beantwortet.")
            if action == "keep":
                question.state, question.answer = "kept", f"👍 Behalten ({who}, {PANEL})."
                message = "Behalten."
            else:
                message = await bot.delete_watched(question)
                question.state, question.answer = "deleted", f"{message} ({who}, {PANEL})"
            bot.store.save()
        for channel_id, message_id in question.messages:
            await self._edit(channel_id, message_id, f"{question.text}\n\n{question.answer}")
        return web.json_response({"message": message})

    async def decide_follow(self, request: web.Request) -> web.Response:
        bot = self.bot
        data = await _body(request)
        fid, action = _whole(data, "id"), _choice(data, "action", ("ok", "no"))
        follow = bot.store.follows.get(fid)
        if follow is None or follow.state != "pending":
            raise PanelError("Dieser Wunsch ist schon erledigt.")
        who = escape(request["user"])
        if action == "no":
            follow.state = "declined"
            bot.store.save()
            result, message = f"❌ Abgelehnt von {who} ({PANEL}).", "Abgelehnt."
            await self._tell(follow.user_id, f"❌ **{escape(follow.label)}** zu verfolgen wurde abgelehnt.")
        else:
            ok, text = await bot.run_follow(follow)
            if not ok:
                raise PanelError(text)
            result, message = f"✅ Freigegeben von {who} ({PANEL}).\n{text}", text
            await self._tell(follow.user_id, f"✅ Freigegeben:\n{text}")
        await self._edit(follow.channel_id, follow.message_id, f"{follow.card_text}\n\n{result}")
        return web.json_response({"message": message})

    async def decide_friend(self, request: web.Request) -> web.Response:
        bot = self.bot
        if bot.jellyfin is None:
            raise PanelError("Jellyfin-Zugänge sind nicht eingerichtet.")
        data = await _body(request)
        user_id = data.get("id")
        if type(user_id) is not int:  # negative: a friend from the web form
            raise PanelError("Die Anfrage ist ungültig.")
        action = _choice(data, "action", ("approve", "decline"))
        friend, note = await decide_friend_request(bot, user_id, action, f"{escape(request['user'])} ({PANEL})")
        if friend is None:
            raise PanelError("Diese Anfrage ist schon erledigt.")
        if friend.from_web:
            await self._edit(friend.channel_id, friend.message_id, f"{friend.card_text}\n{note} Die Mail geht gleich raus.")
            return web.json_response({"message": f"{'Freigegeben' if action == 'approve' else 'Abgelehnt'}: "
                                                 f"{friend.user_name} bekommt gleich eine Mail."})
        await self._edit(friend.channel_id, friend.message_id, f"{friend.card_text}\n{note}")
        if friend.state == FRIEND_APPROVED:
            await self._tell(user_id, approved_text(friend.jellyfin_name), login_view(user_id))
            message = f"Freigegeben: {friend.jellyfin_name} bekommt die Anleitung per Discord."
        else:
            await self._tell(user_id, "❌ Deine Anfrage für einen Zugang zu Jellyfin wurde abgelehnt.")
            message = "Abgelehnt."
        return web.json_response({"message": message})

    async def _tell(self, user_id: int, text: str, view: discord.ui.View | None = None) -> None:
        try:
            await self.bot.tell(user_id, text, view)
        except discord.HTTPException:
            log.warning("Could not message %s", user_id)

    # --- downloads ------------------------------------------------------------------------------
    async def downloads(self, request: web.Request) -> web.Response:
        if self.bot.qbit is None:
            raise PanelError("qBittorrent ist nicht eingerichtet (QBITTORRENT_URL).")
        torrents = [torrent_data(t) for t in await asyncio.to_thread(self.bot.qbit.all_torrents)]
        torrents.sort(key=lambda t: (TORRENT_ORDER.get(t["state"], 9), -t["added"]))
        free = await asyncio.to_thread(self.bot.seerr.free_bytes)
        return web.json_response({"torrents": torrents, "brake": self.brake_info(), "free": free,
                                  "min_free": self.settings.min_free_gb * GB})

    async def torrent_action(self, request: web.Request) -> web.Response:
        if self.bot.qbit is None:
            raise PanelError("qBittorrent ist nicht eingerichtet (QBITTORRENT_URL).")
        hashes = (await _body(request)).get("hashes")
        if not isinstance(hashes, list) or not 0 < len(hashes) <= 500 or not all(
                isinstance(h, str) and _HASH_RE.match(h) for h in hashes):
            raise PanelError("Die Auswahl ist ungültig.")
        action = request.match_info["action"]
        await asyncio.to_thread(self.bot.qbit.pause if action == "pause" else self.bot.qbit.resume, hashes)
        note = ""
        if action == "resume" and self.bot.store.brake_active:
            note = " Die Notbremse ist aktiv und hält sie in einer Minute wieder an, solange zu wenig Platz frei ist."
        return web.json_response({"message": ("Angehalten." if action == "pause" else "Läuft wieder.") + note})

    # --- search, request, delete -----------------------------------------------------------------
    async def search(self, request: web.Request) -> web.Response:
        query = request.query.get("q", "")
        kind = request.query.get("kind") or None
        if kind is not None:
            kind = _kind(kind)
        titles = await asyncio.to_thread(self.bot.seerr.search, query, kind, request.query.get("library") == "1")
        return web.json_response({"results": [title_data(t) for t in titles]})

    async def title(self, request: web.Request) -> web.Response:
        bot = self.bot
        kind = _kind(request.query.get("kind"))
        try:
            tmdb_id = int(request.query.get("id", ""))
        except ValueError:
            raise PanelError("Die Anfrage ist ungültig.") from None
        found = await asyncio.to_thread(bot.seerr.details, kind, tmdb_id)
        rate = await bot.size_rate()
        data = title_data(found)
        data.update({
            "profiles": [{"id": pid, "name": name} for pid, name in bot.profiles.get(kind, [])],
            "free": await asyncio.to_thread(bot.seerr.free_bytes, kind),
            "min_free": self.settings.min_free_gb * GB,
            "rate": {"typical": rate.typical, "low": rate.low, "high": rate.high, "measured": rate.titles > 0},
            "episode_minutes": found.runtime or 45,
            "followed": bool(bot.store.active_follows(tmdb_id=tmdb_id)) if kind == "tv" else False,
            "seerr_url": self.settings.page_url(kind, tmdb_id),
            "sonarr": kind == "tv" and "tv" in bot.arrs,
        })
        return web.json_response(data)

    async def make_request(self, request: web.Request) -> web.Response:
        bot = self.bot
        data = await _body(request)
        kind, tmdb_id = _kind(data.get("kind")), _whole(data, "tmdb_id")
        seasons = number_list(data["seasons"], 200) if data.get("seasons") else None
        profile_id = data.get("profile_id")
        if profile_id is not None and profile_id not in [pid for pid, _ in bot.profiles.get(kind, [])]:
            raise PanelError("Dieses Qualitätsprofil gibt es nicht.")
        found = await asyncio.to_thread(bot.seerr.details, kind, tmdb_id)
        result = await asyncio.to_thread(bot.seerr.request, found, seasons, profile_id)
        notify = result.request_id is not None and self.owner is not None
        if notify:
            bot.store.add_request(result.request_id, kind, tmdb_id, found.label, self.owner, PANEL)
        what = "Der Film" if kind == "movie" else "Die Serie"
        text = (f"⏳ Angefragt! {what} wartet noch auf die Freigabe." if result.status == REQUEST_PENDING
                else f"✅ Angefragt! {what} wird jetzt gesucht und geladen.")
        if notify:
            text += " Sobald alles da ist, sagt der Bot im Discord Bescheid."
            note = await bot.auto_abo(self.owner, found)
            text += f"\n{note}" if note else ""
        if result.status == REQUEST_PENDING:
            bot.in_background(bot.sync_approvals())
        return web.json_response({"message": text})

    async def delete(self, request: web.Request) -> web.Response:
        data = await _body(request)
        kind, tmdb_id = _kind(data.get("kind")), _whole(data, "tmdb_id")
        found = await asyncio.to_thread(self.bot.seerr.details, kind, tmdb_id)
        if not found.in_library:
            raise PanelError(f"{found.label} ist nicht (mehr) in der Bibliothek.")
        deleted, text = await self.bot.delete_title(found)
        return web.json_response({"deleted": deleted, "message": text})

    # --- episode picker -------------------------------------------------------------------------
    def _sonarr(self):
        sonarr = self.bot.arrs.get("tv")
        if sonarr is None:
            raise PanelError("Sonarr ist nicht eingerichtet (SONARR_URL).")
        return sonarr

    async def _episode_view(self, show: dict, episodes: list[dict]) -> dict[str, Any]:
        bot = self.bot
        sonarr = self._sonarr()
        now = datetime.now(timezone.utc)
        loading = await asyncio.to_thread(sonarr.queued_ids)
        rate = await bot.size_rate()
        anime = show.get("seriesType") == "anime"
        rows = episode_rows(episodes, loading, now, self.settings.zone, anime)
        poster = next((i.get("remoteUrl") for i in show.get("images") or []
                       if isinstance(i, dict) and i.get("coverType") == "poster" and i.get("remoteUrl")), None)
        year = show.get("year")
        return {
            "series_id": show["id"], "tmdb_id": show.get("tmdbId"), "poster": poster, "anime": anime,
            "label": f"{show.get('title', '?')} ({year})" if year else str(show.get("title", "?")),
            "added": show["id"] in self.added, "free": await asyncio.to_thread(bot.seerr.free_bytes, "tv"),
            "min_free": self.settings.min_free_gb * GB, "per_episode": per_episode_estimate(show, rate.typical),
            "seasons": season_rows(rows), "first_season": first_season_to_pick(episodes), "episodes": rows,
        }

    async def episodes_open(self, request: web.Request) -> web.Response:
        self._sonarr()
        tmdb_id = _whole(await _body(request), "tmdb_id")
        show, episodes, added, problem = await self.bot.ensure_in_sonarr(tmdb_id)
        if show is None:
            raise PanelError(problem)
        if added:
            self.added[show["id"]] = time.time()
        if not episodes:
            raise PanelError(problem or "Sonarr kennt von dieser Serie noch keine Folgen.")
        return web.json_response(await self._episode_view(show, episodes))

    async def episodes_fetch(self, request: web.Request) -> web.Response:
        bot, sonarr = self.bot, self._sonarr()
        data = await _body(request)
        series_id, ids = _whole(data, "series_id"), set(number_list(data.get("episode_ids")))
        show = next((s for s in await asyncio.to_thread(sonarr.series) if s.get("id") == series_id), None)
        if show is None:
            raise PanelError("Diese Serie ist nicht (mehr) in Sonarr. Bitte die Folgen-Auswahl neu öffnen.")
        episodes = await asyncio.to_thread(sonarr.episodes, series_id)
        loading = await asyncio.to_thread(sonarr.queued_ids)
        chosen = [e for e in episodes if e.get("id") in ids and not e.get("hasFile") and e.get("id") not in loading]
        if not chosen:
            raise PanelError("Ausgewählt ist nichts, was noch fehlt.")
        rate = await bot.size_rate()
        plan = plan_episodes(chosen, datetime.now(timezone.utc), per_episode_estimate(show, rate.typical))
        await asyncio.to_thread(sonarr.want_episodes, show, [e["id"] for e in plan.get + plan.later],
                                [e["id"] for e in plan.get])
        self.added.pop(series_id, None)
        count = len(plan.get)
        lines = [f"✅ **Sonarr sucht jetzt {count} {'Folge' if count == 1 else 'Folgen'}.**" if count
                 else "✅ **Vorgemerkt:** Sonarr holt die Folgen, sobald sie erscheinen."]
        free = await asyncio.to_thread(bot.seerr.free_bytes, "tv")
        lines += plan_lines(plan, free, self.settings.min_free_gb * GB)
        tmdb_id = show.get("tmdbId")
        if self.owner is not None and isinstance(tmdb_id, int) and tmdb_id > 0:
            note = await bot.auto_abo(self.owner, Title("tv", tmdb_id, str(show.get("title") or ""),
                                                        str(show.get("year") or ""), "", None, 0))
            if note:
                lines.append(note)
        return web.json_response({"message": "\n".join(lines)})

    async def episodes_close(self, request: web.Request) -> web.Response:
        series_id = _whole(await _body(request), "series_id")
        if self.added.pop(series_id, None) is not None:
            await asyncio.to_thread(self._sonarr().remove_series, series_id)
            return web.json_response({"message": "Nichts geholt, die Serie ist wieder aus Sonarr entfernt."})
        return web.json_response({"message": ""})

    # --- follows and calendar ----------------------------------------------------------------------
    async def follows(self, request: web.Request) -> web.Response:
        sonarr = self._sonarr()
        now = datetime.now(timezone.utc)
        upcoming = await asyncio.to_thread(sonarr.calendar, now, now + timedelta(days=120))
        first: dict[Any, dict] = {}
        for e in sorted(upcoming, key=lambda e: str(e.get("airDateUtc"))):
            first.setdefault(e.get("seriesId"), e)
        rows = [follow_data(f, first.get(f.series_id), self.settings.zone)
                for f in sorted(self.bot.store.active_follows(), key=lambda f: f.label.casefold())]
        return web.json_response({"follows": rows})

    async def follow_add(self, request: web.Request) -> web.Response:
        self._sonarr()
        data = await _body(request)
        tmdb_id, start = _whole(data, "tmdb_id"), _text(data, "start", 20)
        new_seasons = data.get("new_seasons") is not False
        try:
            parse_start(start)
        except ValueError as error:
            raise PanelError(str(error)) from None
        found = await asyncio.to_thread(self.bot.seerr.details, "tv", tmdb_id)
        follow = Follow(self.owner or 0, PANEL, tmdb_id, found.label, start, new_seasons)
        ok, text = await self.bot.run_follow(follow)
        if not ok:
            raise PanelError(text)
        self.bot.store.add_follow(follow)
        return web.json_response({"message": text})

    async def follow_stop(self, request: web.Request) -> web.Response:
        sonarr = self._sonarr()
        store = self.bot.store
        follow = store.follows.get(_whole(await _body(request), "id"))
        if follow is None or follow.state != "active":
            raise PanelError("Diese Serie wird nicht (mehr) verfolgt.")
        store.drop_follow(follow.fid)
        label = escape(follow.label)
        if store.active_follows(tmdb_id=follow.tmdb_id):
            return web.json_response({"message": f"⏹️ Beendet. Andere verfolgen **{label}** weiter, darum holt Sonarr "
                                                 "die neuen Folgen weiter."})
        series = next((s for s in await asyncio.to_thread(sonarr.series) if s.get("tmdbId") == follow.tmdb_id), None)
        if series is None:
            return web.json_response({"message": f"⏹️ Beendet. **{label}** ist nicht in Sonarr."})
        count = await asyncio.to_thread(sonarr.unfollow, series, datetime.now(timezone.utc))
        return web.json_response({"message": f"⏹️ **{label}** wird nicht mehr verfolgt: {count} angekündigte Folgen und "
                                             "neue Staffeln werden nicht geholt. Was schon da ist, bleibt."})

    async def calendar(self, request: web.Request) -> web.Response:
        bot, sonarr = self.bot, self._sonarr()
        try:
            days = max(1, min(31, int(request.query.get("days", "14"))))
        except ValueError:
            days = 14
        now = datetime.now(timezone.utc)
        today = datetime.now(self.settings.zone).replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = today.astimezone(timezone.utc), (today + timedelta(days=days)).astimezone(timezone.utc)
        episodes = await asyncio.to_thread(sonarr.calendar, start, end)
        loading = await asyncio.to_thread(sonarr.queued_ids)
        movies: list[dict] = []
        loading_movies: set[int] = set()
        radarr = bot.arrs.get("movie")
        if radarr is not None:
            movies = await asyncio.to_thread(radarr.calendar, start, end)
            loading_movies = await asyncio.to_thread(radarr.queued_ids)
        return web.json_response({"days": calendar_days(episodes, movies, loading, loading_movies, start, end, now,
                                                        self.settings.zone), "length": days})

    # --- to-dos: checklists and ideas from Markdown notes (read only) -------------------------------
    def _notes(self) -> dict[str, Any]:
        s = self.settings
        return read_notes(s.panel_todo_dir, s.panel_todo_files, s.panel_obsidian_vault)

    async def todos(self, request: web.Request) -> web.Response:
        if not self.settings.panel_todo_dir:
            raise PanelError("Die To-do-Seite ist nicht eingerichtet (PANEL_TODO_DIR).")
        return web.json_response(await asyncio.to_thread(self._notes))

    # --- manga: search in Prowlarr, load chosen volumes with qBittorrent, read in Kavita ---------------
    def _manga(self):
        if self.bot.manga is None:
            raise PanelError("Manga ist nicht eingerichtet (PROWLARR_URL und qBittorrent).")
        return self.bot.manga

    def _space(self, free: int | None) -> dict[str, Any]:
        return {"free": free, "min_free": self.settings.min_free_gb * GB, "brake": self.brake_info()}

    async def manga_list(self, request: web.Request) -> web.Response:
        manga = self._manga()
        items = await asyncio.to_thread(manga.items)
        for item in items:
            item["short"] = short_name(item["name"])
        free = await asyncio.to_thread(self.bot.seerr.free_bytes)
        return web.json_response({"items": items, "reader": self.settings.manga_reader_url, **self._space(free)})

    async def manga_search(self, request: web.Request) -> web.Response:
        manga = self._manga()
        releases = await asyncio.to_thread(manga.search, request.query.get("q", ""))
        have = {t.get("hash", "").lower() for t in await asyncio.to_thread(manga.qbit.torrents_in, manga.category)}
        return web.json_response({"results": [{
            "id": r.nyaa_id, "title": r.title, "short": short_name(r.title), "size": r.size, "seeders": r.seeders,
            "leechers": r.leechers, "date": r.published, "have": bool(r.info_hash) and r.info_hash in have,
        } for r in releases]})

    async def _picker(self, info_hash: str) -> dict[str, Any]:
        torrent, parts, by_path = await asyncio.to_thread(self._manga().manga, info_hash)
        return {"mode": "more", "hash": info_hash, "title": str(torrent.get("name") or "?"),
                "parts": part_rows(parts, by_path), "overview": overview_label(parts), "total": sum(p.size for p in parts)}

    async def manga_open(self, request: web.Request) -> web.Response:
        """The parts of a search result (fetches its torrent file) or of a manga that is there, to pick from."""
        manga = self._manga()
        data = await _body(request)
        if data.get("hash") is not None:
            info_hash = str(data.get("hash")).lower()
            if not _HASH_RE.match(info_hash):
                raise PanelError("Die Anfrage ist ungültig.")
            answer = await self._picker(info_hash)
        else:
            release = Release(_whole(data, "id"), _text(data, "title", 300) or "?", 0, 0, 0, "", "")
            key, prepared = await asyncio.to_thread(manga.prepare, release)
            info_hash = prepared.torrent.info_hash
            if await asyncio.to_thread(manga.existing, info_hash) is not None:
                answer = await self._picker(info_hash)
            else:
                answer = {"mode": "new", "key": key, "title": release.title, "parts": part_rows(prepared.parts),
                          "overview": overview_label(prepared.parts), "total": sum(p.size for p in prepared.parts)}
        answer["short"] = short_name(answer["title"])
        answer.update(self._space(await asyncio.to_thread(self.bot.seerr.free_bytes)))
        return web.json_response(answer)

    async def manga_load(self, request: web.Request) -> web.Response:
        manga = self._manga()
        data = await _body(request)
        chosen = number_list(data.get("parts"), 2000)
        if not chosen:
            raise PanelError("Ausgewählt ist nichts.")
        key, info_hash = data.get("key"), data.get("hash")
        if isinstance(info_hash, str) and _HASH_RE.match(info_hash.lower()):
            mode = "more"
        elif isinstance(key, str) and re.fullmatch(r"[0-9a-f]{32}", key):
            mode = "new"
        else:
            raise PanelError("Die Anfrage ist ungültig.")
        if mode == "more":
            _, parts, by_path = await asyncio.to_thread(manga.manga, info_hash.lower())
            rows = part_rows(parts, by_path)
            size = sum(parts[i].size for i in set(chosen) if 0 <= i < len(parts) and not rows[i]["have"])
        else:
            parts = manga.prepared(key).parts
            size = sum(parts[i].size for i in set(chosen) if 0 <= i < len(parts))
        free = await asyncio.to_thread(self.bot.seerr.free_bytes)
        if free is not None and free - size < self.settings.min_free_gb * GB:
            raise PanelError(f"Zu wenig Platz: frei sind {size_text(free)}, nach dem Laden wären es "
                             f"{size_text(max(free - size, 0))}. Unter {self.settings.min_free_gb} GB lädt der Bot nichts.")
        if mode == "more":
            info_hash, what, size = await asyncio.to_thread(manga.extend, info_hash.lower(), chosen)
        else:
            info_hash, what, size = await asyncio.to_thread(manga.add, key, chosen)
        if self.owner is not None:
            self.bot.store.manga_wait(info_hash, what, self.owner)
        text = f"⏬ **{what}** ({size_text(size)}) wird geladen."
        text += " Wenn alles da ist, schreibt dir der Bot im Discord." if self.owner is not None else ""
        if self.bot.store.brake_active:
            text += "\nDie Notbremse ist aktiv: Es geht erst weiter, wenn wieder genug Platz frei ist."
        return web.json_response({"message": text, "hash": info_hash})

    async def manga_delete(self, request: web.Request) -> web.Response:
        manga = self._manga()
        info_hash = str((await _body(request)).get("hash") or "").lower()
        if not _HASH_RE.match(info_hash):
            raise PanelError("Die Anfrage ist ungültig.")
        name, size = await asyncio.to_thread(manga.delete, info_hash)
        self.bot.store.manga_done(info_hash)
        return web.json_response({"message": f"🗑️ **{short_name(name)}** gelöscht, {size_text(size)} frei geworden."})

    # --- Minecraft: passed on to the Minecraft bot's admin API ----------------------------------
    async def mc_proxy(self, request: web.Request) -> web.Response:
        if not self.settings.panel_mc_url or self.mc is None:
            raise PanelError("Der Minecraft-Bot ist nicht verbunden (PANEL_MC_URL).")
        tail = request.match_info["tail"]
        if request.method not in ("GET", "POST") or not _MC_PATH_RE.match(tail):
            raise web.HTTPNotFound()
        body = await request.read() if request.method == "POST" else None
        headers = {"Authorization": f"Bearer {self.settings.panel_mc_token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with self.mc.request(request.method, f"{self.settings.panel_mc_url}/api/{tail}",
                                       params=request.query, data=body, headers=headers) as answer:
                status, payload = answer.status, await answer.read()
        except (ClientError, asyncio.TimeoutError):
            raise PanelError("Der Minecraft-Bot ist gerade nicht erreichbar.") from None
        if status == 401:
            log.error("The Minecraft bot refused PANEL_MC_TOKEN")
            return web.json_response({"error": "Der Minecraft-Bot lehnt den Zugang ab (PANEL_MC_TOKEN stimmt nicht)."},
                                     status=502)
        return web.Response(body=payload, status=status, content_type="application/json")


async def start_panel(bot: "SeerrBot") -> Panel:
    panel = Panel(bot)
    await panel.start()
    return panel
