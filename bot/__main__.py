import logging
import sys

import discord

from .config import ConfigError, Settings
from .core import GB
from .discord_bot import SeerrBot
from .jellyfin import JellyfinClient
from .seerr import SeerrClient


def main() -> int:
    discord.utils.setup_logging(level=logging.INFO)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        logging.getLogger("seerrbot").error("%s", exc)
        return 2
    seerr = SeerrClient(settings.seerr_url, settings.seerr_api_key, settings.seerr_language, settings.seerr_user_id,
                        min_free=settings.min_free_gb * GB)
    jellyfin = None
    if settings.friends_enabled:
        jellyfin = JellyfinClient(settings.jellyfin_api_url, settings.jellyfin_api_key, settings.friend_libraries,
                                  settings.friend_bitrate_mbit * 1_000_000)
    SeerrBot(settings, seerr, jellyfin=jellyfin).run(settings.discord_token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
