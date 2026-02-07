from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

from config import Config
from storage.models import RawReport

logger = logging.getLogger(__name__)


class BaseCollector(ABC):
    """Abstract base for all data source collectors.

    Subclasses implement ``collect()`` and ``get_poll_interval()``.
    The ``run()`` loop handles scheduling and exponential backoff.
    """

    name: str = "base"

    def __init__(self, config: Config, report_queue: asyncio.Queue):
        self.config = config
        self.report_queue = report_queue
        self.is_running = False
        self._seen_ids: set[str] = set()

    @abstractmethod
    async def collect(self) -> list[RawReport]:
        """Perform one collection cycle. Return new reports."""
        ...

    @abstractmethod
    def get_poll_interval(self) -> int:
        """Seconds between collection cycles."""
        ...

    def _is_new(self, source_id: str) -> bool:
        """Check if we've already seen this source_id this session."""
        if source_id in self._seen_ids:
            return False
        self._seen_ids.add(source_id)
        # Cap the in-memory set to avoid unbounded growth
        # Reduced from 10K to 2K to save memory on low-RAM servers
        if len(self._seen_ids) > 2_000:
            # Keep the most recent half
            trimmed = list(self._seen_ids)[-1_000:]
            self._seen_ids = set(trimmed)
        return True

    async def run(self) -> None:
        """Main loop: collect, enqueue, sleep, repeat."""
        self.is_running = True
        backoff = 1
        max_backoff = 60  # Cap at 1 minute, not 5 minutes
        consecutive_failures = 0
        max_consecutive_failures = 10  # Reset backoff after this many failures
        cycle_count = 0

        logger.info("[%s] Collector starting", self.name)

        while self.is_running:
            try:
                cycle_count += 1
                logger.info("[%s] Starting collection cycle %d", self.name, cycle_count)

                reports = await self.collect()

                # Reset backoff on success
                backoff = 1
                consecutive_failures = 0

                # Pre-filter stale reports so they never hit the queue
                now = datetime.now(timezone.utc)
                is_trusted = self.name in ("iceout", "stopice")
                max_age = timedelta(hours=6) if is_trusted else timedelta(
                    seconds=self.config.report_max_age_seconds
                )
                fresh = [r for r in reports if (now - r.timestamp) <= max_age]
                stale_count = len(reports) - len(fresh)

                for report in fresh:
                    await self.report_queue.put(report)

                if fresh:
                    msg = "[%s] Collected %d new reports"
                    if stale_count:
                        msg += f" (skipped {stale_count} stale)"
                    logger.info(msg, self.name, len(fresh))
                elif stale_count:
                    logger.info(
                        "[%s] Cycle %d complete, no fresh reports (%d stale skipped)",
                        self.name, cycle_count, stale_count,
                    )
                else:
                    logger.info("[%s] Cycle %d complete, no new reports", self.name, cycle_count)

            except asyncio.CancelledError:
                logger.info("[%s] Collector cancelled", self.name)
                break
            except Exception:
                consecutive_failures += 1
                logger.exception(
                    "[%s] Error during collection (failure %d/%d), backing off %ds",
                    self.name,
                    consecutive_failures,
                    max_consecutive_failures,
                    backoff,
                )

                # If too many consecutive failures, reset backoff to avoid death spiral
                if consecutive_failures >= max_consecutive_failures:
                    logger.warning(
                        "[%s] %d consecutive failures, resetting backoff to prevent spiral",
                        self.name,
                        consecutive_failures,
                    )
                    backoff = 1
                    consecutive_failures = 0

                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue

            await asyncio.sleep(self.get_poll_interval())

        logger.info("[%s] Collector stopped", self.name)

    def stop(self) -> None:
        self.is_running = False
