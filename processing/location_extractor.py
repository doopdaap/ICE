from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass

import spacy
from spacy.matcher import PhraseMatcher

logger = logging.getLogger(__name__)

GEODATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "geodata")


@dataclass
class ExtractedLocation:
    raw_text: str
    neighborhood: str | None
    latitude: float | None
    longitude: float | None
    confidence: float


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute distance in km between two lat/lon points."""
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


class LocationExtractor:
    def __init__(
        self,
        neighborhoods_file: str | None = None,
        landmarks_file: str | None = None,
    ):
        self.nlp = spacy.load("en_core_web_sm")
        self._gazetteer: list[dict] = []
        self._landmarks: list[dict] = []
        self._name_to_entry: dict[str, dict] = {}
        self._matcher = PhraseMatcher(self.nlp.vocab, attr="LOWER")
        self._neighborhoods_file = neighborhoods_file or os.path.join(
            GEODATA_DIR, "minneapolis_neighborhoods.json"
        )
        self._landmarks_file = landmarks_file or os.path.join(
            GEODATA_DIR, "landmarks.json"
        )
        self._load_data()

    def _load_data(self) -> None:
        neighborhoods_path = self._neighborhoods_file
        landmarks_path = self._landmarks_file

        with open(neighborhoods_path, "r") as f:
            self._gazetteer = json.load(f)

        if os.path.exists(landmarks_path):
            with open(landmarks_path, "r") as f:
                self._landmarks = json.load(f)

        # Build lookup and phrase matcher
        patterns = []
        for entry in self._gazetteer:
            name = entry["name"]
            self._name_to_entry[name.lower()] = entry
            patterns.append(self.nlp.make_doc(name))
            for alias in entry.get("aliases", []):
                self._name_to_entry[alias.lower()] = entry
                patterns.append(self.nlp.make_doc(alias))

        for entry in self._landmarks:
            name = entry["name"]
            self._name_to_entry[name.lower()] = entry
            patterns.append(self.nlp.make_doc(name))

        if patterns:
            self._matcher.add("LOCALE_LOCATIONS", patterns)

        logger.info(
            "Loaded %d neighborhoods, %d landmarks",
            len(self._gazetteer),
            len(self._landmarks),
        )

    def extract(self, text: str) -> list[ExtractedLocation]:
        """Extract locations from text using NER + gazetteer matching."""
        doc = self.nlp(text)
        locations: list[ExtractedLocation] = []
        seen: set[str] = set()

        try:
            # 1. PhraseMatcher against known Minneapolis locations
            matches = self._matcher(doc)
            for match_id, start, end in matches:
                span_text = doc[start:end].text
                key = span_text.lower()
                if key in seen:
                    continue
                seen.add(key)

                entry = self._name_to_entry.get(key)
                if entry:
                    centroid = entry.get("centroid", {})
                    locations.append(ExtractedLocation(
                        raw_text=span_text,
                        neighborhood=entry.get("name"),
                        latitude=centroid.get("lat"),
                        longitude=centroid.get("lon"),
                        confidence=0.9,
                    ))

            # 2. spaCy NER for GPE/LOC/FAC entities not already matched
            for ent in doc.ents:
                if ent.label_ not in ("GPE", "LOC", "FAC"):
                    continue
                key = ent.text.lower()
                if key in seen:
                    continue
                seen.add(key)

                entry = self._name_to_entry.get(key)
                if entry:
                    centroid = entry.get("centroid", {})
                    locations.append(ExtractedLocation(
                        raw_text=ent.text,
                        neighborhood=entry.get("name"),
                        latitude=centroid.get("lat"),
                        longitude=centroid.get("lon"),
                        confidence=0.7,
                    ))
                else:
                    # Known NER entity but not in gazetteer — lower confidence
                    locations.append(ExtractedLocation(
                        raw_text=ent.text,
                        neighborhood=None,
                        latitude=None,
                        longitude=None,
                        confidence=0.3,
                    ))
        finally:
            # Explicitly free the spaCy doc to prevent memory accumulation
            del doc

        return locations

    def get_primary_location(
        self, locations: list[ExtractedLocation]
    ) -> tuple[str | None, float | None, float | None]:
        """Pick the best location from a list.
        Returns (neighborhood, lat, lon)."""
        if not locations:
            return None, None, None

        # Sort by confidence descending, prefer ones with neighborhood
        ranked = sorted(
            locations,
            key=lambda loc: (loc.neighborhood is not None, loc.confidence),
            reverse=True,
        )
        best = ranked[0]
        return best.neighborhood, best.latitude, best.longitude
