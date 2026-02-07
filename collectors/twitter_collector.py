"""Twitter/X collector using Playwright with authenticated session.

Uses Playwright to log into X, save session cookies, and scrape both
search results and profile timelines via GraphQL API interception.

Strategy:
    1. Log in once via Playwright, persist cookies to disk
    2. On each cycle: run ICE/MN search queries (primary) + scrape profiles
    3. Intercept GraphQL responses (SearchTimeline, UserTweets) for data
    4. Filter tweets for Minneapolis/MN ICE relevance client-side

NOTE: Government ICE/DHS accounts are deliberately excluded — they only
post after-the-fact press releases, not real-time actionable information.

The browser context persists between cycles to reuse the session.

IMPORTANT: This approach may violate Twitter/X Terms of Service.
Use at your own discretion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from collectors.base import BaseCollector
from storage.models import RawReport

logger = logging.getLogger(__name__)

# ── Cookie file path ─────────────────────────────────────────────────
COOKIE_FILE = Path(".twitter_cookies.json")

# File to cache account validation results (stale/nonexistent accounts)
ACCOUNT_CACHE_FILE = Path(".twitter_account_cache.json")

# Max age for account to be considered active (3 months = ~90 days)
ACCOUNT_STALE_DAYS = 90

# ── ICE keyword regex (universal — not locale-specific) ──────────────

ICE_KEYWORDS_RE = re.compile(
    r"\b(?:"
    r"ice\b|"
    r"immigration\s+(?:enforce|raid|arrest|agent|sweep|operation|custom)|"
    r"deportat|"
    r"deport(?:ed|ing|s)\b|"
    r"federal\s+agent|"
    r"ice\s+(?:officer|agent|arrest|raid|detain|surge|watch)|"
    r"operation\s+(?:metro\s+surge|safeguard|aurora)|"
    r"ero\b|"
    r"detention|"
    r"undocumented|"
    r"immigration\s+checkpoint|"
    r"ice\s+sighting|"
    r"ice\s+spotted|"
    r"know\s+your\s+rights|"
    r"rapid\s+response|"
    r"community\s+alert|"
    r"unmarked\s+(?:van|vehicle|car|suv)"
    r")",
    re.IGNORECASE,
)


def _parse_twitter_date(date_str: str) -> datetime | None:
    """Parse Twitter's date format: 'Thu Feb 05 17:05:15 +0000 2026'."""
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str)
    except Exception:
        pass
    try:
        return datetime.strptime(date_str, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        return None


def _extract_tweet_entry(tr: dict) -> dict | None:
    """Extract a single tweet dict from a GraphQL tweet_results.result node."""
    typename = tr.get("__typename", "")
    if typename == "TweetWithVisibilityResults":
        tr = tr.get("tweet", {})
    elif typename != "Tweet":
        return None

    text = ""
    note = tr.get("note_tweet", {})
    if note:
        note_results = note.get("note_tweet_results", {}).get("result", {})
        text = note_results.get("text", "")

    legacy = tr.get("legacy", {})
    if not text:
        text = legacy.get("full_text", "")

    if not text:
        return None

    core = tr.get("core", {})
    user_r = core.get("user_results", {}).get("result", {})
    user_leg = user_r.get("legacy", {})
    screen_name = user_leg.get("screen_name", "")
    display_name = user_leg.get("name", "")

    tweet_id = tr.get("rest_id", "") or legacy.get("id_str", "")
    created_at = legacy.get("created_at", "")

    return {
        "id": tweet_id,
        "text": text,
        "created_at": created_at,
        "screen_name": screen_name,
        "display_name": display_name,
        "retweet_count": legacy.get("retweet_count", 0),
        "favorite_count": legacy.get("favorite_count", 0),
        "is_retweet": text.startswith("RT @"),
    }


def _extract_tweets_from_entries(entries: list) -> list[dict]:
    """Walk a list of timeline entries and extract tweet dicts."""
    tweets = []
    for entry in entries:
        entry_id = entry.get("entryId", "")
        if not entry_id.startswith("tweet-"):
            continue

        content = entry.get("content", {})
        ic = content.get("itemContent", {})
        tr = ic.get("tweet_results", {}).get("result", {})

        tweet = _extract_tweet_entry(tr)
        if tweet:
            tweets.append(tweet)

    return tweets


def _extract_tweets_from_graphql(data: dict) -> list[dict]:
    """Parse Twitter's GraphQL UserTweets response into flat tweet dicts."""
    tweets = []

    def _walk_instructions(instructions: list) -> None:
        for inst in instructions:
            entries = inst.get("entries", [])
            pin_entry = inst.get("entry")
            if pin_entry:
                entries = [pin_entry] + list(entries)
            tweets.extend(_extract_tweets_from_entries(entries))

    try:
        user_result = data.get("data", {}).get("user", {}).get("result", {})
        timeline_obj = user_result.get("timeline_v2") or user_result.get(
            "timeline", {}
        )
        inner_timeline = timeline_obj.get("timeline", {})
        instructions = inner_timeline.get("instructions", [])
        _walk_instructions(instructions)
    except Exception as e:
        logger.debug("[twitter] GraphQL parse error: %s", e)

    return tweets


def _extract_tweets_from_search(data: dict) -> list[dict]:
    """Parse Twitter's GraphQL SearchTimeline response into flat tweet dicts."""
    tweets = []

    try:
        search_tl = (
            data.get("data", {})
            .get("search_by_raw_query", {})
            .get("search_timeline", {})
            .get("timeline", {})
        )
        instructions = search_tl.get("instructions", [])

        for inst in instructions:
            inst_type = inst.get("type", "")
            # TimelineAddEntries is the main one
            if inst_type == "TimelineAddEntries":
                entries = inst.get("entries", [])
                tweets.extend(_extract_tweets_from_entries(entries))
            # Also check for entries directly on the instruction
            elif "entries" in inst:
                tweets.extend(_extract_tweets_from_entries(inst["entries"]))

    except Exception as e:
        logger.debug("[twitter] Search GraphQL parse error: %s", e)

    return tweets


class TwitterCollector(BaseCollector):
    """Collects tweets about ICE activity via authenticated Playwright scraping.

    Two modes:
    - Authenticated (credentials provided): search queries + profile scraping
    - Unauthenticated (no credentials): profile scraping only

    The browser stays alive between polling cycles to reuse the session.

    Account validation:
    - On first run, validates all accounts and caches results
    - Filters out accounts that don't exist or haven't posted in 3+ months
    - Re-validates accounts weekly
    """

    name = "twitter"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._playwright = None
        self._browser = None
        self._context = None
        self._logged_in = False
        self._login_failed = False
        self._accounts_per_cycle = 5
        self._search_queries_per_cycle = 2
        self._active_accounts: list[str] = []  # Validated active accounts
        self._accounts_validated = False
        # Build locale-aware data
        locale = self.config.locale
        self._geo_re = locale.build_geo_regex()
        self._search_queries = list(locale.twitter_search_queries)
        self._all_monitored = (
            list(locale.twitter_reporter_accounts)
            + list(locale.twitter_activist_accounts)
            + list(locale.twitter_news_accounts)
            + list(locale.twitter_official_accounts)
        )
        self._focused_accounts = {h.lower() for h in locale.twitter_all_mn_focused}

    def _tweet_is_relevant(self, text: str, screen_name: str) -> bool:
        """Check if a tweet is about ICE enforcement in the locale area."""
        sn_lower = screen_name.lower()
        has_ice = bool(ICE_KEYWORDS_RE.search(text))
        has_geo = bool(self._geo_re.search(text))

        if sn_lower in self._focused_accounts:
            return has_ice

        return has_ice and has_geo

    @property
    def _has_credentials(self) -> bool:
        return bool(self.config.twitter_username and self.config.twitter_password)

    async def _ensure_browser(self) -> bool:
        """Launch Playwright browser and restore cookies if available."""
        if self._context is not None:
            return True

        try:
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            )
            self._context = await self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
            )

            # Try to restore saved cookies
            if COOKIE_FILE.exists():
                try:
                    cookies = json.loads(COOKIE_FILE.read_text())
                    await self._context.add_cookies(cookies)
                    self._logged_in = True
                    logger.info("[twitter] Restored saved session cookies")
                except Exception as e:
                    logger.debug("[twitter] Could not restore cookies: %s", e)

            logger.info("[twitter] Playwright browser launched (headed mode)")
            return True

        except Exception as e:
            logger.error("[twitter] Failed to launch browser: %s", e)
            await self._close_browser()
            return False

    async def _save_cookies(self) -> None:
        """Save current browser cookies to disk."""
        if self._context is None:
            return
        try:
            cookies = await self._context.cookies()
            COOKIE_FILE.write_text(json.dumps(cookies, indent=2))
            logger.debug("[twitter] Saved %d cookies to %s", len(cookies), COOKIE_FILE)
        except Exception as e:
            logger.debug("[twitter] Failed to save cookies: %s", e)

    async def _login(self) -> bool:
        """Log into X using Playwright. Returns True on success.

        The login is a multi-step SPA flow:
            1. Enter username → click Next
            2. (Possible) Unusual-activity challenge → user handles manually
            3. Enter password → click Log In
            4. (Possible) 2FA or other challenge → user handles manually

        If any challenge appears that needs human input, the browser window
        stays open for up to 120 seconds so the user can complete it.
        """
        if not self._has_credentials or self._context is None:
            return False

        username = self.config.twitter_username
        password = self.config.twitter_password
        self._login_attempts = getattr(self, "_login_attempts", 0) + 1

        # Only allow 3 automated attempts before giving up this session
        if self._login_attempts > 3:
            logger.error("[twitter] Too many login attempts, giving up")
            self._login_failed = True
            return False

        page = await self._context.new_page()
        try:
            logger.info("[twitter] Login attempt %d for @%s ...", self._login_attempts, username)

            await page.goto(
                "https://x.com/i/flow/login",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(4)

            # ── Step 1: Username ──────────────────────────────────
            username_input = page.locator('input[autocomplete="username"]')
            try:
                await username_input.wait_for(state="visible", timeout=15000)
            except Exception:
                logger.error("[twitter] Username field never appeared")
                return False

            # Type slowly to avoid bot detection
            await username_input.click()
            await asyncio.sleep(0.3)
            await username_input.fill(username)
            await asyncio.sleep(1)

            # Click "Next"
            next_btn = page.locator('button:has-text("Next")')
            await next_btn.click()
            logger.debug("[twitter] Clicked Next after username")
            await asyncio.sleep(3)

            # ── Step 2: Check for challenge or password ───────────
            # X may show: (a) password field, (b) "unusual activity" text
            # input, (c) CAPTCHA, (d) email/phone verification

            password_input = page.locator('input[name="password"]')
            challenge_input = page.locator('input[data-testid="ocfEnterTextTextInput"]')

            # Poll for up to 20 seconds for one of them to appear
            found_step = None
            for attempt in range(40):
                if await password_input.is_visible():
                    found_step = "password"
                    break
                if await challenge_input.is_visible():
                    found_step = "challenge"
                    break
                # Also check if we somehow already got through
                if "login" not in page.url.lower() and "flow" not in page.url.lower():
                    found_step = "already_done"
                    break
                if attempt % 10 == 9:
                    # Log what we can see to help debug
                    try:
                        visible_inputs = await page.locator("input:visible").count()
                        visible_btns = await page.locator("button:visible").count()
                        logger.debug(
                            "[twitter] Waiting for next step... "
                            "(%d visible inputs, %d visible buttons, URL: %s)",
                            visible_inputs, visible_btns, page.url,
                        )
                    except Exception:
                        pass
                await asyncio.sleep(0.5)

            if found_step == "already_done":
                self._logged_in = True
                self._login_failed = False
                self._login_attempts = 0
                await self._save_cookies()
                logger.info("[twitter] Login completed (redirected past login)")
                return True

            if found_step is None:
                # Neither password nor challenge appeared — X may be showing
                # something unexpected. Keep page open for user to handle.
                logger.warning(
                    "[twitter] *** MANUAL INTERVENTION NEEDED ***\n"
                    "  Neither password nor challenge field appeared.\n"
                    "  X may be showing a CAPTCHA or other verification.\n"
                    "  Please complete the login in the browser window.\n"
                    "  Waiting up to 120 seconds..."
                )
                for _ in range(60):
                    await asyncio.sleep(2)
                    if "login" not in page.url.lower() and "flow" not in page.url.lower():
                        self._logged_in = True
                        self._login_failed = False
                        self._login_attempts = 0
                        await self._save_cookies()
                        logger.info("[twitter] Login completed (user handled manually)")
                        return True
                    # Check if password appeared after user action
                    if await password_input.is_visible():
                        found_step = "password"
                        break

                if found_step is None:
                    logger.error("[twitter] Login not completed in time")
                    return False

            # Handle challenge (unusual activity — asks for username/email/phone)
            if found_step == "challenge":
                logger.warning(
                    "[twitter] *** UNUSUAL ACTIVITY CHALLENGE ***\n"
                    "  X is asking to verify your identity.\n"
                    "  Please complete the verification in the browser window.\n"
                    "  You have 120 seconds..."
                )
                # Wait for the password field to appear after user handles challenge
                try:
                    await password_input.wait_for(state="visible", timeout=120000)
                except Exception:
                    # Maybe user completed the whole login already
                    if "login" not in page.url.lower() and "flow" not in page.url.lower():
                        self._logged_in = True
                        self._login_failed = False
                        self._login_attempts = 0
                        await self._save_cookies()
                        logger.info("[twitter] Login completed (user handled challenge)")
                        return True
                    logger.error("[twitter] Challenge not completed in time")
                    return False

            # ── Step 3: Password ──────────────────────────────────
            if await password_input.is_visible():
                await password_input.click()
                await asyncio.sleep(0.3)
                await password_input.fill(password)
                await asyncio.sleep(1)

                login_btn = page.locator('button[data-testid="LoginForm_Login_Button"]')
                await login_btn.click()
                logger.debug("[twitter] Clicked Log In button")
                await asyncio.sleep(5)
            else:
                logger.error("[twitter] Password field never appeared. URL: %s", page.url)
                return False

            # ── Step 4: Check result ──────────────────────────────
            current_url = page.url

            # Success — redirected away from login
            if "login" not in current_url.lower() and "flow" not in current_url.lower():
                self._logged_in = True
                self._login_failed = False
                self._login_attempts = 0
                await self._save_cookies()
                logger.info("[twitter] Login successful for @%s!", username)
                return True

            # Still on login page — might be 2FA or another challenge
            # Give user 120 seconds to handle it in the browser
            logger.warning(
                "[twitter] Still on login page after submitting password.\n"
                "  There may be a 2FA prompt or additional verification.\n"
                "  Please complete it in the browser window (120s timeout)..."
            )

            try:
                # Wait for URL to change away from login/flow
                for _ in range(60):
                    await asyncio.sleep(2)
                    current_url = page.url
                    if "login" not in current_url.lower() and "flow" not in current_url.lower():
                        self._logged_in = True
                        self._login_failed = False
                        self._login_attempts = 0
                        await self._save_cookies()
                        logger.info("[twitter] Login successful (user completed verification)")
                        return True
            except Exception:
                pass

            logger.error("[twitter] Login did not complete. Final URL: %s", page.url)
            return False

        except Exception as e:
            logger.error("[twitter] Login error: %s", e)
            return False
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _manual_login(self) -> bool:
        """Open X login page and let the user log in manually.

        This is the most reliable method since the user handles all
        anti-bot challenges, CAPTCHAs, and 2FA themselves.
        """
        if self._context is None:
            return False

        page = await self._context.new_page()
        try:
            print("\n" + "=" * 60)
            print("  MANUAL X/TWITTER LOGIN REQUIRED")
            print("=" * 60)
            print("  A browser window should now be visible.")
            print("  Please log in manually in that window.")
            print("  Handle any CAPTCHAs, 2FA, or verifications as needed.")
            print("  You have 5 minutes to complete login.")
            print("=" * 60 + "\n")
            logger.info("[twitter] Opening X login page for manual login...")

            await page.goto(
                "https://x.com/i/flow/login",
                wait_until="domcontentloaded",
                timeout=30000,
            )

            # Try to bring the window to front
            try:
                await page.bring_to_front()
            except Exception:
                pass

            # Poll for up to 5 minutes for the user to complete login
            for i in range(150):
                await asyncio.sleep(2)
                try:
                    current_url = page.url
                    if (
                        "login" not in current_url.lower()
                        and "flow" not in current_url.lower()
                    ):
                        self._logged_in = True
                        self._login_failed = False
                        self._login_attempts = 0
                        await self._save_cookies()
                        print("\n[SUCCESS] Login detected! Cookies saved.\n")
                        logger.info(
                            "[twitter] Manual login successful! Cookies saved."
                        )
                        return True
                except Exception:
                    pass

                # Progress updates
                if i == 30:
                    print("[twitter] Still waiting for login... (4 min left)")
                elif i == 60:
                    print("[twitter] Still waiting for login... (3 min left)")
                elif i == 90:
                    print("[twitter] Still waiting for login... (2 min left)")
                elif i == 120:
                    print("[twitter] Still waiting for login... (1 min left)")

            logger.error("[twitter] Manual login timed out after 5 minutes")
            print("\n[TIMEOUT] Login not completed. Try running: python x_login.py\n")
            return False

        except Exception as e:
            logger.error("[twitter] Manual login error: %s", e)
            return False
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _ensure_logged_in(self) -> bool:
        """Ensure we have an authenticated session.

        Priority:
            1. Already logged in (flag set) → return True
            2. Saved cookies on disk → test them
            3. Automated login (fill username + password)
            4. Manual login (user completes in browser)
        """
        if self._logged_in:
            return True

        if not self._has_credentials:
            return False

        if self._login_failed:
            logger.debug("[twitter] Login previously failed, skipping search this cycle")
            return False

        # Try saved cookies
        if COOKIE_FILE.exists():
            page = await self._context.new_page()
            try:
                await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(3)
                if "login" not in page.url.lower() and "flow" not in page.url.lower():
                    self._logged_in = True
                    logger.info("[twitter] Session cookies are valid")
                    return True
                else:
                    logger.info("[twitter] Saved cookies expired, re-authenticating...")
            except Exception:
                pass
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

        # Try automated login first
        result = await self._login()
        if result:
            return True

        # Automated login failed — fall back to manual
        logger.info("[twitter] Automated login failed, switching to manual login...")
        return await self._manual_login()

    async def _scrape_search(self, query: str) -> list[dict]:
        """Run a search query and intercept SearchTimeline GraphQL results."""
        if self._context is None:
            return []

        api_data: list[dict] = []

        async def on_response(response) -> None:
            url = response.url
            if response.status == 200 and "SearchTimeline" in url:
                try:
                    data = await response.json()
                    api_data.append(data)
                except Exception:
                    pass

        page = await self._context.new_page()
        page.on("response", on_response)

        try:
            # URL-encode the query
            from urllib.parse import quote
            encoded_q = quote(query)
            search_url = f"https://x.com/search?q={encoded_q}&src=typed_query&f=live"

            await page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
            await asyncio.sleep(4)

            # Check for login redirect
            if "login" in page.url.lower() or "flow" in page.url.lower():
                logger.debug("[twitter] Search redirected to login")
                self._logged_in = False
                return []

            tweets: list[dict] = []
            for data in api_data:
                tweets.extend(_extract_tweets_from_search(data))

            if tweets:
                logger.debug(
                    "[twitter] Search '%s' returned %d tweets",
                    query[:50], len(tweets),
                )

            return tweets

        except Exception as e:
            logger.debug("[twitter] Error in search '%s': %s", query[:40], e)
            return []
        finally:
            try:
                await page.close()
            except Exception:
                pass

    def _load_account_cache(self) -> dict:
        """Load cached account validation results."""
        if ACCOUNT_CACHE_FILE.exists():
            try:
                return json.loads(ACCOUNT_CACHE_FILE.read_text())
            except Exception:
                pass
        return {}

    def _save_account_cache(self, cache: dict) -> None:
        """Save account validation results to disk."""
        try:
            ACCOUNT_CACHE_FILE.write_text(json.dumps(cache, indent=2))
        except Exception as e:
            logger.debug("[twitter] Failed to save account cache: %s", e)

    async def _check_account_status(self, account: str) -> dict:
        """Check if an account exists and when it last posted.

        Returns dict with:
            - exists: bool
            - last_tweet_date: datetime or None
            - is_stale: bool (no posts in 3+ months)
            - error: str or None
        """
        if self._context is None:
            return {"exists": False, "error": "No browser context"}

        result = {
            "account": account,
            "exists": True,
            "last_tweet_date": None,
            "is_stale": False,
            "error": None,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

        api_data: list[dict] = []

        async def on_response(response) -> None:
            if response.status == 200 and "UserTweets" in response.url:
                try:
                    data = await response.json()
                    api_data.append(data)
                except Exception:
                    pass

        page = await self._context.new_page()
        page.on("response", on_response)

        try:
            await page.goto(
                f"https://x.com/{account}",
                wait_until="domcontentloaded",
                timeout=25000,
            )
            await asyncio.sleep(3)

            # Check for non-existent account
            page_content = await page.content()
            if "This account doesn't exist" in page_content or "Account suspended" in page_content:
                result["exists"] = False
                result["error"] = "Account does not exist or is suspended"
                return result

            if "login" in page.url.lower() or "flow" in page.url.lower():
                result["error"] = "Redirected to login"
                return result

            # Parse tweets to find the most recent date
            tweets: list[dict] = []
            for data in api_data:
                tweets.extend(_extract_tweets_from_graphql(data))

            if not tweets:
                # No tweets found - could be private, empty, or parsing issue
                result["is_stale"] = True
                result["error"] = "No tweets found"
                return result

            # Find the most recent tweet date
            latest_date = None
            for tweet in tweets:
                ts = _parse_twitter_date(tweet.get("created_at", ""))
                if ts and (latest_date is None or ts > latest_date):
                    latest_date = ts

            if latest_date:
                result["last_tweet_date"] = latest_date.isoformat()
                # Check if stale (no posts in 3+ months)
                days_since = (datetime.now(timezone.utc) - latest_date).days
                result["is_stale"] = days_since > ACCOUNT_STALE_DAYS
                result["days_since_last_post"] = days_since

            return result

        except Exception as e:
            result["error"] = str(e)
            return result
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def _validate_accounts(self) -> list[str]:
        """Validate all monitored accounts and return list of active ones.

        Filters out:
        - Accounts that don't exist
        - Accounts that haven't posted in 3+ months
        """
        cache = self._load_account_cache()
        now = datetime.now(timezone.utc)

        # Check if cache is fresh (less than 7 days old)
        cache_age_days = 999
        if "validated_at" in cache:
            try:
                validated_at = datetime.fromisoformat(cache["validated_at"])
                cache_age_days = (now - validated_at).days
            except Exception:
                pass

        # Use cache if fresh
        if cache_age_days < 7 and "active_accounts" in cache:
            active = cache["active_accounts"]
            stale = cache.get("stale_accounts", [])
            missing = cache.get("missing_accounts", [])
            logger.info(
                "[twitter] Using cached account validation (%d active, %d stale, %d missing)",
                len(active), len(stale), len(missing)
            )
            return active

        # Need to re-validate
        logger.info("[twitter] Validating %d monitored accounts...", len(self._all_monitored))

        active_accounts = []
        stale_accounts = []
        missing_accounts = []
        account_details = {}

        for i, account in enumerate(self._all_monitored):
            logger.debug("[twitter] Checking @%s (%d/%d)...", account, i + 1, len(self._all_monitored))

            status = await self._check_account_status(account)
            account_details[account] = status

            if not status["exists"]:
                missing_accounts.append(account)
                logger.warning("[twitter] @%s does not exist or is suspended", account)
            elif status["is_stale"]:
                stale_accounts.append(account)
                days = status.get("days_since_last_post", "?")
                logger.warning("[twitter] @%s is stale (last post %s days ago)", account, days)
            else:
                active_accounts.append(account)
                days = status.get("days_since_last_post", "?")
                logger.debug("[twitter] @%s is active (last post %s days ago)", account, days)

            # Small delay between checks to avoid rate limiting
            await asyncio.sleep(1.5)

        # Save results to cache
        cache = {
            "validated_at": now.isoformat(),
            "active_accounts": active_accounts,
            "stale_accounts": stale_accounts,
            "missing_accounts": missing_accounts,
            "account_details": account_details,
        }
        self._save_account_cache(cache)

        # Log summary
        logger.info(
            "[twitter] Account validation complete: %d active, %d stale, %d missing",
            len(active_accounts), len(stale_accounts), len(missing_accounts)
        )
        if stale_accounts:
            logger.info("[twitter] Stale accounts (skipped): %s", ", ".join(f"@{a}" for a in stale_accounts))
        if missing_accounts:
            logger.info("[twitter] Missing accounts (skipped): %s", ", ".join(f"@{a}" for a in missing_accounts))

        return active_accounts

    async def _scrape_account(self, account: str) -> list[dict]:
        """Load an account's profile and intercept GraphQL tweet data."""
        if self._context is None:
            return []

        api_data: list[dict] = []

        async def on_response(response) -> None:
            if response.status == 200 and "UserTweets" in response.url:
                try:
                    data = await response.json()
                    api_data.append(data)
                except Exception:
                    pass

        page = await self._context.new_page()
        page.on("response", on_response)

        try:
            await page.goto(
                f"https://x.com/{account}",
                wait_until="domcontentloaded",
                timeout=25000,
            )
            await asyncio.sleep(4)

            if "login" in page.url.lower() or "flow" in page.url.lower():
                logger.debug("[twitter] %s redirected to login, skipping", account)
                self._logged_in = False
                return []

            tweets: list[dict] = []
            for data in api_data:
                tweets.extend(_extract_tweets_from_graphql(data))

            return tweets

        except Exception as e:
            logger.debug("[twitter] Error scraping @%s: %s", account, e)
            return []
        finally:
            try:
                await page.close()
            except Exception:
                pass

    def get_poll_interval(self) -> int:
        return self.config.twitter_poll_interval

    def _process_tweets(
        self,
        tweets: list[dict],
        now: datetime,
        source_context: str,
    ) -> list[RawReport]:
        """Filter and convert raw tweet dicts into RawReport objects."""
        reports = []
        for tweet in tweets:
            tweet_id = tweet["id"]
            if not tweet_id:
                continue

            source_id = f"twitter_{tweet_id}"
            if not self._is_new(source_id):
                continue

            text = tweet["text"]
            screen_name = tweet["screen_name"] or "unknown"

            # For search results, always apply the dual-keyword filter
            # (the search query already targeted ICE+MN, but verify)
            if not self._tweet_is_relevant(text, screen_name):
                continue

            ts = _parse_twitter_date(tweet["created_at"])
            if ts is None:
                ts = now

            source_url = f"https://x.com/{screen_name}/status/{tweet_id}"

            reports.append(
                RawReport(
                    source_type="twitter",
                    source_id=source_id,
                    source_url=source_url,
                    author=f"@{screen_name}",
                    text=text,
                    timestamp=ts,
                    collected_at=now,
                    raw_metadata={
                        "tweet_id": tweet_id,
                        "screen_name": screen_name,
                        "display_name": tweet["display_name"],
                        "retweet_count": tweet["retweet_count"],
                        "favorite_count": tweet["favorite_count"],
                        "is_retweet": tweet["is_retweet"],
                        "scrape_method": "playwright_graphql",
                        "source_context": source_context,
                    },
                )
            )

        return reports

    async def collect(self) -> list[RawReport]:
        if not await self._ensure_browser():
            return []

        now = datetime.now(timezone.utc)
        reports: list[RawReport] = []
        cycle_idx = getattr(self, "_cycle_count", 0)
        self._cycle_count = cycle_idx + 1

        # Reset per-cycle login failure flag so we retry next cycle
        self._login_failed = False

        # ── Validate accounts on first run ──────────────────────
        if not self._accounts_validated:
            self._active_accounts = await self._validate_accounts()
            self._accounts_validated = True
            if not self._active_accounts:
                logger.warning("[twitter] No active accounts found, using all accounts")
                self._active_accounts = list(self._all_monitored)

        # ── Phase 1: Authenticated search (primary) ──────────────
        if self._has_credentials:
            logged_in = await self._ensure_logged_in()
            if logged_in:
                # Pick search queries for this cycle
                n_queries = self._search_queries_per_cycle
                start_q = (cycle_idx * n_queries) % len(self._search_queries)
                queries_this_cycle = []
                for i in range(n_queries):
                    idx = (start_q + i) % len(self._search_queries)
                    queries_this_cycle.append(self._search_queries[idx])

                logger.info(
                    "[twitter] Cycle %d: running %d search queries",
                    cycle_idx, len(queries_this_cycle),
                )

                for query in queries_this_cycle:
                    try:
                        tweets = await self._scrape_search(query)
                        search_reports = self._process_tweets(
                            tweets, now, f"search:{query[:40]}"
                        )
                        reports.extend(search_reports)
                        await asyncio.sleep(2)
                    except Exception:
                        logger.warning("[twitter] Search failed for query: %s", query[:40])
            else:
                logger.debug("[twitter] Not logged in, skipping search")

        # ── Phase 2: Profile scraping (supplementary) ────────────
        # Use only validated active accounts
        accounts_to_scrape = self._active_accounts if self._active_accounts else list(self._all_monitored)

        start = (cycle_idx * self._accounts_per_cycle) % len(accounts_to_scrape)
        accounts_this_cycle = []
        for i in range(self._accounts_per_cycle):
            idx = (start + i) % len(accounts_to_scrape)
            accounts_this_cycle.append(accounts_to_scrape[idx])

        logger.info(
            "[twitter] Cycle %d: scraping profiles %s",
            cycle_idx,
            [f"@{a}" for a in accounts_this_cycle],
        )

        for account in accounts_this_cycle:
            try:
                tweets = await self._scrape_account(account)
                profile_reports = self._process_tweets(
                    tweets, now, f"profile:@{account}"
                )
                reports.extend(profile_reports)
                await asyncio.sleep(2)
            except Exception:
                logger.warning("[twitter] Failed to scrape @%s", account)

        if reports:
            logger.info(
                "[twitter] Cycle %d: found %d relevant tweets",
                cycle_idx, len(reports),
            )

        return reports

    async def _close_browser(self) -> None:
        """Close all Playwright resources."""
        # Save cookies before closing
        if self._logged_in:
            await self._save_cookies()

        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass

        self._context = None
        self._browser = None
        self._playwright = None

    def stop(self) -> None:
        super().stop()

    async def cleanup(self) -> None:
        await self._close_browser()
