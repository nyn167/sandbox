"""Starter bots (paper money only). Simple, well-known intraday setups — a baseline to beat, not advice.

orb-nifty     Nifty options: buy an ATM call / put when spot breaks the 9:15–9:30 opening range.
vwap-revert   NSE stocks: buy a stock stretched ≥ 0.8 % below its VWAP once it turns up; exit back at VWAP.
momentum-50   NSE stocks: buy a stock pushing its day high, above VWAP, with Nifty also rising; trailing stop.
"""
from __future__ import annotations

from datetime import date

from .base import Bot


class OrbNifty(Bot):
    name = "orb-nifty"
    interval = 5.0
    description = "Opening-range breakout on Nifty → ATM option buy, +30 % / −20 % on premium, out by 15:00"
    budget, target, stop, max_trades, buffer = 12_000, 0.30, 0.20, 2, 5.0

    def new_day(self):
        self.or_hi = self.or_lo = None

    def _pick(self, typ: str, spot: float) -> tuple[str, int] | None:
        ch = self.sb.chain()
        exps = ch["expiries"]
        exp = exps[1] if len(exps) > 1 and exps[0] == self.clock.now().date().isoformat() else exps[0]
        if exp != ch["expiry"]:
            ch = self.sb.chain(exp)
        rows = sorted(ch["rows"], key=lambda r: abs(r["strike"] - spot))
        atm = rows[0]["strike"]
        # ATM first, then up to 3 strikes out of the money until one lot fits the budget
        cands = sorted((r for r in ch["rows"] if (r["strike"] >= atm if typ == "CE" else r["strike"] <= atm)),
                       key=lambda r: abs(r["strike"] - atm))[:4]
        for r in cands:
            q = r.get(typ)
            if q and q.get("ask") and q["ask"] * q["lot"] <= self.budget:
                return q["symbol"], q["lot"]
        return None

    def step(self):
        spot = self.sb.quote("NIFTY").get("ltp")
        if not spot:
            return
        if not self.clock.at(9, 30):
            self.or_hi = max(self.or_hi or spot, spot)
            self.or_lo = min(self.or_lo or spot, spot)
            return
        mine = [p for p in self.sb.positions() if p["qty"]]
        for p in mine:                                           # manage open trade
            q = self.sb.quote(p["symbol"])
            px = q.get("bid") or q.get("ltp")
            if not px:
                continue
            r = (px - p["avg"]) / p["avg"]
            if r >= self.target or r <= -self.stop or self.clock.at(15, 0):
                why = "target" if r >= self.target else "stop" if r <= -self.stop else "time exit"
                o = self.sb.sell(p["symbol"], p["qty"], product=p["product"], tag=why)
                self.log.info("EXIT %s %s @ %s (%+.0f%%)", p["symbol"], why, o.get("avg_price"), r * 100)
        if mine or self.or_hi is None or self.trades_today >= self.max_trades or self.clock.at(14, 30):
            return
        typ = "CE" if spot > self.or_hi + self.buffer else "PE" if spot < self.or_lo - self.buffer else None
        if typ:
            pick = self._pick(typ, spot)
            if pick:
                self.entered(self.sb.buy(pick[0], pick[1], tag=f"ORB {typ}"),
                             f"spot {spot:.1f} broke {'high' if typ == 'CE' else 'low'} {self.or_hi if typ == 'CE' else self.or_lo:.1f}")


class _StockBot(Bot):
    interval = 10.0
    per_trade, max_open, max_trades = 10_000, 2, 4

    def universe(self) -> dict[str, dict]:
        syms = [i["symbol"] for i in self.sb.instruments("EQ")]
        qs = self.sb.quotes(syms + ["NIFTY"])
        self.remember(qs)
        return qs

    def exit_all_at(self, hh: int, mm: int, mine: list[dict]) -> bool:
        if self.clock.at(hh, mm):
            for p in mine:
                self.sb.sell(p["symbol"], p["qty"], product=p["product"], tag="time exit") if p["qty"] > 0 else \
                    self.sb.buy(p["symbol"], -p["qty"], product=p["product"], tag="time exit")
            return True
        return False


