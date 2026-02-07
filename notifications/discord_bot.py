"""Discord Bot for ICE Activity Alerts.

This module provides a Discord bot that can be invited to any server.
Users can subscribe their channels to receive ICE activity alerts.

Features:
- Invite bot to any server
- Subscribe/unsubscribe channels to alerts
- Filter by location (neighborhoods)
- View active alerts
- Admin commands for server owners

Commands:
    /ice subscribe       - Subscribe this channel to alerts
    /ice unsubscribe     - Unsubscribe this channel
    /ice status          - Show subscription status
    /ice alerts          - Show recent active alerts
    /ice help            - Show help message

Setup:
    1. Create a bot at https://discord.com/developers/applications
    2. Enable MESSAGE CONTENT INTENT in Bot settings
    3. Generate invite URL with permissions: Send Messages, Embed Links
    4. Set DISCORD_BOT_TOKEN in .env
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from storage.models import CorroboratedIncident

logger = logging.getLogger(__name__)

# File to persist subscribed channels across restarts
SUBSCRIPTIONS_FILE = Path("discord_subscriptions.json")


class ICEAlertBot(commands.Bot):
    """Discord bot for distributing ICE activity alerts."""

    def __init__(self, token: str, *, available_cities: list[str] | None = None):
        intents = discord.Intents.default()
        intents.message_content = True

        self.available_cities = available_cities or []

        super().__init__(
            command_prefix="!ice ",
            intents=intents,
            description="ICE Activity Monitor",
        )

        self.token = token
        self.subscribed_channels: dict[int, dict] = {}  # channel_id -> config
        self._load_subscriptions()

    def _load_subscriptions(self) -> None:
        """Load subscribed channels from disk."""
        if SUBSCRIPTIONS_FILE.exists():
            try:
                data = json.loads(SUBSCRIPTIONS_FILE.read_text())
                # Convert string keys back to int
                self.subscribed_channels = {
                    int(k): v for k, v in data.items()
                }
                logger.info(
                    "Loaded %d channel subscriptions", len(self.subscribed_channels)
                )
            except Exception as e:
                logger.error("Failed to load subscriptions: %s", e)
                self.subscribed_channels = {}

    def _save_subscriptions(self) -> None:
        """Save subscribed channels to disk."""
        try:
            SUBSCRIPTIONS_FILE.write_text(
                json.dumps(self.subscribed_channels, indent=2)
            )
        except Exception as e:
            logger.error("Failed to save subscriptions: %s", e)

    async def setup_hook(self) -> None:
        """Called when the bot is starting up."""
        # Add the cog with slash commands
        await self.add_cog(ICECommands(self))
        # Sync slash commands with Discord
        await self.tree.sync()
        logger.info("Slash commands synced")

    async def on_ready(self) -> None:
        """Called when the bot is fully connected."""
        logger.info("Bot is ready! Logged in as %s", self.user)
        logger.info("Bot is in %d servers", len(self.guilds))
        logger.info("Subscribed channels: %d", len(self.subscribed_channels))

        # Set presence
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name="for ICE activity | /ice help"
        )
        await self.change_presence(activity=activity)

    def subscribe_channel(
        self,
        channel_id: int,
        guild_id: int,
        guild_name: str,
        channel_name: str,
        subscribed_by: int,
        city: str = "",
        location_filter: Optional[str] = None,
    ) -> bool:
        """Subscribe a channel to receive alerts for a specific city."""
        if channel_id in self.subscribed_channels:
            return False  # Already subscribed

        self.subscribed_channels[channel_id] = {
            "guild_id": guild_id,
            "guild_name": guild_name,
            "channel_name": channel_name,
            "subscribed_by": subscribed_by,
            "subscribed_at": datetime.now(timezone.utc).isoformat(),
            "city": city,
            "location_filter": location_filter,
            "alert_count": 0,
        }
        self._save_subscriptions()
        return True

    def unsubscribe_channel(self, channel_id: int) -> bool:
        """Unsubscribe a channel from alerts."""
        if channel_id not in self.subscribed_channels:
            return False

        del self.subscribed_channels[channel_id]
        self._save_subscriptions()
        return True

    async def broadcast_alert(self, incident: CorroboratedIncident) -> int:
        """Send an alert to all subscribed channels.

        Returns the number of channels successfully notified.
        """
        embed = self._build_embed(incident)
        success_count = 0
        failed_channels = []

        for channel_id, config in list(self.subscribed_channels.items()):
            # Check city filter
            sub_city = config.get("city", "")
            if sub_city and incident.city:
                if sub_city.lower() != incident.city.lower():
                    continue  # Skip - wrong city

            # Check neighborhood location filter
            location_filter = config.get("location_filter")
            if location_filter:
                if location_filter.lower() not in incident.primary_location.lower():
                    continue  # Skip - doesn't match filter

            try:
                channel = self.get_channel(channel_id)
                if channel is None:
                    channel = await self.fetch_channel(channel_id)

                if channel and isinstance(channel, discord.TextChannel):
                    await channel.send(embed=embed)
                    success_count += 1

                    # Update alert count
                    config["alert_count"] = config.get("alert_count", 0) + 1
                    config["last_alert"] = datetime.now(timezone.utc).isoformat()

            except discord.Forbidden:
                logger.warning(
                    "No permission to post in channel %d (%s)",
                    channel_id, config.get("channel_name", "unknown")
                )
                failed_channels.append(channel_id)
            except discord.NotFound:
                logger.warning("Channel %d no longer exists", channel_id)
                failed_channels.append(channel_id)
            except Exception as e:
                logger.error("Error sending to channel %d: %s", channel_id, e)

        # Clean up failed channels
        for channel_id in failed_channels:
            if channel_id in self.subscribed_channels:
                del self.subscribed_channels[channel_id]

        if failed_channels:
            self._save_subscriptions()

        logger.info(
            "Broadcast alert to %d/%d channels",
            success_count, len(self.subscribed_channels)
        )
        return success_count

    def _build_embed(self, incident: CorroboratedIncident) -> discord.Embed:
        """Build a Discord embed for an incident."""
        is_update = incident.notification_type == "update"

        if is_update:
            title = f"🔄 UPDATE: ICE Activity - {incident.primary_location}"
            color = discord.Color.orange()
        else:
            title = f"🚨 ICE Activity Reported - {incident.primary_location}"
            color = discord.Color.red()

        embed = discord.Embed(
            title=title,
            color=color,
            timestamp=incident.latest_report,
        )

        # Confidence indicator
        conf = incident.confidence_score
        if conf >= 0.7:
            conf_text = "🟢 High"
        elif conf >= 0.5:
            conf_text = "🟡 Medium"
        else:
            conf_text = "🟠 Low"

        embed.add_field(
            name="Confidence",
            value=f"{conf_text} ({conf:.0%})",
            inline=True,
        )

        embed.add_field(
            name="Sources",
            value=f"{incident.source_count} report(s)",
            inline=True,
        )

        embed.add_field(
            name="Platforms",
            value=", ".join(sorted(incident.unique_source_types)),
            inline=True,
        )

        # Report summaries
        reports_to_show = incident.new_reports if is_update else incident.reports
        summaries = []
        for r in reports_to_show[:3]:
            text = r.cleaned_text or r.original_text
            snippet = text[:150] + "..." if len(text) > 150 else text
            source_name = r.source_type.capitalize()
            summaries.append(f"**[{source_name}]** {snippet}")

        if summaries:
            label = "New Reports" if is_update else "Reports"
            embed.add_field(
                name=label,
                value="\n\n".join(summaries),
                inline=False,
            )

        # Coordinates link if available
        if incident.latitude and incident.longitude:
            maps_url = (
                f"https://www.google.com/maps/search/?api=1"
                f"&query={incident.latitude},{incident.longitude}"
            )
            embed.add_field(
                name="Location",
                value=f"[View on Map]({maps_url})",
                inline=True,
            )

        # Footer
        city_label = incident.city.title() if incident.city else "ICE"
        embed.set_footer(
            text=f"{city_label} ICE Monitor | Stay safe, know your rights"
        )

        return embed

    async def start_bot(self) -> None:
        """Start the bot."""
        await self.start(self.token)


class ICECommands(commands.Cog):
    """Slash commands for the ICE Alert Bot."""

    def __init__(self, bot: ICEAlertBot):
        self.bot = bot

    @app_commands.command(name="subscribe", description="Subscribe this channel to ICE activity alerts")
    @app_commands.describe(
        city="Which city to receive alerts for",
        location="Optional: Filter alerts to a specific neighborhood",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def subscribe(
        self,
        interaction: discord.Interaction,
        city: str,
        location: Optional[str] = None,
    ):
        """Subscribe the current channel to alerts for a specific city."""
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "This command only works in text channels.",
                ephemeral=True,
            )
            return

        # Validate city
        valid_cities = {c.lower(): c for c in self.bot.available_cities}
        if city.lower() not in valid_cities:
            cities_list = ", ".join(c.title() for c in sorted(self.bot.available_cities))
            await interaction.response.send_message(
                f"Unknown city: **{city}**\n"
                f"Available cities: {cities_list}\n"
                f"Use `/ice cities` to see the full list.",
                ephemeral=True,
            )
            return

        # Use the canonical name
        city_key = valid_cities[city.lower()]

        success = self.bot.subscribe_channel(
            channel_id=channel.id,
            guild_id=interaction.guild_id,
            guild_name=interaction.guild.name if interaction.guild else "Unknown",
            channel_name=channel.name,
            subscribed_by=interaction.user.id,
            city=city_key,
            location_filter=location,
        )

        if success:
            filter_msg = f" (neighborhood: {location})" if location else ""
            await interaction.response.send_message(
                f"This channel is now subscribed to ICE activity alerts "
                f"for **{city_key.title()}**{filter_msg}.\n\n"
                f"Use `/ice unsubscribe` to stop receiving alerts.",
                ephemeral=False,
            )
            logger.info(
                "Channel subscribed: %s in %s for city %s (by user %d)",
                channel.name,
                interaction.guild.name if interaction.guild else "DM",
                city_key,
                interaction.user.id,
            )
        else:
            await interaction.response.send_message(
                "This channel is already subscribed to alerts.\n"
                "Use `/ice unsubscribe` first, then re-subscribe with a different city.",
                ephemeral=True,
            )

    @subscribe.autocomplete("city")
    async def city_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Provide autocomplete choices for the city parameter."""
        return [
            app_commands.Choice(name=c.title(), value=c)
            for c in self.bot.available_cities
            if current.lower() in c.lower()
        ][:25]

    @app_commands.command(name="unsubscribe", description="Unsubscribe this channel from ICE alerts")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def unsubscribe(self, interaction: discord.Interaction):
        """Unsubscribe the current channel from alerts."""
        success = self.bot.unsubscribe_channel(interaction.channel_id)

        if success:
            await interaction.response.send_message(
                "✅ This channel has been unsubscribed from ICE activity alerts.",
                ephemeral=False,
            )
            logger.info(
                "Channel unsubscribed: %d (by user %d)",
                interaction.channel_id,
                interaction.user.id,
            )
        else:
            await interaction.response.send_message(
                "ℹ️ This channel is not currently subscribed to alerts.",
                ephemeral=True,
            )

    @app_commands.command(name="status", description="View this channel's subscription status")
    async def status(self, interaction: discord.Interaction):
        """Show subscription status for the current channel."""
        config = self.bot.subscribed_channels.get(interaction.channel_id)

        if config:
            subscribed_at = config.get("subscribed_at", "Unknown")
            city = config.get("city", "All cities")
            location_filter = config.get("location_filter", "")
            alert_count = config.get("alert_count", 0)
            last_alert = config.get("last_alert", "Never")

            embed = discord.Embed(
                title="Subscription Status",
                color=discord.Color.green(),
            )
            embed.add_field(name="Status", value="Subscribed", inline=True)
            embed.add_field(name="City", value=city.title() if city else "All cities", inline=True)
            embed.add_field(name="Neighborhood Filter", value=location_filter or "All", inline=True)
            embed.add_field(name="Alerts Received", value=str(alert_count), inline=True)
            embed.add_field(name="Subscribed Since", value=subscribed_at[:10], inline=True)
            embed.add_field(name="Last Alert", value=last_alert[:10] if last_alert != "Never" else "Never", inline=True)

            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(
                "This channel is not subscribed to ICE alerts.\n"
                "Use `/ice subscribe <city>` to start receiving alerts.",
                ephemeral=True,
            )

    @app_commands.command(name="cities", description="List available cities for ICE alerts")
    async def cities(self, interaction: discord.Interaction):
        """List all available cities."""
        if not self.bot.available_cities:
            await interaction.response.send_message(
                "No cities are currently configured.", ephemeral=True
            )
            return

        cities_list = "\n".join(f"- {c.title()}" for c in sorted(self.bot.available_cities))
        await interaction.response.send_message(
            f"**Available cities:**\n{cities_list}\n\n"
            f"Use `/ice subscribe <city>` to subscribe this channel.",
            ephemeral=True,
        )

    @app_commands.command(name="help", description="Show help for ICE Alert Bot")
    async def help(self, interaction: discord.Interaction):
        """Show help message."""
        cities_list = ", ".join(c.title() for c in sorted(self.bot.available_cities))

        embed = discord.Embed(
            title="ICE Activity Monitor",
            description=(
                "This bot monitors multiple sources for ICE enforcement activity "
                "and sends alerts to subscribed channels.\n\n"
                f"**Available cities:** {cities_list}"
            ),
            color=discord.Color.blue(),
        )

        embed.add_field(
            name="Commands",
            value=(
                "`/ice subscribe <city> [location]` - Subscribe this channel to alerts\n"
                "`/ice unsubscribe` - Unsubscribe this channel\n"
                "`/ice status` - View subscription status\n"
                "`/ice cities` - List available cities\n"
                "`/ice help` - Show this help message"
            ),
            inline=False,
        )

        embed.add_field(
            name="Data Sources",
            value=(
                "- **Iceout.org** - Community reports\n"
                "- **StopICE.net** - Alert network\n"
                "- **Bluesky** - Social media\n"
                "- **Instagram** - Community orgs\n"
                "- **Twitter/X** - News & officials"
            ),
            inline=False,
        )

        embed.add_field(
            name="Permissions",
            value="Requires **Manage Channels** permission to subscribe/unsubscribe.",
            inline=False,
        )

        embed.set_footer(text="Stay safe. Know your rights.")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @subscribe.error
    @unsubscribe.error
    @cities.error
    async def permission_error(self, interaction: discord.Interaction, error):
        """Handle permission errors."""
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "❌ You need **Manage Channels** permission to use this command.",
                ephemeral=True,
            )
        else:
            logger.error("Command error: %s", error)
            await interaction.response.send_message(
                "❌ An error occurred. Please try again later.",
                ephemeral=True,
            )


