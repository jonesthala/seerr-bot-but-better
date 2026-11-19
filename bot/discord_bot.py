"""Discord side: the slash commands, pickers under search results, approval buttons and background checks."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import time
from datetime import datetime, timedelta
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
    format_numbers,
    help_text,
    limit_text,
    new_line,
    next_allowed,
    request_done_text,
    shorten,
    size_text,
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
from .store import FRIEND_APPROVED, FRIEND_DECLINED, FRIEND_PENDING, FRIEND_REVOKED, Approval, Friend, Store

log = logging.getLogger("seerrbot.discord")

PICK_TIMEOUT = 300  # seconds a search result stays clickable
SPACE_CHECK_MINUTES = 30
WATCH_MINUTES = 2  # new requests to approve, finished downloads to announce
DIGEST_CHECK_MINUTES = 10
ABO_MINUTES = 10  # how often subscribed shows are checked for new episodes
SERIES_CACHE_SECONDS = 600  # the list of shows for /abo suggestions
AUTOCOMPLETE_SECONDS = 2.5  # Discord gives up after 3 s
MAX_CHECKS_PER_RUN = 30
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
    elif isinstance(error, (SeerrError, JellyfinError)):
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
                 jellyfin: JellyfinClient | None = None) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        # Only the requester may be pinged, never @everyone or roles (titles come from TMDB).
        mentions = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)
        super().__init__(intents=intents, allowed_mentions=mentions)
        self.settings = settings
        self.seerr = seerr
        self.jellyfin = jellyfin  # None: /jellyfin-zugang is not set up
        self.store = store if store is not None else Store(settings.data_dir or None)
        self.tree = GuardedTree(self)
        self.space = SpaceWatch(settings.min_free_gb * GB)
        self.profiles: dict[str, list[tuple[int, str]]] = {"movie": [], "tv": []}
        self._approval_lock = asyncio.Lock()
        self._friend_lock = asyncio.Lock()
        self.login_clicks: dict[int, float] = {}  # Discord user → when the last password was made
        self._series: tuple[float, list[tuple[str, str]]] = (0.0, [])  # (when loaded, shows) for /abo
        self._background: set[asyncio.Task] = set()

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.settings.guild_id)
        for command in build_commands(self):
            self.tree.add_command(command, guild=guild)
        await self.tree.sync(guild=guild)
        self.add_dynamic_items(ApprovalButton, FriendButton, LoginButton)  # buttons keep working after a restart
        await self.load_profiles()
        self.space_loop.start()
        self.watch_loop.start()
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

    @space_loop.before_loop
    @watch_loop.before_loop
    @digest_loop.before_loop
    @abo_loop.before_loop
    async def _wait_until_ready(self) -> None:
        await self.wait_until_ready()

    async def series_list(self) -> list[tuple[str, str]]:
        """The shows in Jellyfin, (id, 'Name (Jahr)'), reloaded every few minutes."""
        loaded, shows = self._series
        if self.jellyfin is not None and (not shows or time.monotonic() - loaded > SERIES_CACHE_SECONDS):
            shows = await asyncio.to_thread(self.jellyfin.series_list)
            self._series = (time.monotonic(), shows)
        return shows

    async def check_abos(self) -> None:
        """Tells subscribers about episodes that came into Jellyfin since the last check."""
        if self.jellyfin is None:
            return
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
        await interaction.response.edit_message(view=self.picker)


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
        embed.set_field_at(0, name="Status", value=text, inline=False)
        await interaction.edit_original_response(embed=embed, view=None)
        if result.status == REQUEST_PENDING:
            bot.in_background(bot.sync_approvals())


class RequestPicker(Picker):
    def __init__(self, bot: SeerrBot, owner: discord.abc.User, titles: list[Title]) -> None:
        super().__init__(bot, owner, titles)
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
        if title.kind == "tv" and not title.seasons:
            title = await asyncio.to_thread(self.bot.seerr.details, "tv", title.tmdb_id)
            self.titles[index] = title
        return title

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
        title = picker.current
        await asyncio.to_thread(picker.bot.seerr.delete, title)
        picker.stop()
        embed = title_embed(picker.bot, title, footer=f"Gelöscht von {interaction.user.display_name}")
        embed.set_field_at(0, name="Status", value=deleted_text(title), inline=False)
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
            embed.add_field(
                name="⚠️ Wirklich löschen?",
                value=f"{what} wird aus Radarr/Sonarr entfernt, **die Dateien sind danach weg**. Zum Bestätigen nochmals klicken.",
                inline=False,
            )
            embed.color = discord.Color.red()
        return embed


# --- slash commands -----------------------------------------------------------
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
        if titles[0].kind == "tv" and not titles[0].seasons:
            titles[0] = await asyncio.to_thread(bot.seerr.details, "tv", titles[0].tmdb_id)
        view = RequestPicker(bot, interaction.user, titles)
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
        items = await asyncio.to_thread(bot.seerr.open_requests)
        free = await asyncio.to_thread(bot.seerr.free_bytes)
        lines = []
        for item, name in items:
            tracked = bot.store.request(item.request_id)
            lines.append(item.line(escape(name), escape(tracked.user_name) if tracked else None))
        if not lines:
            lines.append("📭 Keine offenen Anfragen. Alles Angefragte ist schon da.")
        if free is not None:
            minimum = bot.settings.min_free_gb * GB
            if free < minimum:
                lines.append(f"\n💾 **Nur noch {size_text(free)} frei**, neue Anfragen sind gesperrt.")
            else:
                lines.append(f"\n💾 Noch {size_text(free)} frei.")
        embed = discord.Embed(title="Status", description="\n".join(lines), color=discord.Color.blurple())
        await interaction.followup.send(embed=embed)

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
                for sid, label in shows if text in label.casefold()][:25]

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
        shows = dict(await bot.series_list())
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
