from bs4 import BeautifulSoup
from camoufox.sync_api import Camoufox

# Market section names (Bulgarian)
MARKET_SECTIONS = [
    "Краен Резултат",    # Final Result
    "Следващ Гол",       # Next Goal
    "Голове в Мача",     # Goals in Match
    "Азиатски линии",    # Asian Lines
]


def parse_market_odds(fixture_soup, market_type: str) -> dict:
    """
    Parse odds from a fixture based on the current market type.
    Returns a dict with the market-specific odds.
    """
    odds = {}

    if market_type == "Краен Резултат":
        # Final Result: home/draw/away odds
        odds_els = fixture_soup.find_all("span", class_="ovm-ParticipantOddsOnly_Odds")
        odds = {
            "home": odds_els[0].get_text(strip=True) if len(odds_els) > 0 else "",
            "draw": odds_els[1].get_text(strip=True) if len(odds_els) > 1 else "",
            "away": odds_els[2].get_text(strip=True) if len(odds_els) > 2 else "",
        }

    elif market_type == "Следващ Гол":
        # Next Goal: home/no goal/away with dynamic goal number
        market_group = fixture_soup.find("div", class_="ovm-MarketGroup")
        if market_group:
            header_el = market_group.find("div", class_="ovm-AlternativeMarketHeader")
            header = header_el.get_text(strip=True) if header_el else ""

            participants = market_group.find_all("div", class_="ovm-HorizontalMarket_Participant")
            participant_odds = []
            for p in participants:
                odds_el = p.find("span", class_="ovm-ParticipantNoGoal_Odds")
                is_suspended = "ovm-ParticipantNoGoal_Suspended" in p.get("class", [])
                no_goal_el = p.find("div", class_="ovm-ParticipantNoGoal_NoGoal")

                participant_odds.append({
                    "odds": odds_el.get_text(strip=True) if odds_el else "",
                    "suspended": is_suspended,
                    "is_no_goal": no_goal_el is not None,
                })

            odds = {
                "header": header,
                "home": participant_odds[0]["odds"] if len(participant_odds) > 0 else "",
                "no_goal": participant_odds[1]["odds"] if len(participant_odds) > 1 else "",
                "away": participant_odds[2]["odds"] if len(participant_odds) > 2 else "",
            }

    elif market_type == "Голове в Мача":
        # Goals in Match: over/under with line
        market_group = fixture_soup.find("div", class_="ovm-MarketGroup")
        if market_group:
            header_el = market_group.find("div", class_="ovm-AlternativeMarketHeader")
            header = header_el.get_text(strip=True) if header_el else ""

            odds_els = market_group.find_all("span", class_=lambda x: x and "Odds" in x)
            odds = {
                "header": header,
                "over": odds_els[0].get_text(strip=True) if len(odds_els) > 0 else "",
                "under": odds_els[1].get_text(strip=True) if len(odds_els) > 1 else "",
            }

    elif market_type == "Азиатски линии":
        # Asian Lines: handicap/total with line
        market_group = fixture_soup.find("div", class_="ovm-MarketGroup")
        if market_group:
            header_el = market_group.find("div", class_="ovm-AlternativeMarketHeader")
            header = header_el.get_text(strip=True) if header_el else ""

            odds_els = market_group.find_all("span", class_=lambda x: x and "Odds" in x)
            odds = {
                "header": header,
                "option1": odds_els[0].get_text(strip=True) if len(odds_els) > 0 else "",
                "option2": odds_els[1].get_text(strip=True) if len(odds_els) > 1 else "",
            }

    return odds


def parse_competition_html(html: str, market_type: str = "Краен Резултат") -> dict:
    """
    Parse the competition list HTML and extract structured data.
    """
    soup = BeautifulSoup(html, "html.parser")
    competitions = []

    for comp in soup.find_all("div", class_="ovm-Competition"):
        comp_name_el = comp.find("div", class_="ovm-CompetitionHeader_NameText")
        comp_name = comp_name_el.get_text(strip=True) if comp_name_el else "Unknown"

        fixtures = []
        for fixture in comp.find_all("div", class_="ovm-Fixture"):
            teams = fixture.find_all("div", class_="ovm-FixtureDetailsTwoWay_TeamName")
            team1 = teams[0].get_text(strip=True) if len(teams) > 0 else ""
            team2 = teams[1].get_text(strip=True) if len(teams) > 1 else ""

            score1_el = fixture.find("div", class_="ovm-StandardScoresSoccer_TeamOne")
            score2_el = fixture.find("div", class_="ovm-StandardScoresSoccer_TeamTwo")
            score1 = score1_el.get_text(strip=True) if score1_el else "0"
            score2 = score2_el.get_text(strip=True) if score2_el else "0"

            timer_el = fixture.find("div", class_="ovm-InPlayTimer")
            timer = timer_el.get_text(strip=True) if timer_el else ""

            # Parse odds based on current market type
            odds = parse_market_odds(fixture, market_type)

            fixture_data = {
                "team1": team1,
                "team2": team2,
                "score": f"{score1} - {score2}",
                "time": timer,
                "market_type": market_type,
                "odds": odds,
            }

            # Check for red cards
            if fixture.find("div", class_="ovm-FixtureDetailsTwoWay_RedCard"):
                fixture_data["redCard"] = True

            fixtures.append(fixture_data)

        competitions.append({"name": comp_name, "fixtures": fixtures})

    return {"competitions": competitions}


