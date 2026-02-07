from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from config import Config
from processing.location_extractor import haversine_km
from processing.similarity import SimilarityEngine
from storage.database import Database
from storage.models import CorroboratedIncident, ProcessedReport

logger = logging.getLogger(__name__)

# High-priority sources that can trigger single-source alerts
# These are trusted community reporting platforms with real-time data
# Iceout is the PRIMARY source - it has vetted real-time community reports
HIGH_PRIORITY_SOURCES = {"iceout", "stopice"}


class Correlator:
    """Groups recent reports into clusters and checks corroboration thresholds.

    Supports two notification modes:
    - "new": first-time corroborated incident (2+ sources)
    - "update": new reports added to an already-notified incident
    """

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.similarity = SimilarityEngine()

    async def run_cycle(self) -> list[CorroboratedIncident]:
        """Run one correlation cycle.

        Returns newly corroborated incidents AND updates to existing ones.
        Reports are grouped by city and correlated independently per city
        to prevent cross-city clustering.
        """
        window = self.config.correlation_window_seconds
        since = datetime.now(timezone.utc) - timedelta(seconds=window)

        reports = await self.db.get_recent_relevant(since)
        if not reports:
            logger.debug("No recent relevant reports to correlate")
            return []

        logger.info("Correlating %d recent relevant reports", len(reports))

        # Group reports by city for independent correlation
        from collections import defaultdict
        by_city: dict[str, list[ProcessedReport]] = defaultdict(list)
        for r in reports:
            by_city[r.city or ""].append(r)

        all_incidents: list[CorroboratedIncident] = []

        for city, city_reports in by_city.items():
            if not city:
                logger.debug("Skipping %d reports with no city tag", len(city_reports))
                continue

            incidents = await self._correlate_city(city, city_reports)
            all_incidents.extend(incidents)

        return all_incidents

    async def _correlate_city(
        self, city: str, reports: list[ProcessedReport]
    ) -> list[CorroboratedIncident]:
        """Run correlation for a single city's reports."""
        logger.info("Correlating %d reports for city: %s", len(reports), city)

        # Separate reports into already-clustered and unclustered
        unclustered = [r for r in reports if r.cluster_id is None]
        clustered_by_id: dict[int, list[ProcessedReport]] = {}
        for r in reports:
            if r.cluster_id is not None:
                clustered_by_id.setdefault(r.cluster_id, []).append(r)

        incidents: list[CorroboratedIncident] = []

        # ── Phase 1: Check for updates to existing notified clusters ──
        if unclustered:
            update_incidents = await self._check_cluster_updates(
                unclustered, clustered_by_id, reports, city=city
            )
            incidents.extend(update_incidents)

        # Remove reports that were just assigned to clusters
        still_unclustered = [r for r in unclustered if r.cluster_id is None]

        # ── Phase 2: Find new corroborated clusters among remaining ──
        if len(still_unclustered) >= 2:
            new_incidents = await self._find_new_clusters(still_unclustered, city=city)
            incidents.extend(new_incidents)

        # ── Phase 3: Single-source alerts from high-priority sources ──
        still_unclustered = [r for r in still_unclustered if r.cluster_id is None]
        if still_unclustered:
            high_priority_incidents = await self._check_high_priority_singles(
                still_unclustered, city=city
            )
            incidents.extend(high_priority_incidents)

        return incidents

    async def _check_cluster_updates(
        self,
        unclustered: list[ProcessedReport],
        clustered_by_id: dict[int, list[ProcessedReport]],
        all_reports: list[ProcessedReport],
        city: str = "",
    ) -> list[CorroboratedIncident]:
        """Check if any unclustered reports match existing notified clusters."""
        # Only get clusters that haven't expired (within cluster_expiry_hours)
        expiry_hours = getattr(self.config, "cluster_expiry_hours", 6.0)
        active_clusters = await self.db.get_active_clusters(max_age_hours=expiry_hours)
        if not active_clusters:
            return []

        incidents = []

        for cluster_info in active_clusters:
            cluster_id = cluster_info["id"]
            existing_reports = clustered_by_id.get(cluster_id, [])
            if not existing_reports:
                continue

            # Find unclustered reports that correlate with this cluster
            new_matches = []
            for report in unclustered:
                if report.cluster_id is not None:
                    continue  # Already assigned in this cycle

                score = self._score_against_cluster(report, existing_reports)
                if score >= 0.35:  # Lower threshold for updates
                    new_matches.append(report)

            if not new_matches:
                continue

            # Assign new reports to this cluster
            new_ids = [r.id for r in new_matches if r.id is not None]
            if new_ids:
                await self.db.assign_reports_to_cluster(new_ids, cluster_id)
                for r in new_matches:
                    r.cluster_id = cluster_id

            # Build the update incident
            all_cluster_reports = existing_reports + new_matches
            source_types = {r.source_type for r in all_cluster_reports}
            confidence = self._compute_confidence(all_cluster_reports, source_types)

            timestamps = [r.timestamp for r in all_cluster_reports]
            neighborhoods = [
                r.primary_neighborhood
                for r in all_cluster_reports
                if r.primary_neighborhood
            ]
            primary_location = (
                max(set(neighborhoods), key=neighborhoods.count)
                if neighborhoods
                else cluster_info.get("primary_location", self.config.locale.fallback_location)
            )

            lats = [r.latitude for r in all_cluster_reports if r.latitude is not None]
            lons = [r.longitude for r in all_cluster_reports if r.longitude is not None]

            # Update cluster in DB
            await self.db.update_cluster(
                cluster_id=cluster_id,
                confidence_score=confidence,
                source_count=len(all_cluster_reports),
                unique_source_types=list(source_types),
                latest_report=max(timestamps),
            )

            # Mark new reports as notified
            if new_ids:
                for rid in new_ids:
                    await self.db._db.execute(
                        "UPDATE raw_reports SET notified = 1 WHERE id = ?",
                        (rid,),
                    )
                await self.db._db.commit()

            incidents.append(CorroboratedIncident(
                cluster_id=cluster_id,
                reports=all_cluster_reports,
                primary_location=primary_location,
                latitude=sum(lats) / len(lats) if lats else None,
                longitude=sum(lons) / len(lons) if lons else None,
                confidence_score=confidence,
                source_count=len(all_cluster_reports),
                unique_source_types=source_types,
                earliest_report=min(timestamps),
                latest_report=max(timestamps),
                notification_type="update",
                new_reports=new_matches,
                city=city,
            ))

            logger.info(
                "Cluster %d updated: +%d new reports (%d total, confidence %.2f)",
                cluster_id,
                len(new_matches),
                len(all_cluster_reports),
                confidence,
            )

        return incidents

    def _score_against_cluster(
        self,
        report: ProcessedReport,
        cluster_reports: list[ProcessedReport],
    ) -> float:
        """Score how well a report matches an existing cluster."""
        if not cluster_reports:
            return 0.0

        best_score = 0.0
        window = self.config.correlation_window_seconds

        for cr in cluster_reports:
            # Skip same author
            if report.author == cr.author and report.source_type == cr.source_type:
                continue

            # Temporal
            time_diff = abs((report.timestamp - cr.timestamp).total_seconds())
            if time_diff > window:
                continue
            temporal = 1.0 - (time_diff / window)

            # Geographic
            geo = self._geo_score(report, cr)

            # Content similarity (pairwise between just these two)
            texts = [
                report.cleaned_text or report.original_text,
                cr.cleaned_text or cr.original_text,
            ]
            sim_matrix = self.similarity.compute_pairwise(texts)
            content = sim_matrix[0][1] if sim_matrix else 0.0

            combined = 0.30 * temporal + 0.35 * geo + 0.35 * content
            best_score = max(best_score, combined)

        return best_score

    async def _check_high_priority_singles(
        self, reports: list[ProcessedReport], city: str = ""
    ) -> list[CorroboratedIncident]:
        """Create single-source alerts for high-priority trusted sources.

        Sources like Iceout.org and StopICE.net are community-driven platforms
        with real-time reports that have already been vetted by their systems.
        These can trigger alerts without requiring corroboration from other sources.
        """
        logger.info("Checking %d reports for high-priority single-source alerts", len(reports))
        incidents = []

        for report in reports:
            logger.info(
                "  Checking report: [%s] cluster_id=%s",
                report.source_type, report.cluster_id
            )
            # Only high-priority sources qualify
            if report.source_type not in HIGH_PRIORITY_SOURCES:
                logger.info("    -> Skipping: not high-priority source")
                continue

            # Skip if already clustered
            if report.cluster_id is not None:
                logger.info("    -> Skipping: already clustered")
                continue

            logger.info("    -> CREATING single-source alert for %s", report.source_type)

            # Build single-report incident
            source_types = {report.source_type}

            # Use slightly lower confidence for single-source
            confidence = 0.65  # Trusted but single source

            # Location from the report — use city-specific fallback
            city_locale = self.config.city_locales.get(city)
            fallback = city_locale.fallback_location if city_locale else self.config.locale.fallback_location
            primary_location = report.primary_neighborhood or fallback

            # Create cluster for this single report
            report_ids = [report.id] if report.id is not None else []
            cluster_id = await self.db.create_cluster(
                primary_location=primary_location,
                latitude=report.latitude,
                longitude=report.longitude,
                confidence_score=confidence,
                source_count=1,
                unique_source_types=list(source_types),
                earliest_report=report.timestamp,
                latest_report=report.timestamp,
                city=city,
            )

            if report_ids:
                await self.db.assign_reports_to_cluster(report_ids, cluster_id)
                report.cluster_id = cluster_id

            incident = CorroboratedIncident(
                cluster_id=cluster_id,
                reports=[report],
                primary_location=primary_location,
                latitude=report.latitude,
                longitude=report.longitude,
                confidence_score=confidence,
                source_count=1,
                unique_source_types=source_types,
                earliest_report=report.timestamp,
                latest_report=report.timestamp,
                notification_type="new",
                new_reports=[report],
                city=city,
            )
            incidents.append(incident)

            logger.info(
                "High-priority single-source alert from %s: %s (confidence %.2f)",
                report.source_type,
                primary_location,
                confidence,
            )

        return incidents

    async def _find_new_clusters(
        self, reports: list[ProcessedReport], city: str = ""
    ) -> list[CorroboratedIncident]:
        """Find new corroborated clusters among unclustered reports."""
        if len(reports) < 2:
            return []

        # Build pairwise scores
        pairs = self._score_pairs(reports)

        # Cluster via single-linkage
        clusters = self._cluster(reports, pairs)

        # Filter for corroborated clusters
        incidents = []
        for cluster_reports in clusters:
            source_types = {r.source_type for r in cluster_reports}
            if len(source_types) < self.config.min_corroboration_sources:
                continue

            incident = await self._build_incident(cluster_reports, source_types, city=city)
            if incident is not None:
                incidents.append(incident)

        return incidents

    def _score_pairs(
        self, reports: list[ProcessedReport]
    ) -> dict[tuple[int, int], float]:
        """Compute combined scores for all valid report pairs."""
        n = len(reports)
        window = self.config.correlation_window_seconds

        # Pre-compute TF-IDF similarity matrix
        texts = [r.cleaned_text or r.original_text for r in reports]
        sim_matrix = self.similarity.compute_pairwise(texts)

        scores: dict[tuple[int, int], float] = {}

        for i in range(n):
            for j in range(i + 1, n):
                ri, rj = reports[i], reports[j]

                # Skip same author (not independent)
                if ri.author == rj.author and ri.source_type == rj.source_type:
                    continue

                # Temporal score
                time_diff = abs((ri.timestamp - rj.timestamp).total_seconds())
                if time_diff > window:
                    continue
                temporal_score = 1.0 - (time_diff / window)

                # Geographic score
                geo_score = self._geo_score(ri, rj)

                # Content similarity
                content_score = sim_matrix[i][j] if sim_matrix else 0.0

                # Combined
                combined = (
                    0.30 * temporal_score
                    + 0.35 * geo_score
                    + 0.35 * content_score
                )

                if combined >= 0.40:
                    scores[(i, j)] = combined

        return scores

    def _geo_score(self, a: ProcessedReport, b: ProcessedReport) -> float:
        """Score geographic proximity between two reports."""
        # Both have coordinates
        if (
            a.latitude is not None
            and a.longitude is not None
            and b.latitude is not None
            and b.longitude is not None
        ):
            dist = haversine_km(a.latitude, a.longitude, b.latitude, b.longitude)
            if dist <= self.config.geo_proximity_km:
                return 1.0
            elif dist <= self.config.geo_proximity_km * 3:
                return 0.5
            return 0.2

        # Both have neighborhoods
        if a.primary_neighborhood and b.primary_neighborhood:
            if a.primary_neighborhood == b.primary_neighborhood:
                return 1.0
            return 0.5  # different neighborhoods but both in locale area

        # At least one has some locale reference (they passed keyword filter)
        return 0.3

    def _cluster(
        self,
        reports: list[ProcessedReport],
        pairs: dict[tuple[int, int], float],
    ) -> list[list[ProcessedReport]]:
        """Single-linkage clustering based on qualifying pairs."""
        n = len(reports)

        # Union-Find
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x: int, y: int) -> None:
            px, py = find(x), find(y)
            if px != py:
                parent[px] = py

        for (i, j) in pairs:
            union(i, j)

        # Group by root
        groups: dict[int, list[int]] = {}
        for idx in range(n):
            root = find(idx)
            groups.setdefault(root, []).append(idx)

        # Only return clusters with 2+ reports
        clusters = []
        for indices in groups.values():
            if len(indices) >= 2:
                clusters.append([reports[i] for i in indices])

        return clusters

    async def _build_incident(
        self,
        reports: list[ProcessedReport],
        source_types: set[str],
        city: str = "",
    ) -> CorroboratedIncident | None:
        """Create a CorroboratedIncident and persist it to the database."""
        # Determine primary location — use city-specific fallback
        neighborhoods = [
            r.primary_neighborhood for r in reports if r.primary_neighborhood
        ]
        if neighborhoods:
            primary_location = max(set(neighborhoods), key=neighborhoods.count)
        else:
            city_locale = self.config.city_locales.get(city)
            fallback = city_locale.fallback_location_unspecified if city_locale else self.config.locale.fallback_location_unspecified
            primary_location = fallback

        # Average coordinates from reports that have them
        lats = [r.latitude for r in reports if r.latitude is not None]
        lons = [r.longitude for r in reports if r.longitude is not None]
        avg_lat = sum(lats) / len(lats) if lats else None
        avg_lon = sum(lons) / len(lons) if lons else None

        timestamps = [r.timestamp for r in reports]
        earliest = min(timestamps)
        latest = max(timestamps)

        # Confidence score
        confidence = self._compute_confidence(reports, source_types)

        # Persist cluster
        report_ids = [r.id for r in reports if r.id is not None]
        cluster_id = await self.db.create_cluster(
            primary_location=primary_location,
            latitude=avg_lat,
            longitude=avg_lon,
            confidence_score=confidence,
            source_count=len(reports),
            unique_source_types=list(source_types),
            earliest_report=earliest,
            latest_report=latest,
            city=city,
        )

        if report_ids:
            await self.db.assign_reports_to_cluster(report_ids, cluster_id)

        return CorroboratedIncident(
            cluster_id=cluster_id,
            reports=reports,
            primary_location=primary_location,
            latitude=avg_lat,
            longitude=avg_lon,
            confidence_score=confidence,
            source_count=len(reports),
            unique_source_types=source_types,
            earliest_report=earliest,
            latest_report=latest,
            notification_type="new",
            new_reports=reports,
            city=city,
        )

    def _compute_confidence(
        self,
        reports: list[ProcessedReport],
        source_types: set[str],
    ) -> float:
        """Compute 0.0-1.0 confidence score for a cluster."""
        # Factor 1: Number of sources (more = better, capped at 4)
        source_factor = min(len(reports) / 4.0, 1.0)

        # Factor 2: Source diversity (more platform types = better)
        diversity_factor = min(len(source_types) / 3.0, 1.0)

        # Factor 3: Temporal tightness (closer timestamps = better)
        timestamps = [r.timestamp for r in reports]
        if len(timestamps) >= 2:
            span = (max(timestamps) - min(timestamps)).total_seconds()
            window = self.config.correlation_window_seconds
            tightness = 1.0 - min(span / window, 1.0)
        else:
            tightness = 0.5

        # Factor 4: Geographic precision (having specific neighborhoods = better)
        neighborhoods = [r for r in reports if r.primary_neighborhood]
        geo_factor = len(neighborhoods) / len(reports) if reports else 0.0

        confidence = (
            0.30 * source_factor
            + 0.25 * diversity_factor
            + 0.25 * tightness
            + 0.20 * geo_factor
        )
        return round(min(confidence, 1.0), 3)
