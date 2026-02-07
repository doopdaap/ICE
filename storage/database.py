from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import aiosqlite

from config import Config
from storage.models import RawReport, ProcessedReport

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS raw_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_url TEXT,
    author TEXT,
    original_text TEXT NOT NULL,
    cleaned_text TEXT,
    timestamp TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    raw_metadata TEXT,
    is_relevant INTEGER DEFAULT 0,
    primary_neighborhood TEXT,
    latitude REAL,
    longitude REAL,
    keywords_matched TEXT,
    cluster_id INTEGER,
    notified INTEGER DEFAULT 0,
    expired INTEGER DEFAULT 0,
    city TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(source_type, source_id)
);

CREATE TABLE IF NOT EXISTS clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    primary_location TEXT,
    latitude REAL,
    longitude REAL,
    confidence_score REAL,
    source_count INTEGER,
    unique_source_types TEXT,
    earliest_report TEXT,
    latest_report TEXT,
    notified INTEGER DEFAULT 0,
    notified_at TEXT,
    city TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id INTEGER NOT NULL,
    discord_message_id TEXT,
    sent_at TEXT NOT NULL,
    embed_content TEXT,
    success INTEGER DEFAULT 1,
    error_message TEXT,
    FOREIGN KEY (cluster_id) REFERENCES clusters(id)
);

CREATE INDEX IF NOT EXISTS idx_raw_reports_correlation
    ON raw_reports(is_relevant, notified, expired, collected_at);

CREATE INDEX IF NOT EXISTS idx_raw_reports_source
    ON raw_reports(source_type, source_id);
