import logging
import sys

import discord

from .config import ConfigError, Settings
from .core import GB
from .discord_bot import SeerrBot
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
    SeerrBot(settings, seerr).run(settings.discord_token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
