"""Minneapolis ICE Activity Monitor

Monitors multiple data sources for ICE enforcement activity in Minneapolis,
correlates reports across sources, and forwards corroborated incidents to Discord.

Usage:
    python main.py              # Run normally
    python main.py --dry-run    # Log but don't send to Discord
    python main.py --log-level DEBUG  # Verbose logging
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timedelta, timezone

from config import Config, load_config
from collectors.base import BaseCollector
from collectors.rss_collector import RSSCollector
from correlation.correlator import Correlator
from notifications.discord_notifier import DiscordNotifier
from processing.text_processor import clean_text, is_relevant, get_all_matched_keywords
from storage.database import Database
from storage.models import RawReport

logger = logging.getLogger("ice_monitor")


def setup_logging(level: str) -> None:
    # Ensure logs directory exists
    os.makedirs("logs", exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("logs/ice_monitor.log", encoding="utf-8"),
        ],
    )


class ICEMonitor:
    """Main application orchestrator."""

    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config)
        self.report_queue: asyncio.Queue[RawReport] = asyncio.Queue()
        self.collectors: list[BaseCollector] = []
        self.correlator = Correlator(config, self.db)
        self.notifier = DiscordNotifier(config)
        self._location_extractor = None  # Lazy-loaded (spaCy is heavy)
        self._shutdown_event = asyncio.Event()

    def _init_collectors(self) -> None:
        """Initialize collectors based on available configuration."""
        # RSS is always available (no API keys needed)
        self.collectors.append(
            RSSCollector(self.config, self.report_queue)
        )
        logger.info("RSS collector enabled (%d feeds)", len(self.config.rss_feeds))

        # Reddit requires API credentials
        if self.config.reddit_client_id and self.config.reddit_client_secret:
            from collectors.reddit_collector import RedditCollector
            self.collectors.append(
                RedditCollector(self.config, self.report_queue)
            )
            logger.info(
                "Reddit collector enabled (%d subreddits)",
                len(self.config.reddit_subreddits),
            )
        else:
            logger.info("Reddit collector disabled (no credentials in .env)")

        # Iceout.org (always available, no API keys needed)
        if self.config.iceout_enabled:
            from collectors.iceout_collector import IceoutCollector
            self.collectors.append(
                IceoutCollector(self.config, self.report_queue)
            )
            logger.info("Iceout.org collector enabled (real-time community reports)")
        else:
            logger.info("Iceout.org collector disabled")

        # Twitter (unofficial scraping)
        if self.config.twitter_enabled:
            from collectors.twitter_collector import TwitterCollector
            self.collectors.append(
                TwitterCollector(self.config, self.report_queue)
            )
            logger.info("Twitter collector enabled (unofficial scraping)")
        else:
            logger.info("Twitter collector disabled")

        # Bluesky (free public API, no auth needed)
        if getattr(self.config, "bluesky_enabled", True):
            from collectors.bluesky_collector import BlueskyCollector
            self.collectors.append(
                BlueskyCollector(self.config, self.report_queue)
            )
            logger.info("Bluesky collector enabled (public API)")
        else:
            logger.info("Bluesky collector disabled")

        # StopICE.net (XML data feed, no auth needed)
        if getattr(self.config, "stopice_enabled", True):
            from collectors.stopice_collector import StopICECollector
            self.collectors.append(
                StopICECollector(self.config, self.report_queue)
            )
            logger.info("StopICE.net collector enabled (XML feed)")
        else:
            logger.info("StopICE.net collector disabled")

        # Instagram (Playwright scraping, no auth needed for public profiles)
        if getattr(self.config, "instagram_enabled", True):
            from collectors.instagram_collector import InstagramCollector
            self.collectors.append(
                InstagramCollector(self.config, self.report_queue)
            )
            logger.info("Instagram collector enabled (Playwright scraping)")
        else:
            logger.info("Instagram collector disabled")

    def _get_location_extractor(self):
        """Lazy-load the location extractor to avoid slow startup if not needed."""
        if self._location_extractor is None:
            try:
                from processing.location_extractor import LocationExtractor
                locale = self.config.locale
                self._location_extractor = LocationExtractor(
                    neighborhoods_file=locale.neighborhoods_file,
                    landmarks_file=locale.landmarks_file,
                )
                logger.info("Location extractor loaded (spaCy + gazetteer)")
            except OSError as e:
                logger.warning(
                    "Could not load spaCy model. Run: "
                    "python -m spacy download en_core_web_sm. Error: %s", e
                )
        return self._location_extractor

    async def _process_report(self, report: RawReport) -> None:
        """Process a single raw report: clean, filter, extract location, store."""
        now = datetime.now(timezone.utc)

        # Trusted community sources (iceout, stopice) are pre-validated as
        # ICE-related and have structured location data — skip keyword filtering
        is_trusted_source = report.source_type in ("iceout", "stopice")

        # Freshness filter — discard stale reports
        # Trusted sources get 6 hours (they're already vetted)
        # Other sources get 3 hours
        if is_trusted_source:
            max_age = timedelta(hours=6)
        else:
            max_age = timedelta(seconds=self.config.report_max_age_seconds)

        if (now - report.timestamp) > max_age:
            logger.info(
                "Skipping stale report [%s] from %s (age > %s)",
                report.source_type,
                report.timestamp.isoformat(),
                max_age,
            )
            return

        # Insert into DB (deduplicates via UNIQUE constraint)
        row_id = await self.db.insert_raw_report(report)
        if row_id is None:
            return  # Duplicate

        # Clean and filter
        cleaned = clean_text(report.text)
        if is_trusted_source:
            relevant = True
            keywords = [f"{report.source_type} report"]
        else:
            # Pass source_type for source-aware filtering
            relevant = is_relevant(cleaned, source_type=report.source_type)
            keywords = get_all_matched_keywords(cleaned) if relevant else []

        # Location extraction
        neighborhood = None
        lat = None
        lon = None

        if is_trusted_source:
            # Trusted sources (Iceout, StopICE) provide coordinates in metadata
            lat = report.raw_metadata.get("latitude")
            lon = report.raw_metadata.get("longitude")
            # Try to match coordinates to a neighborhood via the extractor
            if lat and lon:
                extractor = self._get_location_extractor()
                if extractor:
                    from processing.location_extractor import haversine_km
                    best_dist = float("inf")
                    best_neighborhood = None
                    for entry in extractor._gazetteer:
                        c = entry.get("centroid", {})
                        d = haversine_km(lat, lon, c.get("lat", 0), c.get("lon", 0))
                        if d < best_dist:
                            best_dist = d
                            best_neighborhood = entry["name"]

                    # Only use gazetteer match if within 5km of a known neighborhood
                    if best_dist <= 5.0:
                        neighborhood = best_neighborhood
                    else:
                        # Use the raw location description from the source
                        neighborhood = report.raw_metadata.get(
                            "location_description", self.config.locale.fallback_location
                        )
            else:
                neighborhood = report.raw_metadata.get("location_description")
        elif relevant:
            extractor = self._get_location_extractor()
            if extractor:
                locations = extractor.extract(cleaned)
                neighborhood, lat, lon = extractor.get_primary_location(locations)

        await self.db.update_report_processing(
            report_id=row_id,
            cleaned_text=cleaned,
            is_relevant=relevant,
            primary_neighborhood=neighborhood,
            latitude=lat,
            longitude=lon,
            keywords_matched=keywords,
        )

        if relevant:
            logger.info(
                "✓ RELEVANT: [%s] %s (location: %s)",
                report.source_type,
                report.text[:80].replace('\n', ' '),
                neighborhood or "unknown",
            )
        else:
            logger.debug(
                "✗ Not relevant: [%s] %s...",
                report.source_type,
                report.text[:60].replace('\n', ' '),
            )

    async def _processing_loop(self) -> None:
        """Consume reports from the queue and process them."""
        while not self._shutdown_event.is_set():
            try:
                report = await asyncio.wait_for(
                    self.report_queue.get(), timeout=5.0
                )
                await self._process_report(report)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error processing report")

    async def _correlation_loop(self) -> None:
        """Periodically run the correlation algorithm and send notifications.

        Handles both NEW incidents and UPDATES to existing incidents.
        """
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self.config.correlation_check_interval)

                incidents = await self.correlator.run_cycle()

                for incident in incidents:
                    success = await self.notifier.send(incident)
                    ntype = incident.notification_type
                    await self.db.log_notification(
                        cluster_id=incident.cluster_id,
                        embed_content={
                            "location": incident.primary_location,
                            "type": ntype,
                            "source_count": incident.source_count,
                        },
                        success=success,
                    )
                    # Only mark_cluster_notified for new incidents
                    # (updates already have the cluster marked)
                    if success and ntype == "new":
                        await self.db.mark_cluster_notified(incident.cluster_id)

                    if success:
                        logger.info(
                            "%s notification sent: %s (%d sources)",
                            ntype.upper(),
                            incident.primary_location,
                            incident.source_count,
                        )

                # Expire old uncorroborated reports
                expiry_cutoff = datetime.now(timezone.utc) - timedelta(
                    seconds=self.config.correlation_window_seconds
                )
                expired_count = await self.db.expire_old_reports(expiry_cutoff)
                if expired_count:
                    logger.debug("Expired %d uncorroborated reports", expired_count)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in correlation loop")

    async def _daily_cleanup(self) -> None:
        """Purge old data once per day."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(86400)  # 24 hours
                await self.db.purge_old_data(days=7)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in daily cleanup")

    async def run(self) -> None:
        """Start all components and run until shutdown."""
        # Initialize locale-dependent geo keywords
        from processing.text_processor import init_geo_keywords
        init_geo_keywords(self.config.locale)
        logger.info("Geo keywords loaded for locale: %s", self.config.locale.name)

        # Initialize
        await self.db.connect()
        self._init_collectors()

        if not self.collectors:
            logger.error("No collectors configured. Check your .env file.")
            return

        logger.info(
            "Starting ICE Monitor with %d collector(s). Dry run: %s",
            len(self.collectors),
            self.config.dry_run,
        )

        # Check Discord configuration
        has_discord = self.config.discord_webhook_url or self.config.discord_bot_token
        if not has_discord and not self.config.dry_run:
            logger.warning(
                "No Discord configuration found. "
                "Set DISCORD_WEBHOOK_URL or DISCORD_BOT_TOKEN in .env, or use --dry-run"
            )

        # Log Discord mode
        if self.config.discord_bot_token and self.config.discord_webhook_url:
            logger.info("Discord: Webhook + Bot mode (personal channel + multi-server)")
        elif self.config.discord_bot_token:
            logger.info("Discord: Bot mode (multi-server)")
        elif self.config.discord_webhook_url:
            logger.info("Discord: Webhook mode (single channel)")

        # Start Discord bot if configured (runs in background)
        self._bot = None
        if self.config.discord_bot_token:
            try:
                from notifications.discord_bot import ICEAlertBot, _set_bot_instance
                locale = self.config.locale
                self._bot = ICEAlertBot(
                    self.config.discord_bot_token,
                    locale_name=locale.display_name,
                    locale_area=locale.fallback_location,
                )
                _set_bot_instance(self._bot)
                logger.info("Discord bot initialized, starting in background...")
            except ImportError as e:
                logger.warning("Discord bot unavailable: %s", e)

        # Build task list
        tasks = []

        # Discord bot task (if configured)
        if self._bot:
            tasks.append(asyncio.create_task(
                self._bot.start(self._bot.token), name="discord_bot"
            ))

        # Collector tasks
        for collector in self.collectors:
            tasks.append(asyncio.create_task(
                collector.run(), name=f"collector_{collector.name}"
            ))

        # Processing loop
        tasks.append(asyncio.create_task(
            self._processing_loop(), name="processing"
        ))

        # Correlation loop
        tasks.append(asyncio.create_task(
            self._correlation_loop(), name="correlation"
        ))

        # Daily cleanup
        tasks.append(asyncio.create_task(
            self._daily_cleanup(), name="cleanup"
        ))

        logger.info("All tasks started. Press Ctrl+C to stop.")

        try:
            # Wait for shutdown signal
            await self._shutdown_event.wait()
        finally:
            logger.info("Shutting down...")

            # Stop collectors
            for collector in self.collectors:
                collector.stop()

            # Cancel all tasks
            for task in tasks:
                task.cancel()

            await asyncio.gather(*tasks, return_exceptions=True)

            # Cleanup Twitter session if active
            for collector in self.collectors:
                if hasattr(collector, "cleanup"):
                    await collector.cleanup()

            await self.db.close()
            logger.info("Shutdown complete.")

    def request_shutdown(self) -> None:
        self._shutdown_event.set()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ICE Activity Monitor"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log notifications instead of sending to Discord",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Override log level",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config()

    # CLI overrides
    if args.dry_run:
        config = Config(**{**config.__dict__, "dry_run": True})
    log_level = args.log_level or config.log_level
    setup_logging(log_level)

    monitor = ICEMonitor(config)

    # Handle Ctrl+C gracefully
    def _signal_handler(sig, frame):
        logger.info("Received signal %s, requesting shutdown...", sig)
        monitor.request_shutdown()

    signal.signal(signal.SIGINT, _signal_handler)
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, _signal_handler)
    else:
        signal.signal(signal.SIGBREAK, _signal_handler)

    asyncio.run(monitor.run())


if __name__ == "__main__":
    main()
