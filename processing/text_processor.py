from __future__ import annotations

import html
import re
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from processing.locale import Locale

logger = logging.getLogger(__name__)

# --- Two-tier keyword system ---
# A report is relevant only if it matches at least one keyword from EACH tier.

# ICE / immigration enforcement keywords.
# Single words use word-boundary regex; phrases use substring match.
ICE_KEYWORDS_EXACT: set[str] = {
    # These require word boundaries to avoid false positives
    # ("ice" matches "notice", "service" etc. without boundaries)
    "ice",
    "ero",
}

ICE_KEYWORDS_PHRASE: set[str] = {
    # These are specific enough to use substring matching
    "immigration and customs enforcement",
    "immigration enforcement",
    "enforcement and removal",
    "deportation",
    "deportation raid",
    "immigration raid",
    "immigration arrest",
    "federal agents",
    "detained by",
    "detention center",
    "immigration checkpoint",
    "ice agents",
    "ice raid",
    "ice officers",
    "immigration officers",
    "customs enforcement",
    "removal operations",
    "ice vehicle",
    "unmarked van",
    "unmarked vehicle",
    "unmarked suv",
    "ice sighting",
    "ice spotted",
    "ice watch",
    "ice activity",
    "immigration sweep",
    "deportation force",
    "rapid response",
    "know your rights",
    "community alert",
    "ice detainer",
}

# Pre-compiled word-boundary patterns for exact keywords
_ICE_EXACT_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in ICE_KEYWORDS_EXACT) + r")\b",
    re.IGNORECASE,
)

# ── Geographic keywords ───────────────────────────────────────────────
# Loaded at startup from the active locale via init_geo_keywords().
# Falls back to a small default set if init hasn't been called.
GEO_KEYWORDS: set[str] = set()


def init_geo_keywords(locale: Locale) -> None:
    """Populate GEO_KEYWORDS from the active locale.

    Called once at startup from main.py after loading the config/locale.
    """
    global GEO_KEYWORDS
    GEO_KEYWORDS = set(locale.geo_keywords)
    logger.info("text_processor: loaded %d geo keywords from locale '%s'", len(GEO_KEYWORDS), locale.name)
# ── Noise rejection ──────────────────────────────────────────────────
# Terms that cause false positives when "ice" is matched.
# If text contains these WITHOUT a stronger ICE phrase, it's likely noise.
NOISE_CONTEXTS = re.compile(
    r"\b(?:"
    r"ice cream|ice fishing|ice skating|icy roads|"
    r"black ice|ice dam|ice storm|ice hockey|"
    r"ice rink|dry ice|thin ice|break the ice|"
    r"ice scraper|ice melt|ice cold|iced coffee|iced tea"
    r")\b",
    re.IGNORECASE,
)

# ── News article rejection ───────────────────────────────────────────
# These patterns indicate a NEWS ARTICLE about past events, court cases,
# or policy discussions - NOT real-time ICE activity reports.
# We want to filter these out to focus on actionable, real-time alerts.
NEWS_ARTICLE_PATTERNS = re.compile(
    r"\b(?:"
    # Court/legal proceedings
    r"arrested for|charged with|pleaded guilty|found guilty|sentenced to|"
    r"indicted|arraigned|convicted of|faces charges|facing charges|"
    r"appeared in court|court documents|federal complaint|"
    r"justice department|department of justice|doj |"
    r"prosecutor|prosecution|defendant|"
    r"judge.{0,5}s? order|court order|ruling|lawsuit|"
    r"filed suit|legal challenge|appeals court|federal court|"
    r"supreme court|district court|"
    # Threats/crimes AGAINST officers (not ICE enforcement activity)
    r"threatening .{0,30}officer|threat.{0,20}against|"
    r"assault.{0,20}officer|attack.{0,20}officer|"
    r"allegedly threaten|accused of threaten|"
    # Past tense deportation news (not real-time)
    r"was deported|were deported|been deported|got deported|"
    r"sent .{0,20}to mexico|sent .{0,20}to .{0,20}country|"
    r"was sent back|were sent back|"
    r"despite .{0,30}order|defied .{0,20}order|"
    r"violated .{0,20}order|"
    # Policy/political news (not real-time)
    r"executive order|policy change|legislation|lawmakers|"
    r"congress |senate |house bill|proposed bill|"
    r"administration.{0,20}announce|press conference|"
    r"white house|trump administration|biden administration|"
    # Statistics and reports (retrospective)
    r"according to .{0,30}report|study finds|data shows|"
    r"fiscal year|annual report|statistics show|"
    # News article language
    r"the government says|officials said|sources say|"
    r"in a statement|released a statement|"
    r"breaking:|update:|developing:|"
    r"earlier today|yesterday|last week|last month|"
    # Opinion/analysis
    r"opinion:|editorial:|analysis:|commentary:"
    r")\b",
    re.IGNORECASE,
)

