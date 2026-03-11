"""Quick proxy test — launches 1 browser with the given proxy,
navigates to bet365, and checks if it can connect + receive WS data."""

import asyncio
import sys
import os

from camoufox.async_api import AsyncCamoufox
from protocol import decode_frame

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

URL = "https://www.bet365.com/"
PROXY = {
    "server": "http://188.126.20.21:9081",
    "username": "px95358f",
    "password": "4hjzQNheHNjtz7vTXcPwEyxE",
}

msg_count = 0
ws_connected = False
raw_frames = []


def on_frame(payload):
    global msg_count
    if not isinstance(payload, str):
        return
    # Save first few raw frames
    if len(raw_frames) < 5:
        raw_frames.append(payload)
    try:
        msgs = decode_frame(payload)
        msg_count += len(msgs)
    except Exception as e:
        print(f"  [ERR] {e}", file=sys.stderr)


async def main():
    global ws_connected, msg_count

    print("Launching browser with proxy...", file=sys.stderr)
    print(f"  Proxy: {PROXY['server']}", file=sys.stderr)

    cm = AsyncCamoufox(headless=True, proxy=PROXY)
    old_stdout = sys.stdout
    sys.stdout = open(os.devnull, "w")
    try:
        browser = await cm.__aenter__()
    finally:
        sys.stdout.close()
        sys.stdout = old_stdout

    page = await browser.new_page()

    def on_ws(ws):
        global ws_connected
        if "premws" not in ws.url:
            return
        ws_connected = True
        print(f"  [WS] Connected: {ws.url[:80]}...", file=sys.stderr)
        ws.on("framereceived", on_frame)
        ws.on("close", lambda: print("  [WS] Closed", file=sys.stderr))

    page.on("websocket", on_ws)

    print("Navigating to bet365...", file=sys.stderr)
    try:
        await page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        print("  Page loaded.", file=sys.stderr)
    except Exception as e:
        print(f"  Navigation error: {e}", file=sys.stderr)

    content = await page.content()
    page_len = len(content)
    print(f"  Page content length: {page_len} chars", file=sys.stderr)

    if page_len < 1000:
        print("  WARNING: page looks blocked or empty!", file=sys.stderr)

    # Wait up to 30s for WS data
    print("Waiting for WebSocket data (up to 30s)...", file=sys.stderr)
    for i in range(30):
        await asyncio.sleep(1)
        if msg_count > 0:
            print(f"  Got {msg_count} messages after {i+1}s", file=sys.stderr)
            # Wait a few more seconds to collect more
            await asyncio.sleep(3)
            break
    else:
        print("  No WS messages received in 30s.", file=sys.stderr)

    # Dump raw frames
    print("\n=== RAW FRAMES (first 5) ===", file=sys.stderr)
    for i, frame in enumerate(raw_frames):
        preview = repr(frame[:1500])
        print(f"\n--- Frame {i+1} ({len(frame)} chars) ---", file=sys.stderr)
        print(preview, file=sys.stderr)

    # Summary
    print("\n=== RESULT ===", file=sys.stderr)
    print(f"  Page loaded:    {'YES' if page_len > 1000 else 'NO (blocked?)'}", file=sys.stderr)
    print(f"  WS connected:   {'YES' if ws_connected else 'NO'}", file=sys.stderr)
    print(f"  Messages recv:  {msg_count}", file=sys.stderr)
    print(f"  Proxy working:  {'YES' if msg_count > 0 else 'UNCLEAR — no data received'}", file=sys.stderr)

    await cm.__aexit__(None, None, None)
    print("Browser closed.", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
