"""
Bet365 Real-Time Data Stream — Full Market Coverage

Architecture:
  1. SOCKS5 relay on localhost — all browsers connect here. Upstream proxy
     is hot-switchable: on failure, switch upstream and browsers reconnect
     through the relay automatically. No browser restarts needed.
  2. Overview instance: stays on InPlay page, discovers events, streams
     scores/times/overview markets in real time.
  3. Worker pool (--all-markets): one dedicated browser tab per event, each
     maintaining a persistent 6V subscription for full real-time market data.

Resilience:
  - Proxy watchdog pings upstream every 3s (SOCKS5 CONNECT to bet365).
  - On proxy death: rotate upstream on relay → heal workers via page reload.
  - Recovery in ~10-15s instead of 20-30 min full restart.
  - LTESocks API integration for automatic IP reset on block.
  - Full restart only as absolute last resort.

Stream format (JSONL to stdout):
  {"type":"snapshot", ...}   — full state every --snapshot-interval seconds
  {"type":"odds", ...}       — odds change (real-time)
  {"type":"score", ...}      — score change (real-time)
  {"type":"new_event", ...}  — new event appeared
  {"type":"removed", ...}    — event removed
  {"type":"suspended", ...}  — market suspended
  {"type":"resumed", ...}    — market resumed

Usage:
    python main.py --all-markets --proxy "socks5://u:p@host:port"
    python main.py --all-markets --proxy "url1" --proxy "url2"      # failover
    python main.py --all-markets --proxy "url" \\
        --ltesocks-token TOKEN --ltesocks-ports id1,id2             # auto IP reset
"""

import asyncio
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import websockets

from camoufox.async_api import AsyncCamoufox
from protocol import decode_frame
from state import StateManager, ChangeEvent
from proxy_relay import ProxyRelay

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

NAV_DELAY = 2.0
URL = "https://www.bet365.com/"
WS_PORT = 8080
WS_TOKEN = None
WS_CLIENTS: set = set()

# Relay: all browsers connect here; upstream is switchable
RELAY: ProxyRelay | None = None

# GeoIP coordinates for UK (London) — used when browsers go through relay
# since geoip=True can't auto-detect from localhost
UK_GEOIP = {"longitude": -0.1278, "latitude": 51.5074}

# Watchdog tuning
PROXY_CHECK_INTERVAL = 3    # seconds between health checks
HEAL_GRACE_PERIOD = 10      # seconds to wait for auto-reconnect after proxy switch
HEAL_MAX_RETRIES = 3        # heal attempts before full restart fallback
RESTART_BACKOFF = [10, 30, 60, 120]

# Resource types to block (saves RAM/CPU/bandwidth on headless servers)
BLOCKED_RESOURCES = {"image", "media", "font", "stylesheet"}


async def _block_resources(route):
    """Block heavy resources — we only need WebSocket data."""
    if route.request.resource_type in BLOCKED_RESOURCES:
        await route.abort()
    else:
        await route.continue_()


def _browser_kwargs(headless: bool) -> dict:
    """Build Camoufox launch kwargs using the relay proxy."""
    kw = {"headless": headless}
    if RELAY and RELAY.listen_port:
        kw["proxy"] = {"server": f"socks5://127.0.0.1:{RELAY.listen_port}"}
        kw["geoip"] = UK_GEOIP
    return kw


# ─── Proxy Management ────────────────────────────────────────────────────────

