"""
Bet365 Real-Time Data Stream

Outputs JSONL (one JSON object per line) to stdout.
All status/progress logging goes to stderr.

Stream format:
  1. {"type":"snapshot", ...}   — full state (on start + every --snapshot-interval seconds)
  2. {"type":"odds", ...}       — odds change
  3. {"type":"score", ...}      — score change
  4. {"type":"new_event", ...}  — new event appeared
  5. {"type":"removed", ...}    — event removed
  6. {"type":"suspended", ...}  — market suspended
  7. {"type":"resumed", ...}    — market resumed

Usage:
    python main.py                                    # overview only (live)
    python main.py --all-markets                      # load all detail markets + stream
    python main.py --all-markets --instances 3        # 3 browsers, faster initial load
    python main.py --all-markets --refresh 10         # refresh detail markets every 10 min
    python main.py --snapshot-interval 30             # full snapshot every 30s (default: 10)
    python main.py --all-markets --headed             # visible browser (debug)

Pipe examples:
    python main.py --all-markets | your_consumer.py
    python main.py --all-markets > stream.jsonl
    python main.py --all-markets | tee stream.jsonl | your_ws_server.py
"""

import asyncio
import json
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
    def __init__(self, instance_id: int, state: StateManager):
        self.id = instance_id
        self.state = state
        self.msg_count = 0
        self.page = None

    def on_frame(self, payload):
        if not isinstance(payload, str):
            return
        try:
            for msg in decode_frame(payload):
                self.msg_count += 1
                self.state.apply(msg)
        except Exception as e:
            log(f"[I{self.id}] ERR: {e}")

    async def start(self, headless: bool = True):
        log(f"[I{self.id}] Starting browser...")
        self._browser_cm = AsyncCamoufox(headless=headless)
        self._browser = await self._browser_cm.__aenter__()
        self.page = await self._browser.new_page()

        def on_ws(ws):
            if "premws" not in ws.url:
                return
            log(f"[I{self.id}] WS connected")
            ws.on("framereceived", self.on_frame)
            ws.on("close", lambda: log(f"[I{self.id}] WS closed"))

        self.page.on("websocket", on_ws)

        try:
            await self.page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log(f"[I{self.id}] Navigation: {e}")

        content = await self.page.content()
        if len(content) < 1000:
            log(f"[I{self.id}] WARNING: blocked")
            return False

        for _ in range(30):
            await asyncio.sleep(1)
            if any(e.entity_type == "EV" and e.it.startswith("L")
                   for e in self.state.entities.values()):
                await asyncio.sleep(2)
                break

        log(f"[I{self.id}] Ready ({self.msg_count} messages)")
        return True

    async def stop(self):
        try:
            await self._browser_cm.__aexit__(None, None, None)
        except Exception:
            pass

    async def load_events(self, events: list[tuple[str, str, str]]):
        if not events:
            return
        total = len(events)
        log(f"[I{self.id}] Loading {total} events...")
        visited = 0
        for fi, name, detail_hash in events:
            try:
                await self.page.evaluate(f"window.location.hash = '{detail_hash}'")
            except Exception:
                continue
            await asyncio.sleep(NAV_DELAY)
            self.state.snapshot_markets(fi)
            visited += 1
            if visited % 25 == 0:
                log(f"[I{self.id}]   [{visited}/{total}]")

        try:
            await self.page.evaluate("window.location.hash = '#/IP/'")
        except Exception:
            pass
        await asyncio.sleep(2)
        for fi, _, _ in events[-25:]:
            self.state.snapshot_markets(fi)
        log(f"[I{self.id}] Done loading {visited} events")