class VwapRevert(_StockBot):
    name = "vwap-revert"
    description = "Stock ≥0.8 % below VWAP turning up → buy ₹10k; exit at VWAP, −0.6 % stop, out by 15:05"
    stretch, stop = 0.8, 0.6

    def step(self):
        qs = self.universe()
        mine = [p for p in self.sb.positions() if p["qty"]]
        if self.exit_all_at(15, 5, mine):
            return
        for p in mine:
            q = qs.get(p["symbol"], {})
            px, vw = q.get("bid") or q.get("ltp"), q.get("vwap")
            if px and (px <= p["avg"] * (1 - self.stop / 100) or (vw and px >= vw)):
                o = self.sb.sell(p["symbol"], p["qty"], product=p["product"], tag="exit")
                self.log.info("EXIT %s @ %s (%s)", p["symbol"], o.get("avg_price"), "at VWAP" if vw and px >= vw else "stop")
        if len(mine) >= self.max_open or self.trades_today >= self.max_trades or not self.clock.at(9, 45) or self.clock.at(14, 30):
            return
        held = {p["symbol"] for p in mine}
        best = None
        for s, q in qs.items():
            if s == "NIFTY" or s in held or not q.get("ltp") or not q.get("vwap"):
                continue
            gap = (q["ltp"] - q["vwap"]) / q["vwap"] * 100
            turn = self.change(s, 3)
            if gap <= -self.stretch and turn is not None and turn > 0.05 and (best is None or gap < best[1]):
                best = (s, gap, q)
        if best:
            s, gap, q = best
            qty = int(self.per_trade // q["ask"]) if q.get("ask") else 0
            if qty:
                self.entered(self.sb.buy(s, qty, tag="vwap revert"), f"{gap:.2f}% below VWAP, turning up")


class Momentum50(_StockBot):
    name = "momentum-50"
    description = "Stock at its day high, ≥0.5 % above VWAP, Nifty rising → buy ₹10k; 0.8 % trailing stop, out by 15:05"
    trail = 0.8

    def new_day(self):
        self.peak: dict[str, float] = {}

    def step(self):
        qs = self.universe()
        mine = [p for p in self.sb.positions() if p["qty"]]
        if self.exit_all_at(15, 5, mine):
            return
        for p in mine:
            q = qs.get(p["symbol"], {})
            px = q.get("bid") or q.get("ltp")
            if not px:
                continue
            pk = self.peak[p["symbol"]] = max(self.peak.get(p["symbol"], p["avg"]), px)
            if px <= pk * (1 - self.trail / 100):
                o = self.sb.sell(p["symbol"], p["qty"], product=p["product"], tag="trailing stop")
                self.log.info("EXIT %s trail @ %s (peak %.2f)", p["symbol"], o.get("avg_price"), pk)
        nifty = self.change("NIFTY", 15)
        if len(mine) >= self.max_open or self.trades_today >= self.max_trades or not self.clock.at(10, 0) \
                or self.clock.at(14, 30) or nifty is None or nifty <= 0:
            return
        held = {p["symbol"] for p in mine}
        best = None
        for s, q in qs.items():
            if s == "NIFTY" or s in held or not (q.get("ltp") and q.get("vwap") and q.get("high")):
                continue
            above = (q["ltp"] - q["vwap"]) / q["vwap"] * 100
            mom = self.change(s, 15)
            if q["ltp"] >= q["high"] * 0.999 and above >= 0.5 and mom is not None and mom > 0.4 and (best is None or mom > best[1]):
                best = (s, mom, q)
        if best:
            s, mom, q = best
            qty = int(self.per_trade // q["ask"]) if q.get("ask") else 0
            if qty and self.entered(self.sb.buy(s, qty, tag="momentum"), f"+{mom:.2f}% in 15m at day high"):
                self.peak[s] = q["ask"]


BOTS = {b.name: b for b in (OrbNifty, VwapRevert, Momentum50)}
