"""Paper broker: fills, charges, products, risk limits, settlement, persistence, API, and no real-order code."""
import math
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from sandbox.broker import Broker
from sandbox.config import ROOT, load_config
from sandbox.db import DB
from sandbox.market.instruments import IST
from sandbox.market.tick_store import TickStore
from sandbox.market.universe import FNO, EQ_SEG, IDX, Instrument, Registry

EXP = date(2026, 10, 20)


class Clock:
    def __init__(self, dt):
        self.t = dt.timestamp()

    def __call__(self):
        return self.t


def market(tmp_path, mock=True, when=datetime(2026, 10, 14, 10, 0, tzinfo=IST), cfg=None):
    cfg = cfg or load_config()
    reg = Registry()
    reg.add(Instrument("NIFTY", 13, IDX, "IDX"))
    reg.add(Instrument("NIFTY-20OCT26-25000-CE", 1, FNO, "OPT", 65, 0.05, EXP, 25000, "CE"))
    reg.add(Instrument("NIFTY-20OCT26-25000-PE", 2, FNO, "OPT", 65, 0.05, EXP, 25000, "PE"))
    reg.add(Instrument("RELIANCE", 2885, EQ_SEG, "EQ"))
    st, clock = TickStore(), Clock(when)
    b = Broker(cfg, reg, st, DB(tmp_path / "t.sqlite"), mock=mock, clock=clock)
    def px(sid, ltp, bid=None, ask=None):
        st.ticks[sid] = (ltp, 0, 0, bid if bid is not None else float("nan"), ask if ask is not None else float("nan"), 0, clock())
    px(13, 25000.0)
    px(1, 100.0, 99.5, 100.5)
    px(2, 90.0, 89.5, 90.5)
    px(2885, 1400.0, 1399.9, 1400.1)
    return b, px, clock


def test_market_buy_fills_at_ask_and_blocks_cash(tmp_path):
    b, px, _ = market(tmp_path)
    o = b.place("bot1", "NIFTY-20OCT26-25000-CE", "BUY", 65)
    assert o.status == "FILLED" and o.avg_price == 100.5
    fee = b.charges(b.reg.get(o.symbol), "BUY", "MIS", 100.5 * 65)
    assert 20 < fee < 30 and math.isclose(b.cash, 50000 - 100.5 * 65 - fee)
    assert math.isclose(b.equity(), 50000 - fee + (100 - 100.5) * 65)          # marked at LTP


def test_sell_to_close_realises_pnl_and_option_writing_is_refused(tmp_path):
    b, px, _ = market(tmp_path)
    assert b.place("bot1", "NIFTY-20OCT26-25000-PE", "SELL", 65).reason.startswith("option selling")
    b.place("bot1", "NIFTY-20OCT26-25000-CE", "BUY", 65)
    px(1, 120.0, 119.5, 120.5)
    o = b.place("bot1", "NIFTY-20OCT26-25000-CE", "SELL", 65)
    assert o.status == "FILLED" and not b.positions
    tr = b.db.q("SELECT pnl FROM trades ORDER BY id")
    assert tr[-1]["pnl"] == pytest.approx((119.5 - 100.5) * 65)
    assert b.place("bot1", "NIFTY-20OCT26-25000-CE", "SELL", 65).status == "REJECTED"   # nothing left to close


def test_validation_lot_size_order_value_and_funds(tmp_path):
    b, px, _ = market(tmp_path)
    assert "lot size" in b.place("a", "NIFTY-20OCT26-25000-CE", "BUY", 50).reason
    assert "per-order limit" in b.place("a", "RELIANCE", "BUY", 20).reason           # ₹28k > ₹25k
    assert "unknown" in b.place("a", "NOPE", "BUY", 1).reason
    assert b.place("a", "RELIANCE", "BUY", 17).status == "FILLED"                     # ₹23.8k
    assert b.place("b", "RELIANCE", "BUY", 17).status == "FILLED"
    assert "insufficient funds" in b.place("c", "RELIANCE", "BUY", 17).reason        # pot nearly spent


