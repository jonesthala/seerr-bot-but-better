"""Settings, read from environment variables (the .env file)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import timezone, tzinfo
from urllib.parse import urlsplit

from .core import parse_digest

TOKEN_HELP = "Use the bot token: Developer Portal -> your app -> Bot -> Reset Token."


class ConfigError(RuntimeError):
    pass


def _text(name: str, default: str = "") -> str:
    # `docker run --env-file` keeps quotes as part of the value, so drop one matching pair.
    value = os.environ.get(name, default).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def _token() -> str:
    """The bot token, refusing the other values on the app's pages that are easy to mix up with it."""
    token = _required("DISCORD_TOKEN")
    if re.fullmatch(r"[0-9a-fA-F]{64}", token):
        raise ConfigError(f"DISCORD_TOKEN is the app's Public Key. {TOKEN_HELP}")
    if token.isdigit():
        raise ConfigError(f"DISCORD_TOKEN is an ID. {TOKEN_HELP}")
    if re.fullmatch(r"[A-Za-z0-9_-]{32}", token):
        raise ConfigError(f"DISCORD_TOKEN looks like the OAuth2 Client Secret. {TOKEN_HELP}")
    return token


def _required(name: str) -> str:
    value = _text(name)
    if not value:
        raise ConfigError(f"Environment variable {name} is missing")
    return value


def _int(name: str, default: int | None = None) -> int:
    raw = _text(name)
    if not raw:
        if default is None:
            raise ConfigError(f"Environment variable {name} is missing")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc


def _id_set(name: str) -> frozenset[int]:
    raw = _text(name).replace(" ", "")
    try:
        return frozenset(int(part) for part in raw.split(",") if part)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a comma-separated list of numbers") from exc


def _url(name: str) -> str:
    value = _text(name).rstrip("/")
    if value and not value.startswith(("http://", "https://")):
        raise ConfigError(f"{name} must start with http:// or https://")
    return value


def _names(name: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in _text(name).split(",") if part.strip())


def _zone(name: str) -> tzinfo:
    value = _text(name, "UTC") or "UTC"
    if value.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError as exc:  # pragma: no cover - Python < 3.9
        raise ConfigError("TIMEZONE needs Python 3.9 or newer") from exc
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"{name} is not a known time zone, e.g. Europe/Zurich") from exc


