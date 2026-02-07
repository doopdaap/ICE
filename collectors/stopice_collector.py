"""StopICE.net collector - attempts to fetch ICE sighting data from their map.

StopICE.net has a live map but the data API appears to be unreliable
or may require authentication. This collector attempts to:
1. Fetch data from the recentmapdata API endpoint
2. Fall back to scraping the map page with Playwright if available

NOTE: As of 2026-02, the StopICE.net data endpoints appear to be
non-functional or require authentication. This collector may return
empty results until the situation changes.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any

import aiohttp

from collectors.base import BaseCollector
from storage.models import RawReport

logger = logging.getLogger(__name__)

# API endpoint for map data
STOPICE_API_URL = "https://stopice.net/login/"


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


class StopICEDataParser(HTMLParser):
    """Parse StopICE map data response.

    The response (when available) contains elements like:
    <map_data>...</map_data>
    <id>123</id>
    <lat>44.123</lat>
    <long>-93.456</long>
    <location>Minneapolis, MN</location>
    <timestamp>2026-02-05 10:30:00</timestamp>
    <comments>ICE spotted at...</comments>
    """

    def __init__(self):
        super().__init__()
        self.markers = []
        self.current_marker = {}
        self.current_tag = None
        self.current_data = ""
        self.marker_tags = {
            "id", "lat", "long", "location", "timestamp",
            "comments", "priorityimg", "thispriority", "media", "map_data"
        }

    def handle_starttag(self, tag, attrs):
        tag_lower = tag.lower()
        if tag_lower in self.marker_tags:
            self.current_tag = tag_lower
            self.current_data = ""

    def handle_endtag(self, tag):
        tag_lower = tag.lower()
        if tag_lower == self.current_tag:
            if self.current_tag and self.current_data.strip():
                self.current_marker[self.current_tag] = self.current_data.strip()
            if tag_lower == "map_data" and self.current_marker:
                self.markers.append(self.current_marker.copy())
                self.current_marker = {}
            self.current_tag = None
            self.current_data = ""

    def handle_data(self, data):
        if self.current_tag:
            self.current_data += data


class StopICECollector(BaseCollector):
    """Collects ICE reports from StopICE.net.

    Note: The StopICE.net API has been unreliable. This collector
    attempts to fetch data but may return empty results.
    """

    name = "stopice"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._session: aiohttp.ClientSession | None = None
        self._consecutive_failures = 0
        self._last_warning_time: datetime | None = None
        # Locale-aware geo filter — supports multiple centers for multi-locale
        locale = self.config.locale
        self._centers = locale.centers
        self._location_keywords = {kw.lower() for kw in locale.geo_city_names}

    def _is_locale_area_coords(self, lat: float, lon: float) -> bool:
        """Check if coordinates are within any configured locale radius."""
        try:
            for c_lat, c_lon, c_radius in self._centers:
                dist = _haversine_km(lat, lon, c_lat, c_lon)
                if dist <= c_radius:
                    return True
            return False
        except (ValueError, TypeError):
            return False

    def _is_locale_area_text(self, text: str) -> bool:
        """Check if text contains locale area references."""
        text_lower = text.lower()
        return any(kw in text_lower for kw in self._location_keywords)

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def get_poll_interval(self) -> int:
        # Poll every 30 minutes
        # If experiencing failures, back off
        base_interval = getattr(self.config, "stopice_poll_interval", 1800)
        if self._consecutive_failures > 5:
            return base_interval * 2  # Double the interval after repeated failures
        return base_interval

    async def collect(self) -> list[RawReport]:
        """Attempt to fetch ICE reports from StopICE.net."""
        logger.info("[stopice] Starting collection cycle")
        now = datetime.now(timezone.utc)
        reports: list[RawReport] = []

        session = await self._ensure_session()

        # Try the recentmapdata endpoint with different durations
        durations = ["since_yesterday", "today"]
        got_response = False

        for duration in durations:
            try:
                params = {"recentmapdata": "1", "duration": duration}
                async with session.get(
                    STOPICE_API_URL,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15),  # Short timeout
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": "https://stopice.net/login/?maps=1",
                    }
                ) as resp:
                    if resp.status != 200:
                        logger.debug("[stopice] API returned %d for duration=%s", resp.status, duration)
                        continue

                    content = await resp.text()

                    # Check if we got actual data (not just whitespace)
                    if not content or len(content.strip()) < 50:
                        logger.debug("[stopice] Empty/minimal response for duration=%s", duration)
                        continue

                    # Check for marker tags
                    if "<map_data>" not in content.lower() and "<lat>" not in content.lower():
                        logger.debug("[stopice] No marker tags in response for duration=%s", duration)
                        continue

                    got_response = True

                    # Parse the response
                    parser = StopICEDataParser()
                    try:
                        parser.feed(content)
                    except Exception as e:
                        logger.debug("[stopice] Parse error: %s", e)
                        continue

                    if parser.markers:
                        logger.info("[stopice] Got %d markers from duration=%s", len(parser.markers), duration)
                        reports.extend(self._process_markers(parser.markers, now))
                        break  # Got data, don't need to try other durations

            except asyncio.TimeoutError:
                logger.debug("[stopice] Timeout for duration=%s", duration)
            except Exception as e:
                logger.debug("[stopice] Error fetching duration=%s: %s", duration, e)

        if not got_response:
            self._consecutive_failures += 1

            # Only log warning occasionally to avoid spam
            should_warn = (
                self._last_warning_time is None or
                (now - self._last_warning_time).total_seconds() > 3600  # Once per hour
            )
            if should_warn and self._consecutive_failures >= 3:
                logger.warning(
                    "[stopice] StopICE.net API not returning data (attempt %d). "
                    "The service may be down or require authentication.",
                    self._consecutive_failures
                )
                self._last_warning_time = now
        else:
            self._consecutive_failures = 0

        if reports:
            logger.info("[stopice] Found %d locale-area reports", len(reports))

        return reports

    def _process_markers(self, markers: list[dict], now: datetime) -> list[RawReport]:
        """Process parsed markers into RawReport objects."""
        reports = []
        freshness_cutoff = now - timedelta(hours=3)

        for marker in markers:
            try:
                # Parse coordinates
                lat = None
                lon = None
                try:
                    lat = float(marker.get("lat", ""))
                    lon = float(marker.get("long", ""))
                except (ValueError, TypeError):
                    pass

                location = marker.get("location", "")
                comments = marker.get("comments", "")

                # Check if in locale area
                in_area = False
                if lat is not None and lon is not None:
                    in_area = self._is_locale_area_coords(lat, lon)
                if not in_area:
                    in_area = self._is_locale_area_text(f"{location} {comments}")

                if not in_area:
                    continue

                # Parse timestamp
                timestamp = now
                ts_str = marker.get("timestamp", "")
                if ts_str:
                    for fmt in [
                        "%Y-%m-%d %H:%M:%S",
                        "%Y-%m-%d %H:%M",
                        "%m/%d/%Y %H:%M:%S",
                        "%m/%d/%Y %I:%M %p",
                    ]:
                        try:
                            timestamp = datetime.strptime(ts_str, fmt)
                            timestamp = timestamp.replace(tzinfo=timezone.utc)
                            # Adjust from Central to UTC (+6 hours)
                            timestamp = timestamp + timedelta(hours=6)
                            break
                        except ValueError:
                            continue

                # Check freshness
                if timestamp < freshness_cutoff:
                    continue

                # Build source ID
                marker_id = marker.get("id", "")
                if marker_id:
                    source_id = f"stopice_{marker_id}"
                else:
                    source_id = f"stopice_{lat}_{lon}_{timestamp.isoformat()[:10]}"

                if not self._is_new(source_id):
                    continue

                # Build report text
                priority = marker.get("thispriority", "")
                priority_label = f"[{priority.upper()}] " if priority else ""

                report_text = f"[StopICE.net Alert] {priority_label}{location}"
                if comments:
                    report_text += f"\n{comments}"
                if lat and lon:
                    report_text += f"\nCoordinates: {lat}, {lon}"

                reports.append(RawReport(
                    source_type="stopice",
                    source_id=source_id,
                    source_url="https://stopice.net/login/?maps=1",
                    author="stopice.net",
                    text=report_text,
                    timestamp=timestamp,
                    collected_at=now,
                    raw_metadata={
                        "stopice_id": marker_id,
                        "latitude": lat,
                        "longitude": lon,
                        "location": location,
                        "comments": comments,
                        "priority": priority,
                        "priority_img": marker.get("priorityimg"),
                        "media": marker.get("media"),
                        "raw_timestamp": ts_str,
                    },
                ))

            except Exception as e:
                logger.debug("[stopice] Error processing marker: %s", e)
                continue

        return reports

    async def cleanup(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
