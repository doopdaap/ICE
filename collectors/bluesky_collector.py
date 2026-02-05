"""Bluesky collector using the AT Protocol public API.

Bluesky has a free, open API that doesn't require authentication
for reading public posts. This collector:
    1. Searches for ICE/immigration-related posts in Minneapolis area
    2. Monitors specific accounts (journalists, activists, news orgs)

No API key needed - just HTTP requests to the public API.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp

from collectors.base import BaseCollector
from storage.models import RawReport

logger = logging.getLogger(__name__)

# ── Bluesky API endpoints ─────────────────────────────────────────────
BSKY_PUBLIC_API = "https://public.api.bsky.app"

# ── Search queries ────────────────────────────────────────────────────
SEARCH_QUERIES = [
    "ICE Minneapolis",
    "ICE Minnesota",
    "immigration raid Minneapolis",
    "deportation Minnesota",
    "ICE agents Minneapolis",
    "immigration enforcement Minneapolis",
]

# ── Accounts to monitor ───────────────────────────────────────────────
# Bluesky handles (without @)
# These were found by searching for stale Twitter accounts on Bluesky
MONITORED_ACCOUNTS = [
    # ── News orgs ──
    "startribune.bsky.social",       # Star Tribune - active, major MN newspaper
    "bringmethenews.bsky.social",    # Bring Me The News - 6242 posts, very active!
    "sahanjournal.bsky.social",      # Sahan Journal - 908 posts, immigrant community news
    # Note: mprnews.org exists but has 0 posts

    # ── Journalists ──
    "maxnesterak.bsky.social",       # Max Nesterak - 723 posts, MN Reformer journalist
    # Note: mwilliamsonmn.bsky.social and nickvalencia.bsky.social exist but have 0 posts

    # ── Community orgs & activists ──
    "miracmn.bsky.social",           # MIRAC - 37 posts, MN Immigrant Rights Action Committee
    "conmijente.bsky.social",        # Mijente - 21 posts, Latinx organizing collective
    "defend612.bsky.social",         # Defend 612 - 5 posts, Minneapolis rapid-response network
    "sunrisemvmt.bsky.social",       # Sunrise Movement - 367 posts, climate/social justice
    # Note: unitedwedream.org exists but has 0 posts
]

# ── Relevance filtering ───────────────────────────────────────────────
import re

ICE_KEYWORDS_RE = re.compile(
    r"\b(?:"
    r"ice\b|"
    r"immigration\s+(?:enforce|raid|arrest|agent|sweep|operation)|"
    r"deportat|"
    r"deport(?:ed|ing|s)\b|"
    r"federal\s+agent|"
    r"ice\s+(?:officer|agent|arrest|raid|detain)|"
    r"ero\b|"
    r"detention|"
    r"undocumented|"
    r"rapid\s+response|"
    r"community\s+alert|"
    r"unmarked\s+(?:van|vehicle|car|suv)"
    r")",
    re.IGNORECASE,
)

MN_KEYWORDS_RE = re.compile(
    r"\b(?:"
    r"minneapolis|mpls|minnesota|hennepin|"
    r"st\.?\s*paul|saint\s+paul|twin\s+cities|"
    r"bloomington|eden\s+prairie|brooklyn\s+(?:park|center)|"
    r"lake\s+street|cedar[\s-]riverside|uptown|"
    r"whittier|powderhorn|phillips|seward|longfellow"
    r")",
    re.IGNORECASE,
)

# MN-focused accounts don't need geo keyword (only need ICE keyword)
MN_FOCUSED_ACCOUNTS = {
    # News
    "startribune.bsky.social",
    "bringmethenews.bsky.social",
    "sahanjournal.bsky.social",
    # Journalists
    "maxnesterak.bsky.social",
    # MN community orgs
    "miracmn.bsky.social",
    "defend612.bsky.social",
}


def _post_is_relevant(text: str, author_handle: str) -> bool:
    """Check if a post is about ICE enforcement in Minnesota."""
    handle_lower = author_handle.lower()

    has_ice = bool(ICE_KEYWORDS_RE.search(text))
    has_mn = bool(MN_KEYWORDS_RE.search(text))

    # MN-focused accounts only need ICE keyword
    if handle_lower in MN_FOCUSED_ACCOUNTS:
        return has_ice

    return has_ice and has_mn


class BlueskyCollector(BaseCollector):
    """Collects posts from Bluesky about ICE activity via public API."""

    name = "bluesky"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._session: aiohttp.ClientSession | None = None
        self._search_index = 0

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Accept": "application/json"}
            )
        return self._session

    async def _search_posts(self, query: str, limit: int = 25) -> list[dict]:
        """Search Bluesky for posts matching a query."""
        session = await self._ensure_session()

        url = f"{BSKY_PUBLIC_API}/xrpc/app.bsky.feed.searchPosts"
        params = {
            "q": query,
            "limit": limit,
            "sort": "latest",
        }

        try:
            async with session.get(url, params=params, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("posts", [])
                else:
                    logger.debug(
                        "[bluesky] Search failed for '%s': HTTP %d",
                        query, resp.status
                    )
                    return []
        except asyncio.TimeoutError:
            logger.debug("[bluesky] Search timeout for '%s'", query)
            return []
        except Exception as e:
            logger.debug("[bluesky] Search error for '%s': %s", query, e)
            return []

    async def _get_author_feed(self, handle: str, limit: int = 20) -> list[dict]:
        """Get recent posts from a specific author."""
        session = await self._ensure_session()

        # First resolve handle to DID
        resolve_url = f"{BSKY_PUBLIC_API}/xrpc/com.atproto.identity.resolveHandle"
        try:
            async with session.get(
                resolve_url,
                params={"handle": handle},
                timeout=10
            ) as resp:
                if resp.status != 200:
                    logger.debug("[bluesky] Could not resolve handle: %s", handle)
                    return []
                data = await resp.json()
                did = data.get("did")
                if not did:
                    return []
        except Exception as e:
            logger.debug("[bluesky] Handle resolution error for %s: %s", handle, e)
            return []

        # Get author's feed
        feed_url = f"{BSKY_PUBLIC_API}/xrpc/app.bsky.feed.getAuthorFeed"
        params = {
            "actor": did,
            "limit": limit,
            "filter": "posts_no_replies",
        }

        try:
            async with session.get(feed_url, params=params, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return [item.get("post", {}) for item in data.get("feed", [])]
                else:
                    logger.debug(
                        "[bluesky] Feed fetch failed for %s: HTTP %d",
                        handle, resp.status
                    )
                    return []
        except Exception as e:
            logger.debug("[bluesky] Feed error for %s: %s", handle, e)
            return []

    def _parse_post(self, post: dict, now: datetime) -> RawReport | None:
        """Convert a Bluesky post to a RawReport."""
        try:
            # Extract post data
            uri = post.get("uri", "")
            cid = post.get("cid", "")

            record = post.get("record", {})
            text = record.get("text", "")
            created_at_str = record.get("createdAt", "")

            author = post.get("author", {})
            handle = author.get("handle", "")
            display_name = author.get("displayName", handle)

            if not uri or not text:
                return None

            # Parse timestamp
            try:
                # Bluesky uses ISO 8601 format
                ts = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            except Exception:
                ts = now

            # Check relevance
            if not _post_is_relevant(text, handle):
                return None

            # Build source ID and URL
            # URI format: at://did:plc:xxx/app.bsky.feed.post/yyy
            # Convert to web URL
            parts = uri.split("/")
            if len(parts) >= 5:
                rkey = parts[-1]
                web_url = f"https://bsky.app/profile/{handle}/post/{rkey}"
            else:
                web_url = f"https://bsky.app/profile/{handle}"

            source_id = f"bluesky_{cid}" if cid else f"bluesky_{uri}"

            if not self._is_new(source_id):
                return None

            return RawReport(
                source_type="bluesky",
                source_id=source_id,
                source_url=web_url,
                author=f"@{handle}",
                text=text,
                timestamp=ts,
                collected_at=now,
                raw_metadata={
                    "uri": uri,
                    "cid": cid,
                    "handle": handle,
                    "display_name": display_name,
                    "like_count": post.get("likeCount", 0),
                    "repost_count": post.get("repostCount", 0),
                    "reply_count": post.get("replyCount", 0),
                },
            )
        except Exception as e:
            logger.debug("[bluesky] Error parsing post: %s", e)
            return None

    def get_poll_interval(self) -> int:
        """Poll every 2 minutes by default."""
        return getattr(self.config, "bluesky_poll_interval", 120)

    async def collect(self) -> list[RawReport]:
        """Collect posts from Bluesky."""
        now = datetime.now(timezone.utc)
        reports: list[RawReport] = []

        # Rotate through search queries (1 per cycle to avoid rate limits)
        if SEARCH_QUERIES:
            query = SEARCH_QUERIES[self._search_index % len(SEARCH_QUERIES)]
            self._search_index += 1

            logger.debug("[bluesky] Searching: %s", query)
            posts = await self._search_posts(query)

            for post in posts:
                report = self._parse_post(post, now)
                if report:
                    reports.append(report)

            await asyncio.sleep(1)  # Rate limit courtesy

        # Check monitored accounts (rotate through them)
        if MONITORED_ACCOUNTS:
            # Check 2 accounts per cycle
            for i in range(2):
                idx = (self._search_index + i) % len(MONITORED_ACCOUNTS)
                handle = MONITORED_ACCOUNTS[idx]

                logger.debug("[bluesky] Checking @%s", handle)
                posts = await self._get_author_feed(handle)

                for post in posts:
                    report = self._parse_post(post, now)
                    if report:
                        reports.append(report)

                await asyncio.sleep(1)

        if reports:
            logger.info("[bluesky] Found %d relevant posts", len(reports))

        return reports

    async def cleanup(self) -> None:
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
