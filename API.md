# Bet365 Real-Time Data Stream — API Documentation

## Overview

This system captures live in-play sports data from bet365 via WebSocket and streams it as **JSONL** (one JSON object per line) to stdout. You can pipe this into any consumer — a WebSocket relay server, a database writer, or directly into your UI backend.

## Quick Start

```bash
# Stream overview data (events + main market odds)
python main.py

# Stream with ALL detail markets loaded first
python main.py --all-markets

# Faster initial load with 3 browser instances
python main.py --all-markets --instances 3

# Refresh detail markets every 10 minutes (picks up new events too)
python main.py --all-markets --refresh 10

# Full snapshot every 30s instead of default 10s
python main.py --snapshot-interval 30

# Debug mode (visible browser)
python main.py --all-markets --headed
```

### Consuming the Stream

```bash
# Pipe to your consumer
python main.py --all-markets | python your_consumer.py

# Save to file
python main.py --all-markets > stream.jsonl

# Both save and pipe
python main.py --all-markets | tee stream.jsonl | python your_ws_relay.py
```

**stdout** = data (JSONL), **stderr** = status/progress logs.

---

## Message Types

Every message is a single-line JSON object with a `"type"` field and a `"ts"` ISO timestamp.

### 1. `snapshot` — Full State

Emitted on startup and periodically (default: every 10 seconds). Contains the complete current state of all tracked events.

```json
{
  "type": "snapshot",
  "ts": "2026-03-05T14:30:00.123456+00:00",
  "data": {
    "timestamp": "2026-03-05T14:30:00.123456+00:00",
    "event_count": 42,
    "events": {
      "12345678": { ... },
      "12345679": { ... }
    }
  }
}
```

### 2. `odds` — Odds Change

Emitted when a selection's odds change.

```json
{
  "type": "odds",
  "ts": "2026-03-05T14:30:01.456+00:00",
  "fixture_id": "12345678",
  "event": "Arsenal v Chelsea",
  "market": "Full Time Result",
  "selection": "Arsenal",
  "old": "6/4",
  "new": "5/4"
}
```

### 3. `score` — Score Change

Emitted when the score updates.

```json
{
  "type": "score",
  "ts": "2026-03-05T14:31:22.789+00:00",
  "fixture_id": "12345678",
  "event": "Arsenal v Chelsea",
  "old": "1-0",
  "new": "1-1"
}
```

### 4. `new_event` — New Event Appeared

Emitted when a new in-play event starts.

```json
{
  "type": "new_event",
  "ts": "2026-03-05T14:32:00.000+00:00",
  "fixture_id": "12345680",
  "event": "Barcelona v Real Madrid",
  "competition": "La Liga"
}
```

### 5. `removed` — Event Removed

Emitted when an event ends or is removed from the in-play list.

```json
{
  "type": "removed",
  "ts": "2026-03-05T14:45:00.000+00:00",
  "fixture_id": "12345678",
  "event": "Arsenal v Chelsea"
}
```

### 6. `suspended` — Market Suspended

Emitted when a market is suspended (e.g., during a goal review).

```json
{
  "type": "suspended",
  "ts": "2026-03-05T14:31:20.000+00:00",
  "fixture_id": "12345678",
  "event": "Arsenal v Chelsea",
  "market": "Full Time Result"
}
```

### 7. `resumed` — Market Resumed

Emitted when a previously suspended market resumes trading.

```json
{
  "type": "resumed",
  "ts": "2026-03-05T14:31:45.000+00:00",
  "fixture_id": "12345678",
  "event": "Arsenal v Chelsea",
  "market": "Full Time Result"
}
```

---

## Snapshot Data Structure

The `data` field inside a `snapshot` message contains the full state:

```
data
├── timestamp        (string)  ISO timestamp
├── event_count      (int)     total number of live events
└── events           (object)  keyed by fixture_id
    └── "<fixture_id>"
        ├── name           (string)  "Team A v Team B"
        ├── competition    (string)  "Premier League"
        ├── score          (string)  "1-0" or "" if not started
        ├── time_min       (string)  match minute, e.g. "34"
        ├── time_sec       (string)  seconds within minute
        ├── started        (bool)    whether the event has kicked off
        ├── fixture_id     (string)  unique event identifier
        └── markets        (object)  keyed by market name
            └── "<market_name>"
                ├── id          (string)  internal market ID
                ├── suspended   (bool)    whether market is suspended
                └── selections  (array)
                    └── {
                          "name":      "Arsenal",
                          "odds":      "6/4",
                          "decimal":   2.5,
                          "handicap":  null or "-0.5",
                          "suspended": false
                        }
```

### Example Event

```json
{
  "12345678": {
    "name": "Arsenal v Chelsea",
    "competition": "Premier League",
    "score": "1-0",
    "time_min": "34",
    "time_sec": "12",
    "started": true,
    "fixture_id": "12345678",
    "markets": {
      "Full Time Result": {
        "id": "98765",
        "suspended": false,
        "selections": [
          {"name": "Arsenal", "odds": "6/4", "decimal": 2.5, "handicap": null, "suspended": false},
          {"name": "Draw", "odds": "2/1", "decimal": 3.0, "handicap": null, "suspended": false},
          {"name": "Chelsea", "odds": "7/4", "decimal": 2.75, "handicap": null, "suspended": false}
        ]
      },
      "Over/Under 2.5 Goals": {
        "id": "98766",
        "suspended": false,
        "selections": [
          {"name": "Over", "odds": "4/5", "decimal": 1.8, "handicap": null, "suspended": false},
          {"name": "Under", "odds": "1/1", "decimal": 2.0, "handicap": null, "suspended": false}
        ]
      }
    }
  }
}
```