class ProxyManager:
    """Manages multiple upstream proxies with LTESocks API integration."""

    def __init__(self, proxy_urls: list[str],
                 ltesocks_token: str = None,
                 ltesocks_port_ids: list[str] = None):
        self.raw_urls = proxy_urls
        self.ltesocks_token = ltesocks_token
        self.port_ids = ltesocks_port_ids or []
        self._index = 0
        self._consecutive_failures = 0

    @property
    def current_url(self) -> str | None:
        if not self.raw_urls:
            return None
        return self.raw_urls[self._index % len(self.raw_urls)]

    @property
    def current_label(self) -> str:
        url = self.current_url
        if not url:
            return "direct"
        p = urlparse(url)
        return f"{p.scheme}://{p.hostname}:{p.port}"

    @property
    def has_multiple(self) -> bool:
        return len(self.raw_urls) > 1

    @property
    def has_ltesocks(self) -> bool:
        return bool(self.ltesocks_token and self.port_ids)

    def rotate(self) -> str | None:
        """Switch to next proxy URL."""
        if not self.has_multiple:
            return self.current_url
        old = self._index
        self._index = (self._index + 1) % len(self.raw_urls)
        log(f"[Proxy] Rotated: #{old} → #{self._index} ({self.current_label})")
        return self.current_url

    async def reset_ip(self) -> bool:
        """Reset current LTESocks port to get a new IP address."""
        if not self.has_ltesocks:
            return False

        port_id = self.port_ids[self._index % len(self.port_ids)]
        log(f"[Proxy] Resetting LTESocks port {port_id}...")

        try:
            ok = await asyncio.get_event_loop().run_in_executor(
                None, self._api_reset, port_id)
            if not ok:
                return False

            for _ in range(12):
                await asyncio.sleep(5)
                status = await asyncio.get_event_loop().run_in_executor(
                    None, self._api_port_status, port_id)
                if status == "active":
                    log(f"[Proxy] Port {port_id} back online with new IP")
                    return True
                log(f"[Proxy] Port status: {status} (waiting...)")

            log(f"[Proxy] Port {port_id} did not recover in time")
            return False
        except Exception as e:
            log(f"[Proxy] Reset failed: {e}")
            return False

    def _api_reset(self, port_id: str) -> bool:
        req = urllib.request.Request(
            f"https://api.ltesocks.io/v2/ports/{port_id}/reset",
            method="POST",
            headers={"Authorization": self.ltesocks_token},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as e:
            log(f"[Proxy] API reset HTTP {e.code}")
            return False

    def _api_port_status(self, port_id: str) -> str:
        req = urllib.request.Request(
            f"https://api.ltesocks.io/v2/ports/{port_id}",
            headers={"Authorization": self.ltesocks_token},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                return data.get("status", "unknown")
        except Exception:
            return "error"

    def record_failure(self):
        self._consecutive_failures += 1

    def record_success(self):
        self._consecutive_failures = 0

    @property
    def backoff_seconds(self) -> int:
        idx = min(self._consecutive_failures, len(RESTART_BACKOFF) - 1)
        return RESTART_BACKOFF[idx]

    async def recover(self) -> bool:
        """Try to get a working proxy. Returns True if action was taken."""
        if self.has_ltesocks:
            if await self.reset_ip():
                return True
            log("[Proxy] IP reset failed")

        if self.has_multiple:
            self.rotate()
            return True

        log("[Proxy] No failover available, will retry with backoff")
        return False


# ─── Helpers ──────────────────────────────────────────────────────────────────

def ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg: str):
    print(f"[{ts()}] {msg}", file=sys.stderr, flush=True)


def emit(obj: dict):
    """Write a JSON object to stdout and broadcast to WS clients."""
    obj["ts"] = datetime.now(timezone.utc).isoformat()
    line = json.dumps(obj, ensure_ascii=False)
    print(line, flush=True)
    if WS_CLIENTS:
        websockets.broadcast(WS_CLIENTS, line)


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


# ─── Overview Browser ─────────────────────────────────────────────────────────

class Instance:
    """Overview browser — stays on InPlay page, discovers events."""

    def __init__(self, state: StateManager):
        self.state = state
        self.msg_count = 0
        self.page = None
        self._browser_cm = None
        self._browser = None
        # Health tracking
        self.last_msg_ts: float = 0.0
        self.ws_connected: bool = False

    @property
    def is_healthy(self) -> bool:
        if not self.ws_connected:
            return self.last_msg_ts == 0  # still starting up
        return (time.monotonic() - self.last_msg_ts) < 60

    def _on_frame(self, payload):
        if not isinstance(payload, str):
            return
        try:
            for msg in decode_frame(payload):
                self.msg_count += 1
                self.last_msg_ts = time.monotonic()
                self.state.apply(msg)
        except Exception as e:
            log(f"[Overview] ERR: {e}")

    async def start(self, headless: bool = True):
        log("Starting overview browser...")
        kw = _browser_kwargs(headless)
        self._browser_cm = AsyncCamoufox(**kw)
        old_stdout = sys.stdout
        sys.stdout = open(os.devnull, "w")
        try:
            self._browser = await self._browser_cm.__aenter__()
        finally:
            sys.stdout.close()
            sys.stdout = old_stdout
        self.page = await self._browser.new_page()
        await self.page.route("**/*", _block_resources)

        def on_ws(ws):
            if "premws" not in ws.url:
                return
            log("Overview WS connected")
            self.ws_connected = True
            self.last_msg_ts = time.monotonic()
            ws.on("framereceived", self._on_frame)
            ws.on("close", lambda: setattr(self, "ws_connected", False)
                  or log("Overview WS closed"))

        self.page.on("websocket", on_ws)

        try:
            await self.page.goto(URL, wait_until="domcontentloaded",
                                 timeout=60000)
        except Exception as e:
            log(f"Navigation: {e}")

        content = await self.page.content()
        if len(content) < 1000:
            log("WARNING: blocked/geo-restricted")
            return False

        for _ in range(30):
            await asyncio.sleep(1)
            if any(e.entity_type == "EV" and e.it.startswith("L")
                   for e in self.state.entities.values()):
                await asyncio.sleep(2)
                break

        log(f"Overview ready ({self.msg_count} messages)")
        return True

    async def heal(self) -> bool:
        """Reload page to reconnect WS through updated proxy."""
        if not self.page:
            return False
        try:
            log("[Overview] Healing — reloading page...")
            await self.page.reload(wait_until="domcontentloaded",
                                   timeout=60000)
            for _ in range(15):
                await asyncio.sleep(2)
                if self.ws_connected:
                    log("[Overview] Healed — WS reconnected")
                    return True
            log("[Overview] Heal failed — WS did not reconnect")
            return False
        except Exception as e:
            log(f"[Overview] Heal failed: {e}")
            return False

    async def stop(self):
        try:
            await self._browser_cm.__aexit__(None, None, None)
        except Exception:
            pass


# ─── Worker Pool ──────────────────────────────────────────────────────────────

class WorkerPool:
    """Pool of browser tabs with persistent 6V subscriptions."""

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

    def _count_dead(self) -> tuple[list[str], list[str]]:
        """Returns (ws_dead_fis, page_dead_fis)."""
        ws_dead = []
        page_dead = []
        for fi, w in self._workers.items():
            try:
                if w["page"].is_closed():
                    page_dead.append(fi)
                    continue
            except Exception:
                page_dead.append(fi)
                continue
            if not w["info"]["alive"]:
                ws_dead.append(fi)
        return ws_dead, page_dead

    async def _get_browser(self) -> tuple[int, object]:
        """Get browser with capacity or create new one."""
        for i, b in enumerate(self._browsers):
            if b["tab_count"] < self.tabs_per_browser:
                return i, b["browser"]
        kw = _browser_kwargs(self.headless)
        cm = AsyncCamoufox(**kw)
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
            return None

        async with self._lock:
            browser_idx, browser = await self._get_browser()
            self._browsers[browser_idx]["tab_count"] += 1

        page = None
        try:
            page = await browser.new_page()
            await page.route("**/*", _block_resources)

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
                # Re-mark alive on new WS (handles auto-reconnect)
                worker_info["alive"] = True
                ws.on("framereceived", on_frame)
                ws.on("close",
                      lambda: worker_info.__setitem__("alive", False))
                ws_ready.set()

            page.on("websocket", on_ws)

            await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            await asyncio.wait_for(ws_ready.wait(), timeout=20)
            await asyncio.sleep(3)

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
                    return
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

        new = [(fi, n, h) for fi, n, h in events if fi not in self._workers]
        if new:
            log(f"[Pool] {len(new)} new events detected")
            await self.spawn_all(new)

    async def heal_dead(self) -> int:
        """Heal workers with dead WS by reloading their pages.

        Much faster than full respawn — keeps browser alive, just refreshes
        the page so bet365 JS re-establishes the WS connection.
        Returns number of workers healed.
        """
        ws_dead, page_dead = self._count_dead()

        if not ws_dead and not page_dead:
            return 0

        healed = 0

        # 1. Heal WS-dead workers via page reload (fast path)
        if ws_dead:
            log(f"[Heal] Refreshing {len(ws_dead)} workers with dead WS...")
            sem = asyncio.Semaphore(self.concurrency)

            async def _heal_one(fi):
                nonlocal healed
                w = self._workers.get(fi)
                if not w:
                    return
                async with sem:
                    try:
                        page = w["page"]
                        detail_hash = w["detail_hash"]
                        await page.reload(
                            wait_until="domcontentloaded", timeout=30000)
                        await asyncio.sleep(3)
                        await page.evaluate(
                            f"window.location.hash = '{detail_hash}'")
                        await asyncio.sleep(NAV_DELAY)
                        healed += 1
                    except Exception as e:
                        log(f"[Heal] {w['name'][:30]} — reload failed: {e}")
                        # Promote to page_dead for full respawn
                        page_dead.append(fi)

            await asyncio.gather(
                *[_heal_one(fi) for fi in ws_dead],
                return_exceptions=True,
            )

        # 2. Full respawn for page-dead workers (page crashed / browser died)
        if page_dead:
            log(f"[Heal] Respawning {len(page_dead)} crashed workers...")
            for fi in page_dead:
                w = self._workers.pop(fi, None)
                if not w:
                    continue
                try:
                    await w["page"].close()
                except Exception:
                    pass
                async with self._lock:
                    idx = w["browser_idx"]
                    if idx < len(self._browsers):
                        self._browsers[idx]["tab_count"] -= 1
                result = await self._spawn_one(
                    fi, w["name"], w["detail_hash"])
                if result:
                    healed += 1

        log(f"[Heal] {healed}/{len(ws_dead) + len(page_dead)} workers recovered")
        return healed

    async def respawn_dead(self) -> int:
        """Detect and respawn dead workers (normal operation, not proxy failure)."""
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
            return 0

        log(f"[Pool] {len(dead)} dead workers, respawning...")
        for fi in dead:
            w = self._workers.pop(fi)
            name, detail_hash = w["name"], w["detail_hash"]
            try:
                await w["page"].close()
            except Exception:
                pass
            async with self._lock:
                idx = w["browser_idx"]
                if idx < len(self._browsers):
                    self._browsers[idx]["tab_count"] -= 1
            result = await self._spawn_one(fi, name, detail_hash)
            status = "respawned" if result else "respawn FAILED"
            log(f"[Pool] {name[:30]} — {status}")
        return len(dead)

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


# ─── WebSocket Server ─────────────────────────────────────────────────────────

async def ws_handler(websocket):
    """Handle incoming WebSocket connections with token auth."""
    params = parse_qs(urlparse(websocket.request.path).query)
    token = (params.get("token") or [None])[0]
    if WS_TOKEN and token != WS_TOKEN:
        await websocket.close(4001, "Unauthorized")
        return

    WS_CLIENTS.add(websocket)
    log(f"[WS] Client connected ({len(WS_CLIENTS)} total)")
    try:
        async for _ in websocket:
            pass
    finally:
        WS_CLIENTS.discard(websocket)
        log(f"[WS] Client disconnected ({len(WS_CLIENTS)} total)")


# ─── Monitor (main orchestrator) ─────────────────────────────────────────────

class Monitor:
    def __init__(self, headless=True, load_all=False, snapshot_interval=10,
                 tabs_per_browser=5, max_workers=0, concurrency=5,
                 proxy_mgr: ProxyManager = None):
        self.headless = headless
        self.load_all = load_all
        self.snapshot_interval = snapshot_interval
        self.tabs_per_browser = tabs_per_browser
        self.max_workers = max_workers
        self.concurrency = concurrency
        self.proxy_mgr = proxy_mgr or ProxyManager([])
        self.state: StateManager | None = None
        self.overview: Instance | None = None
        self.pool: WorkerPool | None = None
        self._restart = asyncio.Event()  # last-resort full restart signal
        self._healing = False  # prevent concurrent heals

    def _apply_upstream(self):
        """Point the relay at the ProxyManager's current proxy."""
        url = self.proxy_mgr.current_url
        if url and RELAY:
            RELAY.set_upstream_from_url(url)
            log(f"[Proxy] Upstream: {self.proxy_mgr.current_label}")

    async def _snapshot_loop(self):
        while True:
            await asyncio.sleep(self.snapshot_interval)
            if self.state:
                emit({"type": "snapshot", "data": self.state.to_json()})

    async def _event_watcher(self):
        """Watch for new/removed events, manage workers."""
        while True:
            await asyncio.sleep(15)
            if not self.pool or not self.state or self._healing:
                continue
            await self.pool.respawn_dead()
            events = get_navigable_events(self.state)
            await self.pool.sync_events(events)

    async def _proxy_watchdog(self):
        """Test upstream proxy every 3s. On failure: recover + heal."""
        # Give startup time
        await asyncio.sleep(15)
        consecutive_fails = 0
        heal_attempts = 0

        while True:
            await asyncio.sleep(PROXY_CHECK_INTERVAL)

            if self._healing:
                continue

            healthy = await RELAY.check_health(timeout=5)

            if healthy:
                if consecutive_fails > 0:
                    log(f"[Proxy] Upstream recovered "
                        f"(was down for {consecutive_fails} checks)")
                consecutive_fails = 0
                heal_attempts = 0
                self.proxy_mgr.record_success()
                continue

            consecutive_fails += 1

            if consecutive_fails == 1:
                log("[Proxy] Health check failed (waiting for confirmation...)")
                continue  # single failure — could be transient

            # Confirmed: proxy is down
            log(f"[Proxy] DOWN — confirmed ({consecutive_fails} consecutive "
                f"failures, heal attempt #{heal_attempts + 1})")

            if heal_attempts >= HEAL_MAX_RETRIES:
                log("[Proxy] Too many heal failures — triggering full restart")
                emit({"type": "status", "status": "restarting",
                      "reason": "proxy heal exhausted"})
                self._restart.set()
                return

            # ── Recovery sequence ──
            self._healing = True
            emit({"type": "status", "status": "proxy_down",
                  "proxy": self.proxy_mgr.current_label})

            try:
                # Step 1: Get a working proxy (reset IP or rotate)
                await self.proxy_mgr.recover()
                self._apply_upstream()

                # Step 2: Verify new upstream works
                for _ in range(3):
                    await asyncio.sleep(3)
                    if await RELAY.check_health(timeout=5):
                        break
                else:
                    log("[Proxy] New upstream also unhealthy")
                    self.proxy_mgr.record_failure()
                    heal_attempts += 1
                    self._healing = False
                    continue

                log(f"[Proxy] New upstream OK — healing instances "
                    f"(grace period {HEAL_GRACE_PERIOD}s)...")

                # Step 3: Wait for bet365 JS auto-reconnect
                await asyncio.sleep(HEAL_GRACE_PERIOD)

                # Step 4: Heal overview if needed
                if self.overview and not self.overview.ws_connected:
                    await self.overview.heal()

                # Step 5: Heal dead workers
                if self.pool:
                    healed = await self.pool.heal_dead()
                    if healed > 0:
                        log(f"[Proxy] Healed {healed} workers")

                # Step 6: Verify recovery
                await asyncio.sleep(5)
                if self.overview and self.overview.is_healthy:
                    log("[Proxy] Recovery successful!")
                    consecutive_fails = 0
                    heal_attempts = 0
                    self.proxy_mgr.record_success()
                    emit({"type": "status", "status": "recovered",
                          "proxy": self.proxy_mgr.current_label})
                else:
                    log("[Proxy] Overview still unhealthy after heal")
                    heal_attempts += 1
                    self.proxy_mgr.record_failure()

            finally:
                self._healing = False

    async def _teardown(self):
        """Stop all browsers and workers."""
        if self.pool:
            try:
                await self.pool.stop()
            except Exception:
                pass
            self.pool = None
        if self.overview:
            try:
                await self.overview.stop()
            except Exception:
                pass
            self.overview = None

    async def _run_session(self) -> bool:
        """Run one session. Returns True to restart, False to stop."""
        self._restart = asyncio.Event()
        self._healing = False
        self._apply_upstream()

        self.state = StateManager(on_change=on_change)
        self.overview = Instance(self.state)

        mode = "full 6V real-time" if self.load_all else "overview only"
        log(f"Starting session ({mode}, proxy: {self.proxy_mgr.current_label})...")

        ok = await self.overview.start(self.headless)
        if not ok:
            log("Failed to start overview — blocked or proxy down")
            await self._teardown()
            return True

        snap = self.state.to_json()
        log(f"Initial: {snap['event_count']} events")
        self.proxy_mgr.record_success()

        if self.load_all:
            self.pool = WorkerPool(
                self.state, headless=self.headless,
                tabs_per_browser=self.tabs_per_browser,
                max_workers=self.max_workers,
                concurrency=self.concurrency,
            )
            events = get_navigable_events(self.state)
            n_brw = ((len(events) + self.pool.tabs_per_browser - 1)
                     // self.pool.tabs_per_browser)
            log(f"Spawning {len(events)} workers across ~{n_brw} browsers "
                f"({self.pool.tabs_per_browser} tabs/browser, "
                f"{self.pool.concurrency} concurrent)...")
            await self.pool.spawn_all(events)
            snap = self.state.to_json()
            mkts = sum(len(ev["markets"]) for ev in snap["events"].values())
            log(f"Live: {self.pool.count} events, {mkts} total markets")

        emit({"type": "snapshot", "data": self.state.to_json()})
        log("Streaming real-time data...")

        tasks = [
            asyncio.create_task(self._snapshot_loop()),
            asyncio.create_task(self._proxy_watchdog()),
        ]
        if self.load_all and self.pool:
            tasks.append(asyncio.create_task(self._event_watcher()))

        try:
            # Wait for either full-restart signal or manual shutdown
            await self._restart.wait()
            for t in tasks:
                t.cancel()
            await self._teardown()
            return True  # restart

        except (KeyboardInterrupt, asyncio.CancelledError):
            for t in tasks:
                t.cancel()
            await self._teardown()
            return False  # stop

    async def run(self):
        """Main entry — runs sessions with auto-recovery."""
        # Start WebSocket server (persists across restarts)
        ws_server = await websockets.serve(ws_handler, "0.0.0.0", WS_PORT)
        log(f"WebSocket server on port {WS_PORT} "
            f"(token={'required' if WS_TOKEN else 'none'})")

        try:
            while True:
                should_restart = await self._run_session()
                if not should_restart:
                    break

                self.proxy_mgr.record_failure()
                await self.proxy_mgr.recover()

                backoff = self.proxy_mgr.backoff_seconds
                log(f"[Monitor] Full restart in {backoff}s "
                    f"(attempt #{self.proxy_mgr._consecutive_failures})...")
                emit({"type": "status", "status": "full_restart",
                      "next_attempt_sec": backoff})
                await asyncio.sleep(backoff)

        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            log("Shutting down...")
            ws_server.close()
            await ws_server.wait_closed()
            await self._teardown()
            if RELAY:
                await RELAY.stop()
            log("Stopped")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def parse_args():
    global WS_PORT, WS_TOKEN, RELAY
    headless = "--headed" not in sys.argv
    load_all = "--all-markets" in sys.argv
    snapshot_interval = 10
    tabs_per_browser = 5
    max_workers = 0
    concurrency = 5
    proxy_urls = []
    ltesocks_token = None
    ltesocks_port_ids = []

    i = 0
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == "--snapshot-interval" and i + 1 < len(sys.argv):
            snapshot_interval = int(sys.argv[i + 1]); i += 1
        elif arg == "--tabs-per-browser" and i + 1 < len(sys.argv):
            tabs_per_browser = int(sys.argv[i + 1]); i += 1
        elif arg == "--max-workers" and i + 1 < len(sys.argv):
            max_workers = int(sys.argv[i + 1]); i += 1
        elif arg == "--concurrency" and i + 1 < len(sys.argv):
            concurrency = int(sys.argv[i + 1]); i += 1
        elif arg == "--proxy" and i + 1 < len(sys.argv):
            proxy_urls.append(sys.argv[i + 1]); i += 1
        elif arg == "--ws-port" and i + 1 < len(sys.argv):
            WS_PORT = int(sys.argv[i + 1]); i += 1
        elif arg == "--ws-token" and i + 1 < len(sys.argv):
            WS_TOKEN = sys.argv[i + 1]; i += 1
        elif arg == "--ltesocks-token" and i + 1 < len(sys.argv):
            ltesocks_token = sys.argv[i + 1]; i += 1
        elif arg == "--ltesocks-ports" and i + 1 < len(sys.argv):
            ltesocks_port_ids = [p.strip()
                                 for p in sys.argv[i + 1].split(",")]; i += 1
        i += 1

    proxy_mgr = ProxyManager(proxy_urls, ltesocks_token, ltesocks_port_ids)
    if proxy_urls:
        log(f"Proxies: {len(proxy_urls)} configured")
        if ltesocks_token:
            log(f"LTESocks: {len(ltesocks_port_ids)} port(s) for auto IP reset")

    return (headless, load_all, snapshot_interval, tabs_per_browser,
            max_workers, concurrency, proxy_mgr)


async def main():
    global RELAY

    (headless, load_all, snapshot_interval, tabs_per_browser,
     max_workers, concurrency, proxy_mgr) = parse_args()

    # Start local SOCKS5 relay (browsers connect here, upstream is switchable)
    if proxy_mgr.raw_urls:
        RELAY = ProxyRelay()
        await RELAY.start()
        RELAY.set_upstream_from_url(proxy_mgr.current_url)
        log(f"Relay ready on 127.0.0.1:{RELAY.listen_port}")

    monitor = Monitor(
        headless=headless,
        load_all=load_all,
        snapshot_interval=snapshot_interval,
        tabs_per_browser=tabs_per_browser,
        max_workers=max_workers,
        concurrency=concurrency,
        proxy_mgr=proxy_mgr,
    )
    await monitor.run()


if __name__ == "__main__":
    asyncio.run(main())
