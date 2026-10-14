"""Dhan scrip master → NIFTY contract lookup, cached once per day.

Scrip master columns (compact CSV, verified 2026-10):
  SEM_EXM_EXCH_ID, SEM_SEGMENT, SEM_SMST_SECURITY_ID, SEM_INSTRUMENT_NAME, SEM_TRADING_SYMBOL,
  SEM_LOT_UNITS, SEM_EXPIRY_DATE, SEM_STRIKE_PRICE, SEM_OPTION_TYPE, ...
NIFTY options: NSE / D / OPTIDX / "NIFTY-Oct2026-25000-CE";  future: FUTIDX "NIFTY-Oct2026-FUT".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

# Same URL dhanhq.Security.COMPACT_CSV_URL uses; we stream it ourselves to keep memory low.
from dhanhq._security import Security

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
MARKET_CLOSE = dtime(15, 30)

USECOLS = ["SEM_EXM_EXCH_ID", "SEM_SEGMENT", "SEM_SERIES", "SEM_SMST_SECURITY_ID", "SEM_INSTRUMENT_NAME", "SEM_TRADING_SYMBOL",
           "SEM_LOT_UNITS", "SEM_EXPIRY_DATE", "SEM_STRIKE_PRICE", "SEM_OPTION_TYPE"]

# Feed segment codes (dhanhq.marketfeed.MarketFeed.IDX / NSE_FNO)
SEG_IDX = 0
SEG_NSE_FNO = 2


@dataclass
class Universe:
    index_id: int
    future_id: int
    future_symbol: str
    future_expiry: date
    expiry: date
    lot_size: int
    strike_step: float
    options: dict[float, dict[str, int]]                 # strike -> {"CE": id, "PE": id}
    meta: dict[int, tuple[str, float]] = field(default_factory=dict)  # id -> (kind, strike)

    def __post_init__(self):
        self.meta[self.index_id] = ("IDX", 0.0)
        self.meta[self.future_id] = ("FUT", 0.0)
        for k, legs in self.options.items():
            for typ, sid in legs.items():
                self.meta[sid] = (typ, k)

    @property
    def strikes(self) -> list[float]:
        return sorted(self.options)

    def atm(self, spot: float) -> float:
        return round(spot / self.strike_step) * self.strike_step

    def segment(self, sid: int) -> int:
        return SEG_IDX if sid == self.index_id else SEG_NSE_FNO

    def window(self, spot: float, each_side: int) -> list[float]:
        """ATM ± each_side strikes that exist for this expiry."""
        strikes = self.strikes
        atm = self.atm(spot)
        i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - atm))
        return strikes[max(0, i - each_side): i + each_side + 1]

    def window_ids(self, spot: float, each_side: int) -> set[int]:
        return {sid for k in self.window(spot, each_side) for sid in self.options[k].values()}


def _download_nifty_contracts(cache_file: Path, prefix: str) -> pd.DataFrame:
    raw = cache_file.with_suffix(".csv.part")
    log.info("Downloading Dhan scrip master …")
    with requests.get(Security.COMPACT_CSV_URL, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(raw, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    parts = []
    # Chunked read keeps peak RAM low (the full CSV is ~26 MB / 200k+ rows).
    for chunk in pd.read_csv(raw, usecols=USECOLS, chunksize=100_000, low_memory=False):
        nse = chunk.SEM_EXM_EXCH_ID == "NSE"
        fno = (nse & chunk.SEM_INSTRUMENT_NAME.isin(["OPTIDX", "FUTIDX"])
               & chunk.SEM_TRADING_SYMBOL.str.startswith(prefix, na=False))
        cash = nse & (chunk.SEM_SEGMENT == "E") & (chunk.SEM_SERIES == "EQ")   # NSE stocks (Nifty 50 constituents)
        m = fno | cash
        parts.append(chunk[m])
    raw.unlink(missing_ok=True)
    df = pd.concat(parts, ignore_index=True)
    df["SEM_EXPIRY_DATE"] = pd.to_datetime(df.SEM_EXPIRY_DATE).dt.date
    df.to_parquet(cache_file, index=False)
    log.info("Cached %d NIFTY contracts → %s", len(df), cache_file)
    return df


def load_nifty_contracts(cache_dir: Path, prefix: str = "NIFTY-", today: date | None = None) -> pd.DataFrame:
    today = today or datetime.now(IST).date()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"nifty_contracts_v2_{today.isoformat()}.parquet"   # v2: + NSE cash equities
    if cache_file.exists():
        df = pd.read_parquet(cache_file)
    else:
        for old in cache_dir.glob("nifty_contracts_*.parquet"):
            old.unlink()
        df = _download_nifty_contracts(cache_file, prefix)
    return df


def pick_expiry(expiries, now: datetime | None = None, nth: int = 0) -> date:
    """nth upcoming expiry still tradable (0 = nearest; today counts until 15:30 IST)."""
    now = now or datetime.now(IST)
    cutoff = now.date() if now.time() < MARKET_CLOSE else now.date() + timedelta(days=1)
    upcoming = sorted(e for e in set(expiries) if e >= cutoff)
    if len(upcoming) <= nth:
        raise RuntimeError(f"No upcoming NIFTY expiry #{nth + 1} found in scrip master")
    return upcoming[nth]


def equity_ids(df: pd.DataFrame, symbols) -> dict[str, int]:
    """NSE cash-market security IDs for trading symbols (e.g. RELIANCE → 2885)."""
    eq = df[(df.SEM_INSTRUMENT_NAME == "EQUITY")] if "SEM_INSTRUMENT_NAME" in df else df
    if "SEM_SERIES" in eq:
        eq = eq[eq.SEM_SERIES == "EQ"]
    ids = dict(zip(eq.SEM_TRADING_SYMBOL, eq.SEM_SMST_SECURITY_ID.astype(int)))
    return {s: ids[s] for s in symbols if s in ids}


def build_universe(df: pd.DataFrame, index_id: int, now: datetime | None = None, nth: int = 0) -> Universe:
    """Universe for the nth upcoming option expiry (0 = this week, 1 = next week)."""
    opts = df[df.SEM_INSTRUMENT_NAME == "OPTIDX"]
    futs = df[df.SEM_INSTRUMENT_NAME == "FUTIDX"]
    expiry = pick_expiry(opts.SEM_EXPIRY_DATE, now, nth)
    fut_expiry = pick_expiry(futs.SEM_EXPIRY_DATE, now)
    fut = futs[futs.SEM_EXPIRY_DATE == fut_expiry].iloc[0]

    chain = opts[opts.SEM_EXPIRY_DATE == expiry]
    options: dict[float, dict[str, int]] = {}
    for sid, k, typ in zip(chain.SEM_SMST_SECURITY_ID, chain.SEM_STRIKE_PRICE, chain.SEM_OPTION_TYPE):
        options.setdefault(float(k), {})[typ] = int(sid)
    options = {k: v for k, v in options.items() if "CE" in v and "PE" in v}
    strikes = sorted(options)
    # Most common gap between listed strikes (NIFTY = 50).
    step = float(pd.Series(strikes).diff().mode().iloc[0])
    return Universe(index_id=index_id, future_id=int(fut.SEM_SMST_SECURITY_ID),
                    future_symbol=fut.SEM_TRADING_SYMBOL, future_expiry=fut_expiry, expiry=expiry,
                    lot_size=int(chain.SEM_LOT_UNITS.iloc[0]), strike_step=step, options=options)


def build_mock_universe(spot: float, step: float = 50.0, strikes_each_side: int = 80, weeks_ahead: int = 0) -> Universe:
    """Synthetic universe for --mock (fake security IDs, Tuesday expiry `weeks_ahead` weeks out)."""
    today = datetime.now(IST).date()
    expiry = today + timedelta(days=(1 - today.weekday()) % 7 or 7) + timedelta(weeks=weeks_ahead)
    atm = round(spot / step) * step
    options, sid = {}, 900_000 + 50_000 * weeks_ahead
    for i in range(-strikes_each_side, strikes_each_side + 1):
        options[atm + i * step] = {"CE": sid, "PE": sid + 1}
        sid += 2
    return Universe(index_id=13, future_id=800_001, future_symbol="NIFTY-MOCK-FUT",
                    future_expiry=expiry + timedelta(days=14), expiry=expiry, lot_size=65,
                    strike_step=step, options=options)
