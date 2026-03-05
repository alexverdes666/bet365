"""
Zap Protocol v1 decoder for bet365 WebSocket frames.

Frame structure:
  - Multiple messages per frame, separated by \x08
  - Each message: <type_byte><topic>\x01<body>
  - Body prefixes: F| (snapshot), U| (update), I| (insert), D|| (delete)
  - Rows separated by |, fields by ;, key=value pairs
"""

from __future__ import annotations
from dataclasses import dataclass, field

# Delimiters
DELIM_RECORD = "\x01"
DELIM_MESSAGE = "\x08"

# Message type bytes
TYPES = {
    "\x14": "topic_load",
    "\x15": "delta",
    "\x16": "subscribe",
    "\x17": "unsubscribe",
    "\x19": "ping",
    "\x23": "status",
    "\x31": "server_ack",
}

# Entity type hierarchy levels (for stack-based parent detection)
# Must work for both InPlay (CL>EV>MA>PA) and 6V detail (EV>MG>MA>PA) topics
ENTITY_LEVELS = {
    "CL": 0,    # Classification (InPlay root)
    "CT": 1,    # Competition
    "EV": 2,    # Event
    "TG": 3,    # Team Group (6V detail, child of EV)
    "ES": 3,    # Event Stats (6V detail, child of EV)
    "SG": 3,    # Stat Group (6V detail, child of EV)
    "MG": 3,    # Market Group (6V detail, child of EV)
    "TE": 4,    # Team (6V detail, child of TG)
    "SC": 4,    # Stat Category (6V detail, child of ES)
    "ST": 4,    # Stat (6V detail, child of SG)
    "MA": 4,    # Market (child of MG or EV)
    "SL": 5,    # Stat Line (6V detail, child of SC)
    "CO": 5,    # Column (child of MA)
    "PA": 5,    # Selection/Participant (child of MA)
    "IN": 6,
}

# Known 2-letter entity prefixes
ENTITY_TYPES = {"CL", "CT", "EV", "MA", "PA", "CO", "MG", "IN", "ER", "OV"}


@dataclass
class ZapRow:
    entity_type: str | None  # "CL", "EV", "MA", "PA", etc.
    fields: dict[str, str] = field(default_factory=dict)


@dataclass
class ZapMessage:
    msg_type: str       # "topic_load", "delta", "subscribe", "ping", "status"
    topic: str          # e.g. "InPlay_19_0" or IT path
    operation: str      # "F" (snapshot), "U" (update), "I" (insert), "D" (delete), "" (none)
    rows: list[ZapRow] = field(default_factory=list)


def fix_encoding(text: str) -> str:
    """Fix latin-1 encoded UTF-8 text (Cyrillic names etc.)."""
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text


def _parse_row(row_text: str) -> ZapRow | None:
    """Parse a single pipe-delimited row like 'EV;NA=name;OD=1/2;'"""
    if not row_text or row_text.isspace():
        return None

    entity_type = None
    fields = {}

    parts = row_text.split(";")
    for part in parts:
        if not part:
            continue
        if "=" in part:
            key, _, value = part.partition("=")
            fields[key] = value
        elif len(part) <= 3 and part.isalpha() and part.isupper():
            # Entity type token like "CL", "EV", "MA", "PA"
            entity_type = part

    if not entity_type and not fields:
        return None

    return ZapRow(entity_type=entity_type, fields=fields)


def _parse_body(body: str) -> tuple[str, list[ZapRow]]:
    """Parse message body, returns (operation, rows)."""
    if not body:
        return ("", [])

    # Detect operation prefix
    if body.startswith("F|"):
        op = "F"
        body = body[2:]
    elif body.startswith("U|"):
        op = "U"
        body = body[2:]
    elif body.startswith("I|"):
        op = "I"
        body = body[2:]
    elif body.startswith("D|"):
        op = "D"
        return (op, [])
    else:
        op = ""

    # Split on pipe to get rows
    rows = []
    for row_text in body.split("|"):
        row = _parse_row(row_text)
        if row:
            rows.append(row)

    return (op, rows)


def decode_frame(raw: str) -> list[ZapMessage]:
    """Decode a WebSocket frame into ZapMessages.

    Args:
        raw: The raw text from a WebSocket frame (as received by Playwright).

    Returns:
        List of parsed ZapMessage objects.
    """
    # Fix encoding for readable text
    raw = fix_encoding(raw)

    # Split frame into individual messages
    raw_messages = raw.split(DELIM_MESSAGE) if DELIM_MESSAGE in raw else [raw]

    messages = []
    for raw_msg in raw_messages:
        if not raw_msg:
            continue

        # Detect message type from first byte
        first = raw_msg[0]
        msg_type = TYPES.get(first, "unknown")

        if msg_type == "unknown":
            messages.append(ZapMessage(msg_type=msg_type, topic="", operation="", rows=[]))
            continue

        # Strip type byte
        rest = raw_msg[1:]

        if msg_type in ("topic_load", "delta"):
            # Split on record delimiter: topic \x01 body
            parts = rest.split(DELIM_RECORD, 1)
            topic = parts[0] if parts else ""
            body = parts[1] if len(parts) > 1 else ""
            operation, rows = _parse_body(body)
            messages.append(ZapMessage(
                msg_type=msg_type, topic=topic, operation=operation, rows=rows
            ))

        elif msg_type in ("subscribe", "unsubscribe"):
            # \x16\x00topic1,topic2\x01  (subscribe)
            # \x17\x00topic1,topic2\x01  (unsubscribe)
            body = rest.lstrip("\x00").rstrip(DELIM_RECORD)
            topics = body.split(",") if body else []
            messages.append(ZapMessage(
                msg_type=msg_type, topic=",".join(topics), operation="", rows=[]
            ))

        elif msg_type in ("status", "server_ack"):
            messages.append(ZapMessage(
                msg_type=msg_type, topic=rest, operation="", rows=[]
            ))

        elif msg_type == "ping":
            messages.append(ZapMessage(
                msg_type=msg_type, topic="", operation="", rows=[]
            ))

    return messages
