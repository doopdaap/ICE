import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

from processing.locale import Locale, load_locale


load_dotenv()


def _get_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key, str(default)).lower()
    return val in ("true", "1", "yes")


def _get_int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))


def _get_float(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))


@dataclass(frozen=True)
class Config:
    # Locale — all location-specific data lives here
    locale: Locale = field(default_factory=lambda: load_locale())

    # Discord - supports both webhook (single channel) and bot (multi-server) modes
    discord_webhook_url: str = ""      # For webhook mode (original)
    discord_bot_token: str = ""        # For bot mode (publishable)
    discord_bot_client_id: str = ""    # For generating invite URL

    # Twitter/X
    twitter_enabled: bool = False
    twitter_username: str = ""
    twitter_password: str = ""
    twitter_poll_interval: int = 120


    # Reddit
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "ice-monitor:v0.1"
    reddit_subreddits: tuple[str, ...] = ()  # loaded from locale
    reddit_poll_interval: int = 60

    # RSS
    rss_feeds: tuple[str, ...] = ()  # loaded from locale
    rss_poll_interval: int = 300

    # Iceout.org
    iceout_enabled: bool = True
    iceout_poll_interval: int = 90

    # Bluesky
    bluesky_enabled: bool = True
    bluesky_poll_interval: int = 120

    # StopICE.net
    stopice_enabled: bool = True
    stopice_poll_interval: int = 1800  # 30 minutes (data updates nightly)

    # Instagram
    instagram_enabled: bool = True
    instagram_poll_interval: int = 300  # 5 minutes (strict rate limits)

    # Geographic filtering — read from locale, can be overridden via env
    mpls_center_lat: float = 0.0
    mpls_center_lon: float = 0.0
    max_distance_km: float = 50.0

    # Report freshness — discard reports older than this
    report_max_age_seconds: int = 10800  # 3 hours

    # Correlation
    correlation_window_seconds: int = 10800  # 3 hours
    min_corroboration_sources: int = 2
    similarity_threshold: float = 0.35
    geo_proximity_km: float = 3.0
    correlation_check_interval: int = 60

    # Cluster expiry - stops sending update notifications after this many hours
    cluster_expiry_hours: float = 6.0

    # Database
    db_path: str = "ice_monitor.db"

    # General
    log_level: str = "INFO"
    dry_run: bool = False


def load_config() -> Config:
    locale = load_locale()  # uses LOCALE env var, defaults to "minneapolis"

    reddit_subs_raw = os.getenv("REDDIT_SUBREDDITS", "")
    reddit_subs = (
        tuple(s.strip() for s in reddit_subs_raw.split(",") if s.strip())
        if reddit_subs_raw
        else locale.subreddits
    )

    rss_feeds_raw = os.getenv("RSS_FEEDS", "")
    rss_feeds = (
        tuple(s.strip() for s in rss_feeds_raw.split(",") if s.strip())
        if rss_feeds_raw
        else locale.rss_feeds
    )

    return Config(
        locale=locale,
        discord_webhook_url=os.getenv("DISCORD_WEBHOOK_URL", ""),
        discord_bot_token=os.getenv("DISCORD_BOT_TOKEN", ""),
        discord_bot_client_id=os.getenv("DISCORD_BOT_CLIENT_ID", ""),
        twitter_enabled=_get_bool("TWITTER_ENABLED"),
        twitter_username=os.getenv("TWITTER_USERNAME", ""),
        twitter_password=os.getenv("TWITTER_PASSWORD", ""),
        twitter_poll_interval=_get_int("TWITTER_POLL_INTERVAL", 120),
        reddit_client_id=os.getenv("REDDIT_CLIENT_ID", ""),
        reddit_client_secret=os.getenv("REDDIT_CLIENT_SECRET", ""),
        reddit_user_agent=os.getenv("REDDIT_USER_AGENT", "ice-monitor:v0.1"),
        reddit_subreddits=reddit_subs,
        reddit_poll_interval=_get_int("REDDIT_POLL_INTERVAL", 60),
        rss_feeds=rss_feeds,
        rss_poll_interval=_get_int("RSS_POLL_INTERVAL", 300),
        iceout_enabled=_get_bool("ICEOUT_ENABLED", True),
        iceout_poll_interval=_get_int("ICEOUT_POLL_INTERVAL", 90),
        bluesky_enabled=_get_bool("BLUESKY_ENABLED", True),
        bluesky_poll_interval=_get_int("BLUESKY_POLL_INTERVAL", 120),
        stopice_enabled=_get_bool("STOPICE_ENABLED", True),
        stopice_poll_interval=_get_int("STOPICE_POLL_INTERVAL", 1800),
        instagram_enabled=_get_bool("INSTAGRAM_ENABLED", True),
        instagram_poll_interval=_get_int("INSTAGRAM_POLL_INTERVAL", 300),
        mpls_center_lat=_get_float("CENTER_LAT", locale.center_lat),
        mpls_center_lon=_get_float("CENTER_LON", locale.center_lon),
        max_distance_km=_get_float("MAX_DISTANCE_KM", locale.radius_km),
        report_max_age_seconds=_get_int("REPORT_MAX_AGE_SECONDS", 10800),
        correlation_window_seconds=_get_int("CORRELATION_WINDOW_SECONDS", 10800),
        min_corroboration_sources=_get_int("MIN_CORROBORATION_SOURCES", 2),
        similarity_threshold=_get_float("SIMILARITY_THRESHOLD", 0.35),
        geo_proximity_km=_get_float("GEO_PROXIMITY_KM", 3.0),
        correlation_check_interval=_get_int("CORRELATION_CHECK_INTERVAL", 60),
        cluster_expiry_hours=_get_float("CLUSTER_EXPIRY_HOURS", 6.0),
        db_path=os.getenv("DB_PATH", "ice_monitor.db"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        dry_run=_get_bool("DRY_RUN"),
    )
