#!/usr/bin/env python
"""Run the Discord bot standalone.

The bot needs to run as a separate process to:
1. Connect to Discord and stay online
2. Handle slash commands (/ice subscribe, etc.)
3. Receive alerts from the main monitor

Usage:
    python run_bot.py

The main monitor (main.py) will automatically send alerts to the bot
when it's running. You can run both simultaneously:

    Terminal 1: python run_bot.py
    Terminal 2: python main.py
"""

import asyncio
import logging
import sys

from config import load_config
from notifications.discord_bot import ICEAlertBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("discord_bot")


async def main():
    config = load_config()

    if not config.discord_bot_token:
        print("ERROR: DISCORD_BOT_TOKEN not set in .env")
        print()
        print("To set up the bot:")
        print("1. Go to https://discord.com/developers/applications")
        print("2. Create a new application")
        print("3. Go to Bot -> Add Bot")
        print("4. Enable MESSAGE CONTENT INTENT")
        print("5. Copy the token and add to .env:")
        print("   DISCORD_BOT_TOKEN=your_token_here")
        print()
        print("To generate an invite URL:")
        print("1. Go to OAuth2 -> URL Generator")
        print("2. Select scopes: bot, applications.commands")
        print("3. Select permissions: Send Messages, Embed Links")
        print("4. Copy the URL and share it!")
        sys.exit(1)

    locale = config.locale

    print("=" * 60)
    print(f"{locale.display_name} ICE Activity Monitor - Discord Bot")
    print("=" * 60)
    print()
    print("Starting bot...")
    print("Once running, users can invite the bot and use:")
    print("  /ice subscribe - Subscribe channel to alerts")
    print("  /ice unsubscribe - Unsubscribe channel")
    print("  /ice status - View subscription status")
    print("  /ice help - Show help")
    print()
    print("Press Ctrl+C to stop")
    print("=" * 60)

    bot = ICEAlertBot(
        config.discord_bot_token,
        locale_name=locale.display_name,
        locale_area=locale.fallback_location,
    )

    try:
        await bot.start_bot()
    except KeyboardInterrupt:
        logger.info("Shutting down bot...")
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