@dataclass(frozen=True)
class Settings:
    discord_token: str
    guild_id: int
    channel_id: int
    request_role_id: int | None
    admin_role_id: int | None
    admin_user_ids: frozenset[int]
    seerr_url: str
    seerr_api_key: str
    seerr_language: str
    seerr_user_id: int | None
    seerr_public_url: str
    min_free_gb: int = 15
    approval_channel_id: int | None = None
    data_dir: str = "data"
    request_limit: int = 0
    request_limit_days: int = 7
    weekly_digest: tuple[int, int, int] | None = None
    zone: tzinfo = field(default=timezone.utc)
    movie_profiles: tuple[str, ...] = ()
    tv_profiles: tuple[str, ...] = ()
    jellyfin_url: str = ""
    # /jellyfin-zugang: accounts for friends (all four needed, plus admins to approve)
    jellyfin_api_url: str = ""  # where the bot reaches Jellyfin
    jellyfin_api_key: str = ""
    jellyfin_friend_url: str = ""  # the address friends type into the Jellyfin app
    tailscale_share_url: str = ""  # multi-use share invite for the Jellyfin machine
    friend_libraries: tuple[str, ...] = ()
    friend_bitrate_mbit: int = 4
    # /löschen also removes the torrents (qBittorrent login, and Sonarr/Radarr to know which torrents)
    qbittorrent_url: str = ""
    qbittorrent_username: str = ""
    qbittorrent_password: str = ""
    sonarr_url: str = ""
    sonarr_api_key: str = ""
    radarr_url: str = ""
    radarr_api_key: str = ""

    @property
    def torrent_cleanup_enabled(self) -> bool:
        qbit = all((self.qbittorrent_url, self.qbittorrent_username, self.qbittorrent_password))
        return qbit and bool((self.sonarr_url and self.sonarr_api_key) or (self.radarr_url and self.radarr_api_key))

    @property
    def friends_enabled(self) -> bool:
        return self.has_admins and all((self.jellyfin_api_url, self.jellyfin_api_key, self.jellyfin_friend_url,
                                        self.tailscale_share_url))

    def watch_url(self, url: str | None) -> str | None:
        """Seerr's link to a title in Jellyfin, with JELLYFIN_URL as its address if that is set."""
        if not url:
            return None
        if not self.jellyfin_url:
            return url
        parts = urlsplit(url)
        return self.jellyfin_url + url[len(f"{parts.scheme}://{parts.netloc}"):]

    @property
    def has_admins(self) -> bool:
        return self.admin_role_id is not None or bool(self.admin_user_ids)

    @property
    def deleting_enabled(self) -> bool:
        return self.has_admins

    @property
    def approvals_enabled(self) -> bool:
        """Pending requests get Approve/Decline buttons in Discord."""
        return self.has_admins

    @property
    def approvals_channel(self) -> int:
        return self.approval_channel_id or self.channel_id

    def profile_names(self, kind: str) -> tuple[str, ...]:
        return self.movie_profiles if kind == "movie" else self.tv_profiles

    @classmethod
    def from_env(cls) -> "Settings":
        url = _text("SEERR_URL", "http://localhost:5055").rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ConfigError("SEERR_URL must start with http:// or https://")
        public = _text("SEERR_PUBLIC_URL").rstrip("/")
        if public and not public.startswith(("http://", "https://")):
            raise ConfigError("SEERR_PUBLIC_URL must start with http:// or https://")
        jellyfin = _text("JELLYFIN_URL").rstrip("/")
        if jellyfin and not jellyfin.startswith(("http://", "https://")):
            raise ConfigError("JELLYFIN_URL must start with http:// or https://")
        try:
            digest = parse_digest(_text("WEEKLY_DIGEST"))
        except ValueError as exc:
            raise ConfigError("WEEKLY_DIGEST must look like 'sun 18:00' (or empty to switch it off)") from exc
        limit, limit_days = _int("REQUEST_LIMIT", 0), _int("REQUEST_LIMIT_DAYS", 7)
        if limit < 0 or limit_days < 1:
            raise ConfigError("REQUEST_LIMIT must be 0 or more and REQUEST_LIMIT_DAYS at least 1")
        api_url = _url("JELLYFIN_API_URL")
        friend_url = _url("JELLYFIN_FRIEND_URL")
        share_url = _text("TAILSCALE_SHARE_URL")
        if share_url and not share_url.startswith("https://"):
            raise ConfigError("TAILSCALE_SHARE_URL must be the invite link, starting with https://")
        bitrate = _int("JELLYFIN_FRIEND_BITRATE_MBIT", 4)
        if bitrate < 0:
            raise ConfigError("JELLYFIN_FRIEND_BITRATE_MBIT must be 0 (no limit) or more")
        return cls(
            discord_token=_token(),
            guild_id=_int("DISCORD_GUILD_ID"),
            channel_id=_int("DISCORD_CHANNEL_ID"),
            request_role_id=_int("DISCORD_REQUEST_ROLE_ID", 0) or None,
            admin_role_id=_int("DISCORD_ADMIN_ROLE_ID", 0) or None,
            admin_user_ids=_id_set("DISCORD_ADMIN_USER_IDS"),
            seerr_url=url,
            seerr_api_key=_required("SEERR_API_KEY"),
            seerr_language=_text("SEERR_LANGUAGE", "de") or "de",
            seerr_user_id=_int("SEERR_USER_ID", 0) or None,
            seerr_public_url=public,
            min_free_gb=_int("MIN_FREE_GB", 15),
            approval_channel_id=_int("DISCORD_APPROVAL_CHANNEL_ID", 0) or None,
            data_dir=_text("DATA_DIR") or "data",
            request_limit=limit,
            request_limit_days=limit_days,
            weekly_digest=digest,
            zone=_zone("TIMEZONE"),
            movie_profiles=_names("MOVIE_PROFILES"),
            tv_profiles=_names("TV_PROFILES"),
            jellyfin_url=jellyfin,
            jellyfin_api_url=api_url,
            jellyfin_api_key=_text("JELLYFIN_API_KEY"),
            jellyfin_friend_url=friend_url,
            tailscale_share_url=share_url,
            friend_libraries=_names("JELLYFIN_FRIEND_LIBRARIES"),
            friend_bitrate_mbit=bitrate,
            qbittorrent_url=_url("QBITTORRENT_URL"),
            qbittorrent_username=_text("QBITTORRENT_USERNAME"),
            qbittorrent_password=_text("QBITTORRENT_PASSWORD"),
            sonarr_url=_url("SONARR_URL"),
            sonarr_api_key=_text("SONARR_API_KEY"),
            radarr_url=_url("RADARR_URL"),
            radarr_api_key=_text("RADARR_API_KEY"),
        )

    def page_url(self, kind: str, tmdb_id: int) -> str | None:
        """Link to the movie or show in Seerr, if a public address is set."""
        return f"{self.seerr_public_url}/{kind}/{tmdb_id}" if self.seerr_public_url else None

    def issue_url(self, issue_id: int | None) -> str | None:
        return f"{self.seerr_public_url}/issues/{issue_id}" if self.seerr_public_url and issue_id else None