def test_limit_rests_then_fills_and_slm_triggers(tmp_path):
    b, px, _ = market(tmp_path)
    o = b.place("a", "NIFTY-20OCT26-25000-CE", "BUY", 65, "LIMIT", price=95)
    assert o.status == "OPEN" and o.id in b.orders
    px(1, 94.0, 93.5, 94.5)
    b.tick()
    assert o.status == "FILLED" and o.avg_price == 94.5
    s = b.place("a", "NIFTY-20OCT26-25000-CE", "SELL", 65, "SLM", trigger=90)
    assert s.status == "OPEN"
    px(1, 89.0, 88.5, 89.5)
    b.tick()
    assert s.status == "FILLED" and s.avg_price == 88.5 and not b.positions


def test_stock_products_mis_short_and_cnc_rules(tmp_path):
    b, px, _ = market(tmp_path)
    assert "CNC is delivery" in b.place("a", "RELIANCE", "SELL", 5, product="CNC").reason
    assert b.place("a", "RELIANCE", "SELL", 5, product="MIS").status == "FILLED"       # intraday short
    px(2885, 1390.0, 1389.9, 1390.1)
    b.place("a", "RELIANCE", "BUY", 5, product="MIS")
    assert b.db.q("SELECT pnl FROM trades ORDER BY id DESC LIMIT 1")[0]["pnl"] == pytest.approx((1399.9 - 1390.1) * 5)


def test_agent_loss_limit_squares_off_and_blocks_only_that_agent(tmp_path):
    b, px, _ = market(tmp_path)
    b.place("loser", "RELIANCE", "BUY", 17)
    px(2885, 1300.0, 1299.9, 1300.1)                    # −₹1,700 on 17 shares
    ev = b.tick()
    assert any("loser" in e for e in ev) and "loser" in b.agent_blocked and not b.positions
    assert "blocked" in b.place("loser", "RELIANCE", "BUY", 1).reason
    assert b.place("other", "RELIANCE", "BUY", 1).status == "FILLED"


def test_pot_loss_limit_halts_everyone(tmp_path):
    cfg = load_config()
    cfg["agents"]["daily_loss_limit"] = 10_000
    b, px, _ = market(tmp_path, cfg=cfg)
    b.place("a", "RELIANCE", "BUY", 17)
    b.place("b", "RELIANCE", "BUY", 17)
    px(2885, 1320.0, 1319.9, 1320.1)                    # 34 × −80 ≈ −₹2,720
    b.tick()
    assert b.halted and not b.positions
    assert "halted" in b.place("c", "RELIANCE", "BUY", 1).reason


def test_live_rules_market_hours_squareoff_and_expiry_settlement(tmp_path):
    b, px, clock = market(tmp_path, mock=False, when=datetime(2026, 10, 14, 8, 0, tzinfo=IST))
    assert "market closed" in b.place("a", "RELIANCE", "BUY", 1).reason
    clock.t = datetime(2026, 10, 14, 10, 0, tzinfo=IST).timestamp()
    for sid, args in ((13, (25000.0,)), (1, (100.0, 99.5, 100.5)), (2885, (1400.0, 1399.9, 1400.1))):
        px(sid, *args)
    b.place("a", "RELIANCE", "BUY", 2)                               # MIS
    b.place("a", "NIFTY-20OCT26-25000-CE", "BUY", 65, product="NRML")
    clock.t = datetime(2026, 10, 14, 15, 16, tzinfo=IST).timestamp()
    for sid, args in ((13, (25000.0,)), (1, (100.0, 99.5, 100.5)), (2885, (1400.0, 1399.9, 1400.1))):
        px(sid, *args)
    b.tick()
    assert [p.symbol for p in b.positions.values()] == ["NIFTY-20OCT26-25000-CE"]  # MIS closed, NRML kept
    clock.t = datetime(2026, 10, 20, 15, 31, tzinfo=IST).timestamp()
    px(13, 25080.0)
    b.tick()
    last = b.db.q("SELECT price, pnl FROM trades ORDER BY id DESC LIMIT 1")[0]
    assert not b.positions and last["price"] == 80.0 and last["pnl"] == pytest.approx((80 - 100.5) * 65)


