"""Instagram collector using Playwright browser automation.

Instagram doesn't have a public API, but we can scrape public profiles
using Playwright similar to the Twitter collector approach.

Strategy:
    1. Load public profile pages (no login required for public accounts)
    2. Intercept GraphQL API responses to get post data
    3. Filter posts for ICE/Minneapolis relevance
    4. Extract post text, timestamps, and engagement metrics

Monitored accounts from GPT Research:
    - @defend612 - Minneapolis rapid-response network
    - @the5051 - National protest movement against ICE
    - @sunrisetwincities - Twin Cities climate/social justice
    - @indivisible_twincities - Progressive coalition in Minneapolis

IMPORTANT: This approach may violate Instagram/Meta Terms of Service.
Use at your own discretion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from collectors.base import BaseCollector
from storage.models import RawReport

logger = logging.getLogger(__name__)

# ── ICE keyword regex (universal — not locale-specific) ──────────────
ICE_KEYWORDS_RE = re.compile(
    r"\b(?:"
    r"ice\b|"
    r"immigration\s+(?:enforce|raid|arrest|agent|sweep|operation)|"
    r"deportat|"
    r"deport(?:ed|ing|s)\b|"
    r"federal\s+agent|"
    r"ice\s+(?:officer|agent|arrest|raid|detain|sighting|spotted)|"
    r"ero\b|"
    r"detention|"
    r"undocumented|"
    r"rapid\s+response|"
    r"community\s+alert|"
    r"know\s+your\s+rights|"
    r"unmarked\s+(?:van|vehicle|car|suv)"
    r")",
    re.IGNORECASE,
)


def _parse_instagram_timestamp(timestamp: int | str | None) -> datetime:
    """Parse Instagram timestamp (Unix epoch) to datetime."""
    if timestamp is None:
        return datetime.now(timezone.utc)
    try:
        if isinstance(timestamp, str):
            timestamp = int(timestamp)
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


class InstagramCollector(BaseCollector):
    """Collects posts from Instagram about ICE activity via Playwright scraping.

    Uses headed browser to load public Instagram profiles and intercept
    the GraphQL API responses containing post data.
    """

    name = "instagram"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pool = None           # set in _ensure_browser
        self._context = None
        self._accounts_per_cycle = 2  # Check 2 accounts per cycle
        self._cycle_count = 0
        # Build locale-aware data
        locale = self.config.locale
        self._geo_re = locale.build_geo_regex()
        self._monitored_accounts = list(locale.instagram_monitored_accounts)
        self._focused_accounts = {a.lower() for a in locale.instagram_monitored_accounts}

    def _post_is_relevant(self, text: str, username: str) -> bool:
        """Check if a post is about ICE enforcement in the locale area."""
        username_lower = username.lower()
        has_ice = bool(ICE_KEYWORDS_RE.search(text))
        has_geo = bool(self._geo_re.search(text))

        # Locale-focused accounts only need ICE keyword
        if username_lower in self._focused_accounts:
            return has_ice

        return has_ice and has_geo

    async def _ensure_browser(self) -> bool:
        """Obtain a browser context from the shared pool."""
        if self._context is not None:
            return True

        try:
            from collectors.browser_pool import BrowserPool

            self._pool = BrowserPool.shared()
            self._context = await self._pool.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
            )

            logger.info("[instagram] Browser context ready (shared pool)")
            return True

        except Exception as e:
            logger.error("[instagram] Failed to get browser context: %s", e)
            await self._close_browser()
            return False

    async def _scrape_profile(self, username: str) -> list[dict]:
        """Load a public Instagram profile and extract recent posts.

        Instagram embeds post data in a __NEXT_DATA__ script tag or
        through GraphQL API calls. We try both approaches.
        """
        if self._context is None:
            return []

        posts = []
        api_data: list[dict] = []

        async def on_response(response) -> None:
            """Intercept GraphQL responses containing post data."""
            url = response.url
            if response.status == 200:
                # Instagram GraphQL endpoints
                if "graphql" in url or "api/v1/users" in url:
                    try:
                        data = await response.json()
                        api_data.append(data)
                    except Exception:
                        pass

        page = await self._context.new_page()
        page.on("response", on_response)

        try:
            profile_url = f"https://www.instagram.com/{username}/"
            logger.debug("[instagram] Loading profile: %s", profile_url)

            await page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)  # Wait for dynamic content

            # Try to dismiss login modal if it appears
            try:
                # Look for "Not now" or close button on login modal
                not_now_btn = page.locator('text="Not now"')
                if await not_now_btn.is_visible(timeout=2000):
                    await not_now_btn.click()
                    await asyncio.sleep(1)
            except Exception:
                pass

            try:
                # Also try clicking outside the modal or pressing Escape
                close_btn = page.locator('[aria-label="Close"]')
                if await close_btn.is_visible(timeout=1000):
                    await close_btn.click()
                    await asyncio.sleep(1)
            except Exception:
                pass

            try:
                # Press Escape to dismiss any modal
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.5)
            except Exception:
                pass

            # Check if we hit a login wall or the profile doesn't exist
            page_content = await page.content()

            if "Sorry, this page isn't available" in page_content:
                logger.warning("[instagram] @%s profile not found", username)
                return []

            # Check for private account
            if "This account is private" in page_content or "This Account is Private" in page_content:
                logger.warning("[instagram] @%s is a private account", username)
                return []

            if "Log in" in page_content and "to see photos" in page_content.lower():
                logger.warning("[instagram] @%s requires login to view", username)
                return []

            # Try to extract posts from __NEXT_DATA__ script tag
            try:
                next_data = await page.evaluate("""
                    () => {
                        const script = document.querySelector('script#__NEXT_DATA__');
                        if (script) {
                            return JSON.parse(script.textContent);
                        }
                        return null;
                    }
                """)
                if next_data:
                    posts.extend(self._parse_next_data(next_data, username))
            except Exception as e:
                logger.debug("[instagram] Could not parse __NEXT_DATA__: %s", e)

            # Also try to extract from intercepted API responses
            for data in api_data:
                posts.extend(self._parse_api_response(data, username))

            # Try to extract from page HTML as fallback
            if not posts:
                posts.extend(await self._extract_from_html(page, username))

            # Deduplicate by post ID
            seen_ids = set()
            unique_posts = []
            for post in posts:
                post_id = post.get("id")
                if post_id and post_id not in seen_ids:
                    seen_ids.add(post_id)
                    unique_posts.append(post)

            if unique_posts:
                logger.debug(
                    "[instagram] @%s: found %d posts", username, len(unique_posts)
                )

            return unique_posts

        except Exception as e:
            logger.debug("[instagram] Error scraping @%s: %s", username, e)
            return []
        finally:
            try:
                await page.close()
            except Exception:
                pass

    def _parse_next_data(self, data: dict, username: str) -> list[dict]:
        """Extract posts from Instagram's __NEXT_DATA__ JSON."""
        posts = []
        try:
            # Navigate the nested structure to find posts
            # Structure varies, so we try multiple paths

            def find_edges(obj, depth=0):
                """Recursively find edge arrays containing posts."""
                if depth > 10:
                    return []

                edges = []
                if isinstance(obj, dict):
                    # Check for edge_owner_to_timeline_media
                    if "edge_owner_to_timeline_media" in obj:
                        media = obj["edge_owner_to_timeline_media"]
                        edges.extend(media.get("edges", []))

                    # Check for edges directly
                    if "edges" in obj and isinstance(obj["edges"], list):
                        edges.extend(obj["edges"])

                    # Recurse into values
                    for v in obj.values():
                        edges.extend(find_edges(v, depth + 1))

                elif isinstance(obj, list):
                    for item in obj:
                        edges.extend(find_edges(item, depth + 1))

                return edges

            edges = find_edges(data)

            for edge in edges:
                node = edge.get("node", edge)
                if not isinstance(node, dict):
                    continue

                post_id = node.get("id") or node.get("pk")
                shortcode = node.get("shortcode")

                # Get caption text
                caption = ""
                caption_edges = node.get("edge_media_to_caption", {}).get("edges", [])
                if caption_edges:
                    caption = caption_edges[0].get("node", {}).get("text", "")
                elif "caption" in node:
                    cap = node["caption"]
                    if isinstance(cap, dict):
                        caption = cap.get("text", "")
                    elif isinstance(cap, str):
                        caption = cap

                timestamp = node.get("taken_at_timestamp") or node.get("taken_at")

                if post_id and (caption or shortcode):
                    posts.append({
                        "id": str(post_id),
                        "shortcode": shortcode,
                        "text": caption,
                        "timestamp": timestamp,
                        "username": username,
                        "like_count": node.get("edge_liked_by", {}).get("count", 0)
                                     or node.get("like_count", 0),
                        "comment_count": node.get("edge_media_to_comment", {}).get("count", 0)
                                        or node.get("comment_count", 0),
                    })

        except Exception as e:
            logger.debug("[instagram] Error parsing __NEXT_DATA__: %s", e)

        return posts

    def _parse_api_response(self, data: dict, username: str) -> list[dict]:
        """Extract posts from Instagram API response."""
        posts = []
        try:
            # Try to find items/posts in the response
            items = data.get("items", [])
            if not items:
                items = data.get("data", {}).get("user", {}).get("edge_owner_to_timeline_media", {}).get("edges", [])

            for item in items:
                node = item.get("node", item)

                post_id = node.get("id") or node.get("pk")
                shortcode = node.get("code") or node.get("shortcode")

                # Get caption
                caption = ""
                caption_obj = node.get("caption")
                if isinstance(caption_obj, dict):
                    caption = caption_obj.get("text", "")
                elif isinstance(caption_obj, str):
                    caption = caption_obj

                # Alternative caption location
                if not caption:
                    edges = node.get("edge_media_to_caption", {}).get("edges", [])
                    if edges:
                        caption = edges[0].get("node", {}).get("text", "")

                timestamp = node.get("taken_at_timestamp") or node.get("taken_at")

                if post_id:
                    posts.append({
                        "id": str(post_id),
                        "shortcode": shortcode,
                        "text": caption,
                        "timestamp": timestamp,
                        "username": username,
                        "like_count": node.get("like_count", 0),
                        "comment_count": node.get("comment_count", 0),
                    })

        except Exception as e:
            logger.debug("[instagram] Error parsing API response: %s", e)

        return posts

    async def _extract_from_html(self, page, username: str) -> list[dict]:
        """Extract post links from HTML as a fallback."""
        posts = []
        try:
            # Find all post links
            links = await page.evaluate("""
                () => {
                    const links = document.querySelectorAll('a[href*="/p/"]');
                    return Array.from(links).map(a => a.href).slice(0, 12);
                }
            """)

            for link in links:
                # Extract shortcode from URL like /p/ABC123/
                match = re.search(r"/p/([A-Za-z0-9_-]+)/", link)
                if match:
                    shortcode = match.group(1)
                    posts.append({
                        "id": shortcode,
                        "shortcode": shortcode,
                        "text": "",  # Would need to load each post to get caption
                        "timestamp": None,
                        "username": username,
                        "like_count": 0,
                        "comment_count": 0,
                    })

        except Exception as e:
            logger.debug("[instagram] Error extracting from HTML: %s", e)

        return posts

    def get_poll_interval(self) -> int:
        """Poll every 5 minutes by default (Instagram rate limits are strict)."""
        return getattr(self.config, "instagram_poll_interval", 300)

    async def collect(self) -> list[RawReport]:
        """Collect posts from monitored Instagram accounts."""
        if not self._monitored_accounts:
            return []

        if not await self._ensure_browser():
            return []

        now = datetime.now(timezone.utc)
        reports: list[RawReport] = []

        self._cycle_count += 1

        # Rotate through accounts
        start = ((self._cycle_count - 1) * self._accounts_per_cycle) % len(self._monitored_accounts)
        accounts_this_cycle = []
        for i in range(self._accounts_per_cycle):
            idx = (start + i) % len(self._monitored_accounts)
            accounts_this_cycle.append(self._monitored_accounts[idx])

        logger.info(
            "[instagram] Cycle %d: checking @%s",
            self._cycle_count,
            ", @".join(accounts_this_cycle)
        )

        for username in accounts_this_cycle:
            try:
                posts = await self._scrape_profile(username)

                for post in posts:
                    post_id = post.get("id", "")
                    text = post.get("text", "")
                    shortcode = post.get("shortcode", post_id)

                    source_id = f"instagram_{post_id}"

                    if not self._is_new(source_id):
                        continue

                    # Check relevance (or pass through if no text - we got the link)
                    if text and not self._post_is_relevant(text, username):
                        continue

                    timestamp = _parse_instagram_timestamp(post.get("timestamp"))

                    # Build post URL
                    if shortcode:
                        source_url = f"https://www.instagram.com/p/{shortcode}/"
                    else:
                        source_url = f"https://www.instagram.com/{username}/"

                    reports.append(
                        RawReport(
                            source_type="instagram",
                            source_id=source_id,
                            source_url=source_url,
                            author=f"@{username}",
                            text=text or f"[Instagram post from @{username}]",
                            timestamp=timestamp,
                            collected_at=now,
                            raw_metadata={
                                "post_id": post_id,
                                "shortcode": shortcode,
                                "username": username,
                                "like_count": post.get("like_count", 0),
                                "comment_count": post.get("comment_count", 0),
                            },
                        )
                    )

                # Rate limit between accounts
                await asyncio.sleep(3)

            except Exception as e:
                logger.warning("[instagram] Error collecting from @%s: %s", username, e)

        if reports:
            logger.info("[instagram] Found %d relevant posts", len(reports))

        return reports

    async def _close_browser(self) -> None:
        """Release our browser context back to the shared pool."""
        if self._pool and self._context:
            await self._pool.close_context(self._context)

        self._context = None

    def stop(self) -> None:
        super().stop()

    async def cleanup(self) -> None:
        await self._close_browser()
