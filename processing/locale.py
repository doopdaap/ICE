"""Locale configuration loader.

Loads locale-specific data (geo keywords, monitored accounts, coordinates, etc.)
from YAML files in the ``locales/`` directory.  Every piece of location-specific
data lives in the locale file so that adding a new city is as simple as creating
a new YAML file and setting ``LOCALE=<name>`` in ``.env``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Project root — two levels up from processing/locale.py
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOCALES_DIR = _PROJECT_ROOT / "locales"


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Locale:
    """All location-specific configuration for a single metro area."""

    # Identity
    name: str                          # e.g. "minneapolis"
    display_name: str                  # e.g. "Minneapolis/Twin Cities"
    timezone: str                      # IANA tz, e.g. "America/Chicago"

    # Geographic center + radius
    center_lat: float
    center_lon: float
    radius_km: float

    # Fallback strings
    fallback_location: str             # "Minneapolis area"
    fallback_location_unspecified: str  # "Minneapolis (unspecified)"

    # Geodata file paths (absolute)
    neighborhoods_file: str
    landmarks_file: str

    # Keyword sets
    geo_keywords: frozenset[str]       # for text_processor relevance check
    geo_city_names: frozenset[str]     # for collector geo-filtering

    # RSS / Reddit
    rss_feeds: tuple[str, ...]
    subreddits: tuple[str, ...]

    # Platform-specific monitored accounts & queries
    bluesky_search_queries: tuple[str, ...]
    bluesky_monitored_accounts: tuple[str, ...]
    bluesky_trusted_accounts: frozenset[str]

    twitter_search_queries: tuple[str, ...]
    twitter_reporter_accounts: tuple[str, ...]
    twitter_activist_accounts: tuple[str, ...]
    twitter_news_accounts: tuple[str, ...]
    twitter_official_accounts: tuple[str, ...]
    twitter_all_mn_focused: frozenset[str]  # union, lowercased

    instagram_monitored_accounts: tuple[str, ...]

    # Discord display strings
    discord_bot_description: str
    discord_footer_text: str
    discord_subscribe_message: str
    discord_help_description: str

    # ── Derived helpers ───────────────────────────────────────────

    def build_geo_regex(self) -> re.Pattern[str]:
        """Build a compiled regex that matches any geo keyword.

        Useful for collectors that do regex-based filtering on text.
        Multi-word phrases get ``\\s+`` or ``[\\s-]`` between words so
        they match across whitespace variants.
        """
        parts: list[str] = []
        for kw in sorted(self.geo_keywords, key=len, reverse=True):
            escaped = re.escape(kw)
            # Allow flexible whitespace/hyphens in multi-word keywords
            escaped = re.sub(r"\\ ", r"[\\s-]+", escaped)
            parts.append(escaped)
        pattern = r"\b(?:" + "|".join(parts) + r")\b"
        return re.compile(pattern, re.IGNORECASE)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _resolve_path(relative: str) -> str:
    """Turn a project-relative path into an absolute path."""
    return str(_PROJECT_ROOT / relative)


def load_locale(name: str | None = None) -> Locale:
    """Load a locale YAML and return a ``Locale`` instance.

    Parameters
    ----------
    name : str, optional
        Locale name (stem of the YAML file in ``locales/``).
        Defaults to the ``LOCALE`` env var, falling back to ``"minneapolis"``.
    """
    if name is None:
        name = os.getenv("LOCALE", "minneapolis")

    yaml_path = _LOCALES_DIR / f"{name}.yaml"
    if not yaml_path.exists():
        raise FileNotFoundError(
            f"Locale file not found: {yaml_path}\n"
            f"Available locales: {', '.join(p.stem for p in _LOCALES_DIR.glob('*.yaml'))}"
        )

    with open(yaml_path, "r") as f:
        data: dict[str, Any] = yaml.safe_load(f)

    # Build the combined MN-focused Twitter handle set (lowercased)
    tw = data.get("twitter", {})
    all_twitter_handles: set[str] = set()
    for key in ("reporter_accounts", "activist_accounts", "news_accounts", "official_accounts"):
        for handle in tw.get(key, []):
            all_twitter_handles.add(handle.lower())

    bs = data.get("bluesky", {})
    ig = data.get("instagram", {})
    dc = data.get("discord", {})
    center = data.get("center", {})

    locale = Locale(
        name=name,
        display_name=data.get("display_name", name.title()),
        timezone=data.get("timezone", "UTC"),

        center_lat=float(center.get("lat", 0.0)),
        center_lon=float(center.get("lon", 0.0)),
        radius_km=float(data.get("radius_km", 50.0)),

        fallback_location=data.get("fallback_location", f"{name.title()} area"),
        fallback_location_unspecified=data.get(
            "fallback_location_unspecified", f"{name.title()} (unspecified)"
        ),

        neighborhoods_file=_resolve_path(data.get("neighborhoods_file", "")),
        landmarks_file=_resolve_path(data.get("landmarks_file", "")),

        geo_keywords=frozenset(data.get("geo_keywords", [])),
        geo_city_names=frozenset(data.get("geo_city_names", [])),

        rss_feeds=tuple(data.get("rss_feeds", [])),
        subreddits=tuple(data.get("subreddits", [])),

        bluesky_search_queries=tuple(bs.get("search_queries", [])),
        bluesky_monitored_accounts=tuple(bs.get("monitored_accounts", [])),
        bluesky_trusted_accounts=frozenset(bs.get("trusted_accounts", [])),

        twitter_search_queries=tuple(tw.get("search_queries", [])),
        twitter_reporter_accounts=tuple(tw.get("reporter_accounts", [])),
        twitter_activist_accounts=tuple(tw.get("activist_accounts", [])),
        twitter_news_accounts=tuple(tw.get("news_accounts", [])),
        twitter_official_accounts=tuple(tw.get("official_accounts", [])),
        twitter_all_mn_focused=frozenset(all_twitter_handles),

        instagram_monitored_accounts=tuple(ig.get("monitored_accounts", [])),

        discord_bot_description=dc.get("bot_description", "ICE Activity Monitor"),
        discord_footer_text=dc.get("footer_text", "ICE Monitor | Stay safe, know your rights"),
        discord_subscribe_message=dc.get("subscribe_message", "ICE activity is reported in your area"),
        discord_help_description=dc.get("help_description", "Monitors sources for ICE enforcement activity."),
    )

    logger.info(
        "Loaded locale '%s' (%s) — %d geo keywords, center=(%s, %s), radius=%skm",
        locale.name,
        locale.display_name,
        len(locale.geo_keywords),
        locale.center_lat,
        locale.center_lon,
        locale.radius_km,
    )
    return locale
