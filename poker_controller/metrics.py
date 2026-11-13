"""Decision speed and missed actions, collected in memory and written to the database in batches.

A table worker records each PDecision answer's duration (the HTTP call only, without the human-like pause), the
street and the opponents still in the hand, and each missed action. The owner drains the collector every few
minutes and writes one row per (hour, street, opponents) and one row per missed action."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

BANDS = (250, 500, 1000, 2000, 5000)  # ms: Under250 .. Under5000, then Over5000
MAX_EVENTS = 5000


def opponents_in_hand(state: dict, hero: int | None) -> int:
    """Players still holding cards in the hand, without the hero (1..9)."""
    count = sum(1 for i, seat in enumerate(state.get("s") or []) if seat and seat.get("d") and i != hero)
    return max(1, min(9, count))


class DecisionMetrics:
    def __init__(self, clock=lambda: datetime.now(timezone.utc)):
        self.clock = clock
        self.lock = threading.Lock()
        self.rows: dict = {}
        self.events: list = []

    def decision(self, street: int, opponents: int, ms: float):
        if not 1 <= int(street or 0) <= 4:
            return
        ms = max(0, int(round(ms)))
        hour = self.clock().replace(minute=0, second=0, microsecond=0)
        key = (hour, int(street), max(1, min(9, int(opponents or 1))))
        with self.lock:
            row = self.rows.get(key)
            if row is None:
                row = self.rows[key] = {"count": 0, "total": 0, "max": 0, "bands": [0] * (len(BANDS) + 1)}
            row["count"] += 1
            row["total"] += ms
            row["max"] = max(row["max"], ms)
            row["bands"][next((i for i, limit in enumerate(BANDS) if ms < limit), len(BANDS))] += 1

    def missed(self, kind: str, username: str | None, table) -> None:
        with self.lock:
            if len(self.events) < MAX_EVENTS:
                self.events.append({"kind": kind, "username": username, "table": None if table is None else str(table),
                                    "at": self.clock()})

    def drain(self) -> tuple[list[dict], list[dict]]:
        with self.lock:
            rows, events = self.rows, self.events
            self.rows, self.events = {}, []
        out = [{"hour": hour, "street": street, "opponents": opponents, "decisions": r["count"], "total_ms": r["total"],
                "max_ms": r["max"], "bands": r["bands"]} for (hour, street, opponents), r in rows.items()]
        return out, events

    def restore(self, rows: list[dict], events: list[dict]) -> None:
        """Put a batch back after a failed write; it is merged into the next one."""
        with self.lock:
            for r in rows:
                key = (r["hour"], r["street"], r["opponents"])
                row = self.rows.setdefault(key, {"count": 0, "total": 0, "max": 0, "bands": [0] * (len(BANDS) + 1)})
                row["count"] += r["decisions"]
                row["total"] += r["total_ms"]
                row["max"] = max(row["max"], r["max_ms"])
                row["bands"] = [a + b for a, b in zip(row["bands"], r["bands"])]
            self.events = (events + self.events)[:MAX_EVENTS]
