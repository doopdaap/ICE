"""Quick test script to validate Twitter account checking logic."""

import asyncio
import json
from pathlib import Path

# Test ALL accounts from the collector (including GPT Research additions)
from collectors.twitter_collector import MONITORED_ACCOUNTS
TEST_ACCOUNTS = list(MONITORED_ACCOUNTS)

async def main():
    # Import after setting up path
    from collectors.twitter_collector import (
        TwitterCollector,
        ACCOUNT_CACHE_FILE,
        ACCOUNT_STALE_DAYS,
    )
    from config import load_config

    config = load_config()

    print("=" * 60)
    print("Twitter Account Validation Test")
    print("=" * 60)
    print(f"Cache file: {ACCOUNT_CACHE_FILE}")
    print(f"Stale threshold: {ACCOUNT_STALE_DAYS} days")
    print(f"Testing {len(TEST_ACCOUNTS)} accounts: {TEST_ACCOUNTS}")
    print("=" * 60)
    print()

    # Create collector instance
    collector = TwitterCollector(config, asyncio.Queue())

    # Ensure browser is ready
    print("[*] Launching browser...")
    if not await collector._ensure_browser():
        print("[ERROR] Failed to launch browser")
        return

    print("[*] Browser ready. Starting account validation...")
    print()

    results = {}
    for i, account in enumerate(TEST_ACCOUNTS):
        print(f"[{i+1}/{len(TEST_ACCOUNTS)}] Checking @{account}...")

        status = await collector._check_account_status(account)
        results[account] = status

        # Print result
        if not status["exists"]:
            print(f"    [X] DOES NOT EXIST: {status.get('error', 'Unknown error')}")
        elif status["is_stale"]:
            days = status.get("days_since_last_post", "?")
            print(f"    [!] STALE: Last post {days} days ago")
        else:
            days = status.get("days_since_last_post", "?")
            print(f"    [OK] ACTIVE: Last post {days} days ago")

        if status.get("error") and status["exists"]:
            print(f"    Note: {status['error']}")

        print()
        await asyncio.sleep(1)

    # Summary
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    active = [a for a, s in results.items() if s["exists"] and not s["is_stale"]]
    stale = [a for a, s in results.items() if s["exists"] and s["is_stale"]]
    missing = [a for a, s in results.items() if not s["exists"]]

    print(f"Active accounts ({len(active)}): {active}")
    print(f"Stale accounts ({len(stale)}): {stale}")
    print(f"Missing accounts ({len(missing)}): {missing}")
    print()

    # Save test results
    test_cache = {
        "test_run": True,
        "accounts_tested": TEST_ACCOUNTS,
        "results": results,
    }
    Path("test_twitter_results.json").write_text(json.dumps(test_cache, indent=2))
    print("[*] Results saved to test_twitter_results.json")

    # Cleanup
    print("[*] Closing browser...")
    await collector.cleanup()
    print("[*] Done!")


if __name__ == "__main__":
    asyncio.run(main())
