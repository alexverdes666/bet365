"""
Bet365 Real-Time Data Stream — Full Market Coverage

Architecture:
  1. Overview instance: stays on InPlay page, discovers events, streams
     scores/times/overview markets in real time.
  2. Worker pool (--all-markets): one dedicated browser tab per event, each
     maintaining a persistent 6V subscription for full real-time market data.
     New events get workers automatically; ended events get cleaned up.

Stream format (JSONL to stdout):
  {"type":"snapshot", ...}   — full state every --snapshot-interval seconds
  {"type":"odds", ...}       — odds change (real-time)
  {"type":"score", ...}      — score change (real-time)
  {"type":"new_event", ...}  — new event appeared
  {"type":"removed", ...}    — event removed
  {"type":"suspended", ...}  — market suspended
  {"type":"resumed", ...}    — market resumed

Usage:
    python main.py                                      # overview only (live)
    python main.py --all-markets                        # full 6V: 1 tab per event
    python main.py --all-markets --tabs-per-browser 10  # 10 tabs per browser
    python main.py --all-markets --concurrency 8        # 8 parallel worker spawns
    python main.py --all-markets --max-workers 50       # limit to 50 workers
    python main.py --all-markets --headed               # visible browsers (debug)

Pipe examples:
    python main.py --all-markets | your_consumer.py
    python main.py --all-markets > stream.jsonl
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone

from camoufox.async_api import AsyncCamoufox
from protocol import decode_frame
from state import StateManager, ChangeEvent

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

NAV_DELAY = 2.0
URL = "https://www.bet365.com/"


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg: str):
    print(f"[{ts()}] {msg}", file=sys.stderr, flush=True)


def emit(obj: dict):
    """Write a JSON object to stdout (one line)."""
    obj["ts"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def on_change(evt: ChangeEvent):
    """Emit change events as JSONL to stdout."""
    ev_name = evt.context.get("event_name", "")
    fi = evt.context.get("fixture_id", "")

    if evt.kind == "odds":
        old, new = evt.changed["OD"]
        if not ev_name:
            return
        emit({
            "type": "odds",
            "fixture_id": fi,
            "event": ev_name,
            "market": evt.context.get("market_name", ""),
            "selection": evt.fields.get("NA", ""),
            "old": old,
            "new": new,
        })

    elif evt.kind == "score":
        old, new = evt.changed["SS"]
        emit({
            "type": "score",
            "fixture_id": fi,
            "event": ev_name,
            "old": old,
            "new": new,
        })

    elif evt.kind == "new_event" and evt.entity_type == "EV":
        name = evt.fields.get("NA", "")
        if not name:
            return
        emit({
            "type": "new_event",
            "fixture_id": evt.fields.get("FI", ""),
            "event": name,
            "competition": evt.fields.get("CT", ""),
        })

    elif evt.kind == "removed" and evt.entity_type == "EV":
        emit({
            "type": "removed",
            "fixture_id": fi,
            "event": evt.fields.get("NA", evt.it),
        })

    elif evt.kind == "suspended" and evt.entity_type == "MA" and ev_name:
        emit({
            "type": "suspended",
            "fixture_id": fi,
            "event": ev_name,
            "market": evt.context.get("market_name", ""),
        })

    elif evt.kind == "resumed" and evt.entity_type == "MA" and ev_name:
        emit({
            "type": "resumed",
            "fixture_id": fi,
            "event": ev_name,
            "market": evt.context.get("market_name", ""),
        })


def get_navigable_events(state: StateManager) -> list[tuple[str, str, str]]:
    """Get list of (fixture_id, name, detail_hash) for all in-play events."""
    events = []
    for entity in state.entities.values():
        if entity.entity_type != "EV":
            continue
        fi = entity.fields.get("FI", "")
        it = entity.it
        if not fi or it.startswith("OV"):
            continue
        m = re.match(r"L(\d+)-(\d+)-(\d+)-(\d+)-(\d+)-(\d+)", it)
        if not m:
            continue
        sport_id = m.group(1)
        name = entity.fields.get("NA", "")
        detail_hash = f"#/IP/EV{m.group(2)}{m.group(3)}{m.group(4)}{m.group(5)}C{sport_id}/"
        events.append((fi, name, detail_hash))
    return events


class Instance:
    """Overview browser — stays on InPlay page, discovers events."""

    def __init__(self, state: StateManager):
        self.state = state
        self.msg_count = 0
        self.page = None
        self._browser_cm = None
        self._browser = None

    def _on_frame(self, payload):
        if not isinstance(payload, str):
            return
        try:
            for msg in decode_frame(payload):
                self.msg_count += 1
                self.state.apply(msg)
        except Exception as e:
            log(f"[Overview] ERR: {e}")

    async def start(self, headless: bool = True):
        log("Starting overview browser...")
        self._browser_cm = AsyncCamoufox(headless=headless)
        # Suppress camoufox download/model messages from polluting stdout
        old_stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")
        try:
            self._browser = await self._browser_cm.__aenter__()
        finally:
            sys.stdout.close()
            sys.stdout = old_stdout
        self.page = await self._browser.new_page()

        def on_ws(ws):
            if "premws" not in ws.url:
                return
            log("Overview WS connected")
            ws.on("framereceived", self._on_frame)
            ws.on("close", lambda: log("Overview WS closed"))

        self.page.on("websocket", on_ws)

        try:
            await self.page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log(f"Navigation: {e}")

        content = await self.page.content()
        if len(content) < 1000:
            log("WARNING: blocked")
            return False

        for _ in range(30):
            await asyncio.sleep(1)
            if any(e.entity_type == "EV" and e.it.startswith("L")
                   for e in self.state.entities.values()):
                await asyncio.sleep(2)
                break

        log(f"Overview ready ({self.msg_count} messages)")
        return True

    async def stop(self):
        try:
            await self._browser_cm.__aexit__(None, None, None)
        except Exception:
            pass


class WorkerPool:
    """Pool of browser tabs maintaining persistent 6V subscriptions.

    Each event gets a dedicated browser tab that stays on the event's
    detail page, keeping the 6V subscription alive for real-time updates
    on ALL markets.
    """

    def __init__(self, state: StateManager, headless: bool = True,
                 tabs_per_browser: int = 5, max_workers: int = 0,
                 concurrency: int = 5):
        self.state = state
        self.headless = headless
        self.tabs_per_browser = tabs_per_browser
        self.max_workers = max_workers
        self.concurrency = concurrency
        self._browsers: list[dict] = []
        self._workers: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    @property
    def count(self) -> int:
        return len(self._workers)

    async def _get_browser(self) -> tuple[int, object]:
        """Get browser with capacity or create new one. Caller must hold _lock."""
        for i, b in enumerate(self._browsers):
            if b["tab_count"] < self.tabs_per_browser:
                return i, b["browser"]
        cm = AsyncCamoufox(headless=self.headless)
        old_stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")
        try:
            browser = await cm.__aenter__()
        finally:
            sys.stdout.close()
            sys.stdout = old_stdout
        idx = len(self._browsers)
        self._browsers.append({"cm": cm, "browser": browser, "tab_count": 0})
        log(f"[Pool] Browser #{idx} started")
        return idx, browser

    async def _spawn_one(self, fi: str, name: str, detail_hash: str) -> bool:
        """Spawn a single worker tab for an event."""
        if fi in self._workers:
            return True
        if self.max_workers and len(self._workers) >= self.max_workers:
            return None  # limit reached, distinct from failure

        # Claim a browser slot under lock
        async with self._lock:
            browser_idx, browser = await self._get_browser()
            self._browsers[browser_idx]["tab_count"] += 1

        page = None
        try:
            page = await browser.new_page()

            def on_frame(payload):
                if not isinstance(payload, str):
                    return
                try:
                    for msg in decode_frame(payload):
                        self.state.apply(msg)
                except Exception:
                    pass

            ws_ready = asyncio.Event()
            worker_info = {"alive": True}

            def on_ws(ws):
                if "premws" not in ws.url:
                    return
                ws.on("framereceived", on_frame)
                ws.on("close", lambda: worker_info.__setitem__("alive", False))
                ws_ready.set()

            page.on("websocket", on_ws)

            await page.goto(URL, wait_until="domcontentloaded", timeout=60000)

            # Wait for premws WebSocket to connect
            await asyncio.wait_for(ws_ready.wait(), timeout=20)
            await asyncio.sleep(3)

            # Navigate to event detail — triggers 6V subscription
            await page.evaluate(f"window.location.hash = '{detail_hash}'")
            await asyncio.sleep(NAV_DELAY)

            self._workers[fi] = {
                "page": page,
                "browser_idx": browser_idx,
                "name": name,
                "detail_hash": detail_hash,
                "info": worker_info,
            }
            return True

        except Exception as e:
            log(f"[Pool] {name[:30]} — failed: {e}")
            async with self._lock:
                self._browsers[browser_idx]["tab_count"] -= 1
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            return False

    async def spawn_all(self, events: list[tuple[str, str, str]]):
        """Spawn workers for all events with controlled concurrency."""
        sem = asyncio.Semaphore(self.concurrency)
        total = len(events)
        done = [0]

        async def _spawn(fi, name, detail_hash):
            async with sem:
                result = await self._spawn_one(fi, name, detail_hash)
                done[0] += 1
                if result is None:
                    return  # silently skip (max workers reached)
                status = "live" if result else "FAILED"
                log(f"[Pool] {name[:30]} — {status} [{done[0]}/{total}]")

        await asyncio.gather(
            *[_spawn(fi, n, h) for fi, n, h in events],
            return_exceptions=True,
        )
        log(f"[Pool] {self.count}/{total} workers active")

    async def sync_events(self, events: list[tuple[str, str, str]]):
        """Add workers for new events, remove workers for ended events."""
        active_fis = {fi for fi, _, _ in events}

        # Remove workers for ended events
        stale = [fi for fi in self._workers if fi not in active_fis]
        for fi in stale:
            w = self._workers.pop(fi)
            log(f"[Pool] {w['name'][:30]} — ended, removing")
            try:
                await w["page"].close()
            except Exception:
                pass
            async with self._lock:
                idx = w["browser_idx"]
                if idx < len(self._browsers):
                    self._browsers[idx]["tab_count"] -= 1

        # Spawn workers for new events
        new = [(fi, n, h) for fi, n, h in events if fi not in self._workers]
        if new:
            log(f"[Pool] {len(new)} new events detected")
            await self.spawn_all(new)

    async def respawn_dead(self):
        """Detect dead workers (WS closed / page crashed) and respawn them."""
        dead = []
        for fi, w in list(self._workers.items()):
            try:
                page_dead = w["page"].is_closed()
            except Exception:
                page_dead = True
            ws_dead = not w["info"]["alive"]
            if page_dead or ws_dead:
                dead.append(fi)

        if not dead:
            return

        log(f"[Pool] {len(dead)} dead workers, respawning...")
        for fi in dead:
            w = self._workers.pop(fi)
            name, detail_hash = w["name"], w["detail_hash"]
            # Clean up old tab
            try:
                await w["page"].close()
            except Exception:
                pass
            async with self._lock:
                idx = w["browser_idx"]
                if idx < len(self._browsers):
                    self._browsers[idx]["tab_count"] -= 1
            # Respawn
            result = await self._spawn_one(fi, name, detail_hash)
            status = "respawned" if result else "respawn FAILED"
            log(f"[Pool] {name[:30]} — {status}")

    async def stop(self):
        """Shut down all workers and browsers."""
        for w in self._workers.values():
            try:
                await w["page"].close()
            except Exception:
                pass
        self._workers.clear()
        for b in self._browsers:
            try:
                await b["cm"].__aexit__(None, None, None)
            except Exception:
                pass
        self._browsers.clear()


class Monitor:
    def __init__(self, headless=True, load_all=False, snapshot_interval=10,
                 tabs_per_browser=5, max_workers=0, concurrency=5):
        self.headless = headless
        self.load_all = load_all
        self.snapshot_interval = snapshot_interval
        self.state = StateManager(on_change=on_change)
        self.overview = Instance(self.state)
        self.pool = WorkerPool(
            self.state,
            headless=headless,
            tabs_per_browser=tabs_per_browser,
            max_workers=max_workers,
            concurrency=concurrency,
        ) if load_all else None

    async def _snapshot_loop(self):
        while True:
            await asyncio.sleep(self.snapshot_interval)
            emit({"type": "snapshot", "data": self.state.to_json()})

    async def _event_watcher(self):
        """Watch for new/removed events, manage workers, respawn dead ones."""
        while True:
            await asyncio.sleep(15)
            await self.pool.respawn_dead()
            events = get_navigable_events(self.state)
            await self.pool.sync_events(events)

    async def run(self):
        mode = "full 6V real-time" if self.load_all else "overview only"
        log(f"Starting ({mode}, {'headless' if self.headless else 'headed'})...")

        ok = await self.overview.start(self.headless)
        if not ok:
            log("Failed to start. Try --headed")
            return

        snap = self.state.to_json()
        log(f"Initial: {snap['event_count']} events")

        if self.load_all and self.pool:
            events = get_navigable_events(self.state)
            n_browsers = (len(events) + self.pool.tabs_per_browser - 1) // self.pool.tabs_per_browser
            log(f"Spawning {len(events)} workers across ~{n_browsers} browsers "
                f"({self.pool.tabs_per_browser} tabs/browser, "
                f"{self.pool.concurrency} concurrent)...")
            await self.pool.spawn_all(events)
            snap = self.state.to_json()
            mkts = sum(len(ev["markets"]) for ev in snap["events"].values())
            log(f"Live: {self.pool.count} events with full markets, {mkts} total markets")

        emit({"type": "snapshot", "data": self.state.to_json()})
        log("Streaming real-time data...")

        tasks = [asyncio.create_task(self._snapshot_loop())]
        if self.load_all and self.pool:
            tasks.append(asyncio.create_task(self._event_watcher()))

        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            for t in tasks:
                t.cancel()
            log("Stopping...")
            if self.pool:
                await self.pool.stop()
            await self.overview.stop()
            log("Stopped")


def parse_args():
    headless = "--headed" not in sys.argv
    load_all = "--all-markets" in sys.argv
    snapshot_interval = 10
    tabs_per_browser = 5
    max_workers = 0
    concurrency = 5

    for i, arg in enumerate(sys.argv):
        if arg == "--snapshot-interval" and i + 1 < len(sys.argv):
            snapshot_interval = int(sys.argv[i + 1])
        elif arg == "--tabs-per-browser" and i + 1 < len(sys.argv):
            tabs_per_browser = int(sys.argv[i + 1])
        elif arg == "--max-workers" and i + 1 < len(sys.argv):
            max_workers = int(sys.argv[i + 1])
        elif arg == "--concurrency" and i + 1 < len(sys.argv):
            concurrency = int(sys.argv[i + 1])

    return headless, load_all, snapshot_interval, tabs_per_browser, max_workers, concurrency


async def main():
    headless, load_all, snapshot_interval, tabs_per_browser, max_workers, concurrency = parse_args()
    monitor = Monitor(
        headless=headless,
        load_all=load_all,
        snapshot_interval=snapshot_interval,
        tabs_per_browser=tabs_per_browser,
        max_workers=max_workers,
        concurrency=concurrency,
    )
    await monitor.run()


if __name__ == "__main__":
    asyncio.run(main())