class Monitor:
    def __init__(self, n_instances=1, headless=True, load_all=False,
                 refresh_min=0, snapshot_interval=10):
        self.n_instances = n_instances
        self.headless = headless
        self.load_all = load_all
        self.refresh_min = refresh_min
        self.snapshot_interval = snapshot_interval
        self.state = StateManager(on_change=on_change)
        self.instances: list[Instance] = []
        self.all_loaded_fis: set[str] = set()

    async def snapshot_loop(self):
        while True:
            await asyncio.sleep(self.snapshot_interval)
            emit({"type": "snapshot", "data": self.state.to_json()})

    async def refresh_loop(self):
        while True:
            await asyncio.sleep(self.refresh_min * 60)
            events = get_navigable_events(self.state)
            new = [(fi, n, h) for fi, n, h in events if fi not in self.all_loaded_fis]
            for fi, _, _ in new:
                self.all_loaded_fis.add(fi)
            all_events = [(fi, n, h) for fi, n, h in events if fi in self.all_loaded_fis]
            if not all_events:
                continue
            log(f"Refreshing {len(all_events)} events ({len(new)} new)...")
            slices: list[list] = [[] for _ in range(self.n_instances)]
            for i, ev in enumerate(all_events):
                slices[i % self.n_instances].append(ev)
            await asyncio.gather(
                *[inst.load_events(sl) for inst, sl in zip(self.instances, slices)]
            )
            emit({"type": "snapshot", "data": self.state.to_json()})
            log("Refresh complete")

    async def run(self):
        log(f"Starting ({self.n_instances} instance{'s' if self.n_instances > 1 else ''}, "
            f"{'headless' if self.headless else 'headed'})...")

        self.instances = [Instance(i, self.state) for i in range(self.n_instances)]

        ok = await self.instances[0].start(self.headless)
        if not ok:
            log("Failed to start. Try --headed")
            return

        if self.n_instances > 1:
            results = await asyncio.gather(
                *[inst.start(self.headless) for inst in self.instances[1:]],
                return_exceptions=True
            )
            for i, r in enumerate(results, 1):
                if isinstance(r, Exception):
                    log(f"Instance {i} failed: {r}")

        snap = self.state.to_json()
        log(f"Initial: {snap['event_count']} events")

        if self.load_all:
            events = get_navigable_events(self.state)
            total = len(events)
            for fi, _, _ in events:
                self.all_loaded_fis.add(fi)
            slices: list[list] = [[] for _ in range(self.n_instances)]
            for i, ev in enumerate(events):
                slices[i % self.n_instances].append(ev)
            per_inst = [len(s) for s in slices]
            log(f"Loading {total} events ({', '.join(str(n) for n in per_inst)} per instance)...")
            await asyncio.gather(
                *[inst.load_events(sl) for inst, sl in zip(self.instances, slices)]
            )
            snap = self.state.to_json()
            mkts = sum(len(ev["markets"]) for ev in snap["events"].values())
            full = sum(1 for ev in snap["events"].values() if len(ev["markets"]) > 1)
            log(f"Loaded: {full} events with detail markets, {mkts} total markets")

        # Emit initial snapshot
        emit({"type": "snapshot", "data": self.state.to_json()})
        log("Streaming...")

        tasks = [asyncio.create_task(self.snapshot_loop())]
        if self.load_all and self.refresh_min > 0:
            tasks.append(asyncio.create_task(self.refresh_loop()))

        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            for t in tasks:
                t.cancel()
            log("Stopped")
            for inst in self.instances:
                await inst.stop()


def parse_args():
    headless = "--headed" not in sys.argv
    load_all = "--all-markets" in sys.argv
    n_instances = 1
    refresh_min = 0
    snapshot_interval = 10
    for i, arg in enumerate(sys.argv):
        if arg == "--instances" and i + 1 < len(sys.argv):
            n_instances = int(sys.argv[i + 1])
        elif arg == "--refresh" and i + 1 < len(sys.argv):
            refresh_min = int(sys.argv[i + 1])
        elif arg == "--snapshot-interval" and i + 1 < len(sys.argv):
            snapshot_interval = int(sys.argv[i + 1])
    return headless, load_all, n_instances, refresh_min, snapshot_interval


async def main():
    headless, load_all, n_instances, refresh_min, snapshot_interval = parse_args()
    monitor = Monitor(
        n_instances=n_instances, headless=headless,
        load_all=load_all, refresh_min=refresh_min,
        snapshot_interval=snapshot_interval,
    )
    await monitor.run()


if __name__ == "__main__":
    asyncio.run(main())