def test_state_survives_restart(tmp_path):
    b, px, clock = market(tmp_path)
    b.place("a", "NIFTY-20OCT26-25000-CE", "BUY", 65)
    b.place("a", "NIFTY-20OCT26-25000-CE", "SELL", 65, "LIMIT", price=150)
    b2 = Broker(b.cfg, b.reg, b.store, DB(tmp_path / "t.sqlite"), mock=True, clock=clock)
    assert math.isclose(b2.cash, b.cash) and len(b2.positions) == 1 and len(b2.orders) == 1


def test_api_keys_orders_and_admin(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from sandbox import config as cmod, server
    cfg = load_config()
    monkeypatch.setenv("SANDBOX_ADMIN_KEY", "adm_test")
    monkeypatch.setattr(cmod, "ROOT", tmp_path)
    e = server.Engine(cfg, mock=True, db_path=tmp_path / "api.sqlite")
    e.setup()
    c = TestClient(server.make_app(e))
    assert c.get("/api/v1/account").status_code == 401
    assert c.post("/api/admin/agents", json={"name": "x"}, headers={"X-Admin-Key": "wrong"}).status_code == 401
    key = c.post("/api/admin/agents", json={"name": "orb-bot"}, headers={"X-Admin-Key": "adm_test"}).json()["api_key"]
    h = {"X-API-Key": key}
    atm = round(e.broker.mark("NIFTY") / 50) * 50
    sym = next(i.symbol for i in e.reg.options(e.reg.expiries[0]) if i.strike == atm and i.opt == "CE")
    r = c.post("/api/v1/orders", json={"symbol": sym, "side": "BUY", "qty": 65}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "FILLED"
    assert c.get("/api/v1/positions", headers=h).json()[0]["symbol"] == sym
    assert c.post("/api/v1/orders", json={"symbol": sym, "side": "SELL", "qty": 7}, headers=h).status_code == 422
    assert c.get("/api/v1/account", headers=h).json()["agent"]["name"] == "orb-bot"
    assert c.get("/api/v1/chain", headers=h).json()["rows"]
    st = c.get("/api/market/stocks").json()
    assert len(st) == 50 and {"symbol", "ltp", "chg_pct"} <= set(st[0])
    assert c.get("/api/market/chain").json()["rows"]
    adm = {"X-Admin-Key": "adm_test"}
    m = c.post("/api/admin/order", json={"symbol": "RELIANCE", "side": "BUY", "qty": 1}, headers=adm)
    assert m.status_code == 200 and m.json()["agent"] == "manual"
    assert c.post("/api/admin/order", json={"symbol": "RELIANCE", "side": "BUY", "qty": 1}).status_code == 401
    assert c.post("/api/admin/close", json={"agent": "manual", "symbol": "RELIANCE", "product": "MIS"}, headers=adm).status_code == 200
    assert "NYN SANDBOX" in c.get("/").text
    d = c.get("/api/dash").json()
    assert {a["name"] for a in d["agents"]} == {"orb-bot", "manual"} and d["positions"]


def test_no_real_order_code_anywhere():
    """Paper only: the source must never reference Dhan's order / trading APIs."""
    bad = re.compile(r"place_order|modify_order|cancel_order|place_slice_order|Orders\(|dhanhq\.orders|/v2/orders|super_order|forever")
    hits = [f"{p.name}:{i}" for p in (ROOT / "sandbox").rglob("*.py")
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if bad.search(line)]
    assert not hits, hits
