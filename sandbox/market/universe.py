"""Tradable instruments: NIFTY weekly options (2 expiries, ATM ± N strikes) and NSE stocks (Nifty 50 + extras).

Symbols: options "NIFTY-19OCT26-22500-CE", stocks their NSE symbol ("RELIANCE"), the index "NIFTY".
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from .instruments import IST, build_universe, equity_ids

LIST = Path(__file__).resolve().parent.parent / "data" / "nifty50_list.csv"
IDX, EQ_SEG, FNO = 0, 1, 2          # Dhan feed segment codes


@dataclass(frozen=True)
class Instrument:
    symbol: str
    sid: int
    seg: int
    kind: str                       # IDX | EQ | OPT
    lot: int = 1
    tick: float = 0.05
    expiry: date | None = None
    strike: float | None = None
    opt: str | None = None          # CE | PE
    name: str = ""


def opt_symbol(expiry: date, strike: float, typ: str) -> str:
    return f"NIFTY-{expiry:%d%b%y}-{int(strike)}-{typ}".upper()


class Registry:
    def __init__(self):
        self.by_symbol: dict[str, Instrument] = {}
        self.by_sid: dict[int, Instrument] = {}
        self.index: Instrument | None = None

    def add(self, ins: Instrument) -> None:
        self.by_symbol[ins.symbol] = ins
        self.by_sid[ins.sid] = ins
        if ins.kind == "IDX":
            self.index = ins

    def get(self, symbol: str) -> Instrument | None:
        return self.by_symbol.get(symbol.strip().upper())

    def stocks(self) -> list[Instrument]:
        return sorted((i for i in self.by_symbol.values() if i.kind == "EQ"), key=lambda i: i.symbol)

    def options(self, expiry: date | None = None) -> list[Instrument]:
        return sorted((i for i in self.by_symbol.values() if i.kind == "OPT" and (expiry is None or i.expiry == expiry)),
                      key=lambda i: (i.expiry, i.strike, i.opt))

    @property
    def expiries(self) -> list[date]:
        return sorted({i.expiry for i in self.by_symbol.values() if i.kind == "OPT"})

    def feed_list(self) -> list[tuple[int, int]]:
        return [(i.seg, i.sid) for i in self.by_symbol.values()]


def _stock_symbols(extra) -> list[str]:
    syms = list(pd.read_csv(LIST).Symbol)
    return syms + [s.upper() for s in (extra or []) if s.upper() not in syms]


def build_live(cfg: dict, df: pd.DataFrame, spot: float) -> Registry:
    m = cfg["market"]
    reg = Registry()
    reg.add(Instrument("NIFTY", m["index_security_id"], IDX, "IDX", name="NIFTY 50"))
    for n in range(m["option_expiries"]):
        u = build_universe(df, m["index_security_id"], nth=n)
        for k in u.window(spot, m["strikes_each_side"]):
            for typ, sid in u.options[k].items():
                reg.add(Instrument(opt_symbol(u.expiry, k, typ), sid, FNO, "OPT", u.lot_size, 0.05, u.expiry, k, typ))
    for sym, sid in equity_ids(df, _stock_symbols(m.get("extra_stocks"))).items():
        reg.add(Instrument(sym, sid, EQ_SEG, "EQ", 1, 0.05, name=sym))
    return reg


def _next_tuesdays(n: int) -> list[date]:
    d = datetime.now(IST).date()
    first = d + timedelta(days=(1 - d.weekday()) % 7)
    return [first + timedelta(weeks=i) for i in range(n)]


def build_mock(cfg: dict) -> Registry:
    m, spot = cfg["market"], cfg["mock"]["start_spot"]
    reg = Registry()
    reg.add(Instrument("NIFTY", 13, IDX, "IDX", name="NIFTY 50"))
    atm, sid = round(spot / 50) * 50, 900_000
    for exp in _next_tuesdays(m["option_expiries"]):
        for i in range(-m["strikes_each_side"], m["strikes_each_side"] + 1):
            for typ in ("CE", "PE"):
                reg.add(Instrument(opt_symbol(exp, atm + 50 * i, typ), sid, FNO, "OPT", 65, 0.05, exp, atm + 50 * i, typ))
                sid += 1
    for i, sym in enumerate(_stock_symbols(m.get("extra_stocks"))):
        reg.add(Instrument(sym, 700_000 + i, EQ_SEG, "EQ", 1, 0.05, name=sym))
    return reg


# ------------------------------------------------------------------------------ mock prices
def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(spot: float, strike: float, years: float, iv: float, typ: str) -> float:
    years = max(years, 1 / (365 * 24 * 4))
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * years) / (iv * math.sqrt(years))
    d2 = d1 - iv * math.sqrt(years)
    call = spot * _ncdf(d1) - strike * _ncdf(d2)
    return max(0.05, call if typ == "CE" else call - spot + strike)


class MockMarket:
    """Random-walk index + stocks, Black-Scholes options, with bid/ask. Writes into a TickStore."""

    def __init__(self, reg: Registry, store, cfg: dict):
        self.reg, self.store = reg, store
        self.rng = random.Random(cfg["mock"].get("seed"))
        self.spot = float(cfg["mock"]["start_spot"])
        self.px = {i.sid: self.rng.uniform(150, 3500) for i in reg.stocks()}
        self.status, self.fatal = "live (mock)", None
        for sid, p in self.px.items():
            store.prev[sid] = (p, 0)
        store.prev[reg.index.sid] = (self.spot, 0)
        self.step()

    def step(self) -> None:
        import time
        now = time.time()
        self.spot *= math.exp(self.rng.gauss(0, 0.00025))
        t = self.store.ticks
        t[self.reg.index.sid] = (self.spot, 0, 0, float("nan"), float("nan"), 0, now)
        for sid in self.px:
            self.px[sid] *= math.exp(self.rng.gauss(0, 0.0004))
            p = round(self.px[sid], 2)
            t[sid] = (p, 0, 0, round(p * 0.9998, 2), round(p * 1.0002, 2), 0, now)
        today = datetime.now(IST)
        for i in self.reg.options():
            close = datetime.combine(i.expiry, datetime.min.time(), IST) + timedelta(hours=15, minutes=30)
            years = max((close - today).total_seconds(), 60) / (365 * 86400)
            p = round(bs_price(self.spot, i.strike, years, 0.14, i.opt) / 0.05) * 0.05
            spread = max(0.05, round(p * 0.005 / 0.05) * 0.05)
            t[i.sid] = (p, 0, 0, max(0.05, p - spread), p + spread, 0, now)

    def stop(self):
        pass
