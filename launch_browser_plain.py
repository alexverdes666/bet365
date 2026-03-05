from camoufox.sync_api import Camoufox
from datetime import datetime
import time

# Market sections (Bulgarian)
MARKET_SECTIONS = [
    "Краен Резултат",    # Final Result
    "Следващ Гол",       # Next Goal
    "Голове в Мача",     # Goals in Match
    "Азиатски линии",    # Asian Lines
]

# Target div class to track
TARGET_SELECTOR = "div.ovm-OverviewView_Classification.ovm-OverviewView_Classification-1.ovm-Classification"


def click_market_tab(page, market_name: str) -> bool:
    """Click on a market section tab to switch to that market view."""
    try:
        tab_selector = f"div.ovm-ClassificationMarketSwitcherMenu_Item:has-text('{market_name}')"
        tab = page.query_selector(tab_selector)
        if tab:
            tab.click()
            time.sleep(0.3)
            return True
    except Exception as e:
        print(f"Could not click tab '{market_name}': {e}")
    return False


def scrape_all_sections(url: str, headless: bool = False):
    """
    Launch browser with 4 pages, each monitoring a different market section.
    Cycles through all pages rapidly to simulate simultaneous monitoring.
    Press Ctrl+C to stop.
    """
    previous_texts = {market: None for market in MARKET_SECTIONS}

    with Camoufox(headless=headless) as browser:
        print(f"Navigating to {url}...")
        print(f"Opening 4 pages for monitoring...")

        # Create all pages and set each to a different market tab
        pages = {}
        for market in MARKET_SECTIONS:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_selector(TARGET_SELECTOR, timeout=30000)
                if click_market_tab(page, market):
                    pages[market] = page
                    print(f"[{market}] Ready")
                else:
                    print(f"[{market}] Could not select tab")
                    page.close()
            except Exception as e:
                print(f"[{market}] Failed to load: {e}")
                page.close()
            time.sleep(0.5)

        if not pages:
            print("No pages could be initialized. Exiting.")
            return

        print(f"\nMonitoring {len(pages)} sections. Press Ctrl+C to stop.\n")
        print("=" * 60)

        try:
            while True:
                for market, page in pages.items():
                    element = page.query_selector(TARGET_SELECTOR)
                    if element:
                        text = element.inner_text()

                        if text != previous_texts[market]:
                            timestamp = datetime.now().strftime("%H:%M:%S")
                            print(f"\n[{timestamp}] [{market}]")
                            print("-" * 60)
                            print(text)
                            print("-" * 60)
                            previous_texts[market] = text

                time.sleep(0.5)  # Small delay between full cycles

        except KeyboardInterrupt:
            print("\nStopped.")

        finally:
            for page in pages.values():
                page.close()


if __name__ == "__main__":
    url = input("Enter website URL: ").strip()
    if not url:
        print("No URL provided. Exiting.")
        exit(1)

    scrape_all_sections(url)
