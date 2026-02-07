"""Iceout.org collector — real-time community ICE activity reports.

Iceout.org (People Over Papers) is a community reporting platform where
people submit ICE sightings with addresses, photos, and timestamps.

API details:
    Endpoint: GET https://iceout.org/api/report-feed?&since={ISO_TIMESTAMP}
    Format:   MessagePack (application/msgpack)
    Fields:   id, location (GeoJSON Point), location_description,
              category_enum (0=Critical, 1=Active, 2=Observed, 3=Other),
              incident_time, created_at, status (0=Unconfirmed, 1=Confirmed),
              approved, small_thumbnail

Authentication:
    The API uses an Altcha proof-of-work CAPTCHA that requires browser-side
    JavaScript to solve. We use Playwright (headless Chromium) to load the
    site, let the JS handle auth automatically, then intercept the API
    response when the page fetches report data.

We filter to Minneapolis/MN area reports client-side by checking
location_description and coordinates.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import msgpack

from collectors.base import BaseCollector
from storage.models import RawReport

logger = logging.getLogger(__name__)

ICEOUT_SITE_URL = "https://iceout.org/en/"
ICEOUT_API_URL = "https://iceout.org/api/report-feed"

# Category enum to human-readable labels
CATEGORY_LABELS = {
    0: "Critical",
    1: "Active",
    2: "Observed",
    3: "Other",
}

# Status enum
STATUS_LABELS = {
    0: "Not Confirmed",
    1: "Confirmed",
}




def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance in km between two lat/lon points."""
    import math
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))





def _extract_coords(report: dict) -> tuple[float | None, float | None]:
    """Extract (latitude, longitude) from GeoJSON location."""
    loc_str = report.get("location")
    if not loc_str:
        return None, None
    try:
        if isinstance(loc_str, str):
            loc = json.loads(loc_str)
        else:
            loc = loc_str
        coords = loc.get("coordinates", [])
        if len(coords) >= 2:
            return coords[1], coords[0]  # GeoJSON is [lon, lat]
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return None, None