---

## Market Coverage

### Without `--all-markets` (default)

You get the **overview markets** — typically the main 1-3 markets per event (Full Time Result, Match Winner, etc.). These update in real-time via the WebSocket.

### With `--all-markets`

The system navigates into each event's detail page to load **all available markets** (e.g., 100-200+ markets per football match). This includes:

- Full Time Result, Double Chance, Draw No Bet
- Over/Under (various lines: 0.5, 1.5, 2.5, 3.5...)
- Both Teams to Score
- Asian Handicap (various lines)
- Correct Score
- Half Time / Full Time
- Goals (Next Goal, Total Goals, Team Goals)
- Cards, Corners
- Player-specific markets
- Many more depending on the sport and event

Detail markets are cached after the initial load. The overview markets continue receiving live updates via the WebSocket.

Use `--refresh N` (minutes) to periodically reload detail markets and pick up new events.

---

## Odds Format

Odds arrive in **fractional format** (e.g., `"6/4"`, `"1/2"`, `"9/1"`). A pre-computed `decimal` field is included for convenience:

| Fractional | Decimal | Implied Probability |
|-----------|---------|-------------------|
| 1/2       | 1.5     | 66.7%            |
| 4/5       | 1.8     | 55.6%            |
| 1/1       | 2.0     | 50.0%            |
| 6/4       | 2.5     | 40.0%            |
| 2/1       | 3.0     | 33.3%            |
| 9/1       | 10.0    | 10.0%            |

---

## Building a Consumer

### Python — WebSocket Relay Server

Reads JSONL from stdin and broadcasts to connected WebSocket clients:

```python
import asyncio
import json
import sys
import websockets

clients = set()

async def handler(ws):
    clients.add(ws)
    try:
        await ws.wait_closed()
    finally:
        clients.discard(ws)

async def broadcast():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        for ws in list(clients):
            try:
                await ws.send(line)
            except:
                clients.discard(ws)

async def main():
    async with websockets.serve(handler, "0.0.0.0", 8765):
        await broadcast()

asyncio.run(main())
```

Usage:
```bash
python main.py --all-markets | python ws_relay.py
# Clients connect to ws://localhost:8765
```

### JavaScript — Browser Client

```javascript
const ws = new WebSocket("ws://localhost:8765");

// State: full snapshot
let events = {};

ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);

  switch (msg.type) {
    case "snapshot":
      events = msg.data.events;
      renderAll(events);
      break;

    case "odds":
      // Update specific selection in your state
      updateOdds(msg.fixture_id, msg.market, msg.selection, msg.new);
      break;

    case "score":
      // Update score display
      updateScore(msg.fixture_id, msg.new);
      break;

    case "new_event":
      // Add event placeholder (full data comes in next snapshot)
      addEvent(msg.fixture_id, msg.event, msg.competition);
      break;

    case "removed":
      // Remove event from UI
      delete events[msg.fixture_id];
      break;

    case "suspended":
      // Grey out / lock the market
      setMarketSuspended(msg.fixture_id, msg.market, true);
      break;

    case "resumed":
      // Re-enable the market
      setMarketSuspended(msg.fixture_id, msg.market, false);
      break;
  }
};
```

---

## Architecture for a Polymarket-Style Platform

```
┌──────────────┐     JSONL      ┌──────────────┐     WS       ┌──────────────┐
│  main.py     │ ──── stdout ──>│  WS Relay /  │ ──────────>  │  Browser UI  │
│  (bet365     │                │  Backend API │              │  (React/Vue) │
│   scraper)   │                │              │              │              │
└──────────────┘                │  - Store to  │              │  - Live odds │
                                │    DB        │              │  - Markets   │
                                │  - User bets │              │  - Betting   │
                                │  - Balances  │              │  - Portfolio │
                                └──────────────┘              └──────────────┘
```

### Recommended Approach

1. **Data Ingest**: Run `main.py --all-markets --refresh 10` and pipe to your backend
2. **Backend**: Parse JSONL, store events/odds in a database (Redis for speed, Postgres for history), expose via WebSocket to clients
3. **Frontend**: Connect to your backend WebSocket, render markets, handle user interactions
4. **Key Identifiers**: Use `fixture_id` as the primary key for events. Market names are stable strings. Selection names identify individual outcomes within a market.

### Data Flow Tips

- **Snapshots** give you the complete truth every N seconds — use them to reconcile state
- **Delta messages** (odds, score, etc.) give you real-time granular updates between snapshots
- Build your UI state from the initial snapshot, then apply deltas incrementally
- Use `suspended`/`resumed` to lock/unlock betting on specific markets
- The `fixture_id` is stable across the lifetime of an event

---

## CLI Reference

| Flag | Default | Description |
|------|---------|-------------|
| `--all-markets` | off | Load detail markets for every event |
| `--instances N` | 1 | Number of browser instances (parallelizes initial load) |
| `--refresh N` | 0 (off) | Re-navigate events every N minutes |
| `--snapshot-interval N` | 10 | Emit full snapshot every N seconds |
| `--headed` | headless | Show browser window (debug) |

---

## Requirements

- Python 3.10+
- `camoufox` — stealth Firefox browser (`pip install camoufox`)
- Working internet connection (no VPN issues with bet365)

```bash
pip install camoufox
camoufox fetch  # downloads the browser binary
python main.py
```
