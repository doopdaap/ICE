"""Search for active Minneapolis ICE-related Instagram accounts."""

import asyncio
from playwright.async_api import async_playwright

# Alternative accounts to check
ACCOUNTS_TO_CHECK = [
    # Original accounts that might have different handles
    "defend612",
    "defend.612",
    "defend_612",
    "612defend",
    "the5051",
    "the_5051",
    "5051movement",
    # Other potential MN activist accounts
    "mnimmigrantrights",
    "miloandme_mn",
    "navigatemn",
    "isaiah_mn",
    "mnfreedomfund",
    "aabortusc",  # Abort the Supreme Court
]

async def check_account(context, username: str) -> dict:
    """Check if an Instagram account exists and is accessible."""
    page = await context.new_page()
    result = {
        "username": username,
        "exists": False,
        "is_private": False,
        "post_count": 0,
        "error": None,
    }

    try:
        url = f"https://www.instagram.com/{username}/"
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)

        # Dismiss login modal
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
        except Exception:
            pass

        content = await page.content()

        if "Sorry, this page isn't available" in content:
            result["error"] = "not found"
            return result

        result["exists"] = True

        if "This account is private" in content or "This Account is Private" in content:
            result["is_private"] = True
            return result

        # Try to get post count
        try:
            post_count_text = await page.evaluate("""
                () => {
                    const metas = document.querySelectorAll('meta[property="og:description"]');
                    for (const meta of metas) {
                        const content = meta.getAttribute('content') || '';
                        const match = content.match(/([\d,]+) Posts/i);
                        if (match) return match[1].replace(',', '');
                    }
                    // Try finding in page text
                    const spans = Array.from(document.querySelectorAll('span'));
                    for (const span of spans) {
                        const text = span.textContent || '';
                        if (text.includes(' posts')) {
                            const match = text.match(/([\d,]+)/);
                            if (match) return match[1].replace(',', '');
                        }
                    }
                    return '0';
                }
            """)
            result["post_count"] = int(post_count_text) if post_count_text else 0
        except Exception:
            pass

        return result

    except Exception as e:
        result["error"] = str(e)
        return result
    finally:
        await page.close()


async def main():
    print("=" * 60)
    print("Instagram Account Search")
    print("=" * 60)
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        found_accounts = []

        for username in ACCOUNTS_TO_CHECK:
            print(f"Checking @{username}...", end=" ")
            result = await check_account(context, username)

            if result["error"] == "not found":
                print("[NOT FOUND]")
            elif result["is_private"]:
                print("[PRIVATE]")
            elif result["exists"]:
                posts = result["post_count"]
                print(f"[FOUND] {posts} posts")
                if posts > 0:
                    found_accounts.append(result)
            else:
                print(f"[ERROR] {result['error']}")

            await asyncio.sleep(1)

        await browser.close()

    print()
    print("=" * 60)
    print("ACTIVE PUBLIC ACCOUNTS FOUND:")
    print("=" * 60)
    for acc in found_accounts:
        print(f"  @{acc['username']} - {acc['post_count']} posts")

    if not found_accounts:
        print("  (none)")


if __name__ == "__main__":
    asyncio.run(main())
