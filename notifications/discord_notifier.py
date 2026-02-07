from __future__ import annotations

import logging
from datetime import datetime, timezone

from discord_webhook import DiscordWebhook, DiscordEmbed

from config import Config
from storage.models import CorroboratedIncident

logger = logging.getLogger(__name__)

# ── Colors ────────────────────────────────────────────────────────────
COLOR_NEW_HIGH = "FF0000"       # Red — new incident, high confidence
COLOR_NEW_MEDIUM = "FF4500"     # OrangeRed — new incident, medium
COLOR_NEW_LOW = "FF8C00"        # DarkOrange — new incident, low
COLOR_UPDATE = "3498DB"         # Blue — update to existing incident

# ── Source type labels ────────────────────────────────────────────────
SOURCE_LABELS = {
    "twitter": "Twitter/X",
    "reddit": "Reddit",
    "rss": "News (RSS)",
    "iceout": "Iceout.org",
    "stopice": "StopICE.net",
    "bluesky": "Bluesky",
    "instagram": "Instagram",
}


def _confidence_emoji(score: float) -> str:
    if score >= 0.7:
        return "HIGH"
    elif score >= 0.45:
        return "MEDIUM"
    return "LOW"


def _get_color(incident: CorroboratedIncident) -> str:
    if incident.notification_type == "update":
        return COLOR_UPDATE
    score = incident.confidence_score
    if score >= 0.7:
        return COLOR_NEW_HIGH
    elif score >= 0.45:
        return COLOR_NEW_MEDIUM
    return COLOR_NEW_LOW


def _format_time_local(dt: datetime, tz_name: str) -> str:
    """Format a UTC datetime in the locale's timezone."""
    from zoneinfo import ZoneInfo
    local_dt = dt.astimezone(ZoneInfo(tz_name))
    # %-I is Linux-only; use %I and strip leading zero for portability
    raw = local_dt.strftime("%I:%M %p")
    if raw.startswith("0"):
        raw = raw[1:]
    return raw.lower()


