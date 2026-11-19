"""Discord side: the slash commands, pickers under search results, approval buttons and background checks."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import random
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks

from .config import Settings
from .core import (
    AVAILABLE,
    GB,
    ISSUE_TYPES,
    PARTIAL,
    PENDING,
    PROCESSING,
    REQUEST_DECLINED,
    REQUEST_FAILED,
    REQUEST_PENDING,
    NewMedia,
    SeerrError,
    SpaceWatch,
    Title,
    added_since,
    approval_text,
    declined_text,
    deleted_text,
    digest_key,
    done_text,
    failed_text,
    RULE_OF_THUMB,
    SizeRate,
    format_numbers,
    help_text,
    limit_text,
    rate_from_library,
    space_text,
    new_line,
    next_allowed,
    parse_time,
    request_done_text,
    shorten,
    size_text,
    unmanaged_text,
)
from .jellyfin import (
    TAILSCALE_ADMIN,
    JellyfinClient,
    JellyfinError,
    check_name,
    new_episodes_text,
    suggest_name,
    usage_line,
    welcome_text,
)
from .seerr import SeerrClient
from .arr import ArrClient, ArrError, Progress, card, speed_text
from .extras import (
    BRAKE_MARGIN,
    DOWNLOADING,
    MAX_EPISODES,
    EpisodePlan,
    brake_decision,
    brake_pulled_text,
    brake_released_text,
    event_description,
    event_end,
    episodes_deleted_text,
    item_label,
    parse_episodes,
    parse_when,
    per_episode_estimate,
    pick_candidates,
    pick_facts,
    plan_episodes,
    plan_lines,
    select_episodes,
    watched_questions,
    watched_text,
)
from .torrents import QbitClient, TorrentCleaner, TorrentError
from .store import (FRIEND_APPROVED, FRIEND_DECLINED, FRIEND_PENDING, FRIEND_REVOKED, Approval, Friend, Store,
                    WatchQuestion)

log = logging.getLogger("seerrbot.discord")

PICK_TIMEOUT = 300  # seconds a search result stays clickable
SPACE_CHECK_MINUTES = 30
WATCH_MINUTES = 2  # new requests to approve, finished downloads to announce
DIGEST_CHECK_MINUTES = 10
ABO_MINUTES = 10  # how often subscribed shows are checked for new episodes
SERIES_CACHE_SECONDS = 600  # the list of shows for /abo suggestions
RATE_CACHE_SECONDS = 3600  # GB per hour measured in the library, for the size estimate
AUTOCOMPLETE_SECONDS = 2.5  # Discord gives up after 3 s
MAX_CHECKS_PER_RUN = 30
BRAKE_MINUTES = 1  # the download brake looks this often at the free space
LIBRARY_CACHE_SECONDS = 600  # movies, shows and genres for /zufall and /filmabend
WATCHED_MINUTES = 5  # how often Jellyfin is asked what was watched
NO_ROLE = "🔒 Anfragen dürfen nur Leute mit der Rolle für Film- und Serienwünsche."
NO_ADMIN = "🔒 Löschen dürfen nur Admins."
NO_DELETING = "🔒 Löschen ist nicht eingerichtet: Es fehlen `DISCORD_ADMIN_USER_IDS` oder `DISCORD_ADMIN_ROLE_ID`."
NO_APPROVE = "🔒 Freigeben und Ablehnen dürfen nur Admins."
NO_FRIEND_ADMIN = "🔒 Das dürfen nur Admins."
NOT_YOURS = "🔒 Dieser Knopf ist nicht für dich."

# A title picked from the autocomplete list arrives as "movie:45745"
_PICKED = re.compile(r"^(movie|tv):(\d{1,9})$")

_COLORS = {
    AVAILABLE: discord.Color.green(),
    PARTIAL: discord.Color.gold(),
    PENDING: discord.Color.orange(),
    PROCESSING: discord.Color.blue(),
}


class NotAllowed(app_commands.CheckFailure):
    """A failed check whose message can be shown as it is."""


def escape(text: str) -> str:
    return discord.utils.escape_markdown(text)


def title_embed(bot: "SeerrBot", title: Title, footer: str | None = None) -> discord.Embed:
    embed = discord.Embed(
        title=title.label,
        description=title.overview or "Keine Beschreibung vorhanden.",
        url=bot.settings.page_url(title.kind, title.tmdb_id),
        color=_COLORS.get(title.status, discord.Color.blurple()),
    )
    embed.set_author(name=title.kind_text)
    if title.poster:
        embed.set_thumbnail(url=title.poster)
    embed.add_field(name="Status", value=title.status_text(), inline=False)
    if title.seasons_text():
        embed.add_field(name="Staffeln", value=title.seasons_text(), inline=False)
    if footer:
        embed.set_footer(text=footer)
    return embed


def watch_view(url: str | None) -> discord.ui.View | None:
    """A 'Jetzt ansehen' link button, if there is a link."""
    if not url:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Jetzt ansehen", emoji="▶️", url=url))
    return view


async def report_error(interaction: discord.Interaction, error: BaseException) -> None:
    if isinstance(error, NotAllowed):
        message = str(error)
    elif isinstance(error, app_commands.CheckFailure):
        message = "🔒 Das darfst du hier nicht."
    elif isinstance(error, (SeerrError, JellyfinError, TorrentError, ArrError)):
        message = f"⚠️ {error}"
    else:
        log.error("Interaction failed", exc_info=error)
        message = "⚠️ Da ist etwas schiefgelaufen. Die Details stehen im Bot-Log."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        log.warning("Could not send the error message")


class GuardedTree(app_commands.CommandTree):
    """Answers only in the configured channel and turns errors into short messages."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        channel_id = interaction.client.settings.channel_id  # type: ignore[attr-defined]
        if interaction.channel_id == channel_id:
            return True
        if interaction.type is not discord.InteractionType.autocomplete:
            await interaction.response.send_message(f"Bitte im <#{channel_id}> benutzen.", ephemeral=True)
        return False

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if not isinstance(error, app_commands.CheckFailure):
            error = getattr(error, "original", error)
        await report_error(interaction, error)