# ── Source-based trust levels ─────────────────────────────────────────
# Different sources get different filtering treatment:
# - TRUSTED: Community reporting platforms (Iceout, StopICE) - skip news filtering
# - COMMUNITY: Activist Twitter/social accounts - lighter filtering
# - NEWS: RSS/news sources - strict filtering, require real-time signals
TRUSTED_SOURCES = {"iceout", "stopice"}
COMMUNITY_SOURCES = {"twitter", "bluesky", "reddit"}
NEWS_SOURCES = {"rss"}

# ── Real-time activity signals ───────────────────────────────────────
# These phrases strongly indicate CURRENT/ONGOING ICE activity.
# If present, we should NOT filter out the report even if it has some news-like words.
# NOTE: Be careful - don't include generic news words like "alert" or "breaking"
REALTIME_SIGNALS = re.compile(
    r"\b(?:"
    r"right now|happening now|currently at|"
    r"just saw|just spotted|spotted at|seen at|"
    r"ice (?:is |are )?here|they.{0,10}here|"
    r"at .{0,30}right now|"
    r"heads up|"
    r"avoid .{0,20}area|stay away from|"
    r"confirmed sighting|unconfirmed sighting|"
    r"ice sighting|ice spotted|"
    r"iceout\.org|community report|"
    r"rapid response|know your rights"
    r")\b",
    re.IGNORECASE,
)

# Pre-compile a URL stripping pattern
_URL_RE = re.compile(r"https?://\S+")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def clean_text(text: str) -> str:
    """Normalize text for processing."""
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _URL_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def _match_ice_keywords(text_lower: str) -> list[str]:
    """Find ICE-related keyword matches with word-boundary awareness."""
    matches = []

    # Check exact keywords with word boundaries
    for m in _ICE_EXACT_RE.finditer(text_lower):
        matches.append(m.group())

    # Check phrase keywords with substring matching
    for kw in ICE_KEYWORDS_PHRASE:
        if kw in text_lower:
            matches.append(kw)

    return matches


def _match_geo_keywords(text_lower: str) -> list[str]:
    """Find geographic keyword matches."""
    return [kw for kw in GEO_KEYWORDS if kw in text_lower]


def find_matching_keywords(text: str) -> tuple[list[str], list[str]]:
    """Return (matched_ice_keywords, matched_geo_keywords) found in text."""
    text_lower = text.lower()
    ice_matches = _match_ice_keywords(text_lower)
    geo_matches = _match_geo_keywords(text_lower)
    return ice_matches, geo_matches


def is_relevant(text: str, source_type: str = "unknown") -> bool:
    """Check if text is about real-time ICE enforcement activity.

    Filters applied:
    1. Must match at least one ICE keyword AND one geo keyword
    2. Rejects noise contexts ("ice cream", "ice fishing", etc.)
    3. For NEWS sources: Requires explicit real-time signals to pass
    4. For COMMUNITY sources: Rejects obvious news articles
    5. For TRUSTED sources: Minimal filtering (just keywords + noise)

    The goal is to surface ACTIONABLE reports about ICE activity
    happening NOW, not news coverage of past events.

    Args:
        text: The text to analyze
        source_type: Source identifier ('rss', 'twitter', 'iceout', etc.)
    """
    text_lower = text.lower()
    ice_matches = _match_ice_keywords(text_lower)
    geo_matches = _match_geo_keywords(text_lower)

    if not ice_matches or not geo_matches:
        return False

    # If the only ICE match is the bare word "ice", check for noise contexts
    if ice_matches == ["ice"] or all(m == "ice" for m in ice_matches):
        if NOISE_CONTEXTS.search(text_lower):
            return False

    # Source-based filtering strategy
    has_realtime_signal = bool(REALTIME_SIGNALS.search(text_lower))
    has_news_pattern = bool(NEWS_ARTICLE_PATTERNS.search(text_lower))

    # TRUSTED sources (Iceout, StopICE): These are curated community platforms
    # that only have real ICE reports. Minimal filtering needed.
    if source_type in TRUSTED_SOURCES:
        return True

    # NEWS sources (RSS): These are news websites that may report
    # on court cases, policy, past events. Reject only if NEWS patterns found.
    if source_type in NEWS_SOURCES:
        if has_realtime_signal:
            return True
        # If it has news article patterns (court cases, past tense, etc.), reject it
        if has_news_pattern:
            logger.info(
                "Rejecting news article: [%s] %s...",
                source_type,
                text[:80].replace('\n', ' ')
            )
            return False
        # Otherwise allow it through - it matched ICE + geo keywords
        return True

    # COMMUNITY sources (Twitter, Bluesky, Reddit): Mixed content.
    # Allow through unless it has clear news article patterns.
    if has_realtime_signal:
        return True

    if has_news_pattern:
        logger.debug(
            "Rejecting news article: [%s] %s...",
            source_type,
            text[:80].replace('\n', ' ')
        )
        return False

    return True


def get_all_matched_keywords(text: str) -> list[str]:
    """Return combined list of all matched keywords."""
    ice_matches, geo_matches = find_matching_keywords(text)
    return ice_matches + geo_matches
