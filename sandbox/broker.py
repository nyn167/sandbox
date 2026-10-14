"""Paper broker — PAPER MONEY ONLY. There is deliberately no code path to a real broker.

One shared ₹ pot; every order belongs to an agent (bot / API client), so P&L and limits are per agent.

Fills
  MARKET  buy at the ask (sell at the bid); without a two-sided quote, LTP ± slippage. Whole quantity, no partials.
  LIMIT   rests until marketable, fills at the limit or better.
  SLM     stop-market: triggers when LTP crosses the trigger, then fills like MARKET.
Products
  options: MIS (closed 15:15) or NRML (carried; settled at intrinsic value after expiry). Buying only — selling
           is allowed only to close a long (no option writing: its margin is far above the pot).
  stocks:  MIS (intraday, long or short, closed 15:15) or CNC (delivery, long only).
Margin: cash is blocked at value × rate (options/CNC 100 %, MIS 1/leverage) and released with P&L on close.
"""
from __future__ import annotations

import logging
import math
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, time as dtime

from .market.instruments import IST
from .market.tick_store import ASK, BID, LTP, RECV

log = logging.getLogger(__name__)
EPS = 1e-9


@dataclass
class Order:
    id: str
    agent: str
    symbol: str
    side: str                    # BUY | SELL
    qty: int
    type: str                    # MARKET | LIMIT | SLM
    product: str                 # MIS | NRML | CNC
    price: float | None = None
    trigger: float | None = None
    status: str = "OPEN"         # OPEN | FILLED | CANCELLED | REJECTED
    created: float = 0.0
    updated: float = 0.0
    filled_qty: int = 0
    avg_price: float | None = None
    charges: float = 0.0
    reason: str = ""
    tag: str = ""


@dataclass
class Position:
    agent: str
    symbol: str
    product: str
    qty: int = 0                 # signed
    avg: float = 0.0
    margin: float = 0.0          # cash blocked for this position
    realized: float = 0.0        # gross, before charges
    charges: float = 0.0


class OrderError(ValueError):
    pass


def _t(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _ok(x) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x) and x > 0


