"""City tagger — determines which city a report belongs to.

Given a report's text and optional coordinates, matches it to the best-fit
city from the loaded locales.  Coordinates are checked first (most precise),
then keyword match count is used as a fallback.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from processing.locale import Locale

from processing.location_extractor import haversine_km

logger = logging.getLogger(__name__)


class CityTagger:
    """Determines which city a report belongs to."""

    def __init__(self, city_locales: dict[str, Locale]):
        self._city_keywords: dict[str, set[str]] = {}
        self._city_centers: dict[str, list[tuple[float, float, float]]] = {}

        for name, locale in city_locales.items():
            self._city_keywords[name] = {str(kw).lower() for kw in locale.geo_keywords}
            self._city_centers[name] = list(locale.centers)

        logger.info(
            "CityTagger initialized with %d cities: %s",
            len(city_locales),
            ", ".join(sorted(city_locales)),
        )

    def tag(
        self,
        text: str,
        lat: float | None = None,
        lon: float | None = None,
    ) -> str:
        """Return the city name this report belongs to, or '' if no match."""
        # Priority 1: coordinate match (most precise)
        if lat is not None and lon is not None:
            for name, centers in self._city_centers.items():
                for c_lat, c_lon, c_radius in centers:
                    if haversine_km(lat, lon, c_lat, c_lon) <= c_radius:
                        return name

        # Priority 2: keyword match count
        text_lower = text.lower()
        best_city = ""
        best_count = 0
        for name, keywords in self._city_keywords.items():
            count = sum(1 for kw in keywords if kw in text_lower)
            if count > best_count:
                best_count = count
                best_city = name

        return best_city