class IceoutCollector(BaseCollector):
    """Collects ICE activity reports from Iceout.org via headless browser.

    The Iceout.org API requires browser-based Altcha proof-of-work
    authentication that can't be replicated with plain HTTP requests.
    We use Playwright to run a headless Chromium instance that:

    1. Navigates to the site (JS handles auth automatically)
    2. Intercepts the report-feed API response (MessagePack)
    3. Parses and filters reports to Minneapolis metro area

    The browser context persists between polling cycles to maintain the
    authenticated session.
    """

    name = "iceout"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._authenticated = False
        self._intercepted_data: list[bytes] = []
        self._polls_since_full_auth = 0  # Track polls since last full navigation
        self._polls_since_browser_restart = 0  # Track for memory management
        # Locale-aware geo filter
        locale = self.config.locale
        self._center_lat = locale.center_lat
        self._center_lon = locale.center_lon
        self._radius_km = locale.radius_km
        self._location_keywords = {kw.lower() for kw in locale.geo_city_names}

    def _is_locale_area(self, report: dict) -> bool:
        """Check if an Iceout report is within the configured locale radius."""
        loc_str = report.get("location")
        if loc_str:
            try:
                if isinstance(loc_str, str):
                    loc = json.loads(loc_str)
                else:
                    loc = loc_str
                coords = loc.get("coordinates", [])
                if len(coords) >= 2:
                    lon, lat = coords[0], coords[1]
                    dist = _haversine_km(lat, lon, self._center_lat, self._center_lon)
                    if dist <= self._radius_km:
                        return True
                    else:
                        desc = report.get("location_description", "unknown")
                        logger.debug(
                            "[iceout] Rejecting report %.1f km from locale center: %s",
                            dist, desc[:50]
                        )
                        return False
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # Fallback: check location description text
        desc = (report.get("location_description") or "").lower()
        return any(kw in desc for kw in self._location_keywords)

    def _kill_orphan_browsers(self) -> None:
        """Kill any orphaned Chromium processes to prevent memory leaks."""
        try:
            import subprocess
            result = subprocess.run(
                ["pkill", "-f", "chromium"],
                capture_output=True,
                timeout=5
            )
            if result.returncode == 0:
                logger.info("[iceout] Killed orphan browser processes")
        except Exception as e:
            logger.debug("[iceout] Could not clean orphan browsers: %s", e)

    async def _ensure_browser(self) -> bool:
        """Launch Playwright browser if not already running."""
        logger.info("[iceout] Ensuring browser is available...")

        if self._page is not None:
            try:
                # Check if page is still alive
                await self._page.title()
                logger.info("[iceout] Existing browser session is alive")
                return True
            except Exception:
                # Page died, reset everything
                logger.info("[iceout] Existing browser session died, resetting")
                await self._close_browser()

        # Kill any orphaned browser processes before launching new one
        self._kill_orphan_browsers()

        try:
            logger.info("[iceout] Launching new Playwright browser...")
            from playwright.async_api import async_playwright

            logger.info("[iceout] Starting Playwright...")
            self._playwright = await asyncio.wait_for(
                async_playwright().start(),
                timeout=30.0
            )
            logger.info("[iceout] Playwright started, launching Chromium...")
            self._browser = await asyncio.wait_for(
                self._playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",  # Helps on low-memory servers
                    ],
                ),
                timeout=30.0
            )
            logger.info("[iceout] Chromium launched, creating context...")
            self._context = await self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 720},
            )
            self._page = await self._context.new_page()

            # Set up response interception for the report-feed API
            self._page.on("response", self._on_response)

            logger.info("[iceout] Headless browser launched successfully")
            return True

        except asyncio.TimeoutError:
            logger.error("[iceout] Browser launch timed out after 30s")
            await self._close_browser()
            return False
        except Exception as e:
            logger.error("[iceout] Failed to launch browser: %s", e)
            await self._close_browser()
            return False

    async def _on_response(self, response) -> None:
        """Intercept API responses containing report data."""
        if "report-feed" in response.url and response.status == 200:
            try:
                body = await response.body()
                # Only keep the most recent response to prevent memory leak
                self._intercepted_data = [body]
                logger.debug(
                    "[iceout] Intercepted report-feed response (%d bytes)",
                    len(body),
                )
            except Exception as e:
                logger.debug("[iceout] Failed to read response body: %s", e)

    async def _navigate_and_fetch(self) -> bytes | None:
        """Navigate to iceout.org and capture the API response.

        The site's JavaScript automatically:
        1. Performs Altcha proof-of-work auth
        2. Fetches the report feed
        3. Renders the map

        We intercept step 2's response.
        """
        if self._page is None:
            return None

        self._intercepted_data.clear()

        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=3)
        since_str = since.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        # Force full re-authentication every 5 polls to avoid stale sessions
        self._polls_since_full_auth += 1
        if self._polls_since_full_auth >= 5:
            logger.info("[iceout] Forcing full re-authentication (5 polls reached)")
            self._authenticated = False
            self._polls_since_full_auth = 0

        # If already authenticated, fetch the API directly via the page context
        if self._authenticated:
            logger.info("[iceout] Using cached auth, fetching API directly")
            try:
                # Use page.evaluate to make a fetch from the authenticated context
                # Add timeout to prevent indefinite hangs
                js_code = f"""
                async () => {{
                    const resp = await fetch(
                        '{ICEOUT_API_URL}?&since={since_str}',
                        {{ credentials: 'include' }}
                    );
                    if (!resp.ok) return null;
                    const buf = await resp.arrayBuffer();
                    return Array.from(new Uint8Array(buf));
                }}
                """
                result = await asyncio.wait_for(
                    self._page.evaluate(js_code),
                    timeout=30.0
                )
                if result is not None:
                    logger.info("[iceout] Direct API fetch successful (%d bytes)", len(result))
                    return bytes(result)

                # Fetch failed (maybe session expired), fall through to full nav
                logger.warning(
                    "[iceout] In-page fetch returned None (session may have expired), re-navigating..."
                )
                self._authenticated = False
            except asyncio.TimeoutError:
                logger.warning("[iceout] In-page fetch timed out after 30s, re-navigating...")
                self._authenticated = False
            except Exception as e:
                logger.warning("[iceout] In-page fetch error: %s", e)
                self._authenticated = False

        # Full navigation — let the site's JS handle auth
        logger.info("[iceout] Navigating to %s for auth", ICEOUT_SITE_URL)
        try:
            await self._page.goto(
                ICEOUT_SITE_URL,
                wait_until="networkidle",
                timeout=60000,
            )
            logger.info("[iceout] Navigation complete, checking for intercepted data")

            # Wait longer for Altcha proof-of-work + API calls to complete
            # (increased from 3s to handle slower auth cycles)
            logger.info("[iceout] Waiting 10 seconds for auth + API completion...")
            await asyncio.sleep(10)

            # Check if we intercepted the report-feed response
            if self._intercepted_data:
                self._authenticated = True
                logger.info(
                    "[iceout] Intercepted %d API response(s) during navigation, using most recent (%d bytes)",
                    len(self._intercepted_data),
                    len(self._intercepted_data[-1])
                )
                return self._intercepted_data[-1]  # Use most recent

            # The initial page load may fetch all reports without a since param.
            # Try fetching with our specific time filter now that we're authed.
            try:
                js_code = f"""
                async () => {{
                    const resp = await fetch(
                        '{ICEOUT_API_URL}?&since={since_str}',
                        {{ credentials: 'include' }}
                    );
                    if (!resp.ok) return null;
                    const buf = await resp.arrayBuffer();
                    return Array.from(new Uint8Array(buf));
                }}
                """
                result = await asyncio.wait_for(
                    self._page.evaluate(js_code),
                    timeout=30.0
                )
                if result is not None:
                    self._authenticated = True
                    logger.info("[iceout] Post-nav fetch successful (%d bytes)", len(result))
                    return bytes(result)
            except asyncio.TimeoutError:
                logger.warning("[iceout] Post-nav fetch timed out after 30s")
            except Exception as e:
                logger.warning("[iceout] Post-nav fetch error: %s", e)

            # Last resort: check intercepted data from initial page load
            if self._intercepted_data:
                self._authenticated = True
                return self._intercepted_data[-1]

            logger.warning("[iceout] No report data captured after navigation")
            return None

        except Exception as e:
            logger.warning("[iceout] Navigation failed: %s", e)
            return None

    async def _close_browser(self) -> None:
        """Close all Playwright resources."""
        try:
            if self._page:
                await self._page.close()
        except Exception:
            pass
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

        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._authenticated = False

    def get_poll_interval(self) -> int:
        return self.config.iceout_poll_interval

    async def collect(self) -> list[RawReport]:
        logger.info("[iceout] Starting collection cycle")

        # Wrap entire collection in a timeout to prevent indefinite hangs
        try:
            return await asyncio.wait_for(
                self._do_collect(),
                timeout=120.0  # 2 minute max for entire collection cycle
            )
        except asyncio.TimeoutError:
            logger.error("[iceout] Collection cycle timed out after 120s, resetting browser")
            await self._close_browser()
            return []

    async def _do_collect(self) -> list[RawReport]:
        """Internal collection logic with timeout wrapper."""
        # Recycle browser every 20 polls (~40 min at 2-min intervals) to prevent memory growth
        self._polls_since_browser_restart += 1
        if self._polls_since_browser_restart >= 20:
            logger.info("[iceout] Recycling browser to free memory (20 polls reached)")
            await self._close_browser()
            self._polls_since_browser_restart = 0

        if not await self._ensure_browser():
            logger.warning("[iceout] Browser not available, skipping cycle")
            return []

        now = datetime.now(timezone.utc)

        try:
            raw_bytes = await self._navigate_and_fetch()
            if raw_bytes is None:
                logger.warning(
                    "[iceout] No data received from API (auth: %s, polls_since_auth: %d)",
                    self._authenticated,
                    self._polls_since_full_auth
                )
                return []
            logger.info("[iceout] Received %d bytes from API (auth: %s)", len(raw_bytes), self._authenticated)

            data = msgpack.unpackb(raw_bytes, raw=False)

        except Exception as e:
            logger.warning("[iceout] Failed to fetch/parse reports: %s", e)
            # Reset browser on unexpected errors
            await self._close_browser()
            return []

        if not isinstance(data, list):
            # Might be JSON instead of msgpack if server sent a different format
            if isinstance(raw_bytes, bytes):
                try:
                    data = json.loads(raw_bytes)
                except (json.JSONDecodeError, ValueError):
                    pass
            if not isinstance(data, list):
                logger.warning(
                    "[iceout] Unexpected response type: %s", type(data)
                )
                return []

        reports: list[RawReport] = []
        mpls_count = 0
        skipped_seen = 0
        skipped_stale = 0

        for item in data:
            # Filter to locale area
            if not self._is_locale_area(item):
                continue

            mpls_count += 1
            report_id = item.get("id")
            if report_id is None:
                continue

            source_id = f"iceout_{report_id}"
            if not self._is_new(source_id):
                skipped_seen += 1
                continue

            # Parse timestamps
            incident_time_str = item.get("incident_time")
            created_at_str = item.get("created_at")

            if incident_time_str:
                try:
                    incident_time = datetime.fromisoformat(
                        incident_time_str.replace("Z", "+00:00")
                    )
                except ValueError:
                    incident_time = now
            else:
                incident_time = now

            # Enforce 6-hour freshness (trusted source gets longer window)
            age = now - incident_time
            if age > timedelta(hours=6):
                continue

            if created_at_str:
                try:
                    created_at = datetime.fromisoformat(
                        created_at_str.replace("Z", "+00:00")
                    )
                except ValueError:
                    created_at = now
            else:
                created_at = now

            lat, lon = _extract_coords(item)
            category = CATEGORY_LABELS.get(item.get("category_enum"), "Unknown")
            status = STATUS_LABELS.get(item.get("status"), "Unknown")
            location_desc = item.get("location_description", "Unknown location")

            # Build readable text from structured data
            text = (
                f"[Iceout.org {category} Report] {location_desc}\n"
                f"Status: {status}\n"
                f"Incident time: {incident_time_str or 'unknown'}"
            )

            # Build a report-specific URL. Iceout.org is an SPA, so
            # we include the report ID as a fragment for reference.
            report_url = f"https://iceout.org/en/#report-{report_id}"

            reports.append(RawReport(
                source_type="iceout",
                source_id=source_id,
                source_url=report_url,
                author="iceout.org",
                text=text,
                timestamp=incident_time,
                collected_at=now,
                raw_metadata={
                    "iceout_id": report_id,
                    "category": category,
                    "category_enum": item.get("category_enum"),
                    "status": status,
                    "status_enum": item.get("status"),
                    "approved": item.get("approved"),
                    "location_description": location_desc,
                    "latitude": lat,
                    "longitude": lon,
                    "thumbnail": item.get("small_thumbnail"),
                },
            ))

        if reports:
            logger.info(
                "[iceout] Found %d NEW locale-area reports (of %d total, %d in area, %d already seen)",
                len(reports),
                len(data),
                mpls_count,
                skipped_seen,
            )
        else:
            logger.info(
                "[iceout] No new reports (%d total, %d in locale area, %d already seen)",
                len(data),
                mpls_count,
                skipped_seen,
            )

        return reports

    async def cleanup(self) -> None:
        await self._close_browser()
