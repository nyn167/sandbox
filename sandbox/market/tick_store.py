"""Latest tick per security, held in memory.

Each entry is an immutable tuple replaced in one assignment, so readers on another
thread never see a half-updated record. Only the feed thread writes.
"""
from __future__ import annotations

# Tuple layout
LTP, OI, VOLUME, BID, ASK, LTT, RECV = range(7)
FIELDS = ("ltp", "oi", "volume", "bid", "ask", "ltt", "recv")
EMPTY = (float("nan"), 0, 0, float("nan"), float("nan"), 0, 0.0)


class TickStore:
    __slots__ = ("ticks", "prev", "quote", "disconnect_code", "packets", "unknown")

    def __init__(self):
        self.ticks: dict[int, tuple] = {}
        self.prev: dict[int, tuple[float, int]] = {}   # sid -> (prev_close, prev_oi)
        self.quote: dict[int, tuple] = {}               # sid -> (avg_price/VWAP, open, high, low) from Quote packets
        self.disconnect_code: int | None = None         # last code-50 reason from the server
        self.packets = 0                                # total packets parsed (for ticks/sec)
        self.unknown = 0                                # packets with unrecognised response codes

    def ltp(self, sid: int) -> float:
        t = self.ticks.get(sid)
        return t[LTP] if t else float("nan")

    def get(self, sid: int) -> tuple:
        return self.ticks.get(sid, EMPTY)

    def drop(self, sids):
        for s in sids:
            self.ticks.pop(s, None)
