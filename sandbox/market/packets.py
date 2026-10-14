"""Dhan v2 Live Market Feed binary packet parser.

Layouts from https://dhanhq.co/docs/v2/live-market-feed/ (all little-endian) and cross-checked
against dhanhq 2.3.0 marketfeed.py. Header (8 bytes): <B code, H msg_len, B segment, I security_id>.

This is the feed hot path: parse and update the store, nothing else.
"""
from __future__ import annotations

import struct
import time

from .tick_store import TickStore, LTP, OI, VOLUME, BID, ASK, LTT

HEADER = struct.Struct("<BHBI")              # 8 bytes
TICKER = struct.Struct("<fI")                # code 2: ltp, ltt                         → 16 bytes total
QUOTE = struct.Struct("<fHIfIIIffff")        # code 4: ltp, ltq, ltt, atp, vol, tsq, tbq, o, c, h, l → 50
OI_PKT = struct.Struct("<I")                 # code 5: oi                               → 12
PREV = struct.Struct("<fI")                  # code 6: prev close, prev oi              → 16
FULL = struct.Struct("<fHIfIIIIIIffff")      # code 8: ltp, ltq, ltt, atp, vol, tsq, tbq, oi, oi_hi, oi_lo, o, c, h, l
DEPTH = struct.Struct("<IIHHff")             # per level: bid_qty, ask_qty, bid_n, ask_n, bid, ask (20 bytes)
DISCONNECT = struct.Struct("<H")             # code 50: reason code                     → 10

SIZES = {2: 16, 4: 50, 5: 12, 6: 16, 7: 8, 8: 162, 50: 10}

DISCONNECT_REASONS = {
    800: "Internal server error",
    804: "Requested number of instruments exceeds limit",
    805: "Too many connections/requests (oldest socket dropped) - may get blocked",
    806: "Data APIs not subscribed",
    807: "Access token expired",
    808: "Authentication failed (client ID or token invalid)",
    809: "Access token invalid",
    810: "Client ID invalid",
    811: "Invalid expiry date",
    812: "Invalid date format",
    813: "Invalid SecurityId",
    814: "Invalid request",
}
# Reconnecting won't help for these; the user must fix credentials/subscription.
FATAL_CODES = {806, 807, 808, 809, 810}


def parse_into(store: TickStore, data: bytes) -> None:
    """Parse one websocket message (may hold several packets) into the store."""
    if not isinstance(data, (bytes, bytearray)):
        return
    ticks = store.ticks
    n = len(data)
    off = 0
    now = time.time()
    while off + 8 <= n:
        code = data[off]
        size = SIZES.get(code)
        if size is None or off + size > n:
            store.unknown += 1
            return
        _, _, _seg, sid = HEADER.unpack_from(data, off)
        p = off + 8
        if code == 8:
            ltp, _ltq, ltt, _atp, vol, _tsq, _tbq, oi, *_ = FULL.unpack_from(data, p)
            _bq, _aq, _bn, _an, bid, ask = DEPTH.unpack_from(data, p + FULL.size)
            ticks[sid] = (ltp, oi, vol, bid, ask, ltt, now)
        elif code == 2:
            ltp, ltt = TICKER.unpack_from(data, p)
            o = ticks.get(sid)
            ticks[sid] = (ltp, o[OI], o[VOLUME], o[BID], o[ASK], ltt, now) if o else \
                (ltp, 0, 0, float("nan"), float("nan"), ltt, now)
        elif code == 4:
            ltp, _ltq, ltt, atp, vol, _tsq, _tbq, op, _cl, hi, lo = QUOTE.unpack_from(data, p)
            store.quote[sid] = (atp, op, hi, lo)
            o = ticks.get(sid)
            ticks[sid] = (ltp, o[OI] if o else 0, vol, o[BID] if o else float("nan"),
                          o[ASK] if o else float("nan"), ltt, now)
        elif code == 5:
            (oi,) = OI_PKT.unpack_from(data, p)
            o = ticks.get(sid)
            ticks[sid] = (o[LTP], oi, o[VOLUME], o[BID], o[ASK], o[LTT], now) if o else \
                (float("nan"), oi, 0, float("nan"), float("nan"), 0, now)
        elif code == 6:
            store.prev[sid] = PREV.unpack_from(data, p)
        elif code == 50:
            (reason,) = DISCONNECT.unpack_from(data, p)
            store.disconnect_code = reason
        store.packets += 1
        off += size