class SeerrBot(discord.Client):
    def __init__(self, settings: Settings, seerr: SeerrClient, store: Store | None = None,
                 jellyfin: JellyfinClient | None = None, torrents: TorrentCleaner | None = None,
                 arrs: dict[str, ArrClient] | None = None, qbit: QbitClient | None = None) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        # Only the requester may be pinged, never @everyone or roles (titles come from TMDB).
        mentions = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)
        super().__init__(intents=intents, allowed_mentions=mentions)
        self.settings = settings
        self.seerr = seerr
        self.jellyfin = jellyfin  # None: /jellyfin-zugang is not set up
        self.torrents = torrents  # None: /löschen leaves the torrents in qBittorrent
        self.arrs = arrs or {}  # "tv" → Sonarr, "movie" → Radarr: details in /status
        self.qbit = qbit  # speed and sources of running downloads in /status
        self.store = store if store is not None else Store(settings.data_dir or None)
        self.tree = GuardedTree(self)
        self.space = SpaceWatch(settings.min_free_gb * GB)
        self.profiles: dict[str, list[tuple[int, str]]] = {"movie": [], "tv": []}
        self._approval_lock = asyncio.Lock()
        self._watch_lock = asyncio.Lock()
        self._friend_lock = asyncio.Lock()
        self.login_clicks: dict[int, float] = {}  # Discord user → when the last password was made
        self._series: tuple[float, list[tuple[str, str, int | None]]] = (0.0, [])  # (when loaded, shows) for /abo
        self.status_clicks: dict[int, float] = {}  # status message → last refresh
        self._posters: dict[tuple[str, int], str | None] = {}  # for the /status cards
        self._rate: tuple[float, SizeRate] = (0.0, RULE_OF_THUMB)  # (when measured, GB per hour)
        self._library: tuple[float, list[dict], list[str]] = (0.0, [], [])  # (when loaded, items, genres)
        self._sonarr: tuple[float, list[dict]] = (0.0, [])  # (when loaded, shows) for /folgen suggestions
        self._background: set[asyncio.Task] = set()

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.settings.guild_id)
        for command in build_commands(self):
            self.tree.add_command(command, guild=guild)
        await self.tree.sync(guild=guild)
        self.add_dynamic_items(ApprovalButton, FriendButton, LoginButton, StatusRefresh, WatchedButton)  # after a restart
        await self.load_profiles()
        self.space_loop.start()
        self.watch_loop.start()
        if self.qbit is not None and self.settings.brake_free_gb > 0:
            self.brake_loop.start()
        if self.jellyfin is not None and self.settings.watched_ask and self.settings.has_admins:
            self.watched_loop.start()
        if self.settings.weekly_digest:
            self.digest_loop.start()
        if self.jellyfin is not None:
            self.abo_loop.start()

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)

    # --- helpers ---------------------------------------------------------------
    async def post(self, text: str | None, *, view: discord.ui.View | None = None,
                   embed: discord.Embed | None = None, channel_id: int | None = None) -> discord.Message:
        channel_id = channel_id or self.settings.channel_id
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        kwargs: dict = {}
        if view is not None:
            kwargs["view"] = view
        if embed is not None:
            kwargs["embed"] = embed
        return await channel.send(text, **kwargs)  # type: ignore[union-attr]

    async def tell(self, user_id: int, text: str, view: discord.ui.View | None = None) -> None:
        """A direct message; if the person doesn't take DMs, in the channel with a mention."""
        try:
            user = self.get_user(user_id) or await self.fetch_user(user_id)
            await user.send(text, view=view) if view is not None else await user.send(text)
            return
        except discord.HTTPException:
            log.info("No DM possible to %s, using the channel", user_id)
        await self.post(f"<@{user_id}> {text}", view=view)

    def in_background(self, coroutine) -> None:
        async def run() -> None:
            try:
                await coroutine
            except Exception:  # noqa: BLE001 - logged, the next regular check catches up
                log.exception("Background task failed")

        task = asyncio.create_task(run())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def may_request(self, interaction: discord.Interaction) -> bool:
        role_id = self.settings.request_role_id
        if role_id is None:
            return True
        return any(role.id == role_id for role in getattr(interaction.user, "roles", []))

    def is_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in self.settings.admin_user_ids:
            return True
        role_id = self.settings.admin_role_id
        return role_id is not None and any(role.id == role_id for role in getattr(interaction.user, "roles", []))

    def limit_reached(self, interaction: discord.Interaction) -> str | None:
        """The message to show if this person used up their requests; admins have no limit."""
        limit, days = self.settings.request_limit, self.settings.request_limit_days
        if not limit or self.is_admin(interaction):
            return None
        until = next_allowed(self.store.request_times(interaction.user.id), limit, days, time.time())
        if until is None:
            return None
        return limit_text(limit, days, datetime.fromtimestamp(until, self.settings.zone))

    async def load_profiles(self) -> None:
        for kind in ("movie", "tv"):
            names = list(self.settings.profile_names(kind))
            if names and not self.profiles[kind]:
                try:
                    self.profiles[kind] = await asyncio.to_thread(self.seerr.profiles, kind, names)
                except SeerrError:
                    log.warning("Could not load the quality profiles for %s, trying again later", kind)

    async def new_embed(self, items: list[NewMedia], heading: str) -> discord.Embed:
        lines = []
        for item in items:
            name = await asyncio.to_thread(self.seerr.name_of, item.kind, item.tmdb_id)
            shown = dataclasses.replace(item, url=self.settings.watch_url(item.url))
            lines.append(new_line(shown, escape(name), self.settings.zone))
        return discord.Embed(title=heading, description="\n".join(lines)[:4000], color=discord.Color.green())

    # --- background checks ---------------------------------------------------------
    @tasks.loop(minutes=SPACE_CHECK_MINUTES)
    async def space_loop(self) -> None:
        """Posts once when free space gets low and once when there is enough again."""
        try:
            message = self.space.update(await asyncio.to_thread(self.seerr.free_bytes))
            if message:
                await self.post(message)
        except Exception:  # noqa: BLE001 - the loop must keep running
            log.exception("Free space check failed")

    @tasks.loop(minutes=WATCH_MINUTES)
    async def watch_loop(self) -> None:
        for check, what in ((self.sync_approvals, "Approval"), (self.check_requests, "Request"),
                            (self.load_profiles, "Profile")):
            try:
                await check()
            except Exception:  # noqa: BLE001 - the loop must keep running
                log.exception("%s check failed", what)

    @tasks.loop(minutes=DIGEST_CHECK_MINUTES)
    async def digest_loop(self) -> None:
        try:
            await self.post_digest()
        except Exception:  # noqa: BLE001 - the loop must keep running
            log.exception("Weekly digest failed")

    @tasks.loop(minutes=ABO_MINUTES)
    async def abo_loop(self) -> None:
        try:
            await self.check_abos()
        except Exception:  # noqa: BLE001 - the loop must keep running
            log.exception("Abo check failed")

    @tasks.loop(minutes=BRAKE_MINUTES)
    async def brake_loop(self) -> None:
        try:
            await self.check_brake()
        except Exception:  # noqa: BLE001 - the loop must keep running
            log.exception("Download brake check failed")

    @tasks.loop(minutes=WATCHED_MINUTES)
    async def watched_loop(self) -> None:
        try:
            await self.check_watched()
        except Exception:  # noqa: BLE001 - the loop must keep running
            log.exception("Watched check failed")

    @space_loop.before_loop
    @watch_loop.before_loop
    @digest_loop.before_loop
    @abo_loop.before_loop
    @brake_loop.before_loop
    @watched_loop.before_loop
    async def _wait_until_ready(self) -> None:
        await self.wait_until_ready()

    async def series_list(self) -> list[tuple[str, str, int | None]]:
        """The shows in Jellyfin, (id, 'Name (Jahr)', TMDB id), reloaded every few minutes."""
        loaded, shows = self._series
        if self.jellyfin is not None and (not shows or time.monotonic() - loaded > SERIES_CACHE_SECONDS):
            shows = await asyncio.to_thread(self.jellyfin.series_list)
            self._series = (time.monotonic(), shows)
        return shows

    async def size_rate(self) -> SizeRate:
        """GB per hour of the own library, measured at most once an hour; the rule of thumb without Jellyfin."""
        measured, rate = self._rate
        if self.jellyfin is not None and (not measured or time.monotonic() - measured > RATE_CACHE_SECONDS):
            try:
                rate = rate_from_library(await asyncio.to_thread(self.jellyfin.size_rates))
                self._rate = (time.monotonic(), rate)
            except Exception:  # noqa: BLE001 - the estimate is a nice-to-have, keep the last value
                log.warning("Could not measure the library for the size estimate")
        return rate

    async def check_abos(self) -> None:
        """Tells subscribers about episodes that came into Jellyfin since the last check."""
        if self.jellyfin is None:
            return
        await self.resolve_pending_abos()
        for series_id in self.store.subscribed_series():
            try:
                present = await asyncio.to_thread(self.jellyfin.episodes, series_id)
            except JellyfinError as error:
                if error.status is None:
                    return  # Jellyfin is down; try again next time
                log.warning("Abo: could not read the episodes of %s: %s", series_id, error)
                continue
            new = self.store.new_episodes(series_id, list(present))
            if not new:
                continue
            name = escape(self.store.abo_names.get(series_id, "einer Serie"))
            text = new_episodes_text(name, {key: escape(present[key]) for key in new})
            for user_id in self.store.subscribers(series_id):
                await self.tell(user_id, text)

    async def auto_abo(self, user_id: int, title: Title) -> str:
        """After a show request: subscribe the requester, so new episodes arrive as DM. Returns the note for them."""
        if self.jellyfin is None or title.kind != "tv":
            return ""
        try:
            show = next((s for s in await self.series_list() if s[2] == title.tmdb_id), None)
            if show is None:  # not in Jellyfin yet: subscribe as soon as it shows up
                self.store.add_pending_abo(title.tmdb_id, user_id)
            else:
                present = await asyncio.to_thread(self.jellyfin.episodes, show[0])
                self.store.subscribe(user_id, show[0], show[1], list(present))
        except Exception:  # noqa: BLE001 - the request itself worked, the abo is a nice-to-have
            log.warning("Automatic abo for %s failed", title.label)
            return ""
        return "📺 Sobald Folgen in Jellyfin sind, bekommst du eine DM (Abo, beenden mit `/abo`)."

    async def resolve_pending_abos(self) -> None:
        """Turns automatic abos into real ones once the requested show is in Jellyfin."""
        self.store.drop_old_pending_abos()
        if not self.store.pending_abos:
            return
        self._series = (0.0, self._series[1])  # look at Jellyfin now, not at the cached list
        shows = {tmdb: (sid, label) for sid, label, tmdb in await self.series_list() if tmdb}
        for tmdb_id in list(self.store.pending_abos):
            if tmdb_id in shows:
                sid, label = shows[tmdb_id]
                for user_id in self.store.take_pending_abos(tmdb_id):
                    self.store.subscribe(user_id, sid, label, [])  # everything there now is new for them

    async def poster(self, kind: str, tmdb_id: int) -> str | None:
        """The poster of a title for /status, remembered after the first time."""
        key = (kind, tmdb_id)
        if key not in self._posters:
            try:
                self._posters[key] = (await asyncio.to_thread(self.seerr.details, kind, tmdb_id)).poster
            except SeerrError:
                return None
        return self._posters[key]

    async def library(self) -> tuple[list[dict], list[str]]:
        """Movies, shows and genres from Jellyfin for /zufall and /filmabend, reloaded every few minutes."""
        loaded, items, genres = self._library
        if self.jellyfin is not None and (not items or time.monotonic() - loaded > LIBRARY_CACHE_SECONDS):
            items = await asyncio.to_thread(self.jellyfin.library_items)
            genres = await asyncio.to_thread(self.jellyfin.genres)
            self._library = (time.monotonic(), items, genres)
        return items, genres

    async def sonarr_series(self) -> list[dict]:
        """The shows in Sonarr for the /folgen suggestions, reloaded every few minutes."""
        loaded, shows = self._sonarr
        sonarr = self.arrs.get("tv")
        if sonarr is not None and (not shows or time.monotonic() - loaded > SERIES_CACHE_SECONDS):
            shows = await asyncio.to_thread(sonarr.series)
            self._sonarr = (time.monotonic(), shows)
        return shows

    def jellyfin_link(self, item_id: str) -> str | None:
        base = self.settings.jellyfin_url or self.settings.jellyfin_friend_url or self.settings.jellyfin_api_url
        return f"{base.rstrip('/')}/web/index.html#!/details?id={item_id}" if base else None

    async def check_brake(self) -> None:
        """Pauses the running downloads when the disk is nearly full, starts them again once there is room."""
        if self.qbit is None or self.settings.brake_free_gb <= 0:
            return
        limit = self.settings.brake_free_gb * GB
        free = await asyncio.to_thread(self.seerr.free_bytes)
        decision = brake_decision(free, self.store.brake_active, limit)
        if decision == "pull" and free is not None:
            running = [t for t in await asyncio.to_thread(self.qbit.all_torrents)
                       if t.get("state") in DOWNLOADING and t.get("hash")]
            if not running:
                return
            await asyncio.to_thread(self.qbit.pause, [str(t["hash"]) for t in running])
            log.warning("Download brake: %s free, paused %d downloads", size_text(free), len(running))
            if self.store.brake_pull([str(t["hash"]).lower() for t in running]):
                await self.post(brake_pulled_text(free, [escape(str(t.get("name") or "?")) for t in running], limit))
        elif decision == "release" and free is not None:
            hashes = self.store.brake_hashes
            still = [str(t["hash"]) for t in await asyncio.to_thread(self.qbit.torrents, hashes) if t.get("hash")]
            await asyncio.to_thread(self.qbit.resume, still)
            self.store.brake_release()
            log.info("Download brake released: %s free, resumed %d downloads", size_text(free), len(still))
            await self.post(brake_released_text(free, len(still)))

    async def delete_title(self, title: Title) -> tuple[bool, str]:
        """Deletes a movie or show with its files, and its torrents if the bot may (/löschen, and after watching).
        Returns (deleted, the status text)."""
        arr = self.arrs.get(title.kind)
        if arr is not None and not await asyncio.to_thread(arr.manages, title.tmdb_id):
            # The files were there before Sonarr/Radarr: Seerr would say "deleted" and leave them on the disk.
            folder = None
            if self.jellyfin is not None:
                try:
                    folder = await asyncio.to_thread(self.jellyfin.folder_name, title.kind, title.tmdb_id)
                except JellyfinError:
                    pass
            return False, unmanaged_text(title, arr.name, folder)
        torrents: list[dict] = []
        if self.torrents is not None:
            # Before deleting: Sonarr/Radarr forget the downloads with the title. If that or qBittorrent fails,
            # nothing is deleted, so it never ends up half done.
            torrents = await asyncio.to_thread(self.torrents.prepare, title.kind, title.tmdb_id)
        await asyncio.to_thread(self.seerr.delete, title)
        removed, problem = None, None
        if self.torrents is not None:
            try:
                removed = await asyncio.to_thread(self.torrents.remove, torrents)
            except TorrentError as error:
                problem = str(error)
        return True, deleted_text(title, removed, problem)

    async def delete_watched(self, question: WatchQuestion) -> str:
        """'Löschen' under a 'Geschaut' question: the movie as with /löschen, or just the watched episodes."""
        if question.tmdb_id is None:
            return "⚠️ Ohne TMDB-Nummer in Jellyfin weiss der Bot nicht, was das ist. Bitte von Hand löschen."
        if question.kind == "movie":
            title = await asyncio.to_thread(self.seerr.details, "movie", question.tmdb_id)
            if not title.in_library:
                return "ℹ️ Der Film ist schon weg."
            return (await self.delete_title(title))[1]
        sonarr = self.arrs.get("tv")
        if sonarr is None:
            return "⚠️ Sonarr ist nicht eingerichtet (`SONARR_URL`), einzelne Folgen kann der Bot nicht löschen."
        show, episodes = await asyncio.to_thread(sonarr.find_episodes, question.tmdb_id,
                                                 [(s, e) for s, e in question.episodes])
        if show is None:
            return "⚠️ Sonarr kennt diese Serie nicht, die Folgen lagen schon vorher da. Bitte von Hand löschen."
        with_file = [e for e in episodes if e.get("hasFile")]
        if not with_file:
            return "ℹ️ Diese Folgen sind schon weg."
        hashes = await asyncio.to_thread(sonarr.episode_hashes, show["id"], [e["id"] for e in with_file])
        busy = await asyncio.to_thread(sonarr.queue_hashes)
        freed = await asyncio.to_thread(sonarr.drop_episodes, with_file)
        removed, problem = None, None
        if self.torrents is not None:
            try:
                removed = await asyncio.to_thread(self.torrents.remove_finished, hashes, busy)
            except TorrentError as error:
                problem = str(error)
        return episodes_deleted_text(len(with_file), freed, removed, problem)

    async def not_watched_by(self, question: WatchQuestion, users: list[dict]) -> list[str]:
        """Other Jellyfin users who haven't finished it: for a movie everyone else, for episodes those who
        started the show."""
        others = []
        for user in users:
            name = str(user.get("Name") or "")
            if not user.get("Id") or name in question.viewers:
                continue
            ids = question.item_ids + ([question.series_id] if question.series_id else [])
            data = await asyncio.to_thread(self.jellyfin.user_data, str(user["Id"]), ids)
            if question.kind == "tv":
                show = data.get(question.series_id, {})
                started = (show.get("PlayedPercentage") or 0) > 0 or show.get("Played")
                if started and not all(data.get(i, {}).get("Played") for i in question.item_ids):
                    others.append(name)
            elif not all(data.get(i, {}).get("Played") for i in question.item_ids):
                others.append(name)
        return others

    async def check_watched(self) -> None:
        """Asks the admins about movies and episodes watched to the end since the last look."""
        if self.jellyfin is None:
            return
        started = datetime.now(timezone.utc)
        since = parse_time(self.store.watch_since)
        if since is None:  # first start: only what is watched from now on counts
            self.store.set_watch_since(started.isoformat())
            return
        users = [u for u in await asyncio.to_thread(self.jellyfin.users) if u.get("Id")]
        played = {str(u["Id"]): (str(u.get("Name") or "?"), await asyncio.to_thread(self.jellyfin.recently_played, str(u["Id"])))
                  for u in users}
        series_tmdb = {sid: tmdb for sid, _, tmdb in await self.series_list()}
        for question in watched_questions(played, since, set(self.store.watch_asked), series_tmdb):
            try:
                others = await self.not_watched_by(question, users)
            except JellyfinError:
                others = []
            question.viewers = [escape(v) for v in question.viewers]
            question.label = escape(question.label)
            question.text = watched_text(question, [escape(o) for o in others])
            question = self.store.add_watch_question(question)
            question.messages = await self.ask_admins(question.text, lambda q=question.qid: watched_view(q))
            self.store.save()
        self.store.set_watch_since(started.isoformat())

    async def ask_admins(self, text: str, make_view) -> list[list[int]]:
        """A DM to every admin listed by id; without any, in the approvals channel. Returns the messages."""
        sent = []
        for user_id in sorted(self.settings.admin_user_ids):
            try:
                user = self.get_user(user_id) or await self.fetch_user(user_id)
                message = await user.send(text, view=make_view())
                sent.append([message.channel.id, message.id])
            except discord.HTTPException:
                log.info("No DM possible to admin %s", user_id)
        if not sent:
            message = await self.post(text, view=make_view(), channel_id=self.settings.approvals_channel)
            sent.append([message.channel.id, message.id])
        return sent

    async def status_embeds(self) -> list[discord.Embed]:
        """/status: a summary card, then one card per open request with bars, episodes and downloads, then
        downloads without a request in Seerr (from /folgen or added in Sonarr/Radarr directly)."""
        items = await asyncio.to_thread(self.seerr.open_requests)
        free = await asyncio.to_thread(self.seerr.free_bytes)
        now = datetime.now(timezone.utc)
        requested = {(i.kind, i.tmdb_id) for i, _ in items}
        extra: list[tuple[str, int, str]] = []
        details: dict[tuple[str, int], Progress] = {}
        problems: list[str] = []
        for kind, arr in self.arrs.items():
            try:
                queued = await asyncio.to_thread(arr.queued_titles)
                extra += [(kind, tmdb_id, name) for tmdb_id, name in queued.items() if (kind, tmdb_id) not in requested]
                ids = [i.tmdb_id for i, _ in items if i.kind == kind and i.request_status != REQUEST_PENDING]
                ids += [tmdb_id for k, tmdb_id, _ in extra if k == kind]
                if ids:
                    found = await asyncio.to_thread(arr.progress, ids, now)
                    details.update({(kind, tmdb_id): progress for tmdb_id, progress in found.items()})
            except ArrError as error:
                problems.append(f"⚠️ {error}")
        live: dict[str, dict] = {}
        hashes = [d.hash for p in details.values() for d in p.downloads if d.hash and d.state == "downloading"]
        if self.qbit is not None and hashes:
            try:
                live = {str(t.get("hash")).lower(): t for t in await asyncio.to_thread(self.qbit.torrents, hashes)}
            except ArrError:
                pass  # without speed
        colors = {"blue": discord.Color.blue(), "orange": discord.Color.orange(), "green": discord.Color.green(),
                  "gold": discord.Color.gold()}
        cards: list[discord.Embed] = []
        speed, active = 0, 0

        async def new_card(kind: str, tmdb_id: int, name: str) -> discord.Embed:
            embed = discord.Embed(title=shorten(name, 256), url=self.settings.page_url(kind, tmdb_id))
            embed.set_author(name="📺 Serie" if kind == "tv" else "🎬 Film")
            poster = await self.poster(kind, tmdb_id)
            if poster:
                embed.set_thumbnail(url=poster)
            return embed

        def fill(embed: discord.Embed, kind: str, progress: Progress) -> None:
            nonlocal speed, active
            info = card(kind, progress, live, now)
            speed += info.speed
            active += sum(1 for d in progress.downloads if d.state == "downloading")
            embed.description = info.headline
            embed.color = colors[info.color]
            for field, value in (("Folgen", info.counts), ("Lädt gerade", "\n".join(info.active)),
                                 ("Pausiert / Probleme", "\n".join(info.other)), ("Fehlt noch", info.missing)):
                if value:
                    embed.add_field(name=field, value=value[:1024], inline=False)

        for item, name in items[:STATUS_CARDS]:
            tracked = self.store.request(item.request_id)
            who = tracked.user_name if tracked else item.requested_by
            embed = await new_card(item.kind, item.tmdb_id, name)
            progress = details.get((item.kind, item.tmdb_id))
            if item.request_status == REQUEST_PENDING:
                embed.description = "⏳ **Wartet auf Freigabe** durch einen Admin."
                embed.color = discord.Color.light_grey()
            elif progress is None:
                embed.description = item.state(now)
                embed.color = discord.Color.blurple()
            else:
                fill(embed, item.kind, progress)
            if who:
                embed.set_footer(text=f"Angefragt von {who}")
            cards.append(embed)
        for kind, tmdb_id, name in extra:
            progress = details.get((kind, tmdb_id))
            if progress is None or len(cards) >= STATUS_CARDS:
                continue
            embed = await new_card(kind, tmdb_id, name)
            fill(embed, kind, progress)
            embed.set_footer(text="Ohne Anfrage in Seerr geladen, z.B. mit /folgen")
            cards.append(embed)
        parts = []
        if items:
            parts.append(f"**{len(items)}** offene {'Anfrage' if len(items) == 1 else 'Anfragen'}")
        if extra:
            parts.append(f"**{len(extra)}** ohne Anfrage")
        if active:
            parts.append(f"⬇️ **{active}** {'Download' if active == 1 else 'Downloads'} aktiv")
        if speed:
            parts.append(speed_text(speed))
        head = [" · ".join(parts)] if parts else ["📭 Keine offenen Anfragen. Alles Angefragte ist schon da."]
        if free is not None:
            minimum = self.settings.min_free_gb * GB
            head.append(f"💾 **Nur noch {size_text(free)} frei**, neue Anfragen sind gesperrt." if free < minimum
                        else f"💾 Noch **{size_text(free)}** frei")
        if self.store.brake_active:
            resume_at = size_text(self.settings.brake_free_gb * GB + BRAKE_MARGIN)
            head.append(f"🛑 **Notbremse aktiv:** Downloads sind angehalten, bis wieder {resume_at} frei sind.")
        if len(items) > STATUS_CARDS:
            head.append(f"… und {len(items) - STATUS_CARDS} weitere Anfragen")
        summary = discord.Embed(title="📊 Status", description="\n".join(head + problems), color=discord.Color.blurple())
        summary.set_footer(text=f"Stand {datetime.now(self.settings.zone):%H:%M} · 🔄 zum Aktualisieren unten klicken")
        return [summary] + cards

    async def sync_approvals(self) -> None:
        """Posts Approve/Decline buttons for new pending requests (also those made in Seerr itself)
        and updates the messages of requests that were handled in Seerr."""
        if not self.settings.approvals_enabled:
            return
        async with self._approval_lock:
            pending = await asyncio.to_thread(self.seerr.pending_requests)
            pending_ids = {state.request_id for state in pending}
            for state in reversed(pending):  # oldest first
                if self.store.approval(state.request_id) is not None:
                    continue
                name = await asyncio.to_thread(self.seerr.name_of, state.kind, state.tmdb_id)
                tracked = self.store.request(state.request_id)
                who = tracked.user_name if tracked else state.requested_by
                text = approval_text(escape(name), state.kind, state.seasons, escape(who))
                message = await self.post(text, view=approval_view(state.request_id),
                                          channel_id=self.settings.approvals_channel)
                self.store.add_approval(state.request_id, message.channel.id, message.id, text)
            for approval in self.store.open_approvals():
                if approval.request_id in pending_ids:
                    continue
                state = await asyncio.to_thread(self.seerr.request_state, approval.request_id)
                if state is None:
                    note = "🗑️ Die Anfrage gibt es nicht mehr."
                elif state.status == REQUEST_PENDING:
                    continue  # still waiting, just not among the newest
                elif state.status == REQUEST_DECLINED:
                    note = "❌ In Seerr abgelehnt."
                else:
                    note = "✅ In Seerr freigegeben."
                await self.close_approval_message(approval, note)

    async def close_approval_message(self, approval: Approval, note: str) -> None:
        try:
            channel = self.get_channel(approval.channel_id) or await self.fetch_channel(approval.channel_id)
            message = channel.get_partial_message(approval.message_id)  # type: ignore[union-attr]
            await message.edit(content=f"{approval.text}\n{note}", view=None)
        except discord.HTTPException:
            log.warning("Could not update the approval message for request %s", approval.request_id)
        self.store.close_approval(approval.request_id)

    async def check_requests(self) -> None:
        """Tells requesters when their movie or show is there, or was declined or failed."""
        for tracked in self.store.waiting()[:MAX_CHECKS_PER_RUN]:
            try:
                state = await asyncio.to_thread(self.seerr.request_state, tracked.request_id)
                done = state is not None and await asyncio.to_thread(self.seerr.is_done, state)
            except SeerrError:
                return  # Seerr is down; try again next time
            mention, name = f"<@{tracked.user_id}>", escape(tracked.label)
            if state is None:
                pass  # deleted in Seerr: nothing to say
            elif state.status == REQUEST_DECLINED:
                await self.post(declined_text(mention, name))
            elif state.status == REQUEST_FAILED:
                await self.post(failed_text(mention, name))
            elif done:
                await self.post(done_text(mention, name), view=watch_view(self.settings.watch_url(state.media_url)))
            else:
                continue
            self.store.mark_notified(tracked.request_id)

    async def post_digest(self) -> None:
        schedule = self.settings.weekly_digest
        if schedule is None:
            return
        now = datetime.now(self.settings.zone)
        key = digest_key(now, schedule)
        if key is None or key == self.store.digest:
            return
        items = added_since(await asyncio.to_thread(self.seerr.recently_added, 50), now - timedelta(days=7))
        if items:
            await self.post(None, embed=await self.new_embed(items[:25], "📺 Neu diese Woche"))
        self.store.set_digest(key)


