"""Settings, read from environment variables (the .env file)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

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

    @property
    def deleting_enabled(self) -> bool:
        return self.admin_role_id is not None or bool(self.admin_user_ids)

    @classmethod
    def from_env(cls) -> "Settings":
        url = _text("SEERR_URL", "http://localhost:5055").rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ConfigError("SEERR_URL must start with http:// or https://")
        public = _text("SEERR_PUBLIC_URL").rstrip("/")
        if public and not public.startswith(("http://", "https://")):
            raise ConfigError("SEERR_PUBLIC_URL must start with http:// or https://")
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
        )

    def page_url(self, kind: str, tmdb_id: int) -> str | None:
        """Link to the movie or show in Seerr, if a public address is set."""
        return f"{self.seerr_public_url}/{kind}/{tmdb_id}" if self.seerr_public_url else None
