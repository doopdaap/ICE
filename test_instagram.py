"""Test Instagram collector."""

import asyncio
from collectors.instagram_collector import InstagramCollector, MONITORED_ACCOUNTS
from config import load_config

async def main():
    print("=" * 60)
    print("Instagram Collector Test")
    print("=" * 60)
    print(f"Monitoring {len(MONITORED_ACCOUNTS)} accounts:")
    for acc in MONITORED_ACCOUNTS:
        print(f"  - @{acc}")
    print("=" * 60)
    print()

    config = load_config()
    collector = InstagramCollector(config, asyncio.Queue())

    print("[*] Launching browser...")
    if not await collector._ensure_browser():
        print("[ERROR] Failed to launch browser")
        return

    print("[*] Browser ready. Testing profile scraping...")
    print()

    for username in MONITORED_ACCOUNTS:
        print(f"[*] Scraping @{username}...")
        try:
            posts = await collector._scrape_profile(username)
            if posts:
                print(f"    [OK] Found {len(posts)} posts")
                # Show first post preview
                first = posts[0]
                text_raw = first.get("text", "") or ""
                text_clean = text_raw.encode('ascii', 'ignore').decode()[:80]
                text_preview = (text_clean + "...") if text_clean else "(no caption)"
                print(f"    Latest: {text_preview}")
            else:
                print(f"    [!] No posts found (may require login or account is private)")
        except Exception as e:
            print(f"    [ERROR] {e}")
        print()
        await asyncio.sleep(2)

    print("[*] Closing browser...")
    await collector.cleanup()
    print("[*] Done!")


if __name__ == "__main__":
    asyncio.run(main())
