from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawReport:
    """Standardized output from any collector."""
    source_type: str          # "twitter", "reddit", "rss"
    source_id: str            # unique ID from origin platform
    source_url: str
    author: str
    text: str
    timestamp: datetime       # when originally posted
    collected_at: datetime    # when we fetched it
    raw_metadata: dict = field(default_factory=dict)


@dataclass
class ProcessedReport:
    """A report after NLP processing and keyword filtering."""
    id: int | None
    source_type: str
    source_id: str
    source_url: str
    author: str
    original_text: str
    cleaned_text: str
    timestamp: datetime
    collected_at: datetime
    locations: list[dict] = field(default_factory=list)
    primary_neighborhood: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    keywords_matched: list[str] = field(default_factory=list)
    is_relevant: bool = False
    cluster_id: int | None = None
    city: str = ""


@dataclass
class CorroboratedIncident:
    """A cluster of reports that corroborate each other."""
    cluster_id: int
    reports: list[ProcessedReport]
    primary_location: str
    latitude: float | None
    longitude: float | None
    confidence_score: float
    source_count: int
    unique_source_types: set[str]
    earliest_report: datetime
    latest_report: datetime
    notified: bool = False
    # "new" = first notification for this incident
    # "update" = additional source confirmed existing incident
    notification_type: str = "new"
    new_reports: list[ProcessedReport] = field(default_factory=list)
    city: str = ""
