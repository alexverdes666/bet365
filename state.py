"""
Stateful entity tree manager for bet365 WebSocket data.

Maintains a live tree of entities (CL > EV > MA > PA) indexed by their
IT= topic path for O(1) delta application. Emits change events for
meaningful updates (odds, scores, suspensions).
"""

from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from protocol import ZapMessage, ZapRow, ENTITY_LEVELS

log = logging.getLogger(__name__)


@dataclass
class ChangeEvent:
    kind: str           # "odds", "score", "new_event", "removed", "suspended", "resumed", "field"
    entity_type: str    # "EV", "MA", "PA", etc.
    it: str             # entity IT path
    fields: dict        # current fields
    changed: dict       # field -> (old, new)
    context: dict       # parent info for display (event name, market name, etc.)


@dataclass
class Entity:
    entity_type: str
    it: str
    fields: dict[str, str] = field(default_factory=dict)
    children: list[Entity] = field(default_factory=list)
    parent: Entity | None = field(default=None, repr=False)


def _is_ov_mirror(it: str) -> bool:
    """True for OV-mirror entities/topics (NOT OVM — that has unique market data)."""
    return it.startswith("OV") and not it.startswith("OVM")


class StateManager:
    def __init__(self, on_change: Callable[[ChangeEvent], None] | None = None):
        self.entities: dict[str, Entity] = {}  # IT -> Entity
        self.topics: dict[str, list[Entity]] = {}  # topic_name -> root entities
        self.on_change = on_change
        self._market_cache: dict[str, dict] = {}  # fi -> {market_name: market_data}

    # Topics that contain actual sports data (not config/endpoints)
    SPORTS_TOPIC_PREFIXES = ("InPlay", "OVInPlay", "OVM", "L", "OV", "P")

    # Entity types that indicate sports data topics
    SPORTS_ENTITIES = {"CL", "CT", "EV", "MA", "PA", "MG", "CO"}

    def apply(self, msg: ZapMessage) -> list[ChangeEvent]:
        """Apply a decoded message to the state. Returns change events."""
        if msg.msg_type == "topic_load" and msg.operation == "F":
            # Only process topics with entity rows that have proper entity types
            if msg.rows and any(r.entity_type in self.SPORTS_ENTITIES for r in msg.rows):
                return self._apply_snapshot(msg.topic, msg.rows)
        elif msg.msg_type == "delta":
            if msg.operation == "U":
                return self._apply_update(msg.topic, msg.rows)
            elif msg.operation == "I":
                return self._apply_insert(msg.topic, msg.rows)
            elif msg.operation == "D":
                return self._apply_delete(msg.topic)
        return []

    def _apply_snapshot(self, topic: str, rows: list[ZapRow]) -> list[ChangeEvent]:
        """Process a full snapshot (F| message). Builds entity hierarchy."""
        # Clear previous data for this topic
        if topic in self.topics:
            for root in self.topics[topic]:
                self._remove_entity_tree(root)

        # Stack-based hierarchy builder
        roots = []
        stack: list[tuple[int, Entity]] = []  # (level, entity)
        changes = []

        for row in rows:
            et = row.entity_type or "?"
            it = row.fields.get("IT", "")

            entity = Entity(entity_type=et, it=it, fields=dict(row.fields))

            level = ENTITY_LEVELS.get(et, 99)

            # Pop stack until we find a valid parent (strictly lower level)
            while stack and stack[-1][0] >= level:
                stack.pop()

            if stack:
                parent = stack[-1][1]
                entity.parent = parent
                parent.children.append(entity)
            else:
                roots.append(entity)

            stack.append((level, entity))

            # Register in index
            if it:
                self.entities[it] = entity

            # Emit new event for non-OV-mirror topics (OVM has unique data)
            if et == "EV" and not _is_ov_mirror(it):
                changes.append(self._make_change("new_event", entity, {}))

        self.topics[topic] = roots
        return changes

    def _apply_update(self, topic: str, rows: list[ZapRow]) -> list[ChangeEvent]:
        """Apply field updates to an existing entity."""
        entity = self.entities.get(topic)
        if not entity:
            return []

        # Skip OV-mirror change events (but keep OVM — unique market data)
        emit = not _is_ov_mirror(topic)

        changes = []
        # Merge fields from all rows (usually just one for U| operations)
        for row in rows:
            changed = {}
            for key, new_val in row.fields.items():
                old_val = entity.fields.get(key)
                if old_val != new_val:
                    changed[key] = (old_val, new_val)
                    entity.fields[key] = new_val

            if changed and emit:
                kind = self._classify_change(entity.entity_type, changed)
                changes.append(self._make_change(kind, entity, changed))

        # Also handle bare field updates (no entity type, just fields on the topic)
        if not rows:
            return changes

        # If rows had no entity type but had fields, apply directly
        for row in rows:
            if row.entity_type is None and row.fields:
                changed = {}
                for key, new_val in row.fields.items():
                    old_val = entity.fields.get(key)
                    if old_val != new_val:
                        changed[key] = (old_val, new_val)
                        entity.fields[key] = new_val
                if changed:
                    kind = self._classify_change(entity.entity_type, changed)
                    changes.append(self._make_change(kind, entity, changed))

        return changes

    def _apply_insert(self, topic: str, rows: list[ZapRow]) -> list[ChangeEvent]:
        """Insert new entity. Topic path: parent_IT/new_IT."""
        # Extract parent IT from topic path
        parts = topic.rsplit("/", 1)
        if len(parts) < 2:
            return []

        parent_it = parts[0]
        parent = self.entities.get(parent_it)

        changes = []
        for row in rows:
            et = row.entity_type or "?"
            it = row.fields.get("IT", "")

            entity = Entity(entity_type=et, it=it, fields=dict(row.fields))
            entity.parent = parent

            if parent:
                parent.children.append(entity)

            if it:
                self.entities[it] = entity

            if et == "EV":
                changes.append(self._make_change("new_event", entity, {}))

        return changes

    def _apply_delete(self, topic: str) -> list[ChangeEvent]:
        """Delete entity. Topic path ends with the entity's IT."""
        # Last segment of / path is the entity IT
        parts = topic.rsplit("/", 1)
        it = parts[-1]

        entity = self.entities.get(it)
        if not entity:
            return []

        changes = []
        if entity.entity_type == "EV":
            changes.append(self._make_change("removed", entity, {}))

        # Remove from parent
        if entity.parent:
            try:
                entity.parent.children.remove(entity)
            except ValueError:
                pass

        # Recursively remove from index
        self._remove_entity_tree(entity)

        return changes

    def _remove_entity_tree(self, entity: Entity, _depth: int = 0):
        """Recursively remove entity and all children from the index."""
        if _depth > 20:
            return
        for child in entity.children:
            self._remove_entity_tree(child, _depth + 1)
        self.entities.pop(entity.it, None)

    def _classify_change(self, entity_type: str, changed: dict) -> str:
        """Determine the kind of change based on entity type and fields."""
        if "OD" in changed:
            return "odds"
        if "SS" in changed:
            return "score"
        if "SU" in changed:
            _, new = changed["SU"]
            return "suspended" if new == "1" else "resumed"
        return "field"

    def _make_change(self, kind: str, entity: Entity, changed: dict) -> ChangeEvent:
        """Create a ChangeEvent with parent context for display."""
        ctx = self._get_context(entity)
        event = ChangeEvent(
            kind=kind,
            entity_type=entity.entity_type,
            it=entity.it,
            fields=dict(entity.fields),
            changed=changed,
            context=ctx,
        )
        if self.on_change:
            self.on_change(event)
        return event

    def _get_context(self, entity: Entity) -> dict:
        """Walk up the tree to build display context."""
        ctx = {}
        current = entity
        seen = set()
        while current and id(current) not in seen:
            seen.add(id(current))
            if current.entity_type == "EV":
                ctx["event_name"] = current.fields.get("NA", "")
                ctx["competition"] = current.fields.get("CT", "")
                ctx["score"] = current.fields.get("SS", "")
                ctx["fixture_id"] = current.fields.get("FI", "")
            elif current.entity_type == "MA":
                # Only set market_name if not already set (prefer MA over MG)
                if "market_name" not in ctx:
                    ctx["market_name"] = current.fields.get("NA", "")
            elif current.entity_type == "MG":
                ctx.setdefault("market_group", current.fields.get("NA", ""))
                # Use MG name as market_name if MA didn't have one
                if not ctx.get("market_name"):
                    ctx["market_name"] = current.fields.get("NA", "")
            elif current.entity_type == "CL":
                ctx["classification"] = current.fields.get("NA", "")
            current = current.parent
        return ctx

    def snapshot_markets(self, fi: str) -> dict:
        """Snapshot all current markets for a fixture and cache them.

        Called after navigating into an event detail page to persist market
        data before the 6V subscription gets cleaned up on navigation away.
        """
        markets: dict = {}
        for entity in self.entities.values():
            if entity.entity_type != "EV":
                continue
            entity_fi = entity.fields.get("FI", "")
            # Match by FI field, or by IT pattern for 6V entities (6V{fixtureID}...)
            if entity_fi == fi or (fi and entity.it.startswith(f"6V{fi}")):
                self._collect_markets(entity, markets)
        if markets:
            self._market_cache[fi] = markets
        return markets

    def to_json(self) -> dict:
        """Export clean JSON of all events with markets and selections."""
        events = {}

        for entity in self.entities.values():
            if entity.entity_type != "EV":
                continue

            # Skip OV-mirror duplicates — keep OVM (unique markets) and L-prefixed (canonical)
            it = entity.it
            if _is_ov_mirror(it):
                continue

            fi = entity.fields.get("FI", "")
            if not fi:
                continue

            # Merge into existing event if we already have it (InPlay overview + 6V detail)
            if fi not in events:
                events[fi] = {
                    "name": entity.fields.get("NA", ""),
                    "competition": entity.fields.get("CT", ""),
                    "score": entity.fields.get("SS", ""),
                    "time_min": entity.fields.get("TM", ""),
                    "time_sec": entity.fields.get("TS", ""),
                    "started": entity.fields.get("FS") == "1",
                    "fixture_id": fi,
                    "markets": {},
                }

            ev = events[fi]
            # Update live fields if this entity has them
            if entity.fields.get("SS"):
                ev["score"] = entity.fields["SS"]
            if entity.fields.get("TM"):
                ev["time_min"] = entity.fields["TM"]
            if entity.fields.get("TS"):
                ev["time_sec"] = entity.fields["TS"]

            self._collect_markets(entity, ev["markets"])

        # Merge cached detailed markets (from 6V snapshots captured during navigation)
        for fi, cached_markets in self._market_cache.items():
            if fi in events:
                for mkt_name, mkt_data in cached_markets.items():
                    # Don't overwrite live markets (InPlay data is more current)
                    if mkt_name not in events[fi]["markets"]:
                        events[fi]["markets"][mkt_name] = mkt_data

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_count": len(events),
            "events": events,
        }

    def _collect_markets(self, parent: Entity, markets: dict):
        """Recursively collect markets from entity children (handles both EV>MA and EV>MG>MA)."""
        for child in parent.children:
            if child.entity_type == "MG":
                # Market Group — use MG name as the group, recurse for MA children
                mg_name = child.fields.get("NA", "")
                for ma in child.children:
                    if ma.entity_type == "MA":
                        market_name = mg_name or ma.fields.get("NA", ma.fields.get("ID", ""))
                        self._add_market(ma, market_name, markets)
            elif child.entity_type == "MA":
                market_name = child.fields.get("NA", child.fields.get("ID", ""))
                self._add_market(child, market_name, markets)

    def _add_market(self, ma: Entity, market_name: str, markets: dict):
        """Add a single market with its selections to the markets dict."""
        # If market name already exists, append a suffix to avoid overwriting
        key = market_name
        if key in markets:
            mid = ma.fields.get("ID", "")
            key = f"{market_name} ({mid})" if mid else market_name

        market = {
            "id": ma.fields.get("ID", ""),
            "suspended": ma.fields.get("SU") == "1",
            "selections": [],
        }
        for pa in ma.children:
            if pa.entity_type != "PA":
                continue
            sel = {
                "name": pa.fields.get("NA", ""),
                "odds": pa.fields.get("OD", ""),
                "decimal": _frac_to_dec(pa.fields.get("OD", "")),
                "handicap": pa.fields.get("HA") or None,
                "suspended": pa.fields.get("SU") == "1",
            }
            market["selections"].append(sel)

        markets[key] = market


def _frac_to_dec(odds: str) -> float | None:
    """Convert fractional odds like '9/1' to decimal 10.0."""
    if not odds or "/" not in odds:
        return None
    try:
        num, den = odds.split("/")
        if int(den) == 0:
            return None
        return round(int(num) / int(den) + 1, 3)
    except (ValueError, ZeroDivisionError):
        return None