class DiscordNotifier:
    """Sends notifications to Discord via webhook and/or bot.

    Supports running BOTH modes simultaneously:
    - Webhook mode: Sends to your configured channel (for personal use)
    - Bot mode: Broadcasts to all servers that have subscribed (for public)

    If both are configured, alerts go to both your webhook AND all bot subscribers.
    """

    def __init__(self, config: Config):
        self.webhook_url = config.discord_webhook_url
        self.bot_token = config.discord_bot_token
        self.dry_run = config.dry_run
        self._use_webhook = bool(self.webhook_url)
        self._use_bot = bool(self.bot_token)
        self._locale = config.locale
        self._city_locales = config.city_locales

    def _build_new_incident_embed(
        self, incident: CorroboratedIncident
    ) -> DiscordEmbed:
        """Build embed for a NEW incident alert — designed for fast scanning."""
        color = _get_color(incident)
        conf = _confidence_emoji(incident.confidence_score)

        # Title: location front and center, with city
        city_locale = self._city_locales.get(incident.city) if incident.city else None
        fallback = city_locale.fallback_location if city_locale else self._locale.fallback_location
        location = incident.primary_location or fallback
        city_label = incident.city.title() if incident.city else ""
        title = f"ICE ACTIVITY: {location}"
        if city_label and city_label.lower() not in location.lower():
            title += f" ({city_label})"

        embed = DiscordEmbed(
            title=title,
            color=color,
        )

        # Topline summary — the most important info in 1-2 lines
        tz = city_locale.timezone if city_locale else self._locale.timezone
        time_str = _format_time_local(incident.earliest_report, tz)
        if incident.earliest_report != incident.latest_report:
            time_str += f" - {_format_time_local(incident.latest_report, tz)}"

        platform_names = sorted(
            SOURCE_LABELS.get(s, s) for s in incident.unique_source_types
        )

        summary = (
            f"**{conf} confidence** | "
            f"{incident.source_count} reports across "
            f"{', '.join(platform_names)}\n"
            f"First reported: {time_str}"
        )
        embed.set_description(summary)

        # Source excerpts — compact, one per source
        for r in incident.reports[:6]:
            source_label = SOURCE_LABELS.get(r.source_type, r.source_type)
            # Truncate to keep it scannable
            excerpt = r.original_text[:120].replace("\n", " ").strip()
            if len(r.original_text) > 120:
                excerpt += "..."

            if r.source_url:
                link = f"🔗 [View on {source_label}]({r.source_url})"
            else:
                link = ""
            embed.add_embed_field(
                name=f"{source_label} — {r.author}",
                value=f"{excerpt}\n{link}",
                inline=False,
            )

        embed.set_footer(
            text=(
                "ICE Activity Monitor | Unverified community reporting | "
                "Confirm before acting"
            )
        )
        embed.set_timestamp(datetime.now(timezone.utc).isoformat())

        return embed

    def _build_update_embed(
        self, incident: CorroboratedIncident
    ) -> DiscordEmbed:
        """Build embed for an UPDATE to an existing incident."""
        color = COLOR_UPDATE
        city_locale = self._city_locales.get(incident.city) if incident.city else None
        fallback = city_locale.fallback_location if city_locale else self._locale.fallback_location
        location = incident.primary_location or fallback
        city_label = incident.city.title() if incident.city else ""
        title = f"UPDATE: {location}"
        if city_label and city_label.lower() not in location.lower():
            title += f" ({city_label})"

        embed = DiscordEmbed(
            title=title,
            color=color,
        )

        new_reports = incident.new_reports or []
        conf = _confidence_emoji(incident.confidence_score)

        summary = (
            f"**{len(new_reports)} new source(s)** confirming earlier reports\n"
            f"Now at **{conf}** confidence | "
            f"{incident.source_count} total reports"
        )
        embed.set_description(summary)

        # Only show the NEW reports that triggered this update
        for r in new_reports[:4]:
            source_label = SOURCE_LABELS.get(r.source_type, r.source_type)
            excerpt = r.original_text[:120].replace("\n", " ").strip()
            if len(r.original_text) > 120:
                excerpt += "..."

            if r.source_url:
                link = f"🔗 [View on {source_label}]({r.source_url})"
            else:
                link = ""
            embed.add_embed_field(
                name=f"NEW: {source_label} — {r.author}",
                value=f"{excerpt}\n{link}",
                inline=False,
            )

        embed.set_footer(
            text=(
                "ICE Activity Monitor | Unverified community reporting | "
                "Confirm before acting"
            )
        )
        embed.set_timestamp(datetime.now(timezone.utc).isoformat())

        return embed

    def _build_embed(self, incident: CorroboratedIncident) -> DiscordEmbed:
        if incident.notification_type == "update":
            return self._build_update_embed(incident)
        return self._build_new_incident_embed(incident)

    async def send(self, incident: CorroboratedIncident) -> bool:
        """Send an incident to Discord. Returns True if at least one method succeeds.

        Sends via BOTH webhook and bot if both are configured:
        - Webhook: Your personal/admin channel
        - Bot: All subscribed community servers
        """
        if self.dry_run:
            ntype = incident.notification_type.upper()
            methods = []
            if self._use_webhook:
                methods.append("webhook")
            if self._use_bot:
                methods.append("bot")
            logger.info(
                "[discord] DRY RUN: Would send %s for cluster %d (%s, %d sources) via %s",
                ntype,
                incident.cluster_id,
                incident.primary_location,
                incident.source_count,
                " + ".join(methods) or "nothing configured",
            )
            return True

        success = False

        logger.info(
            "[discord] SENDING notification for cluster %d: %s (%d sources)",
            incident.cluster_id,
            incident.primary_location,
            incident.source_count,
        )

        # Send via webhook (your personal channel)
        if self._use_webhook:
            logger.info("[discord] Attempting webhook send...")
            webhook_ok = await self._send_via_webhook(incident)
            logger.info("[discord] Webhook result: %s", webhook_ok)
            success = success or webhook_ok

        # Send via bot (all subscribed servers)
        if self._use_bot:
            logger.info("[discord] Attempting bot send...")
            bot_ok = await self._send_via_bot(incident)
            logger.info("[discord] Bot result: %s", bot_ok)
            success = success or bot_ok

        return success

    async def _send_via_bot(self, incident: CorroboratedIncident) -> bool:
        """Send alert via bot to all subscribed channels."""
        try:
            from notifications.discord_bot import send_alert, _bot_instance

            # Bot must be running to send alerts
            if _bot_instance is None:
                logger.debug(
                    "[discord] Bot not running - start with run_bot.py or remove "
                    "DISCORD_BOT_TOKEN from .env to disable bot mode"
                )
                return False

            count = await send_alert(incident)
            if count > 0:
                logger.info(
                    "[discord] Bot sent %s notification to %d channels for cluster %d",
                    incident.notification_type,
                    count,
                    incident.cluster_id,
                )
                return True
            else:
                logger.debug("[discord] Bot mode: no subscribed channels to notify")
                return False
        except ImportError as e:
            logger.warning("[discord] Bot mode unavailable: %s", e)
            return False
        except Exception:
            logger.exception("[discord] Error sending via bot")
            return False

    async def _send_via_webhook(self, incident: CorroboratedIncident) -> bool:
        """Send alert via webhook to single channel."""
        embed = self._build_embed(incident)

        if not self.webhook_url:
            logger.warning("[discord] No webhook URL configured, skipping")
            return False

        try:
            webhook = DiscordWebhook(
                url=self.webhook_url,
                username="ICE Activity Monitor",
            )
            webhook.add_embed(embed)
            response = webhook.execute()

            if response and response.status_code in (200, 204):
                logger.info(
                    "[discord] Webhook sent %s notification for cluster %d",
                    incident.notification_type,
                    incident.cluster_id,
                )
                return True
            else:
                status = response.status_code if response else "no response"
                logger.error(
                    "[discord] Failed to send webhook, status: %s", status
                )
                return False

        except Exception:
            logger.exception("[discord] Error sending webhook")
            return False
