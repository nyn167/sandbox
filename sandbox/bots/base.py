"""Bot framework: each bot is a small class that trades through the sandbox API (paper money only).

A bot sees time through `self.clock` — real IST in live mode; in mock mode the "session" starts when the bot
starts and runs 20× faster, so a full day's logic (opening range, entries, time exits) can be tested in minutes.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from datetime import datetime, time as dtime, timedelta

from ..market.instruments import IST
from ..sdk import Sandbox

SESSION_START = dtime(9, 15)


class Clock:
    def __init__(self, mock: bool, speed: float = 20.0):
        self.mock, self.speed, self.t0 = mock, speed, time.time()

    def now(self) -> datetime:
        if not self.mock:
            return datetime.now(IST)
        start = datetime.combine(datetime.now(IST).date(), SESSION_START, IST)
        return start + timedelta(seconds=(time.time() - self.t0) * self.speed)

    def at(self, hh: int, mm: int) -> bool:
        return self.now().time() >= dtime(hh, mm)


class Bot:
    name = "bot"
    interval = 5.0                         # seconds between steps (real time)
    description = ""

    def __init__(self, sb: Sandbox, mock: bool = False):
        self.sb, self.mock = sb, mock
        self.clock = Clock(mock)
        self.log = logging.getLogger(f"bot.{self.name}")
        self.history: dict[str, deque] = defaultdict(lambda: deque(maxlen=600))   # symbol -> (ts, ltp)
        self.day = None
        self.trades_today = 0

    # ---- helpers
    def remember(self, quotes: dict[str, dict]) -> None:
        now = self.clock.now().timestamp()
        for s, q in quotes.items():
            if q.get("ltp"):
                self.history[s].append((now, q["ltp"]))

    def change(self, symbol: str, minutes: float) -> float | None:
        h = self.history.get(symbol)
        if not h or len(h) < 2:
            return None
        cut = h[-1][0] - minutes * 60
        old = next((p for t, p in h if t >= cut), None)
        return (h[-1][1] - old) / old * 100 if old else None

    def entered(self, order: dict, what: str) -> bool:
        if order.get("status") == "FILLED":
            self.trades_today += 1
            self.log.info("ENTRY %s @ %s (%s)", order["symbol"], order.get("avg_price"), what)
            return True
        self.log.info("entry refused for %s: %s", order.get("symbol"), order.get("reason"))
        return False

    # ---- lifecycle
    def new_day(self) -> None:
        """Reset per-day state (called once per session date)."""

    def step(self) -> None:
        raise NotImplementedError

    def run_once(self) -> None:
        d = self.clock.now().date()
        if d != self.day:
            self.day, self.trades_today = d, 0
            self.history.clear()
            self.new_day()
        acct = self.sb.account()
        if acct.get("market") not in ("open", "mock"):
            return
        self.step()

    def run_forever(self, stop_flag=lambda: False) -> None:
        self.log.info("started (%s)", "mock clock ×20" if self.mock else "live IST clock")
        while not stop_flag():
            t0 = time.time()
            try:
                self.run_once()
            except Exception as e:                      # one bad step must not kill the bot
                self.log.warning("step failed: %s", e)
            time.sleep(max(0.5, self.interval - (time.time() - t0)))
