"""StopICE.net collector - nationwide ICE alert network with XML data feeds.

StopICE.net is an SMS/web-based ICE alert system with 500k+ subscribers.
They publish daily XML data exports of all reports.

Data includes:
- Location (city, state, coordinates)
- Timestamp
- Alert type (Confirmed, Sighting, Unconfirmed, etc.)
- Description

We filter for Minneapolis/Minnesota area reports.
"""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from collectors.base import BaseCollector
from storage.models import RawReport

logger = logging.getLogger(__name__)

# XML data feed URL - updated nightly
STOPICE_DATA_URL = "https://stopice.net/data/all-reports.xml"

# Alternative: monthly file for current month
# STOPICE_DATA_URL = "https://stopice.net/data/2026-02-reports.xml"

# Minneapolis metro area filter
MPLS_CENTER_LAT = 44.9778
MPLS_CENTER_LON = -93.2650
MAX_DISTANCE_KM = 50.0  # ~31 miles

# Text-based location filter
MN_LOCATION_KEYWORDS = {
    "minneapolis", "mpls", "st paul", "saint paul", "minnesota", "mn",
    "eden prairie", "bloomington", "brooklyn park", "brooklyn center",
    "richfield", "golden valley", "st louis park", "crystal",
    "plymouth", "maple grove", "eagan", "burnsville",
    "shakopee", "lakeville", "roseville", "woodbury",
    "hennepin", "ramsey county", "dakota county",
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


def _is_mpls_area(report: dict) -> bool:
    """Check if a StopICE report is in the Minneapolis metro area."""
    lat = report.get("latitude")
    lon = report.get("longitude")

    if lat is not None and lon is not None:
        try:
            dist = _haversine_km(float(lat), float(lon), MPLS_CENTER_LAT, MPLS_CENTER_LON)
            return dist <= MAX_DISTANCE_KM
        except (ValueError, TypeError):
            pass

    # Fallback: text-based location check
    location = (report.get("location") or "").lower()
    city = (report.get("city") or "").lower()
    state = (report.get("state") or "").lower()

    combined = f"{location} {city} {state}"
    return any(kw in combined for kw in MN_LOCATION_KEYWORDS)


class StopICECollector(BaseCollector):
    """Collects ICE reports from StopICE.net XML data feed."""

    name = "stopice"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def get_poll_interval(self) -> int:
        # Poll every 30 minutes - data updates nightly but we want fresh reports
        return getattr(self.config, "stopice_poll_interval", 1800)

    async def collect(self) -> list[RawReport]:
        session = await self._ensure_session()
        now = datetime.now(timezone.utc)
        reports: list[RawReport] = []

        try:
            async with session.get(STOPICE_DATA_URL, timeout=60) as resp:
                if resp.status != 200:
                    logger.warning("[stopice] Failed to fetch data: HTTP %d", resp.status)
                    return []

                xml_content = await resp.text()

        except asyncio.TimeoutError:
            logger.warning("[stopice] Request timeout")
            return []
        except Exception as e:
            logger.warning("[stopice] Request error: %s", e)
            return []

        # Parse XML
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as e:
            logger.warning("[stopice] XML parse error: %s", e)
            return []

        # Find all report elements (adjust based on actual XML structure)
        # Common structures: <reports><report>...</report></reports>
        # or <alerts><alert>...</alert></alerts>
        report_elements = root.findall(".//report") or root.findall(".//alert") or root.findall(".//item")

        if not report_elements:
            # Try direct children
            report_elements = list(root)

        freshness_cutoff = now - timedelta(hours=3)

        for elem in report_elements:
            try:
                report_data = self._parse_report_element(elem)
                if not report_data:
                    continue

                # Check if in Minneapolis area
                if not _is_mpls_area(report_data):
                    continue

                # Check freshness
                timestamp = report_data.get("timestamp")
                if timestamp and timestamp < freshness_cutoff:
                    continue

                # Build source ID
                report_id = report_data.get("id") or report_data.get("timestamp", "").isoformat()
                source_id = f"stopice_{report_id}"

                if not self._is_new(source_id):
                    continue

                # Build text
                alert_type = report_data.get("type", "Report")
                location = report_data.get("location") or report_data.get("city", "Unknown location")
                description = report_data.get("description", "")

                text = f"[StopICE.net {alert_type}] {location}"
                if description:
                    text += f"\n{description}"

                reports.append(RawReport(
                    source_type="stopice",
                    source_id=source_id,
                    source_url="https://stopice.net",
                    author="stopice.net",
                    text=text,
                    timestamp=timestamp or now,
                    collected_at=now,
                    raw_metadata={
                        "stopice_id": report_data.get("id"),
                        "alert_type": alert_type,
                        "location": location,
                        "city": report_data.get("city"),
                        "state": report_data.get("state"),
                        "latitude": report_data.get("latitude"),
                        "longitude": report_data.get("longitude"),
                        "status": report_data.get("status"),
                    },
                ))

            except Exception as e:
                logger.debug("[stopice] Error parsing report: %s", e)
                continue

        if reports:
            logger.info("[stopice] Found %d Minneapolis-area reports", len(reports))

        return reports

    def _parse_report_element(self, elem: ET.Element) -> dict | None:
        """Parse an XML report element into a dict."""
        data = {}

        # Try common field names
        field_mappings = {
            "id": ["id", "report_id", "alert_id"],
            "type": ["type", "alert_type", "category"],
            "location": ["location", "address", "place"],
            "city": ["city"],
            "state": ["state"],
            "latitude": ["latitude", "lat"],
            "longitude": ["longitude", "lon", "lng"],
            "description": ["description", "details", "text", "content"],
            "timestamp": ["timestamp", "date", "datetime", "created", "reported_at"],
            "status": ["status", "verified"],
        }

        for field, possible_names in field_mappings.items():
            for name in possible_names:
                # Try as child element
                child = elem.find(name)
                if child is not None and child.text:
                    data[field] = child.text.strip()
                    break
                # Try as attribute
                if name in elem.attrib:
                    data[field] = elem.attrib[name]
                    break

        # Parse timestamp
        if "timestamp" in data:
            try:
                ts_str = data["timestamp"]
                # Try ISO format
                if "T" in ts_str:
                    data["timestamp"] = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                else:
                    # Try common formats
                    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S"]:
                        try:
                            data["timestamp"] = datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
                            break
                        except ValueError:
                            continue
            except Exception:
                data["timestamp"] = None

        # Parse coordinates
        for coord in ["latitude", "longitude"]:
            if coord in data:
                try:
                    data[coord] = float(data[coord])
                except (ValueError, TypeError):
                    data[coord] = None

        return data if data else None

    async def cleanup(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
