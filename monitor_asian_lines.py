from camoufox.sync_api import Camoufox
from datetime import datetime
import time

MARKET_NAME = "Азиатски линии"  # Asian Lines
TARGET_SELECTOR = "div.ovm-OverviewView_Classification.ovm-OverviewView_Classification-1.ovm-Classification"


def click_market_tab(page, market_name: str) -> bool:
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


def monitor(url: str, headless: bool = False):
    previous_text = None

    with Camoufox(headless=headless) as browser:
        page = browser.new_page()
        print(f"[{MARKET_NAME}] Navigating to {url}...")
        page.goto(url, wait_until="domcontentloaded")

        try:
            page.wait_for_selector(TARGET_SELECTOR, timeout=30000)
            click_market_tab(page, MARKET_NAME)
            print(f"[{MARKET_NAME}] Monitoring started. Press Ctrl+C to stop.\n")

            while True:
                element = page.query_selector(TARGET_SELECTOR)
                if element:
                    text = element.inner_text()
                    if text != previous_text:
                        timestamp = datetime.now().strftime("%H:%M:%S")
                        print(f"\n[{timestamp}] [{MARKET_NAME}]")
                        print("-" * 60)
                        print(text)
                        print("-" * 60)
                        previous_text = text
                time.sleep(1)

        except KeyboardInterrupt:
            print("\nStopped.")
        except Exception as e:
            print(f"Error: {e}")


if __name__ == "__main__":
    url = input("Enter website URL: ").strip()
    if not url:
        print("No URL provided. Exiting.")
        exit(1)
    monitor(url)