class Broker:
    def __init__(self, cfg: dict, reg, store, db, mock: bool = False, clock=time.time):
        self.cfg, self.reg, self.store, self.db, self.mock, self.clock = cfg, reg, store, db, mock, clock
        m = cfg["market"]
        self.t_open, self.t_close, self.t_sq = _t(m["open"]), _t(m["close"]), _t(m["squareoff"])
        self.orders: dict[str, Order] = {}
        self.positions: dict[tuple, Position] = {}
        self.halted = ""                                   # reason, empty = trading allowed
        self.agent_blocked: dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------------ state
    def _load(self):
        db = self.db
        self.cash = db.get("cash", float(self.cfg["account"]["capital"]))
        self.day = db.get("day")
        self.day_start_equity = db.get("day_start_equity", self.cash)
        self.halted = db.get("halted", "")
        self.agent_blocked = db.get("agent_blocked", {})
        for r in db.q("SELECT data FROM orders WHERE status='OPEN'"):
            o = Order(**__import__("json").loads(r["data"]))
            self.orders[o.id] = o
        for r in db.q("SELECT data FROM positions"):
            p = Position(**__import__("json").loads(r["data"]))
            self.positions[(p.agent, p.symbol, p.product)] = p
        self._roll_day()

    def _roll_day(self):
        """New trading day: record the starting equity (loss limits count from it) and clear blocks."""
        today = self._now().date().isoformat()
        if self.day != today:
            self.day = today
            self.day_start_equity = self.equity()
            self.halted, self.agent_blocked = "", {}
            self.db.set("day", self.day)
            self.db.set("day_start_equity", self.day_start_equity)
            self._save_state()

    def _save_state(self):
        self.db.set("cash", round(self.cash, 4))
        self.db.set("halted", self.halted)
        self.db.set("agent_blocked", self.agent_blocked)

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), IST)

    # ------------------------------------------------------------------ prices
    def quote(self, symbol: str) -> dict | None:
        ins = self.reg.get(symbol)
        if not ins:
            return None
        t = self.store.ticks.get(ins.sid)
        prev = self.store.prev.get(ins.sid)
        if not t:
            return {"symbol": ins.symbol, "ltp": None, "bid": None, "ask": None, "age": None,
                    "prev_close": prev[0] if prev else None}
        f = lambda v: round(v, 2) if _ok(v) else None
        return {"symbol": ins.symbol, "ltp": f(t[LTP]), "bid": f(t[BID]), "ask": f(t[ASK]),
                "age": round(self.clock() - t[RECV], 1) if t[RECV] else None,
                "prev_close": f(prev[0]) if prev else None, "lot": ins.lot, "kind": ins.kind,
                "expiry": ins.expiry.isoformat() if ins.expiry else None, "strike": ins.strike, "opt": ins.opt,
                **self._day_stats(ins.sid)}

    def _day_stats(self, sid: int) -> dict:
        """VWAP and day high/low from Dhan Quote packets (stocks); empty when not available."""
        q = self.store.quote.get(sid)
        if not q:
            return {}
        f = lambda v: round(v, 2) if _ok(v) else None
        return {"vwap": f(q[0]), "open": f(q[1]), "high": f(q[2]), "low": f(q[3])}

    def _fill_price(self, ins, side: str) -> float | None:
        t = self.store.ticks.get(ins.sid)
        if not t or not _ok(t[LTP]):
            return None
        if not self.mock and self.clock() - t[RECV] > self.cfg["market"]["stale_after_sec"]:
            return None
        slip = self.cfg["fills"]["slippage_pct"]["OPT" if ins.kind == "OPT" else "EQ"] / 100
        px = (t[ASK] if _ok(t[ASK]) else t[LTP] * (1 + slip)) if side == "BUY" else \
             (t[BID] if _ok(t[BID]) else t[LTP] * (1 - slip))
        return self._round(px, ins.tick)

    def mark(self, symbol: str) -> float | None:
        ins = self.reg.get(symbol)
        t = self.store.ticks.get(ins.sid) if ins else None
        return t[LTP] if t and _ok(t[LTP]) else None

    @staticmethod
    def _round(px: float, tick: float) -> float:
        return round(round(px / tick) * tick, 2)

    # ------------------------------------------------------------------ charges / margin
    def charges(self, ins, side: str, product: str, value: float) -> float:
        c = self.cfg["charges"]
        if ins.kind == "OPT":
            k, kp = "OPT", "OPT"
        else:
            k, kp = "EQ", ("EQ_CNC" if product == "CNC" else "EQ_MIS")
        # intraday (MIS) pays ₹20 per executed order + 18 % GST on it; swing (option NRML / stock CNC) pays
        # no brokerage - only the statutory charges (STT, exchange, SEBI, stamp duty, GST on those fees)
        brokerage = c["brokerage_per_order"][kp if ins.kind == "EQ" else ("OPT_MIS" if product == "MIS" else "OPT_NRML")]
        stt = 0.0
        if ins.kind == "OPT" and side == "SELL":
            stt = value * c["stt_pct"]["OPT_SELL"] / 100
        elif kp == "EQ_MIS" and side == "SELL":
            stt = value * c["stt_pct"]["EQ_MIS_SELL"] / 100
        elif kp == "EQ_CNC":
            stt = value * c["stt_pct"]["EQ_CNC"] / 100
        exch = value * c["exchange_pct"][k] / 100
        sebi = value * c["sebi_per_crore"] / 1e7
        stamp = value * c["stamp_pct_buy"][kp] / 100 if side == "BUY" else 0.0
        gst = (brokerage + exch + sebi) * c["gst_pct"] / 100
        return round(brokerage + stt + exch + sebi + stamp + gst, 2)

    def margin_rate(self, ins, product: str) -> float:
        return 1.0 / max(1.0, self.cfg["fills"]["mis_leverage"]) if ins.kind == "EQ" and product == "MIS" else 1.0

    # ------------------------------------------------------------------ account views
    def unrealized(self, p: Position) -> float:
        m = self.mark(p.symbol)
        return (m - p.avg) * p.qty if m is not None and p.qty else 0.0

    def equity(self) -> float:
        return self.cash + sum(p.margin + self.unrealized(p) for p in self.positions.values())

    def agent_pnl_today(self, agent: str) -> float:
        start = datetime.combine(self._now().date(), dtime(0, 0), IST).timestamp()
        r = self.db.q("SELECT COALESCE(SUM(pnl),0) AS pnl, COALESCE(SUM(charges),0) AS ch FROM trades WHERE agent=? AND ts>=?",
                      (agent, start))[0]
        return r["pnl"] - r["ch"] + sum(self.unrealized(p) for p in self.positions.values() if p.agent == agent)

    # ------------------------------------------------------------------ orders
    def place(self, agent: str, symbol: str, side: str, qty: int, type: str = "MARKET", product: str | None = None,
              price: float | None = None, trigger: float | None = None, tag: str = "") -> Order:
        now = self.clock()
        ins = self.reg.get(symbol or "")
        side, type = (side or "").upper(), (type or "MARKET").upper()
        o = Order(uuid.uuid4().hex[:12], agent, ins.symbol if ins else str(symbol), side, int(qty or 0), type,
                  (product or ("MIS")).upper(), price, trigger, "OPEN", now, now, tag=str(tag)[:40])
        try:
            self._validate(o, ins)
        except OrderError as e:
            o.status, o.reason = "REJECTED", str(e)
            self.db.save_order(asdict(o))
            return o
        self.orders[o.id] = o
        self.db.save_order(asdict(o))
        self._try_fill(o)
        return o

    def _validate(self, o: Order, ins) -> None:
        if ins is None or ins.kind not in ("OPT", "EQ"):
            raise OrderError(f"unknown or untradable symbol {o.symbol}")
        if o.side not in ("BUY", "SELL"):
            raise OrderError("side must be BUY or SELL")
        if o.qty <= 0 or (ins.kind == "OPT" and o.qty % ins.lot):
            raise OrderError(f"quantity must be a positive multiple of the lot size ({ins.lot})")
        if o.type not in ("MARKET", "LIMIT", "SLM"):
            raise OrderError("type must be MARKET, LIMIT or SLM")
        ok_products = ("MIS", "NRML") if ins.kind == "OPT" else ("MIS", "CNC")
        if o.product not in ok_products:
            raise OrderError(f"product for {ins.kind} must be one of {ok_products}")
        if o.type == "LIMIT":
            if not _ok(o.price):
                raise OrderError("LIMIT needs a positive price")
            o.price = self._round(float(o.price), ins.tick)
        if o.type == "SLM":
            if not _ok(o.trigger):
                raise OrderError("SLM needs a positive trigger")
            o.trigger = self._round(float(o.trigger), ins.tick)
        now = self._now()
        if not self.mock:
            if now.weekday() >= 5 or not (self.t_open <= now.time() < self.t_close):
                raise OrderError("market closed (09:15–15:30 IST, weekdays)")
            if ins.expiry and ins.expiry < now.date():
                raise OrderError("contract expired")
        pos = self.positions.get((o.agent, o.symbol, o.product))
        held = pos.qty if pos else 0
        signed = o.qty if o.side == "BUY" else -o.qty
        closing = held != 0 and (held > 0) != (signed > 0)
        if closing and abs(signed) > abs(held) and not (ins.kind == "EQ" and o.product == "MIS"):
            raise OrderError(f"can only close up to {abs(held)} (no reversal for this product)")
        if not closing:
            if ins.kind == "OPT" and o.side == "SELL":
                raise OrderError("option selling (writing) is not allowed — sell only to close a long")
            if o.product == "CNC" and o.side == "SELL":
                raise OrderError("CNC is delivery: you can only sell shares you hold")
            if self.halted:
                raise OrderError(f"trading halted: {self.halted}")
            if o.agent in self.agent_blocked:
                raise OrderError(f"agent blocked for today: {self.agent_blocked[o.agent]}")
            if o.product == "MIS" and not self.mock and now.time() >= self.t_sq:
                raise OrderError("no new intraday (MIS) positions after square-off time")
            a = self.cfg["agents"]
            open_pos = sum(1 for p in self.positions.values() if p.agent == o.agent and p.qty)
            if not held and open_pos >= a["max_open_positions"]:
                raise OrderError(f"max {a['max_open_positions']} open positions per agent")
            ref = o.price if o.type == "LIMIT" else (o.trigger if o.type == "SLM" else None) or \
                self._fill_price(ins, o.side) or self.mark(o.symbol)
            if ref is None:
                raise OrderError("no live price for this instrument")
            value = ref * o.qty
            if value > a["max_order_value"]:
                raise OrderError(f"order value ₹{value:,.0f} above the ₹{a['max_order_value']:,} per-order limit")
            need = value * self.margin_rate(ins, o.product) + self.charges(ins, o.side, o.product, value)
            reserved = sum(self._reserve(x) for x in self.orders.values() if x.status == "OPEN")
            if need > self.cash - reserved + EPS:
                raise OrderError(f"insufficient funds: need ₹{need:,.0f}, available ₹{self.cash - reserved:,.0f}")

    def _reserve(self, o: Order) -> float:
        """Cash earmarked by a resting opening order (so two orders can't spend the same rupees)."""
        ins = self.reg.get(o.symbol)
        pos = self.positions.get((o.agent, o.symbol, o.product))
        signed = o.qty if o.side == "BUY" else -o.qty
        if not ins or (pos and pos.qty and (pos.qty > 0) != (signed > 0)):
            return 0.0
        ref = o.price or o.trigger or self.mark(o.symbol) or 0.0
        return ref * o.qty * self.margin_rate(ins, o.product)

    def cancel(self, agent: str | None, order_id: str, reason: str = "cancelled") -> Order | None:
        o = self.orders.get(order_id)
        if not o or (agent and o.agent != agent) or o.status != "OPEN":
            return None
        o.status, o.reason, o.updated = "CANCELLED", reason, self.clock()
        self.db.save_order(asdict(o))
        del self.orders[o.id]
        return o

    def _try_fill(self, o: Order) -> bool:
        ins = self.reg.get(o.symbol)
        t = self.store.ticks.get(ins.sid)
        ltp = t[LTP] if t else None
        if o.type == "SLM":
            if not _ok(ltp) or (o.side == "BUY" and ltp < o.trigger) or (o.side == "SELL" and ltp > o.trigger):
                return False
        px = self._fill_price(ins, o.side)
        if px is None:
            if o.type == "MARKET":
                self.cancel(None, o.id, "no live price — market order cancelled")
            return False
        if o.type == "LIMIT":
            if (o.side == "BUY" and px > o.price) or (o.side == "SELL" and px < o.price):
                return False
            px = min(px, o.price) if o.side == "BUY" else max(px, o.price)
        self._execute(o, ins, px)
        return True

    def _execute(self, o: Order, ins, px: float) -> None:
        key = (o.agent, o.symbol, o.product)
        p = self.positions.get(key) or Position(o.agent, o.symbol, o.product)
        signed = o.qty if o.side == "BUY" else -o.qty
        value = px * o.qty
        fee = self.charges(ins, o.side, o.product, value)
        rate = self.margin_rate(ins, o.product)
        pnl = 0.0
        if p.qty and (p.qty > 0) != (signed > 0):                         # closing (maybe reversing)
            q = min(abs(p.qty), abs(signed))
            pnl = (px - p.avg) * q * (1 if p.qty > 0 else -1)
            release = p.margin * q / abs(p.qty)
            self.cash += release + pnl
            p.margin -= release
            p.realized += pnl
            p.qty += q if p.qty < 0 else -q
            rest = abs(signed) - q
            if p.qty == 0:
                p.avg, p.margin = 0.0, 0.0
            if rest:                                                       # reversal (MIS stocks only)
                p.qty, p.avg = (rest if signed > 0 else -rest), px
                p.margin = px * rest * rate
                self.cash -= p.margin
        else:                                                              # opening / adding
            new_qty = p.qty + signed
            p.avg = (p.avg * abs(p.qty) + px * o.qty) / abs(new_qty)
            p.qty = new_qty
            p.margin += value * rate
            self.cash -= value * rate
        self.cash -= fee
        p.charges += fee
        o.status, o.filled_qty, o.avg_price, o.charges, o.updated = "FILLED", o.qty, px, fee, self.clock()
        self.positions[key] = p
        self.db.save_order(asdict(o))
        self.db.save_position(asdict(p))
        if p.qty == 0 and p.margin == 0:
            self.positions.pop(key, None)
        self.db.add_trade({"ts": o.updated, "agent": o.agent, "order_id": o.id, "symbol": o.symbol, "side": o.side,
                           "qty": o.qty, "price": px, "product": o.product, "charges": fee, "pnl": round(pnl, 2)})
        self.orders.pop(o.id, None)
        self._save_state()

    # ------------------------------------------------------------------ periodic work
    def tick(self) -> list[str]:
        """Match resting orders, enforce limits, square off / settle. Returns event texts."""
        events: list[str] = []
        now = self._now()
        self._roll_day()
        for o in list(self.orders.values()):
            if o.status == "OPEN":
                self._try_fill(o)
        # whole-pot loss limit
        lim = self.cfg["account"]["daily_loss_limit"]
        if not self.halted and self.equity() - self.day_start_equity <= -lim:
            self.halted = f"daily loss limit ₹{lim:,} reached"
            events.append(self._event("RISK", f"Pot down ₹{lim:,} today — all positions squared off, trading halted"))
            self.square_off(None, reason="pot daily loss limit")
            self._save_state()
        # per-agent loss limit
        alim = self.cfg["agents"]["daily_loss_limit"]
        for agent in {p.agent for p in self.positions.values()}:
            if agent not in self.agent_blocked and self.agent_pnl_today(agent) <= -alim:
                self.agent_blocked[agent] = f"daily loss limit ₹{alim:,} reached"
                events.append(self._event("RISK", f"{agent} down ₹{alim:,} today — its positions squared off", agent))
                self.square_off(agent, reason="agent daily loss limit")
                self._save_state()
        if not self.mock:
            if now.weekday() < 5 and self.t_sq <= now.time() < self.t_close and \
                    any(p.product == "MIS" and p.qty for p in self.positions.values()):
                n = self.square_off(None, product="MIS", reason="intraday square-off")
                events.append(self._event("INFO", f"Intraday square-off: {n} MIS position(s) closed"))
            events += self._settle_expired(now)
        return events

    def square_off(self, agent: str | None, product: str | None = None, reason: str = "square-off") -> int:
        for o in list(self.orders.values()):
            if (agent is None or o.agent == agent) and (product is None or o.product == product):
                self.cancel(None, o.id, reason)
        n = 0
        for p in list(self.positions.values()):
            if p.qty and (agent is None or p.agent == agent) and (product is None or p.product == product):
                ins = self.reg.get(p.symbol)
                side = "SELL" if p.qty > 0 else "BUY"
                px = self._fill_price(ins, side) or self.mark(p.symbol)
                if px is None:
                    continue
                o = Order(uuid.uuid4().hex[:12], p.agent, p.symbol, side, abs(p.qty), "MARKET", p.product,
                          created=self.clock(), tag=reason[:40])
                self._execute(o, ins, px)
                n += 1
        return n

    def _settle_expired(self, now: datetime) -> list[str]:
        out = []
        spot = self.mark("NIFTY")
        for p in list(self.positions.values()):
            ins = self.reg.get(p.symbol)
            if not p.qty or not ins or ins.kind != "OPT" or spot is None:
                continue
            if ins.expiry < now.date() or (ins.expiry == now.date() and now.time() >= self.t_close):
                intrinsic = max(0.0, spot - ins.strike) if ins.opt == "CE" else max(0.0, ins.strike - spot)
                o = Order(uuid.uuid4().hex[:12], p.agent, p.symbol, "SELL", abs(p.qty), "MARKET", p.product,
                          created=self.clock(), tag="expiry settlement")
                self._execute(o, ins, round(intrinsic, 2))
                out.append(self._event("INFO", f"{p.symbol} expired — settled at intrinsic ₹{intrinsic:.2f}", p.agent))
        return out

    def _event(self, level: str, text: str, agent: str = "") -> str:
        log.info("%s %s", level, text)
        self.db.event(level, text, agent)
        return text

    # ------------------------------------------------------------------ admin
    def halt(self, reason: str) -> None:
        self.halted = reason or "halted by admin"
        self._save_state()
        self._event("ADMIN", f"Trading halted: {self.halted}")

    def resume(self) -> None:
        self.halted, self.agent_blocked = "", {}
        self._save_state()
        self._event("ADMIN", "Trading resumed")

    def reset_account(self) -> None:
        """Wipe positions/orders and restore the starting capital (history kept)."""
        for o in list(self.orders.values()):
            self.cancel(None, o.id, "account reset")
        for p in list(self.positions.values()):
            p.qty, p.margin = 0, 0.0
            self.db.save_position(asdict(p))
        self.positions.clear()
        self.cash = float(self.cfg["account"]["capital"])
        self.day_start_equity = self.cash
        self.db.set("day_start_equity", self.cash)
        self.halted, self.agent_blocked = "", {}
        self._save_state()
        self._event("ADMIN", f"Account reset to ₹{self.cash:,.0f}")