# ─────────────────────────────────────────────────────────────────────
# Integration with main.py
# ─────────────────────────────────────────────────────────────────────

_bot_instance: Optional[ICEAlertBot] = None


def _set_bot_instance(bot: ICEAlertBot) -> None:
    """Set the global bot instance (called by main.py)."""
    global _bot_instance
    _bot_instance = bot
    logger.info("Bot instance registered for alert broadcasting")


async def init_bot(token: str, *, available_cities: list[str] | None = None) -> ICEAlertBot:
    """Initialize and return the bot instance."""
    global _bot_instance
    if _bot_instance is None:
        _bot_instance = ICEAlertBot(token, available_cities=available_cities)
    return _bot_instance


async def send_alert(incident: CorroboratedIncident) -> int:
    """Send an alert to all subscribed channels.

    This is called by the notifier when an incident is detected.
    Returns the number of channels notified.
    """
    global _bot_instance
    if _bot_instance is None:
        logger.warning("Bot not initialized, cannot send alert")
        return 0
    return await _bot_instance.broadcast_alert(incident)


def get_invite_url(client_id: str) -> str:
    """Generate the bot invite URL."""
    permissions = discord.Permissions(
        send_messages=True,
        embed_links=True,
        read_message_history=True,
        use_application_commands=True,
    )
    return discord.utils.oauth_url(client_id, permissions=permissions)