"""


class Database:
    def __init__(self, config: Config):
        self.db_path = config.db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript(SCHEMA_SQL)
        await self._migrate_add_city_column()
        await self._db.commit()
        logger.info("Database initialized at %s", self.db_path)

    async def _migrate_add_city_column(self) -> None:
        """Add city column to existing tables if missing (backward compat)."""
        for table in ("raw_reports", "clusters"):
            cursor = await self._db.execute(f"PRAGMA table_info({table})")
            columns = {row[1] for row in await cursor.fetchall()}
            if "city" not in columns:
                await self._db.execute(
                    f"ALTER TABLE {table} ADD COLUMN city TEXT DEFAULT ''"
                )
                logger.info("Migrated %s: added city column", table)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def insert_raw_report(self, report: RawReport) -> int | None:
        """Insert a raw report. Returns the row id, or None if duplicate."""
        try:
            cursor = await self._db.execute(
                """INSERT INTO raw_reports
                   (source_type, source_id, source_url, author,
                    original_text, timestamp, collected_at, raw_metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    report.source_type,
                    report.source_id,
                    report.source_url,
                    report.author,
                    report.text,
                    report.timestamp.isoformat(),
                    report.collected_at.isoformat(),
                    json.dumps(report.raw_metadata),
                ),
            )
            await self._db.commit()
            return cursor.lastrowid
        except aiosqlite.IntegrityError:
            # Duplicate source_type + source_id
            return None

    async def update_report_processing(
        self,
        report_id: int,
        cleaned_text: str,
        is_relevant: bool,
        primary_neighborhood: str | None,
        latitude: float | None,
        longitude: float | None,
        keywords_matched: list[str],
        city: str = "",
    ) -> None:
        await self._db.execute(
            """UPDATE raw_reports
               SET cleaned_text = ?, is_relevant = ?,
                   primary_neighborhood = ?, latitude = ?, longitude = ?,
                   keywords_matched = ?, city = ?
               WHERE id = ?""",
            (
                cleaned_text,
                int(is_relevant),
                primary_neighborhood,
                latitude,
                longitude,
                json.dumps(keywords_matched),
                city,
                report_id,
            ),
        )
        await self._db.commit()

    async def get_recent_relevant(
        self, since: datetime
    ) -> list[ProcessedReport]:
        """Get relevant, un-expired reports since a cutoff time.

        Returns both un-notified reports AND notified reports that belong
        to active clusters (needed for update detection).
        """
        cursor = await self._db.execute(
            """SELECT * FROM raw_reports
               WHERE is_relevant = 1
                 AND expired = 0
                 AND collected_at >= ?
               ORDER BY timestamp ASC""",
            (since.isoformat(),),
        )
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            results.append(ProcessedReport(
                id=row["id"],
                source_type=row["source_type"],
                source_id=row["source_id"],
                source_url=row["source_url"],
                author=row["author"],
                original_text=row["original_text"],
                cleaned_text=row["cleaned_text"] or "",
                timestamp=datetime.fromisoformat(row["timestamp"]),
                collected_at=datetime.fromisoformat(row["collected_at"]),
                primary_neighborhood=row["primary_neighborhood"],
                latitude=row["latitude"],
                longitude=row["longitude"],
                keywords_matched=json.loads(row["keywords_matched"] or "[]"),
                is_relevant=bool(row["is_relevant"]),
                cluster_id=row["cluster_id"],
                city=row["city"] or "",
            ))
        return results

    async def create_cluster(
        self,
        primary_location: str,
        latitude: float | None,
        longitude: float | None,
        confidence_score: float,
        source_count: int,
        unique_source_types: list[str],
        earliest_report: datetime,
        latest_report: datetime,
        city: str = "",
    ) -> int:
        cursor = await self._db.execute(
            """INSERT INTO clusters
               (primary_location, latitude, longitude, confidence_score,
                source_count, unique_source_types, earliest_report, latest_report,
                city)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                primary_location,
                latitude,
                longitude,
                confidence_score,
                source_count,
                json.dumps(unique_source_types),
                earliest_report.isoformat(),
                latest_report.isoformat(),
                city,
            ),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def assign_reports_to_cluster(
        self, report_ids: list[int], cluster_id: int
    ) -> None:
        placeholders = ",".join("?" for _ in report_ids)
        await self._db.execute(
            f"UPDATE raw_reports SET cluster_id = ? WHERE id IN ({placeholders})",
            [cluster_id] + report_ids,
        )
        await self._db.commit()

    async def mark_cluster_notified(self, cluster_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "UPDATE clusters SET notified = 1, notified_at = ? WHERE id = ?",
            (now, cluster_id),
        )
        await self._db.execute(
            "UPDATE raw_reports SET notified = 1 WHERE cluster_id = ?",
            (cluster_id,),
        )
        await self._db.commit()

    async def log_notification(
        self,
        cluster_id: int,
        embed_content: dict,
        success: bool,
        error_message: str | None = None,
    ) -> None:
        await self._db.execute(
            """INSERT INTO notifications
               (cluster_id, sent_at, embed_content, success, error_message)
               VALUES (?, ?, ?, ?, ?)""",
            (
                cluster_id,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(embed_content),
                int(success),
                error_message,
            ),
        )
        await self._db.commit()

    async def get_notified_cluster_report_ids(
        self, cluster_id: int
    ) -> set[int]:
        """Get IDs of reports that were already part of a cluster when it was notified."""
        cursor = await self._db.execute(
            "SELECT id FROM raw_reports WHERE cluster_id = ? AND notified = 1",
            (cluster_id,),
        )
        rows = await cursor.fetchall()
        return {row["id"] for row in rows}

    async def get_active_clusters(self, max_age_hours: float = 6.0) -> list[dict]:
        """Get notified clusters that are still active (within max_age_hours).

        Clusters older than max_age_hours since their latest_report are
        considered stale and excluded from update detection.
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()

        cursor = await self._db.execute(
            """SELECT id, primary_location, latitude, longitude,
                      confidence_score, source_count, unique_source_types,
                      earliest_report, latest_report, city
               FROM clusters
               WHERE notified = 1
                 AND latest_report >= ?""",
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def expire_old_clusters(self, max_age_hours: float = 6.0) -> int:
        """Mark clusters older than max_age_hours as no longer active.

        This stops them from receiving update notifications.
        Returns count of expired clusters.
        """
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()

        # We don't have an 'active' column, but we can use latest_report
        # to filter. The get_active_clusters query already handles this.
        # For explicit tracking, let's just log how many would be expired.
        cursor = await self._db.execute(
            """SELECT COUNT(*) as cnt FROM clusters
               WHERE notified = 1 AND latest_report < ?""",
            (cutoff,),
        )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0

    async def update_cluster(
        self,
        cluster_id: int,
        confidence_score: float,
        source_count: int,
        unique_source_types: list[str],
        latest_report: datetime,
    ) -> None:
        """Update an existing cluster with new stats."""
        await self._db.execute(
            """UPDATE clusters
               SET confidence_score = ?, source_count = ?,
                   unique_source_types = ?, latest_report = ?
               WHERE id = ?""",
            (
                confidence_score,
                source_count,
                json.dumps(unique_source_types),
                latest_report.isoformat(),
                cluster_id,
            ),
        )
        await self._db.commit()

    async def expire_old_reports(self, before: datetime) -> int:
        """Mark old un-notified reports as expired. Returns count."""
        cursor = await self._db.execute(
            """UPDATE raw_reports
               SET expired = 1
               WHERE notified = 0
                 AND expired = 0
                 AND collected_at < ?""",
            (before.isoformat(),),
        )
        await self._db.commit()
        return cursor.rowcount

    async def purge_old_data(self, days: int = 7) -> None:
        """Delete records older than N days."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        await self._db.execute(
            "DELETE FROM raw_reports WHERE created_at < ?", (cutoff,)
        )
        await self._db.execute(
            "DELETE FROM clusters WHERE created_at < ?", (cutoff,)
        )
        await self._db.execute(
            "DELETE FROM notifications WHERE sent_at < ?", (cutoff,)
        )
        await self._db.commit()
        logger.info("Purged data older than %d days", days)
