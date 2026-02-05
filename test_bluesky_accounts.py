"""Test script to find Bluesky accounts for stale Twitter users."""

import asyncio
import aiohttp
import json
from pathlib import Path

BSKY_PUBLIC_API = "https://public.api.bsky.app"

# Stale Twitter accounts to search for on Bluesky
# These are accounts that were stale/inactive on Twitter
ACCOUNTS_TO_FIND = [
    # Journalists
    "maxnesterak",
    "deenafaywinter",
    "MWilliamsonMN",
    "MNReformer",
    "rachstou",
    "Ibrahim_Hirsi",
    "NickValencia",
    # Community orgs
    "UnitedWeDream",
    "LORICHALEZE",
    "TCAILAction",
    "miracmn",
    "ConMijente",
    "defend612",
    "the5051",
    "SunriseMVMT",
    "IndivisibleTeam",
    # News
    "MPRnews",
    "kaboremn",  # KARE 11
    "BringMeTheNews",
    "SahanJournal",
    # Officials
    "MplsMayor",
    "MayorKaohly",
]

# Common Bluesky handle patterns to try
def get_possible_handles(username: str) -> list[str]:
    """Generate possible Bluesky handles for a username."""
    base = username.lower()
    return [
        f"{base}.bsky.social",
        f"{base}news.bsky.social",
        f"{base}mn.bsky.social",
    ]


async def check_handle(session: aiohttp.ClientSession, handle: str) -> dict | None:
    """Check if a Bluesky handle exists and get profile info."""
    url = f"{BSKY_PUBLIC_API}/xrpc/app.bsky.actor.getProfile"
    try:
        async with session.get(url, params={"actor": handle}, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                return {
                    "handle": data.get("handle"),
                    "displayName": data.get("displayName"),
                    "description": data.get("description", "")[:100],
                    "followersCount": data.get("followersCount", 0),
                    "postsCount": data.get("postsCount", 0),
                }
            return None
    except Exception:
        return None


async def search_users(session: aiohttp.ClientSession, query: str) -> list[dict]:
    """Search Bluesky for users matching a query."""
    url = f"{BSKY_PUBLIC_API}/xrpc/app.bsky.actor.searchActors"
    try:
        async with session.get(url, params={"q": query, "limit": 10}, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("actors", [])
            return []
    except Exception:
        return []


async def main():
    print("=" * 70)
    print("Bluesky Account Search")
    print("=" * 70)
    print(f"Searching for {len(ACCOUNTS_TO_FIND)} accounts...")
    print("=" * 70)
    print()

    found_accounts = []
    not_found = []

    async with aiohttp.ClientSession() as session:
        for username in ACCOUNTS_TO_FIND:
            print(f"Searching for: {username}")

            # Try exact handle matches first
            found = False
            for handle in get_possible_handles(username):
                profile = await check_handle(session, handle)
                if profile:
                    print(f"  [FOUND] {profile['handle']}")
                    print(f"          Name: {profile['displayName']}")
                    print(f"          Posts: {profile['postsCount']}, Followers: {profile['followersCount']}")
                    found_accounts.append({
                        "twitter": username,
                        "bluesky": profile["handle"],
                        "displayName": profile["displayName"],
                        "postsCount": profile["postsCount"],
                        "followersCount": profile["followersCount"],
                    })
                    found = True
                    break
                await asyncio.sleep(0.3)

            # If not found by handle, try search
            if not found:
                results = await search_users(session, username)
                if results:
                    # Show top results
                    print(f"  [SEARCH] Found {len(results)} potential matches:")
                    for actor in results[:3]:
                        h = actor.get("handle", "")
                        name = actor.get("displayName", "").encode('ascii', 'ignore').decode()
                        posts = actor.get("postsCount", 0)
                        print(f"           - {h} ({name}) - {posts} posts")

                        # Check if it looks like a match
                        if username.lower() in h.lower() or username.lower() in name.lower():
                            found_accounts.append({
                                "twitter": username,
                                "bluesky": h,
                                "displayName": name,
                                "postsCount": posts,
                                "followersCount": actor.get("followersCount", 0),
                                "searchMatch": True,
                            })
                            found = True
                else:
                    print(f"  [NOT FOUND] No matches")

            if not found:
                not_found.append(username)

            print()
            await asyncio.sleep(0.5)

    # Summary
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"\nFound {len(found_accounts)} accounts on Bluesky:")
    for acc in found_accounts:
        search_note = " (search match)" if acc.get("searchMatch") else ""
        print(f"  @{acc['twitter']} -> {acc['bluesky']}{search_note}")
        print(f"      {acc['displayName']} - {acc['postsCount']} posts")

    print(f"\nNot found ({len(not_found)}):")
    for u in not_found:
        print(f"  @{u}")

    # Save results
    results = {
        "found": found_accounts,
        "not_found": not_found,
    }
    Path("bluesky_search_results.json").write_text(json.dumps(results, indent=2))
    print("\n[*] Results saved to bluesky_search_results.json")


if __name__ == "__main__":
    asyncio.run(main())
