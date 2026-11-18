"""Discord side: /film, /serie, /status and /löschen, with a picker for search results and seasons."""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import tasks

from .config import Settings
from .core import (
    AVAILABLE,
    GB,
    PARTIAL,
    PENDING,
    PROCESSING,
    SeerrError,
    SpaceWatch,
    Title,
    deleted_text,
    format_numbers,
    request_done_text,
    shorten,
    size_text,
)
from .seerr import SeerrClient

log = logging.getLogger("seerrbot.discord")

PICK_TIMEOUT = 300  # seconds a search result stays clickable
SPACE_CHECK_MINUTES = 30
NO_ROLE = "🔒 Anfragen dürfen nur Leute mit der Rolle für Film- und Serienwünsche."
NO_ADMIN = "🔒 Löschen dürfen nur Admins."
NO_DELETING = "🔒 Löschen ist nicht eingerichtet: Es fehlen `DISCORD_ADMIN_USER_IDS` oder `DISCORD_ADMIN_ROLE_ID`."

_COLORS = {
    AVAILABLE: discord.Color.green(),
    PARTIAL: discord.Color.gold(),
    PENDING: discord.Color.orange(),
    PROCESSING: discord.Color.blue(),
}


class NotAllowed(app_commands.CheckFailure):
    """A failed check whose message can be shown as it is."""


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


async def report_error(interaction: discord.Interaction, error: BaseException) -> None:
    if isinstance(error, NotAllowed):
        message = str(error)
    elif isinstance(error, app_commands.CheckFailure):
        message = "🔒 Das darfst du hier nicht."
    elif isinstance(error, SeerrError):
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
    def __init__(self, settings: Settings, seerr: SeerrClient) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.settings = settings
        self.seerr = seerr
        self.tree = GuardedTree(self)
        self.space = SpaceWatch(settings.min_free_gb * GB)

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.settings.guild_id)
        for command in build_commands(self):
            self.tree.add_command(command, guild=guild)
        await self.tree.sync(guild=guild)
        self.space_loop.start()

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)

    async def post(self, text: str) -> None:
        channel = self.get_channel(self.settings.channel_id) or await self.fetch_channel(self.settings.channel_id)
        await channel.send(text)  # type: ignore[union-attr]

    @tasks.loop(minutes=SPACE_CHECK_MINUTES)
    async def space_loop(self) -> None:
        """Posts once when free space gets low and once when there is enough again."""
        try:
            message = self.space.update(await asyncio.to_thread(self.seerr.free_bytes))
            if message:
                await self.post(message)
        except Exception:  # noqa: BLE001 - the loop must keep running
            log.exception("Free space check failed")

    @space_loop.before_loop
    async def _wait_until_ready(self) -> None:
        await self.wait_until_ready()

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
            for index, title in enumerate(picker.titles)
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


class RequestButton(discord.ui.Button):
    def __init__(self, picker: "RequestPicker") -> None:
        super().__init__(label="Anfragen", emoji="📥", style=discord.ButtonStyle.success, row=2)
        self.picker = picker

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = self.picker.bot
        if not bot.may_request(interaction):
            await interaction.response.send_message(NO_ROLE, ephemeral=True)
            return
        await interaction.response.defer()
        title = self.picker.current
        status = await asyncio.to_thread(bot.seerr.request, title, self.picker.seasons)
        self.picker.stop()
        what = title.label
        if title.kind == "tv" and self.picker.seasons:
            what += f", Staffel {format_numbers(self.picker.seasons)}"
        embed = title_embed(bot, title, footer=f"Angefragt von {interaction.user.display_name}: {what}")
        embed.set_field_at(0, name="Status", value=request_done_text(title.kind, status), inline=False)
        await interaction.edit_original_response(embed=embed, view=None)


class RequestPicker(Picker):
    def __init__(self, bot: SeerrBot, owner: discord.abc.User, titles: list[Title]) -> None:
        super().__init__(bot, owner, titles)
        self.seasons: list[int] | None = None
        self.season_select: SeasonSelect | None = None
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
        if self.season_select is not None:
            self.remove_item(self.season_select)
            self.season_select = None
        self.seasons = title.requestable_seasons()[:25] if title.kind == "tv" else None
        if self.seasons and len(self.seasons) > 1:
            self.season_select = SeasonSelect(self, self.seasons)
            self.add_item(self.season_select)
        self.update_button()

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

    async def search_and_show(interaction: discord.Interaction, query: str, kind: str) -> None:
        await interaction.response.defer(thinking=True)
        titles = await asyncio.to_thread(bot.seerr.search, query, kind)
        if not titles:
            what = "Filme" if kind == "movie" else "Serien"
            await interaction.followup.send(f"🔎 Keine {what} gefunden für **{discord.utils.escape_markdown(query.strip())}**.")
            return
        if kind == "tv":
            titles[0] = await asyncio.to_thread(bot.seerr.details, "tv", titles[0].tmdb_id)
        view = RequestPicker(bot, interaction.user, titles)
        hint = f"{len(titles)} Treffer, oben kannst du einen anderen wählen." if len(titles) > 1 else None
        view.message = await interaction.followup.send(embed=view.embed(view.current, hint), view=view, wait=True)

    @app_commands.command(name="film", description="Einen Film suchen und anfragen")
    @app_commands.describe(titel="Name des Films, z.B. Sintel")
    @app_commands.check(requester)
    async def film(interaction: discord.Interaction, titel: str) -> None:
        await search_and_show(interaction, titel, "movie")

    @app_commands.command(name="serie", description="Eine Serie suchen und anfragen")
    @app_commands.describe(titel="Name der Serie, z.B. Pioneer One")
    @app_commands.check(requester)
    async def serie(interaction: discord.Interaction, titel: str) -> None:
        await search_and_show(interaction, titel, "tv")

    @app_commands.command(name="status", description="Offene Anfragen, was gerade geladen wird und wie viel Platz frei ist")
    async def status(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        items = await asyncio.to_thread(bot.seerr.open_requests)
        free = await asyncio.to_thread(bot.seerr.free_bytes)
        lines = [item.line(discord.utils.escape_markdown(name)) for item, name in items]
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

    @app_commands.command(name="löschen", description="Einen Film oder eine Serie samt Dateien löschen (nur Admins)")
    @app_commands.describe(titel="Name des Films oder der Serie")
    @app_commands.check(admin)
    async def loeschen(interaction: discord.Interaction, titel: str) -> None:
        await interaction.response.defer(thinking=True)
        titles = await asyncio.to_thread(bot.seerr.search, titel, None, True)
        if not titles:
            await interaction.followup.send(
                f"🔎 Nichts davon ist in der Bibliothek: **{discord.utils.escape_markdown(titel.strip())}**."
            )
            return
        view = DeletePicker(bot, interaction.user, titles)
        view.message = await interaction.followup.send(embed=view.embed(view.current), view=view, wait=True)

    return [film, serie, status, loeschen]