def get_fixture_key(fixture: dict) -> str:
    """Create a unique key for a fixture to track changes."""
    return f"{fixture['team1']} vs {fixture['team2']}"


def compare_iterations(prev: dict, curr: dict) -> list[dict]:
    """
    Compare two iterations and return list of changes.
    """
    changes = []

    # Build lookup for previous fixtures
    prev_fixtures = {}
    for comp in prev.get("competitions", []):
        for fixture in comp.get("fixtures", []):
            key = f"{comp['name']}|{get_fixture_key(fixture)}"
            prev_fixtures[key] = fixture

    # Compare with current fixtures
    for comp in curr.get("competitions", []):
        for fixture in comp.get("fixtures", []):
            key = f"{comp['name']}|{get_fixture_key(fixture)}"
            prev_fixture = prev_fixtures.get(key)

            if prev_fixture:
                # Check for changes
                if prev_fixture["score"] != fixture["score"]:
                    changes.append({
                        "type": "score_change",
                        "competition": comp["name"],
                        "match": get_fixture_key(fixture),
                        "old_score": prev_fixture["score"],
                        "new_score": fixture["score"],
                    })
                if prev_fixture["odds"] != fixture["odds"]:
                    changes.append({
                        "type": "odds_change",
                        "competition": comp["name"],
                        "match": get_fixture_key(fixture),
                        "old_odds": prev_fixture["odds"],
                        "new_odds": fixture["odds"],
                    })
                if prev_fixture["time"] != fixture["time"]:
                    changes.append({
                        "type": "time_change",
                        "competition": comp["name"],
                        "match": get_fixture_key(fixture),
                        "old_time": prev_fixture["time"],
                        "new_time": fixture["time"],
                    })
            else:
                # New fixture appeared
                changes.append({
                    "type": "new_fixture",
                    "competition": comp["name"],
                    "match": get_fixture_key(fixture),
                    "fixture": fixture,
                })

    return changes


def click_market_tab(page, market_name: str) -> bool:
    """
    Click on a market section tab to switch to that market view.
    Returns True if successful, False otherwise.
    """
    import time
    try:
        # Find the tab by text content
        tab_selector = f"div.ovm-ClassificationMarketSwitcherMenu_Item:has-text('{market_name}')"
        tab = page.query_selector(tab_selector)
        if tab:
            tab.click()
            time.sleep(0.3)  # Wait for UI to update
            return True
    except Exception as e:
        print(f"Could not click tab '{market_name}': {e}")
    return False


def scrape_all_markets(page) -> dict:
    """
    Scrape all market sections by clicking through each tab.
    Returns a dict with data for each market type.
    """
    import time
    all_data = {}

    for market in MARKET_SECTIONS:
        if click_market_tab(page, market):
            time.sleep(0.5)  # Wait for content to load
            element = page.query_selector("div.ovm-CompetitionList")
            if element:
                html = element.inner_html()
                result = parse_competition_html(html, market)
                all_data[market] = result

    return all_data


def scrape_realtime(url: str, headless: bool = False, markets: list = None):
    """
    Launch browser and continuously track changes in real-time.
    Press Ctrl+C to stop.

    Args:
        url: The URL to scrape
        headless: Run browser in headless mode
        markets: List of market sections to scrape. Defaults to all.
    """
    from datetime import datetime
    import time

    if markets is None:
        markets = MARKET_SECTIONS

    previous_results = {}  # Store previous results for each market

    with Camoufox(headless=headless) as browser:
        page = browser.new_page()

        print(f"Navigating to {url}...")
        page.goto(url, wait_until="domcontentloaded")

        try:
            page.wait_for_selector("div.ovm-CompetitionList", timeout=30000)
            print("Connected! Tracking live changes...")
            print(f"Scraping markets: {', '.join(markets)}")
            print("Press Ctrl+C to stop.\n")

            while True:
                for market in markets:
                    if click_market_tab(page, market):
                        time.sleep(0.3)
                        element = page.query_selector("div.ovm-CompetitionList")
                        if element:
                            html = element.inner_html()
                            result = parse_competition_html(html, market)

                            prev_result = previous_results.get(market)
                            if prev_result:
                                changes = compare_iterations(prev_result, result)
                                if changes:
                                    timestamp = datetime.now().strftime("%H:%M:%S")
                                    for change in changes:
                                        market_label = f"[{market[:10]}]"
                                        if change["type"] == "score_change":
                                            print(f"[{timestamp}]{market_label} GOAL! {change['match']} | {change['old_score']} -> {change['new_score']}")
                                        elif change["type"] == "odds_change":
                                            print(f"[{timestamp}]{market_label} ODDS: {change['match']} | {change['old_odds']} -> {change['new_odds']}")
                                        elif change["type"] == "time_change":
                                            print(f"[{timestamp}]{market_label} TIME: {change['match']} | {change['old_time']} -> {change['new_time']}")
                                        elif change["type"] == "new_fixture":
                                            print(f"[{timestamp}]{market_label} NEW: {change['match']}")

                            previous_results[market] = result

                time.sleep(1)  # Small delay between full cycles

        except KeyboardInterrupt:
            print("\nStopped.")

        except Exception as e:
            print(f"Error: {e}")


if __name__ == "__main__":
    url = input("Enter website URL: ").strip()
    if not url:
        print("No URL provided. Exiting.")
        exit(1)

    scrape_realtime(url)
