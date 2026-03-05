import asyncio
from camoufox.async_api import AsyncCamoufox
from datetime import datetime

# Selector for individual event detail market grid
EVENT_DETAIL_SELECTOR = "div.ipe-EventViewDetail_MarketGrid"

# Selector for fixture containers in the live view
FIXTURE_SELECTOR = "div.ovm-Fixture.ovm-Fixture-horizontal"

# Selector for the clickable area inside each fixture
FIXTURE_CLICK_SELECTOR = "div.ovm-FixtureDetailsTwoWay_Wrapper"

# Selector for the live events list
LIVE_LIST_SELECTOR = "div.ovm-CompetitionList"

# Resources to block for performance (keep CSS, block media only)
BLOCKED_RESOURCES = ["**/*.{png,jpg,jpeg,gif,svg,webp,mp4,webm,woff,woff2,ttf,eot,ico}"]

# Lock for thread-safe printing
print_lock = asyncio.Lock()


async def safe_print(*args):
    """Async-safe print."""
    async with print_lock:
        print(*args)


async def block_resources(page):
    """Block heavy resources to save bandwidth/CPU."""
    async def handle_route(route):
        await route.abort()

    for pattern in BLOCKED_RESOURCES:
        await page.route(pattern, handle_route)


async def get_fixture_info(fixture) -> dict:
    """Extract basic info from a fixture element for identification."""
    try:
        teams = await fixture.query_selector_all("div.ovm-FixtureDetailsTwoWay_TeamName")
        team1 = await teams[0].inner_text() if len(teams) > 0 else "Unknown"
        team2 = await teams[1].inner_text() if len(teams) > 1 else "Unknown"
        return {"team1": team1.strip(), "team2": team2.strip()}
    except Exception:
        return {"team1": "Unknown", "team2": "Unknown"}


async def monitor_single_match(context, base_url: str, fixture_index: int, match_key: str, start_delay: float = 0, max_retries: int = 3):
    """
    Monitor a single match in its own isolated context.
    Each match appears as a separate visitor.
    """
    # Stagger start to avoid rate limiting
    if start_delay > 0:
        await asyncio.sleep(start_delay)

    previous_data = ""
    page = None
    connected = False

    # Retry loop for initial connection
    for attempt in range(max_retries):
        try:
            if page:
                await page.close()
            page = await context.new_page()

            # Block heavy resources
            await block_resources(page)

            await safe_print(f"[{match_key}] Navigating (attempt {attempt + 1})...")
            await page.goto(base_url, timeout=90000, wait_until="domcontentloaded")

            # Wait a bit for JS to initialize
            await asyncio.sleep(3)

            # Wait for fixture list with longer timeout
            await page.wait_for_selector(LIVE_LIST_SELECTOR, timeout=60000)

            # Get fixtures and click on our assigned one
            fixtures = await page.query_selector_all(FIXTURE_SELECTOR)
            if fixture_index >= len(fixtures):
                await safe_print(f"[{match_key}] Fixture not found (index {fixture_index})")
                return

            click_target = await fixtures[fixture_index].query_selector(FIXTURE_CLICK_SELECTOR)
            if not click_target:
                await safe_print(f"[{match_key}] Click target not found")
                return

            await click_target.click()
            await asyncio.sleep(2)

            # Wait for event detail to load
            await page.wait_for_selector(EVENT_DETAIL_SELECTOR, timeout=20000)
            await safe_print(f"[{match_key}] Connected!")
            connected = True
            break  # Success, exit retry loop

        except Exception as e:
            await safe_print(f"[{match_key}] Attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(5)  # Wait before retry
            else:
                await safe_print(f"[{match_key}] All retries exhausted")
                return

    if not connected:
        return

    # Monitor loop (outside retry loop)
    try:
        while True:
            try:
                element = await page.query_selector(EVENT_DETAIL_SELECTOR)
                if element:
                    detail_text = await element.inner_text()

                    if detail_text and detail_text != previous_data:
                        timestamp = datetime.now().strftime("%H:%M:%S")
                        await safe_print(f"\n[{timestamp}] {match_key}")
                        await safe_print("=" * 60)
                        await safe_print(detail_text)
                        await safe_print("=" * 60)
                        previous_data = detail_text

            except Exception as e:
                await safe_print(f"[{match_key}] Scrape error: {e}")

            await asyncio.sleep(2)  # Poll interval

    except Exception as e:
        await safe_print(f"[{match_key}] Fatal error: {e}")
    finally:
        try:
            await page.close()
        except:
            pass


async def get_all_fixtures(browser, url: str, max_events: int) -> list:
    """
    Open a temporary page to get the list of all fixtures.
    Returns list of (index, match_key) tuples.
    """
    context = await browser.new_context()
    page = await context.new_page()

    try:
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_selector(LIVE_LIST_SELECTOR, timeout=30000)

        fixtures = await page.query_selector_all(FIXTURE_SELECTOR)
        fixture_count = min(len(fixtures), max_events)

        print(f"[Main] Found {len(fixtures)} fixtures, will monitor {fixture_count}")

        fixture_list = []
        for i in range(fixture_count):
            info = await get_fixture_info(fixtures[i])
            match_key = f"{info['team1']} vs {info['team2']}"
            fixture_list.append((i, match_key))
            print(f"  [{i+1}] {match_key}")

        return fixture_list

    finally:
        await page.close()
        await context.close()


async def main(url: str, max_events: int = 30, headless: bool = False):
    """
    Main entry point. Launches one browser and creates isolated contexts for each match.
    """
    print(f"[Main] Starting async monitor for up to {max_events} events...")

    async with AsyncCamoufox(headless=headless) as browser:
        print("[Main] Browser launched. Getting fixture list...")

        # Get list of fixtures
        fixture_list = await get_all_fixtures(browser, url, max_events)

        if not fixture_list:
            print("[Main] No fixtures found. Exiting.")
            return

        print(f"\n[Main] Spawning {len(fixture_list)} isolated contexts (staggered)...")

        # Create a separate context for each match (isolates cookies/sessions)
        # This prevents detection - each match appears as a distinct visitor
        # Stagger spawning by 2 seconds each to avoid rate limiting
        tasks = []
        for i, (fixture_index, match_key) in enumerate(fixture_list):
            context = await browser.new_context()
            start_delay = i * 2.0  # Stagger by 2 seconds
            task = monitor_single_match(context, url, fixture_index, match_key, start_delay)
            tasks.append(task)

        print(f"[Main] All contexts spawned. Monitoring... Press Ctrl+C to stop.\n")

        # Run all match monitors concurrently
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            print("\n[Main] Cancelled.")


if __name__ == "__main__":
    url = input("Enter website URL: ").strip()
    if not url:
        print("No URL provided. Exiting.")
        exit(1)

    max_events_input = input("Max events to monitor (default 30): ").strip()
    max_events = int(max_events_input) if max_events_input.isdigit() else 30

    headless_input = input("Headless mode? (y/N): ").strip().lower()
    headless = headless_input == 'y'

    try:
        asyncio.run(main(url, max_events, headless))
    except KeyboardInterrupt:
        print("\nStopped.")
