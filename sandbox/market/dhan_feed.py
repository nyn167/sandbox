"""Live Dhan v2 market feed.

Uses dhanhq.marketfeed.MarketFeed for the connection URL, JSON subscribe (RequestCode 21) and
unsubscribe (22) messages, but runs our own receive loop so we get:
  - a parse-only hot path (packets.parse_into, no string formatting / printing),
  - exponential-backoff reconnects that resubscribe the *current* instrument list,
  - disconnect reason codes logged, and no retry on credential errors (806-810).

The feed runs in its own thread + event loop (dhanhq's MarketFeed creates its own loop).
"""
from __future__ import annotations

import asyncio
import logging
import threading

import websockets
from dhanhq import DhanContext
from dhanhq.marketfeed import MarketFeed

from ..config import TOKEN_HELP
from .packets import DISCONNECT_REASONS, FATAL_CODES, parse_into
from .tick_store import TickStore

log = logging.getLogger(__name__)
FULL = MarketFeed.Full      # 21
TICKER = MarketFeed.Ticker  # 15
QUOTE = MarketFeed.Quote    # 17
# Verified live 2026-10-12: Dhan sends NOTHING for indices (IDX_I) subscribed in FULL mode;
# Ticker mode streams them (code 2 packets ~10/s + one code 6 previous-close packet).
# Futures and options use FULL (LTP, OI, volume, depth).


def _mode(seg: int) -> int:
    """Index → Ticker; cash stocks (Nifty 50 constituents) → Quote (LTP + VWAP + OHLC); F&O → Full."""
    if seg == MarketFeed.IDX:
        return TICKER
    if seg == MarketFeed.NSE:
        return QUOTE
    return FULL


class _LeanMarketFeed(MarketFeed):
    def process_data(self, data):  # never called by our loop; kept so the base class stays lean
        return None


class DhanFeed:
    def __init__(self, client_id: str, access_token: str, store: TickStore, backoff_max: float = 60):
        self._client_id = client_id
        self._token = access_token
        self.store = store
        self.backoff_max = backoff_max
        self.status = "init"
        self.fatal: str | None = None
        self.reconnects = 0
        self._feed: _LeanMarketFeed | None = None
        self._running = False
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- public API (called from the main thread) -------------------------------------------
    def start(self, instruments: list[tuple[int, int]]):
        self._initial = [(seg, str(sid), _mode(seg)) for seg, sid in instruments]
        self._running = True
        self._thread = threading.Thread(target=self._thread_main, name="dhan-feed", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def subscribe(self, instruments: list[tuple[int, int]]):
        if instruments and self._feed:
            self._feed.subscribe_symbols([(seg, str(sid), _mode(seg)) for seg, sid in instruments])

    def unsubscribe(self, instruments: list[tuple[int, int]]):
        if instruments and self._feed:
            self._feed.unsubscribe_symbols([(seg, str(sid), _mode(seg)) for seg, sid in instruments])

    def stop(self):
        self._running = False
        if self._feed and self._feed.ws is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(self._feed.ws.close(), self._feed.loop)
                fut.result(timeout=3)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)

    # ---- feed thread ---------------------------------------------------------------------------
    def _redact(self, msg: str) -> str:
        return msg.replace(self._token, "<token>")

    def _thread_main(self):
        ctx = DhanContext(self._client_id, self._token)
        self._feed = _LeanMarketFeed(ctx, self._initial, version="v2")
        self._ready.set()
        loop = self._feed.loop
        loop.run_until_complete(self._supervise())
        # let websockets' keepalive / close tasks finish so nothing is left pending at exit
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    async def _supervise(self):
        feed, store = self._feed, self.store
        delay = 1.0
        while self._running:
            store.disconnect_code = None
            try:
                self.status = "connecting"
                feed.ws = None
                await feed.connect()            # opens socket + subscribes feed.instruments
                self.status = "live"
                log.info("Dhan feed connected, %d instruments subscribed (index: Ticker, F&O: FULL)",
                         len(feed.instruments))
                delay = 1.0
                ws = feed.ws
                while self._running:
                    parse_into(store, await ws.recv())
            except websockets.ConnectionClosed as e:
                rc = e.rcvd
                log.warning("Dhan feed closed (ws code=%s reason=%r)",
                            rc.code if rc else None, rc.reason if rc else "")
            except websockets.InvalidStatus as e:
                code = e.response.status_code
                log.error("Dhan feed handshake rejected: HTTP %s", code)
                if code in (401, 403):
                    self.fatal = f"HTTP {code} on connect — access token invalid or expired."
            except Exception as e:  # network down, DNS, etc.
                log.warning("Dhan feed error: %s", self._redact(f"{type(e).__name__}: {e}"))

            code = store.disconnect_code
            if code is not None:
                log.error("Dhan disconnect code %s: %s", code, DISCONNECT_REASONS.get(code, "unknown"))
                if code in FATAL_CODES:
                    self.fatal = f"Dhan disconnect {code}: {DISCONNECT_REASONS[code]}"
            if self.fatal:
                self.status = "stopped"
                log.error("%s Not reconnecting.%s", self.fatal, TOKEN_HELP)
                self._running = False
                break
            if not self._running:
                break
            self.status = f"reconnect in {delay:.0f}s"
            self.reconnects += 1
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.backoff_max)
        self.status = "stopped"
