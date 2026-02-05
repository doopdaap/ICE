"""Debug Instagram scraping for specific accounts."""

import asyncio
from playwright.async_api import async_playwright

ACCOUNTS_TO_CHECK = [
    "defend612",
    "the5051",
]

async def main():
    print("Checking Instagram accounts that failed...")
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # Visible browser
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        for username in ACCOUNTS_TO_CHECK:
            print(f"Checking @{username}...")
            page = await context.new_page()

            try:
                url = f"https://www.instagram.com/{username}/"
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(5)

                content = await page.content()

                # Check various states
                if "Sorry, this page isn't available" in content:
                    print(f"  -> Account does not exist")
                elif "This Account is Private" in content:
                    print(f"  -> Account is PRIVATE (requires follow)")
                elif "Log in" in content and "to see photos" in content.lower():
                    print(f"  -> Requires login to view")
                else:
                    # Try to find post count
                    post_count = await page.evaluate("""
                        () => {
                            // Look for post count in meta or spans
                            const spans = document.querySelectorAll('span');
                            for (const span of spans) {
                                if (span.textContent && span.textContent.includes(' posts')) {
                                    return span.textContent;
                                }
                            }
                            return 'unknown';
                        }
                    """)
                    print(f"  -> Account appears accessible, posts: {post_count}")

                    # Check for login modal
                    login_modal = await page.query_selector('[role="dialog"]')
                    if login_modal:
                        print(f"  -> NOTE: Login modal detected (may block content)")

            except Exception as e:
                print(f"  -> Error: {e}")
            finally:
                await page.close()

            print()

        await browser.close()

    print("Done - browser closed")


if __name__ == "__main__":
    asyncio.run(main())
