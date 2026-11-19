import logging
import sys

import discord

from .config import ConfigError, Settings
from .core import GB
from .discord_bot import SeerrBot
from .jellyfin import JellyfinClient
from .torrents import ArrClient, QbitClient, TorrentCleaner
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
    torrents = None
    if settings.torrent_cleanup_enabled:
        arrs = {}
        if settings.sonarr_url and settings.sonarr_api_key:
            arrs["tv"] = ArrClient(settings.sonarr_url, settings.sonarr_api_key, "tv")
        if settings.radarr_url and settings.radarr_api_key:
            arrs["movie"] = ArrClient(settings.radarr_url, settings.radarr_api_key, "movie")
        torrents = TorrentCleaner(QbitClient(settings.qbittorrent_url, settings.qbittorrent_username,
                                             settings.qbittorrent_password), arrs)
    SeerrBot(settings, seerr, jellyfin=jellyfin, torrents=torrents).run(settings.discord_token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