# --- approval buttons (persistent: the custom id carries the request) -------------------------
class ApprovalButton(discord.ui.DynamicItem[discord.ui.Button], template=r"seerr:(?P<action>approve|decline):(?P<id>[0-9]+)"):
    def __init__(self, request_id: int, action: str) -> None:
        approve = action == "approve"
        super().__init__(discord.ui.Button(
            label="Freigeben" if approve else "Ablehnen",
            emoji="✅" if approve else "✖️",
            style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
            custom_id=f"seerr:{action}:{request_id}",
        ))
        self.request_id = request_id
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match["id"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await handle_approval(interaction, self.request_id, self.action)
        except Exception as error:  # noqa: BLE001 - shown to the admin, details in the log
            await report_error(interaction, error)


def approval_view(request_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ApprovalButton(request_id, "approve"))
    view.add_item(ApprovalButton(request_id, "decline"))
    return view


async def handle_approval(interaction: discord.Interaction, request_id: int, action: str) -> None:
    bot: SeerrBot = interaction.client  # type: ignore[assignment]
    if not bot.is_admin(interaction):
        await interaction.response.send_message(NO_APPROVE, ephemeral=True)
        return
    await interaction.response.defer()
    who = escape(interaction.user.display_name)
    try:
        await asyncio.to_thread(bot.seerr.set_request_status, request_id, action)
        note = f"✅ Freigegeben von {who}." if action == "approve" else f"❌ Abgelehnt von {who}."
    except SeerrError as error:
        if error.status != 404:
            raise
        note, action = "🗑️ Die Anfrage gibt es nicht mehr.", "gone"
    approval = bot.store.approval(request_id)
    text = approval.text if approval else (interaction.message.content if interaction.message else "")
    await interaction.edit_original_response(content=f"{text}\n{note}", view=None)
    bot.store.close_approval(request_id)
    tracked = bot.store.request(request_id)
    if action == "decline" and tracked is not None and not tracked.notified:
        await bot.post(declined_text(f"<@{tracked.user_id}>", escape(tracked.label)))
        bot.store.mark_notified(request_id)


# --- /status refresh (persistent, anyone may click) ---------------------------------------------
class WatchedButton(discord.ui.DynamicItem[discord.ui.Button], template=r"watched:(?P<action>del|keep):(?P<id>[0-9]+)"):
    """Löschen / Behalten under a 'Geschaut' question; works after a restart."""

    def __init__(self, question_id: int, action: str) -> None:
        delete = action == "del"
        super().__init__(discord.ui.Button(
            label="Löschen" if delete else "Behalten", emoji="🗑️" if delete else "👍",
            style=discord.ButtonStyle.danger if delete else discord.ButtonStyle.secondary,
            custom_id=f"watched:{action}:{question_id}"))
        self.question_id, self.action = question_id, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match["id"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_watched(interaction, self.question_id, self.action)


def watched_view(question_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(WatchedButton(question_id, "del"))
    view.add_item(WatchedButton(question_id, "keep"))
    return view


async def handle_watched(interaction: discord.Interaction, question_id: int, action: str) -> None:
    bot: SeerrBot = interaction.client  # type: ignore[assignment]
    if not bot.is_admin(interaction):
        await interaction.response.send_message(NO_FRIEND_ADMIN, ephemeral=True)
        return
    question = bot.store.watch_questions.get(question_id)
    if question is None or question.state != "open":
        await interaction.response.edit_message(view=None)
        return
    await interaction.response.defer()
    async with bot._watch_lock:
        if question.state != "open":
            return
        who = interaction.user.display_name
        if action == "keep":
            question.state, question.answer = "kept", f"👍 Behalten ({escape(who)})."
        else:
            try:
                result = await bot.delete_watched(question)
            except (SeerrError, ArrError, JellyfinError) as error:
                await report_error(interaction, error)
                return
            question.state, question.answer = "deleted", f"{result} ({escape(who)})"
        bot.store.save()
    text = f"{question.text}\n\n{question.answer}"[:2000]
    await interaction.edit_original_response(content=text, view=None)
    for channel_id, message_id in question.messages:  # the other admins' copies
        if interaction.message is not None and message_id == interaction.message.id:
            continue
        try:
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            await channel.get_partial_message(message_id).edit(content=text, view=None)  # type: ignore[union-attr]
        except discord.HTTPException:
            pass


STATUS_COOLDOWN = 10  # seconds between two refreshes of the same message
STATUS_CARDS = 9  # one card per title; Discord allows 10 embeds per message, one is the summary


class StatusRefresh(discord.ui.DynamicItem[discord.ui.Button], template=r"status:refresh"):
    def __init__(self) -> None:
        super().__init__(discord.ui.Button(label="Aktualisieren", emoji="🔄", style=discord.ButtonStyle.secondary,
                                           custom_id="status:refresh"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: SeerrBot = interaction.client  # type: ignore[assignment]
        key = interaction.message.id if interaction.message else 0
        if time.monotonic() - bot.status_clicks.get(key, 0.0) < STATUS_COOLDOWN:
            await interaction.response.send_message("⏳ Gerade eben aktualisiert.", ephemeral=True)
            return
        bot.status_clicks[key] = time.monotonic()
        try:
            await interaction.response.defer()
            await interaction.edit_original_response(embeds=await bot.status_embeds(), view=status_view())
        except Exception as error:  # noqa: BLE001 - shown to who clicked, details in the log
            await report_error(interaction, error)


def status_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(StatusRefresh())
    return view


# --- Jellyfin accounts for friends (/jellyfin-zugang) -------------------------------------------
LOGIN_COOLDOWN = 20  # seconds between two new passwords, so a double click doesn't void the first
SPACE_TOP = 10  # titles shown by /speicher
DECLINED_WAIT_DAYS = 7  # after a "no", asking again only after this long


def friend_card_text(friend: Friend) -> str:
    again = " Das Konto gab es schon, es wird wieder geöffnet." if friend.jellyfin_id else ""
    return (f"🔑 **{escape(friend.user_name)}** möchte Zugang zu Jellyfin als "
            f"**{escape(friend.jellyfin_name)}**.{again}")


def approved_text(name: str) -> str:
    return (f"🎉 Dein Zugang zu Jellyfin ist freigegeben, Benutzer **{escape(name)}**. Klick auf "
            "«Zugangsdaten anzeigen»: Du bekommst die Anleitung und ein Passwort, das nur du siehst.")


class FriendButton(discord.ui.DynamicItem[discord.ui.Button], template=r"jf:(?P<action>approve|decline):(?P<id>[0-9]+)"):
    def __init__(self, user_id: int, action: str) -> None:
        approve = action == "approve"
        super().__init__(discord.ui.Button(
            label="Freigeben" if approve else "Ablehnen",
            emoji="✅" if approve else "✖️",
            style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
            custom_id=f"jf:{action}:{user_id}",
        ))
        self.user_id = user_id
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match["id"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await handle_friend_decision(interaction, self.user_id, self.action)
        except Exception as error:  # noqa: BLE001 - shown to the admin, details in the log
            await report_error(interaction, error)


def friend_view(user_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(FriendButton(user_id, "approve"))
    view.add_item(FriendButton(user_id, "decline"))
    return view


class LoginButton(discord.ui.DynamicItem[discord.ui.Button], template=r"jf:login:(?P<id>[0-9]+)"):
    def __init__(self, user_id: int) -> None:
        super().__init__(discord.ui.Button(label="Zugangsdaten anzeigen", emoji="🔑",
                                           style=discord.ButtonStyle.primary, custom_id=f"jf:login:{user_id}"))
        self.user_id = user_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            await show_login(interaction, self.user_id)
        except Exception as error:  # noqa: BLE001 - shown to the friend, details in the log
            await report_error(interaction, error)


def login_view(user_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(LoginButton(user_id))
    return view


async def handle_friend_decision(interaction: discord.Interaction, user_id: int, action: str) -> None:
    bot: SeerrBot = interaction.client  # type: ignore[assignment]
    if not bot.is_admin(interaction):
        await interaction.response.send_message(NO_APPROVE, ephemeral=True)
        return
    if bot.jellyfin is None:
        raise JellyfinError("Jellyfin-Zugänge sind nicht eingerichtet.")
    await interaction.response.defer()
    async with bot._friend_lock:
        friend = bot.store.friend(user_id)
        if friend is None or friend.state != FRIEND_PENDING:
            text = interaction.message.content if interaction.message else ""
            await interaction.edit_original_response(content=f"{text}\nℹ️ Schon erledigt.", view=None)
            return
        who = escape(interaction.user.display_name)
        if action == "approve":
            if friend.jellyfin_id:
                await asyncio.to_thread(bot.jellyfin.set_access, friend.jellyfin_id, True)
            else:
                friend.jellyfin_id = await asyncio.to_thread(bot.jellyfin.create_friend, friend.jellyfin_name)
            friend.state, note = FRIEND_APPROVED, f"✅ Freigegeben von {who}."
        else:
            friend.state, note = FRIEND_DECLINED, f"❌ Abgelehnt von {who}."
        friend.decided = time.time()
        bot.store.save_friend(friend)
    await interaction.edit_original_response(content=f"{friend.card_text}\n{note}", view=None)
    if friend.state == FRIEND_APPROVED:
        await bot.tell(user_id, approved_text(friend.jellyfin_name), login_view(user_id))
    else:
        await bot.tell(user_id, "❌ Deine Anfrage für einen Zugang zu Jellyfin wurde abgelehnt.")


async def show_login(interaction: discord.Interaction, user_id: int) -> None:
    """A new password plus the whole guide, visible only to the friend; nothing is stored."""
    bot: SeerrBot = interaction.client  # type: ignore[assignment]
    if interaction.user.id != user_id:
        await interaction.response.send_message(NOT_YOURS, ephemeral=True)
        return
    friend = bot.store.friend(user_id)
    if bot.jellyfin is None or friend is None or friend.state != FRIEND_APPROVED or not friend.jellyfin_id:
        await interaction.response.send_message("🔒 Dein Zugang zu Jellyfin ist nicht (mehr) aktiv.", ephemeral=True)
        return
    last = bot.login_clicks.get(user_id, 0.0)
    if time.monotonic() - last < LOGIN_COOLDOWN:
        await interaction.response.send_message("⏳ Gerade eben gab es ein neues Passwort, es steht in der "
                                                "Nachricht darüber.", ephemeral=True)
        return
    bot.login_clicks[user_id] = time.monotonic()
    await interaction.response.defer(ephemeral=True, thinking=True)
    password = await asyncio.to_thread(bot.jellyfin.reset_password, friend.jellyfin_id)
    text = welcome_text(friend.jellyfin_name, password, bot.settings.jellyfin_friend_url,
                        bot.settings.tailscale_share_url)
    await interaction.followup.send(text, ephemeral=True)


class AccessForm(discord.ui.Modal, title="Zugang zu Jellyfin"):
    def __init__(self, bot: SeerrBot, default: str) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.name = discord.ui.TextInput(label="Benutzername für Jellyfin", placeholder="z.B. max.muster",
                                         default=default or None, min_length=3, max_length=20)
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = self.name.value.strip()
        problem = check_name(name)
        if problem is None and self.bot.store.name_taken(name, interaction.user.id):
            problem = "Diesen Benutzernamen hat schon jemand angefragt. Nimm bitte einen anderen."
        if problem:
            await interaction.response.send_message(f"⚠️ {problem}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if await asyncio.to_thread(self.bot.jellyfin.find_user, name) is not None:
            await interaction.followup.send("⚠️ Diesen Benutzernamen gibt es in Jellyfin schon. Nimm bitte einen "
                                            "anderen.", ephemeral=True)
            return
        await submit_friend_request(interaction, name, "")

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await report_error(interaction, error)


async def submit_friend_request(interaction: discord.Interaction, name: str, jellyfin_id: str) -> None:
    """Saves the request and posts Approve/Decline for the admins. The interaction must be deferred."""
    bot: SeerrBot = interaction.client  # type: ignore[assignment]
    async with bot._friend_lock:
        friend = Friend(user_id=interaction.user.id, user_name=interaction.user.display_name, jellyfin_name=name,
                        created=time.time(), jellyfin_id=jellyfin_id)
        friend.card_text = friend_card_text(friend)
        message = await bot.post(friend.card_text, view=friend_view(friend.user_id),
                                 channel_id=bot.settings.approvals_channel)
        friend.channel_id, friend.message_id = message.channel.id, message.id
        bot.store.save_friend(friend)
    await interaction.followup.send("📨 Anfrage gesendet. Sobald ein Admin sie freigibt, bekommst du eine Nachricht "
                                    "vom Bot.", ephemeral=True)


# --- pickers under a search result ---------------------------------------------
class Picker(discord.ui.View):
    """Search results of one person: a select menu for the hits plus the subclass's buttons."""

    def __init__(self, bot: SeerrBot, owner: discord.abc.User, titles: list[Title]) -> None:
        super().__init__(timeout=PICK_TIMEOUT)
        self.bot = bot
        self.owner = owner
        self.titles = titles
        self.current = titles[0]
        self.message: discord.Message | None = None
        if len(titles) > 1:
            self.add_item(ResultSelect(self))

    async def choose(self, index: int) -> Title:
        """The hit at index, with any details the picker needs."""
        return self.titles[index]

    def show(self, title: Title) -> None:
        self.current = title

    def embed(self, title: Title, footer: str | None = None) -> discord.Embed:
        return title_embed(self.bot, title, footer)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner.id:
            return True
        await interaction.response.send_message(
            f"Das ist die Suche von {self.owner.display_name}. Starte eine eigene, dann bekommst du eigene Knöpfe.",
            ephemeral=True,
        )
        return False

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await report_error(interaction, error)

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass


class ResultSelect(discord.ui.Select):
    def __init__(self, picker: Picker) -> None:
        options = [
            discord.SelectOption(
                label=shorten(f"{title.label} · {title.kind_text}", 100),
                value=str(index),
                description=shorten(title.status_text(), 100),
                default=index == 0,
            )
            for index, title in enumerate(picker.titles[:25])
        ]
        super().__init__(placeholder="Anderen Treffer wählen", options=options, row=0)
        self.picker = picker

    async def callback(self, interaction: discord.Interaction) -> None:
        for option in self.options:
            option.default = option.value == self.values[0]
        await interaction.response.defer()
        title = await self.picker.choose(int(self.values[0]))
        self.picker.show(title)
        await interaction.edit_original_response(embed=self.picker.embed(title), view=self.picker)


class CancelButton(discord.ui.Button):
    def __init__(self, picker: Picker) -> None:
        super().__init__(label="Abbrechen", style=discord.ButtonStyle.secondary, row=2)
        self.picker = picker

    async def callback(self, interaction: discord.Interaction) -> None:
        self.picker.stop()
        await interaction.response.edit_message(content="Abgebrochen, nichts geändert.", embed=None, view=None)


# --- requesting ----------------------------------------------------------------
class SeasonSelect(discord.ui.Select):
    def __init__(self, picker: "RequestPicker", numbers: list[int]) -> None:
        options = [discord.SelectOption(label=f"Staffel {n}", value=str(n), default=True) for n in numbers[:25]]
        super().__init__(placeholder="Staffeln wählen", min_values=1, max_values=len(options), options=options, row=1)
        self.picker = picker

    async def callback(self, interaction: discord.Interaction) -> None:
        for option in self.options:
            option.default = option.value in self.values
        self.picker.seasons = sorted(int(value) for value in self.values)
        self.picker.update_button()
        # the size estimate depends on the seasons
        await interaction.response.edit_message(embed=self.picker.embed(self.picker.current), view=self.picker)


class ProfileSelect(discord.ui.Select):
    """Quality or language, as offered by MOVIE_PROFILES / TV_PROFILES."""

    def __init__(self, picker: "RequestPicker", profiles: list[tuple[int, str]]) -> None:
        options = [
            discord.SelectOption(label=shorten(name, 100), value=str(profile_id), default=index == 0)
            for index, (profile_id, name) in enumerate(profiles[:25])
        ]
        super().__init__(placeholder="Qualität wählen", options=options, row=3)
        self.picker = picker

    async def callback(self, interaction: discord.Interaction) -> None:
        for option in self.options:
            option.default = option.value == self.values[0]
        self.picker.profile_id = int(self.values[0])
        await interaction.response.edit_message(view=self.picker)


class RequestButton(discord.ui.Button):
    def __init__(self, picker: "RequestPicker") -> None:
        super().__init__(label="Anfragen", emoji="📥", style=discord.ButtonStyle.success, row=2)
        self.picker = picker

    async def callback(self, interaction: discord.Interaction) -> None:
        picker, bot = self.picker, self.picker.bot
        if not bot.may_request(interaction):
            await interaction.response.send_message(NO_ROLE, ephemeral=True)
            return
        limited = bot.limit_reached(interaction)
        if limited:
            await interaction.response.send_message(limited, ephemeral=True)
            return
        await interaction.response.defer()
        title = picker.current
        result = await asyncio.to_thread(bot.seerr.request, title, picker.seasons, picker.profile_id)
        picker.stop()
        if result.request_id is not None:
            bot.store.add_request(result.request_id, title.kind, title.tmdb_id, title.label,
                                  interaction.user.id, interaction.user.display_name)
        what = title.label
        if title.kind == "tv" and picker.seasons:
            what += f", Staffel {format_numbers(picker.seasons)}"
        profile = picker.profile_name()
        if profile:
            what += f" · {profile}"
        embed = title_embed(bot, title, footer=f"Angefragt von {interaction.user.display_name}: {what}")
        text = request_done_text(title.kind, result.status, notify=result.request_id is not None)
        if result.request_id is not None:
            note = await bot.auto_abo(interaction.user.id, title)
            text += f"\n{note}" if note else ""
        embed.set_field_at(0, name="Status", value=text, inline=False)
        await interaction.edit_original_response(embed=embed, view=None)
        if result.status == REQUEST_PENDING:
            bot.in_background(bot.sync_approvals())


def needs_details(title: Title) -> bool:
    """Search results lack the seasons of a show and every running time: ask Seerr for the details."""
    return title.runtime is None or (title.kind == "tv" and not title.seasons)


class RequestPicker(Picker):
    def __init__(self, bot: SeerrBot, owner: discord.abc.User, titles: list[Title],
                 rate: SizeRate = RULE_OF_THUMB, free: int | None = None) -> None:
        super().__init__(bot, owner, titles)
        self.rate = rate  # for the size estimate
        self.free = free
        self.seasons: list[int] | None = None
        self.season_select: SeasonSelect | None = None
        self.profile_id: int | None = None
        self.profile_select: ProfileSelect | None = None
        self.request_button = RequestButton(self)
        self.add_item(self.request_button)
        self.add_item(CancelButton(self))
        self.show(self.current)

    async def choose(self, index: int) -> Title:
        title = self.titles[index]
        if needs_details(title):
            title = await asyncio.to_thread(self.bot.seerr.details, title.kind, title.tmdb_id)
            self.titles[index] = title
        return title

    def embed(self, title: Title, footer: str | None = None) -> discord.Embed:
        embed = super().embed(title, footer)
        if title.requestable:
            text = space_text(title, self.seasons, self.rate, self.free, self.bot.settings.min_free_gb * GB)
            if text:
                embed.add_field(name="💾 Platz", value=text[:1024], inline=False)
        return embed

    def show(self, title: Title) -> None:
        super().show(title)
        for select in (self.season_select, self.profile_select):
            if select is not None:
                self.remove_item(select)
        self.season_select = self.profile_select = None
        self.seasons = title.requestable_seasons()[:25] if title.kind == "tv" else None
        if self.seasons and len(self.seasons) > 1:
            self.season_select = SeasonSelect(self, self.seasons)
            self.add_item(self.season_select)
        profiles = self.bot.profiles.get(title.kind) or []
        self.profile_id = profiles[0][0] if profiles else None
        if len(profiles) > 1 and title.requestable:
            self.profile_select = ProfileSelect(self, profiles)
            self.add_item(self.profile_select)
        self.update_button()

    def profile_name(self) -> str | None:
        profiles = self.bot.profiles.get(self.current.kind) or []
        if len(profiles) < 2:
            return None
        return next((name for profile_id, name in profiles if profile_id == self.profile_id), None)

    def update_button(self) -> None:
        title = self.current
        self.request_button.disabled = not title.requestable
        if title.kind == "tv" and self.seasons:
            self.request_button.label = shorten(f"Staffel {format_numbers(self.seasons)} anfragen", 80)
        else:
            self.request_button.label = "Anfragen"


# --- deleting --------------------------------------------------------------------
class DeleteButton(discord.ui.Button):
    def __init__(self, picker: "DeletePicker") -> None:
        super().__init__(label="Löschen", emoji="🗑️", style=discord.ButtonStyle.danger, row=2)
        self.picker = picker

    async def callback(self, interaction: discord.Interaction) -> None:
        picker = self.picker
        if not picker.bot.is_admin(interaction):
            await interaction.response.send_message(NO_ADMIN, ephemeral=True)
            return
        if not picker.armed:  # first click only asks
            picker.armed = True
            self.label = "Ja, endgültig löschen"
            await interaction.response.edit_message(embed=picker.embed(picker.current), view=picker)
            return
        await interaction.response.defer()
        bot, title = picker.bot, picker.current
        deleted, text = await bot.delete_title(title)
        picker.stop()
        who = interaction.user.display_name
        embed = title_embed(bot, title, footer=f"Gelöscht von {who}" if deleted else f"Nicht gelöscht · {who}")
        embed.set_field_at(0, name="Status", value=text, inline=False)
        await interaction.edit_original_response(embed=embed, view=None)


class DeletePicker(Picker):
    def __init__(self, bot: SeerrBot, owner: discord.abc.User, titles: list[Title]) -> None:
        super().__init__(bot, owner, titles)
        self.armed = False
        self.delete_button = DeleteButton(self)
        self.add_item(self.delete_button)
        self.add_item(CancelButton(self))
        self.show(self.current)

    def show(self, title: Title) -> None:
        super().show(title)
        self.armed = False
        self.delete_button.label = "Löschen"

    def embed(self, title: Title, footer: str | None = None) -> discord.Embed:
        embed = title_embed(self.bot, title, footer)
        if self.armed:
            what = "Der Film" if title.kind == "movie" else "Die ganze Serie mit allen Staffeln"
            torrents = " und ihre Torrents in qBittorrent" if self.bot.torrents is not None else ""
            embed.add_field(
                name="⚠️ Wirklich löschen?",
                value=f"{what} wird aus Radarr/Sonarr entfernt, **die Dateien{torrents} sind danach weg**. "
                      "Zum Bestätigen nochmals klicken.",
                inline=False,
            )
            embed.color = discord.Color.red()
        return embed


# --- slash commands -----------------------------------------------------------
WEEKDAY_NAMES = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")


def fetch_image(url: str) -> bytes | None:
    """A poster for the Discord event; None if it can't be loaded (the event works without)."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "seerr-discord-bot"})
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 (poster URL from Seerr/TMDB)
            data = response.read(8_000_001)
    except Exception:  # noqa: BLE001 - just no picture
        return None
    return data if 0 < len(data) <= 8_000_000 else None


class EpisodeConfirm(discord.ui.View):
    """/folgen: what would be fetched, and the button that tells Sonarr."""

    def __init__(self, bot: SeerrBot, owner: discord.abc.User, show: dict, plan: EpisodePlan, free: int | None) -> None:
        super().__init__(timeout=PICK_TIMEOUT)
        self.bot, self.owner, self.show, self.plan, self.free = bot, owner, show, plan, free
        self.message: discord.Message | None = None

    @property
    def label(self) -> str:
        year = self.show.get("year")
        return f"{self.show.get('title', '?')} ({year})" if year else str(self.show.get("title", "?"))

    def embed(self, head: str | None = None, color: discord.Color | None = None) -> discord.Embed:
        lines = plan_lines(self.plan, self.free, self.bot.settings.min_free_gb * GB)
        if not self.plan.get and not self.plan.later:
            lines.insert(0, "✅ Diese Folgen sind alle schon da.")
        embed = discord.Embed(title=f"📺 {self.label}", description="\n".join(([head] if head else []) + lines)[:4000],
                              color=color or discord.Color.blurple())
        poster = next((i.get("remoteUrl") for i in self.show.get("images") or []
                       if isinstance(i, dict) and i.get("coverType") == "poster" and i.get("remoteUrl")), None)
        if poster:
            embed.set_thumbnail(url=poster)
        embed.set_footer(text="Nur diese Folgen, nicht die ganze Staffel.")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner.id:
            return True
        await interaction.response.send_message(NOT_YOURS, ephemeral=True)
        return False

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await report_error(interaction, error)

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Holen", emoji="⬇️", style=discord.ButtonStyle.success)
    async def fetch_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        wanted = [e["id"] for e in self.plan.get + self.plan.later]
        await asyncio.to_thread(self.bot.arrs["tv"].want_episodes, self.show, wanted, [e["id"] for e in self.plan.get])
        self.stop()
        count = len(self.plan.get)
        head = (f"✅ **Sonarr sucht jetzt {count} {'Folge' if count == 1 else 'Folgen'}.** Fortschritt in `/status`."
                if count else "✅ **Vorgemerkt:** Sonarr holt die Folgen, sobald sie erscheinen.")
        tmdb_id = self.show.get("tmdbId")
        if isinstance(tmdb_id, int) and tmdb_id > 0:
            title = Title("tv", tmdb_id, str(self.show.get("title") or ""), str(self.show.get("year") or ""), "", None, 0)
            note = await self.bot.auto_abo(interaction.user.id, title)
            if note:
                head += f"\n{note}"
        embed = self.embed(head, discord.Color.green())
        embed.set_footer(text=f"Geholt von {interaction.user.display_name}")
        await interaction.edit_original_response(embed=embed, view=None)

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(embed=self.embed("❌ Abgebrochen, nichts geholt.", discord.Color.light_grey()),
                                                view=None)


class RandomPick(discord.ui.View):
    """/zufall: a suggestion from the library that wasn't watched, and a button for the next one."""

    def __init__(self, bot: SeerrBot, owner: discord.abc.User, kind: str | None, genre: str | None) -> None:
        super().__init__(timeout=PICK_TIMEOUT)
        self.bot, self.owner, self.kind, self.genre = bot, owner, kind, genre
        self.message: discord.Message | None = None
        self.played: set[str] | None = None
        self.seen: set[str] = set()
        self.link: discord.ui.Button | None = None
        friend = bot.store.friend(owner.id)
        # friends with a Jellyfin account get what *they* haven't watched; everyone else what nobody has
        self.jellyfin_user = friend.jellyfin_id if friend is not None and friend.state == FRIEND_APPROVED else None

    async def roll(self) -> discord.Embed | None:
        items, _ = await self.bot.library()
        if self.played is None:
            self.played = await asyncio.to_thread(self.bot.jellyfin.played_ids, self.jellyfin_user or None)
        candidates = pick_candidates(items, self.played, self.kind, self.genre)
        if not candidates:
            return None
        fresh = [i for i in candidates if str(i["Id"]) not in self.seen] or candidates
        item = random.choice(fresh)
        self.seen.add(str(item["Id"]))
        overview = shorten(str(item.get("Overview") or "Keine Beschreibung vorhanden."), 400)
        embed = discord.Embed(title=f"🎲 {item_label(item)}", description=f"{pick_facts(item)}\n\n{escape(overview)}",
                              color=discord.Color.purple())
        tmdb = str((item.get("ProviderIds") or {}).get("Tmdb") or "")
        if tmdb.isdigit():
            poster = await self.bot.poster("movie" if item.get("Type") == "Movie" else "tv", int(tmdb))
            if poster:
                embed.set_thumbnail(url=poster)
        whose = "Hast du noch nicht geschaut" if self.jellyfin_user else "Hat noch niemand fertig geschaut"
        embed.set_footer(text=f"{whose} · {len(candidates)} zur Auswahl")
        self._set_link(self.bot.jellyfin_link(str(item["Id"])))
        return embed

    def _set_link(self, url: str | None) -> None:
        if self.link is not None:
            self.remove_item(self.link)
            self.link = None
        if url:
            self.link = discord.ui.Button(style=discord.ButtonStyle.link, label="Jetzt ansehen", emoji="▶️", url=url)
            self.add_item(self.link)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner.id:
            return True
        await interaction.response.send_message(NOT_YOURS, ephemeral=True)
        return False

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await report_error(interaction, error)

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Nochmal", emoji="🎲", style=discord.ButtonStyle.primary)
    async def again(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        embed = await self.roll()
        if embed is not None:
            await interaction.edit_original_response(embed=embed, view=self)


def build_commands(bot: SeerrBot) -> list[app_commands.Command]:
    def requester(interaction: discord.Interaction) -> bool:
        if bot.may_request(interaction):
            return True
        raise NotAllowed(NO_ROLE)

    def admin(interaction: discord.Interaction) -> bool:
        if not bot.settings.deleting_enabled:
            raise NotAllowed(NO_DELETING)
        if bot.is_admin(interaction):
            return True
        raise NotAllowed(NO_ADMIN)

    async def suggest(current: str, kind: str | None, library_only: bool = False) -> list[app_commands.Choice[str]]:
        """Titles for Discord's suggestions while typing; the value tells which one was picked."""
        text = current.strip()
        if len(text) < 2 or _PICKED.match(text):
            return []
        try:
            titles = await asyncio.wait_for(
                asyncio.to_thread(bot.seerr.search, text, kind, library_only), AUTOCOMPLETE_SECONDS)
        except Exception:  # noqa: BLE001 - no suggestions is fine, typing still works
            return []
        return [
            app_commands.Choice(name=shorten(f"{t.label} · {t.kind_text} · {t.status_text()}", 100),
                                value=f"{t.kind}:{t.tmdb_id}")
            for t in titles
        ]

    async def suggest_movie(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await suggest(current, "movie")

    async def suggest_show(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await suggest(current, "tv")

    async def suggest_library(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await suggest(current, None, library_only=True)

    async def find(query: str, kind: str | None, library_only: bool = False) -> list[Title]:
        """A picked suggestion ('movie:45745') or the search results for typed text."""
        picked = _PICKED.match(query.strip())
        if picked and kind in (None, picked.group(1)):
            title = await asyncio.to_thread(bot.seerr.details, picked.group(1), int(picked.group(2)))
            return [title] if title.in_library or not library_only else []
        return await asyncio.to_thread(bot.seerr.search, query, kind, library_only)

    async def show_picker(interaction: discord.Interaction, titles: list[Title], hint: str | None) -> None:
        if needs_details(titles[0]):
            titles[0] = await asyncio.to_thread(bot.seerr.details, titles[0].kind, titles[0].tmdb_id)
        rate = await bot.size_rate()
        try:
            free = await asyncio.to_thread(bot.seerr.free_bytes)
        except Exception:  # noqa: BLE001 - only for the estimate
            free = None
        view = RequestPicker(bot, interaction.user, titles, rate, free)
        view.message = await interaction.followup.send(embed=view.embed(view.current, hint), view=view, wait=True)

    async def search_and_show(interaction: discord.Interaction, query: str, kind: str) -> None:
        await interaction.response.defer(thinking=True)
        titles = await find(query, kind)
        if not titles:
            what = "Filme" if kind == "movie" else "Serien"
            await interaction.followup.send(f"🔎 Keine {what} gefunden für **{escape(query.strip())}**.")
            return
        hint = f"{len(titles)} Treffer, oben kannst du einen anderen wählen." if len(titles) > 1 else None
        await show_picker(interaction, titles, hint)

    @app_commands.command(name="film", description="Einen Film suchen und anfragen")
    @app_commands.describe(titel="Name des Films, z.B. Sintel")
    @app_commands.autocomplete(titel=suggest_movie)
    @app_commands.check(requester)
    async def film(interaction: discord.Interaction, titel: str) -> None:
        await search_and_show(interaction, titel, "movie")

    @app_commands.command(name="serie", description="Eine Serie suchen und anfragen")
    @app_commands.describe(titel="Name der Serie, z.B. Pioneer One")
    @app_commands.autocomplete(titel=suggest_show)
    @app_commands.check(requester)
    async def serie(interaction: discord.Interaction, titel: str) -> None:
        await search_and_show(interaction, titel, "tv")

    @app_commands.command(name="trending", description="Was gerade beliebt ist, zum direkt Anfragen")
    @app_commands.describe(art="Nur Filme oder nur Serien")
    @app_commands.choices(art=[app_commands.Choice(name="Filme", value="movie"),
                               app_commands.Choice(name="Serien", value="tv")])
    @app_commands.check(requester)
    async def trending(interaction: discord.Interaction, art: Optional[app_commands.Choice[str]] = None) -> None:
        await interaction.response.defer(thinking=True)
        titles = await asyncio.to_thread(bot.seerr.trending, art.value if art else None)
        if not titles:
            await interaction.followup.send("📭 Seerr liefert gerade keine Trends.")
            return
        await show_picker(interaction, titles, "Gerade beliebt. Oben kannst du einen anderen Titel wählen.")

    @app_commands.command(name="neu", description="Was zuletzt in die Bibliothek gekommen ist")
    async def neu(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        items = await asyncio.to_thread(bot.seerr.recently_added, 10)
        if not items:
            await interaction.followup.send("📭 In der Bibliothek ist noch nichts.")
            return
        await interaction.followup.send(embed=await bot.new_embed(items, "🆕 Neu in der Bibliothek"))

    @app_commands.command(name="status", description="Offene Anfragen, was gerade geladen wird und wie viel Platz frei ist")
    async def status(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        await interaction.followup.send(embeds=await bot.status_embeds(), view=status_view())

    @app_commands.command(name="problem", description="Ein Problem mit einem Film oder einer Serie melden")
    @app_commands.describe(
        titel="Film oder Serie aus der Bibliothek",
        art="Was stimmt nicht?",
        beschreibung="Kurz beschreiben, z.B. «kein Ton ab Minute 20»",
        staffel="Nur bei Serien: welche Staffel",
        folge="Nur bei Serien: welche Folge",
    )
    @app_commands.choices(art=[app_commands.Choice(name=name, value=value) for value, name in ISSUE_TYPES.items()])
    @app_commands.autocomplete(titel=suggest_library)
    @app_commands.check(requester)
    async def problem(interaction: discord.Interaction, titel: str, art: app_commands.Choice[int],
                      beschreibung: app_commands.Range[str, 3, 500],
                      staffel: Optional[app_commands.Range[int, 1, 100]] = None,
                      folge: Optional[app_commands.Range[int, 1, 500]] = None) -> None:
        await interaction.response.defer(thinking=True)
        titles = await find(titel, None, library_only=True)
        if not titles:
            await interaction.followup.send(f"🔎 Nichts davon ist in der Bibliothek: **{escape(titel.strip())}**.")
            return
        title = titles[0]
        message = f"{beschreibung.strip()}\n\nGemeldet von {interaction.user.display_name} über Discord."
        issue_id = await asyncio.to_thread(bot.seerr.report_issue, title, art.value, message, staffel, folge)
        where = ""
        if title.kind == "tv" and staffel:
            where = f", Staffel {staffel}" + (f", Folge {folge}" if folge else "")
        embed = discord.Embed(
            title=f"🛠️ Problem gemeldet: {title.label}{where}",
            description=f"**{art.name}:** {escape(beschreibung.strip())}\n\nEin Admin schaut es sich an.",
            url=bot.settings.issue_url(issue_id),
            color=discord.Color.orange(),
        )
        if title.poster:
            embed.set_thumbnail(url=title.poster)
        embed.set_footer(text=f"Gemeldet von {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="löschen", description="Einen Film oder eine Serie samt Dateien löschen (nur Admins)")
    @app_commands.describe(titel="Name des Films oder der Serie")
    @app_commands.autocomplete(titel=suggest_library)
    @app_commands.check(admin)
    async def loeschen(interaction: discord.Interaction, titel: str) -> None:
        await interaction.response.defer(thinking=True)
        titles = await find(titel, None, library_only=True)
        if not titles:
            await interaction.followup.send(f"🔎 Nichts davon ist in der Bibliothek: **{escape(titel.strip())}**.")
            return
        view = DeletePicker(bot, interaction.user, titles)
        view.message = await interaction.followup.send(embed=view.embed(view.current), view=view, wait=True)

    @app_commands.command(name="hilfe", description="Welche Befehle es gibt und wie man sie benutzt")
    async def hilfe(interaction: discord.Interaction) -> None:
        registered = [(c.name, c.description)
                      for c in bot.tree.get_commands(guild=discord.Object(id=bot.settings.guild_id))]
        text = help_text(registered, bot.is_admin(interaction), bot.may_request(interaction))
        embed = discord.Embed(title="❓ Was der Bot kann", description=text[:4000], color=discord.Color.blurple())
        embed.set_footer(text="Die Befehle gehen nur in diesem Kanal. Antworten mit 🔒 sieht nur, wer gefragt hat.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    commands = [film, serie, trending, neu, status, problem, loeschen]
    if bot.jellyfin is not None:
        commands += friend_commands(bot, requester)
    commands += extra_commands(bot, requester)
    return commands + [hilfe]


def friend_commands(bot: SeerrBot, requester) -> list[app_commands.Command]:
    """/jellyfin-zugang for everyone who may request; for admins the list, taking access away and /speicher."""
    def friend_admin(interaction: discord.Interaction) -> bool:
        if bot.is_admin(interaction):
            return True
        raise NotAllowed(NO_FRIEND_ADMIN)

    @app_commands.command(name="jellyfin-zugang", description="Zugang zu Jellyfin beantragen, oder deine Zugangsdaten nochmals anzeigen")
    @app_commands.check(requester)
    async def jellyfin_zugang(interaction: discord.Interaction) -> None:
        friend = bot.store.friend(interaction.user.id)
        if friend is not None and friend.state == FRIEND_APPROVED:
            await interaction.response.send_message(
                f"✅ Du hast schon Zugang als **{escape(friend.jellyfin_name)}**. Mit dem Knopf bekommst du die "
                "Anleitung und ein neues Passwort.", view=login_view(friend.user_id), ephemeral=True)
        elif friend is not None and friend.state == FRIEND_PENDING:
            await interaction.response.send_message("⏳ Deine Anfrage wartet noch auf Freigabe.", ephemeral=True)
        elif (friend is not None and friend.state == FRIEND_DECLINED
              and time.time() - friend.decided < DECLINED_WAIT_DAYS * 86400):
            await interaction.response.send_message("❌ Deine letzte Anfrage wurde abgelehnt. Frag bei Bedarf "
                                                    "direkt einen Admin.", ephemeral=True)
        elif friend is not None and friend.jellyfin_id:
            # had an account before: ask to open the same one again
            await interaction.response.defer(ephemeral=True, thinking=True)
            await submit_friend_request(interaction, friend.jellyfin_name, friend.jellyfin_id)
        else:
            await interaction.response.send_modal(AccessForm(bot, suggest_name(interaction.user.display_name)))

    @app_commands.command(name="jellyfin-entziehen", description="Den Zugang einer Person zu Jellyfin sperren (nur Admins)")
    @app_commands.describe(person="Wer keinen Zugang mehr haben soll")
    @app_commands.check(friend_admin)
    async def jellyfin_entziehen(interaction: discord.Interaction, person: discord.User) -> None:
        friend = bot.store.friend(person.id)
        if friend is None or friend.state != FRIEND_APPROVED:
            await interaction.response.send_message(f"ℹ️ {escape(person.display_name)} hat keinen aktiven Zugang.",
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with bot._friend_lock:
            await asyncio.to_thread(bot.jellyfin.set_access, friend.jellyfin_id, False)
            friend.state, friend.decided = FRIEND_REVOKED, time.time()
            bot.store.save_friend(friend)
        await interaction.followup.send(
            f"🚫 Das Jellyfin-Konto **{escape(friend.jellyfin_name)}** ist gesperrt, die Person sieht keine Inhalte "
            f"mehr. Im Tailscale-Share bleibt sie, bis du sie dort entfernst: <{TAILSCALE_ADMIN}> → jellyfin → "
            "Share.", ephemeral=True)

    @app_commands.command(name="jellyfin-liste", description="Wer Zugang zu Jellyfin hat oder angefragt hat (nur Admins)")
    @app_commands.check(friend_admin)
    async def jellyfin_liste(interaction: discord.Interaction) -> None:
        icons = {FRIEND_PENDING: "⏳", FRIEND_APPROVED: "✅", FRIEND_DECLINED: "❌", FRIEND_REVOKED: "🚫"}
        lines = []
        for friend in bot.store.all_friends():
            when = datetime.fromtimestamp(friend.decided or friend.created, bot.settings.zone).strftime("%d.%m.%Y")
            lines.append(f"{icons.get(friend.state, '•')} <@{friend.user_id}> · **{escape(friend.jellyfin_name)}**"
                         f" · {when}")
        embed = discord.Embed(title="Zugang zu Jellyfin", color=discord.Color.blurple(),
                              description="\n".join(lines)[:4000] or "Noch niemand hat angefragt.")
        embed.set_footer(text="✅ hat Zugang · ⏳ wartet · ❌ abgelehnt · 🚫 gesperrt")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="speicher", description="Was am meisten Platz braucht und ob es jemand geschaut hat (nur Admins)")
    @app_commands.check(friend_admin)
    async def speicher(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        usages = await asyncio.to_thread(bot.jellyfin.usage)
        free = await asyncio.to_thread(bot.seerr.free_bytes)
        requested = {(t.kind, t.tmdb_id): t.user_name for t in bot.store.requests.values()}
        lines = [usage_line(rank, dataclasses.replace(u, name=escape(u.name)), size_text(u.size),
                            escape(requested[(u.kind, u.tmdb_id)]) if (u.kind, u.tmdb_id) in requested else None,
                            bot.settings.zone)
                 for rank, u in enumerate(usages[:SPACE_TOP], start=1)]
        total = sum(u.size for u in usages)
        head = f"Bibliothek: {size_text(total)} in {len(usages)} Titeln"
        if free is not None:
            head += f", noch {size_text(free)} frei"
        embed = discord.Embed(title="💾 Was am meisten Platz braucht", color=discord.Color.blurple(),
                              description=(head + ".\n\n" + "\n".join(lines))[:4000] if lines
                              else "📭 In der Bibliothek ist noch nichts.")
        embed.set_footer(text="Löschen mit /löschen. Platz wird erst frei, wenn auch der Torrent weg ist.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def suggest_series(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        try:
            shows = await asyncio.wait_for(bot.series_list(), AUTOCOMPLETE_SECONDS)
        except Exception:  # noqa: BLE001 - no suggestions is fine, typing still works
            return []
        text, mine = current.strip().casefold(), set(bot.store.user_abos(interaction.user.id))
        return [app_commands.Choice(name=shorten(("🔔 " if sid in mine else "") + label, 100), value=sid)
                for sid, label, _ in shows if text in label.casefold()][:25]

    @app_commands.command(name="abo", description="Serie abonnieren: Du bekommst eine Nachricht, sobald neue Folgen da sind")
    @app_commands.describe(serie="Serie aus der Bibliothek. Nochmals wählen beendet das Abo. Leer lassen: deine Abos")
    @app_commands.autocomplete(serie=suggest_series)
    @app_commands.check(requester)
    async def abo(interaction: discord.Interaction, serie: Optional[str] = None) -> None:
        user_id = interaction.user.id
        if serie is None:
            names = [f"**{escape(bot.store.abo_names.get(s, s))}**" for s in bot.store.user_abos(user_id)]
            text = ("🔔 Deine Abos: " + ", ".join(names) + ". Nochmals `/abo <serie>` beendet eines.") if names else \
                "🔕 Du hast keine Abos. Mit `/abo <serie>` bekommst du eine Nachricht, sobald neue Folgen da sind."
            await interaction.response.send_message(text, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        shows = {sid: label for sid, label, _ in await bot.series_list()}
        wanted = serie.strip().casefold()
        series_id = serie if serie in shows else next(
            (sid for sid, label in shows.items() if label.casefold().startswith(wanted)), None)
        if series_id is None:
            await interaction.followup.send(f"🔎 Diese Serie ist nicht in der Bibliothek: **{escape(serie.strip())}**. "
                                            "Mit `/serie` kannst du sie anfragen.", ephemeral=True)
            return
        present = await asyncio.to_thread(bot.jellyfin.episodes, series_id)
        name = shows[series_id]
        if bot.store.toggle_abo(user_id, series_id, name, list(present)):
            text = (f"🔔 Abo für **{escape(name)}**: Ich schicke dir eine Nachricht, sobald neue Folgen da sind "
                    f"(jetzt {len(present)} Folgen). Nochmals `/abo` mit dieser Serie beendet es.")
        else:
            text = f"🔕 Abo für **{escape(name)}** beendet."
        await interaction.followup.send(text, ephemeral=True)

    return [jellyfin_zugang, jellyfin_entziehen, jellyfin_liste, speicher, abo]


def extra_commands(bot: SeerrBot, requester) -> list[app_commands.Command]:
    """/folgen (admins, needs Sonarr), /zufall and /filmabend (need Jellyfin)."""
    def only_admin(interaction: discord.Interaction) -> bool:
        if bot.is_admin(interaction):
            return True
        raise NotAllowed(NO_FRIEND_ADMIN)

    async def suggest_sonarr(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        try:
            shows = await asyncio.wait_for(bot.sonarr_series(), AUTOCOMPLETE_SECONDS)
        except Exception:  # noqa: BLE001 - no suggestions is fine, typing still works
            return []
        text = current.strip().casefold()
        found = sorted((s for s in shows if text in str(s.get("title", "")).casefold()),
                       key=lambda s: str(s.get("sortTitle") or s.get("title") or ""))
        return [app_commands.Choice(name=shorten(f"{s.get('title', '?')} ({s.get('year')})", 100), value=str(s["id"]))
                for s in found][:25]

    @app_commands.command(name="folgen", description="Einzelne Folgen einer Serie holen statt ganzer Staffeln (nur Admins)")
    @app_commands.describe(serie="Serie aus Sonarr", folgen="z.B. S23E18-E25, S23 oder bei Anime 1155-1170")
    @app_commands.autocomplete(serie=suggest_sonarr)
    @app_commands.check(only_admin)
    async def folgen(interaction: discord.Interaction, serie: str, folgen: str) -> None:
        await interaction.response.defer(thinking=True)
        sonarr = bot.arrs["tv"]
        shows = await asyncio.to_thread(sonarr.series)  # fresh, the show is written back if it isn't monitored
        wanted = serie.strip()
        show = next((s for s in shows if str(s["id"]) == wanted), None) or next(
            (s for s in shows if str(s.get("title", "")).casefold().startswith(wanted.casefold())), None)
        if show is None:
            await interaction.followup.send(f"🔎 Diese Serie ist nicht in Sonarr: **{escape(wanted)}**. "
                                            "Zuerst mit `/serie` anfragen.")
            return
        try:
            chosen = select_episodes(await asyncio.to_thread(sonarr.episodes, show["id"]), parse_episodes(folgen))
        except ValueError as error:
            await interaction.followup.send(f"⚠️ {error}")
            return
        name = escape(str(show.get("title") or "?"))
        if not chosen:
            await interaction.followup.send(f"🔎 Diese Folgen gibt es bei **{name}** nicht: {escape(folgen.strip())}")
            return
        if len(chosen) > MAX_EPISODES:
            await interaction.followup.send(f"⚠️ Das wären {len(chosen)} Folgen, mehr als {MAX_EPISODES} auf einmal geht "
                                            "nicht. Ganze Staffeln lieber mit `/serie` anfragen.")
            return
        rate = await bot.size_rate()
        plan = plan_episodes(chosen, datetime.now(timezone.utc), per_episode_estimate(show, rate.typical))
        try:
            free = await asyncio.to_thread(bot.seerr.free_bytes, "tv")
        except Exception:  # noqa: BLE001 - only for the estimate
            free = None
        view = EpisodeConfirm(bot, interaction.user, show, plan, free)
        if not plan.get and not plan.later:
            await interaction.followup.send(embed=view.embed())
            return
        view.message = await interaction.followup.send(embed=view.embed(), view=view, wait=True)

    async def suggest_genre(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        try:
            _, genres = await asyncio.wait_for(bot.library(), AUTOCOMPLETE_SECONDS)
        except Exception:  # noqa: BLE001
            return []
        text = current.strip().casefold()
        return [app_commands.Choice(name=g[:100], value=g[:100]) for g in genres if text in g.casefold()][:25]

    @app_commands.command(name="zufall", description="Schlägt einen Film oder eine Serie vor, die du noch nicht geschaut hast")
    @app_commands.describe(art="Nur Filme oder nur Serien", genre="Nur dieses Genre, z.B. Comedy oder Animation")
    @app_commands.choices(art=[app_commands.Choice(name="Filme", value="movie"),
                               app_commands.Choice(name="Serien", value="tv")])
    @app_commands.autocomplete(genre=suggest_genre)
    async def zufall(interaction: discord.Interaction, art: Optional[app_commands.Choice[str]] = None,
                     genre: Optional[str] = None) -> None:
        await interaction.response.defer(thinking=True)
        view = RandomPick(bot, interaction.user, art.value if art else None, genre)
        embed = await view.roll()
        if embed is None:
            what = {"movie": "Filme", "tv": "Serien"}.get(art.value if art else "", "Filme oder Serien")
            where = f" im Genre **{escape(genre)}**" if genre else ""
            await interaction.followup.send(f"📭 Keine {what}{where} gefunden, die noch nicht geschaut wurden.")
            return
        view.message = await interaction.followup.send(embed=embed, view=view, wait=True)

    async def suggest_title(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        try:
            items, _ = await asyncio.wait_for(bot.library(), AUTOCOMPLETE_SECONDS)
        except Exception:  # noqa: BLE001
            return []
        text = current.strip().casefold()
        return [app_commands.Choice(name=shorten(f"{item_label(i)} · {'Film' if i.get('Type') == 'Movie' else 'Serie'}", 100),
                                    value=str(i["Id"])) for i in items if text in item_label(i).casefold()][:25]

    @app_commands.command(name="filmabend", description="Filmabend planen: Discord-Event mit Erinnerung und SyncPlay-Anleitung")
    @app_commands.describe(titel="Film oder Serie aus der Bibliothek", zeit="z.B. 20:00, morgen 20:15, fr 20:00 oder 21.11. 20:00")
    @app_commands.autocomplete(titel=suggest_title)
    @app_commands.check(requester)
    async def filmabend(interaction: discord.Interaction, titel: str, zeit: str) -> None:
        await interaction.response.defer(thinking=True)
        items, _ = await bot.library()
        wanted = titel.strip()
        item = next((i for i in items if str(i["Id"]) == wanted), None) or next(
            (i for i in items if item_label(i).casefold().startswith(wanted.casefold())), None)
        if item is None:
            await interaction.followup.send(f"🔎 Das ist nicht in der Bibliothek: **{escape(wanted)}**. "
                                            "Mit `/film` oder `/serie` kannst du es anfragen.")
            return
        try:
            start = parse_when(zeit, datetime.now(bot.settings.zone))
        except ValueError as error:
            await interaction.followup.send(f"⚠️ {error}")
            return
        label = item_label(item)
        link = bot.jellyfin_link(str(item["Id"]))
        tmdb = str((item.get("ProviderIds") or {}).get("Tmdb") or "")
        poster = await bot.poster("movie" if item.get("Type") == "Movie" else "tv", int(tmdb)) if tmdb.isdigit() else None
        details = {"name": shorten(f"🍿 Filmabend: {label}", 100), "start_time": start,
                   "end_time": event_end(start, item.get("RunTimeTicks")), "entity_type": discord.EntityType.external,
                   "privacy_level": discord.PrivacyLevel.guild_only, "location": "Jellyfin · SyncPlay",
                   "description": event_description(label, link)}
        image = await asyncio.to_thread(fetch_image, poster) if poster else None
        if image:
            details["image"] = image
        try:
            event = await interaction.guild.create_scheduled_event(**details, reason=f"/filmabend von {interaction.user}")
        except discord.Forbidden:
            await interaction.followup.send("⚠️ Dem Bot fehlt die Berechtigung **Events erstellen**. Ein Admin kann sie in "
                                            "den Servereinstellungen unter Rollen geben.")
            return
        when = f"{WEEKDAY_NAMES[start.weekday()]}, {start:%d.%m.} um {start:%H:%M}"
        embed = discord.Embed(title=f"🍿 Filmabend: {label}", url=event.url, color=discord.Color.gold(),
                              description=f"📅 **{when}**\n\nKlick im Event auf **Interessiert**, dann erinnert dich "
                                          "Discord, wenn es losgeht. Geschaut wird zusammen mit SyncPlay in Jellyfin, "
                                          "die Anleitung steht im Event.")
        if poster:
            embed.set_thumbnail(url=poster)
        embed.set_footer(text=f"Geplant von {interaction.user.display_name}")
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Zum Event", emoji="📅", url=event.url))
        if link:
            view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="In Jellyfin", emoji="▶️", url=link))
        await interaction.followup.send(embed=embed, view=view)

    commands: list[app_commands.Command] = []
    if "tv" in bot.arrs:
        commands.append(folgen)
    if bot.jellyfin is not None:
        commands += [zufall, filmabend]
    return commands
