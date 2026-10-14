"""Sandbox server: market data → paper broker → REST API for bots / AI agents + dashboard data.

PAPER TRADING ONLY. Market data comes from its own Dhan feed connection (read-only token from the trader's
.env); orders never leave this process.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import Body, Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from .broker import Broker
from .config import DATA, ROOT, admin_key, dhan_credentials
from .db import DB
from .market.instruments import IST, load_nifty_contracts
from .market.tick_store import TickStore
from .market.universe import Registry, build_live, build_mock, MockMarket

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, cfg: dict, mock: bool = False, db_path=None):
        self.cfg, self.mock = cfg, mock
        self.store = TickStore()
        import os
        self.db = DB(db_path or Path(os.getenv("SANDBOX_DB") or DATA / "db" / ("mock.sqlite" if mock else "sandbox.sqlite")))
        self.reg: Registry | None = None
        self.feed = None
        self.broker: Broker | None = None
        self.started = time.time()
        self.events: list[str] = []

    # ---- market data
    def setup(self) -> None:
        if self.mock:
            self.reg = build_mock(self.cfg)
            self.feed = MockMarket(self.reg, self.store, self.cfg)
        else:
            cid, tok = dhan_credentials(self.cfg)
            df = load_nifty_contracts(DATA / "cache", self.cfg["market"]["symbol_prefix"])
            spot = self._rest_spot(cid, tok)
            self.reg = build_live(self.cfg, df, spot)
            self._seed_quotes(cid, tok)
            from .market.dhan_feed import DhanFeed
            self.feed = DhanFeed(cid, tok, self.store)
            self.feed.start(self.reg.feed_list())
        self.broker = Broker(self.cfg, self.reg, self.store, self.db, mock=self.mock)
        log.info("Sandbox ready: %d instruments (%d options, %d stocks), %s", len(self.reg.by_symbol),
                 len(self.reg.options()), len(self.reg.stocks()), "MOCK" if self.mock else "live Dhan feed")

    def _rest_quotes(self, cid, tok, segs: dict) -> dict:
        from dhanhq import DhanContext
        from dhanhq._market_feed import MarketFeed as RestMarketFeed      # market-data REST only
        r = RestMarketFeed(DhanContext(cid, tok)).quote_data(segs)
        d = r.get("data", {})
        return d.get("data", d) if isinstance(d, dict) else {}

    def _rest_spot(self, cid, tok) -> float:
        idx = self.cfg["market"]["index_security_id"]
        d = self._rest_quotes(cid, tok, {"IDX_I": [idx]})
        return float(d["IDX_I"][str(idx)]["last_price"])

    def _seed_quotes(self, cid, tok) -> None:
        """Last known prices for everything (so the site shows the close off-hours). recv=0 → not tradable."""
        segs = {"IDX_I": [], "NSE_EQ": [], "NSE_FNO": []}
        name = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO"}
        for i in self.reg.by_symbol.values():
            segs[name[i.seg]].append(i.sid)
        time.sleep(1.1)
        try:
            d = self._rest_quotes(cid, tok, {k: v for k, v in segs.items() if v})
        except Exception as e:
            log.warning("Quote seeding skipped: %s", e)
            return
        for seg, rows in d.items():
            for sid, x in (rows or {}).items():
                dep = x.get("depth") or {}
                bid = ((dep.get("buy") or [{}])[0]).get("price") or float("nan")
                ask = ((dep.get("sell") or [{}])[0]).get("price") or float("nan")
                ltp = float(x.get("last_price") or float("nan"))
                self.store.ticks.setdefault(int(sid), (ltp, int(x.get("oi") or 0), int(x.get("volume") or 0),
                                                       bid or float("nan"), ask or float("nan"), 0, 0.0))

    # ---- loops
    async def run_loops(self) -> None:
        nxt_eq = 0.0
        while True:
            if not self.mock:
                now = datetime.now(IST)
                if now.weekday() < 5 and now.strftime("%H:%M") == "08:55" and time.time() - self.started > 600:
                    log.info("Daily restart: fresh token, expiries and strikes for today's session")
                    raise SystemExit(0)
                if getattr(self.feed, "fatal", None) and time.time() - self.started > 120:
                    log.warning("Feed stopped (%s) — restarting in a minute to pick up a new token", self.feed.fatal)
                    raise SystemExit(3)
            if self.mock:
                self.feed.step()
            try:
                for e in self.broker.tick():
                    self.events.append(e)
            except Exception:
                log.exception("broker tick failed")
            now = time.time()
            if now >= nxt_eq:
                nxt_eq = now + 60
                self.db.equity_point(now, self.broker.equity(), self.broker.cash)
            await asyncio.sleep(1.0 if self.mock else 0.5)

    # ---- views
    def account(self, agent: str | None = None) -> dict:
        b = self.broker
        eq = b.equity()
        out = {"capital": self.cfg["account"]["capital"], "equity": round(eq, 2), "cash": round(b.cash, 2),
               "day_pnl": round(eq - b.day_start_equity, 2), "total_pnl": round(eq - self.cfg["account"]["capital"], 2),
               "halted": b.halted, "market": self.market_state()}
        if agent:
            out["agent"] = {"name": agent, "pnl_today": round(b.agent_pnl_today(agent), 2),
                            "blocked": b.agent_blocked.get(agent, ""), **self.agent_totals(agent)}
        return out

    def agent_totals(self, agent: str) -> dict:
        r = self.db.q("SELECT COUNT(*) AS n, COALESCE(SUM(pnl),0) AS pnl, COALESCE(SUM(charges),0) AS ch, "
                      "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS wins, SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END) AS losses "
                      "FROM trades WHERE agent=?", (agent,))[0]
        unreal = sum(self.broker.unrealized(p) for p in self.broker.positions.values() if p.agent == agent)
        closed = (r["wins"] or 0) + (r["losses"] or 0)
        return {"trades": r["n"], "realized": round(r["pnl"], 2), "charges": round(r["ch"], 2),
                "unrealized": round(unreal, 2), "net": round(r["pnl"] - r["ch"] + unreal, 2),
                "win_rate": round(100 * (r["wins"] or 0) / closed, 1) if closed else None}

    def market_state(self) -> str:
        if self.mock:
            return "mock"
        now = datetime.now(IST)
        b = self.broker
        return "open" if now.weekday() < 5 and b.t_open <= now.time() < b.t_close else "closed"

    def positions(self, agent: str | None = None) -> list[dict]:
        out = []
        for p in self.broker.positions.values():
            if agent and p.agent != agent:
                continue
            m = self.broker.mark(p.symbol)
            out.append({**asdict(p), "avg": round(p.avg, 2), "margin": round(p.margin, 2), "ltp": m,
                        "unrealized": round(self.broker.unrealized(p), 2)})
        return out

    def dash(self) -> dict:
        names = [a["name"] for a in self.db.agents()]
        return {"t": datetime.now(IST).strftime("%H:%M:%S"), "feed": getattr(self.feed, "status", "?"),
                "mode": "MOCK" if self.mock else "LIVE", "spot": self.broker.mark("NIFTY"),
                "limits": {"pot": self.cfg["account"]["daily_loss_limit"], **self.cfg["agents"]},
                "account": self.account(),
                "agents": sorted(({"name": n, **self.agent_totals(n), "blocked": self.broker.agent_blocked.get(n, "")}
                                  for n in names), key=lambda a: -a["net"]),
                "positions": self.positions(),
                "orders": [asdict(o) for o in self.broker.orders.values()],
                "trades": self.db.q("SELECT * FROM trades ORDER BY id DESC LIMIT 50"),
                "events": self.db.q("SELECT * FROM events ORDER BY id DESC LIMIT 30"),
                "equity": self.db.q("SELECT ts, equity FROM equity WHERE ts >= ? ORDER BY ts",
                                    (time.time() - 7 * 86400,))}


# ------------------------------------------------------------------------------ API
def make_app(engine: Engine) -> FastAPI:
    app = FastAPI(title="NYN paper-trading sandbox", docs_url="/docs", redoc_url=None)
    adm = admin_key()

    def agent(x_api_key: str = Header(default="")) -> str:
        name = engine.db.agent_for_key(x_api_key)
        if not name:
            raise HTTPException(401, "missing or invalid X-API-Key")
        return name

    def admin(x_admin_key: str = Header(default="")) -> None:
        import hmac
        if not hmac.compare_digest(x_admin_key, adm):
            raise HTTPException(401, "admin key required")

    @app.get("/", include_in_schema=False)
    async def index():
        page = ROOT / "web" / "index.html"
        return HTMLResponse(page.read_text(encoding="utf-8") if page.exists() else "<h1>NYN Sandbox</h1>")

    # ---- market data (any agent)
    @app.get("/api/v1/instruments")
    async def instruments(kind: str = "", expiry: str = "", _: str = Depends(agent)):
        items = engine.reg.by_symbol.values()
        return [{"symbol": i.symbol, "kind": i.kind, "lot": i.lot, "expiry": i.expiry.isoformat() if i.expiry else None,
                 "strike": i.strike, "opt": i.opt} for i in items
                if (not kind or i.kind == kind.upper()) and (not expiry or (i.expiry and i.expiry.isoformat() == expiry))]

    @app.get("/api/v1/quote")
    async def quote(symbols: str, _: str = Depends(agent)):
        return [q for s in symbols.split(",")[:200] if (q := engine.broker.quote(s))]

    @app.get("/api/v1/chain")
    async def chain(expiry: str = "", _: str = Depends(agent)):
        exps = engine.reg.expiries
        exp = next((e for e in exps if e.isoformat() == expiry), exps[0] if exps else None)
        rows = {}
        for i in engine.reg.options(exp):
            rows.setdefault(i.strike, {"strike": i.strike})[i.opt] = engine.broker.quote(i.symbol)
        return {"expiry": exp.isoformat() if exp else None, "expiries": [e.isoformat() for e in exps],
                "spot": engine.broker.mark("NIFTY"), "rows": [rows[k] for k in sorted(rows)]}

    # ---- trading (own orders / positions only)
    @app.post("/api/v1/orders")
    async def place(body: dict = Body(...), me: str = Depends(agent)):
        o = engine.broker.place(me, body.get("symbol", ""), body.get("side", ""), int(body.get("qty") or 0),
                                body.get("type", "MARKET"), body.get("product"), body.get("price"),
                                body.get("trigger"), body.get("tag", ""))
        return JSONResponse(asdict(o), status_code=200 if o.status != "REJECTED" else 422)

    @app.get("/api/v1/orders")
    async def orders(status: str = "open", me: str = Depends(agent)):
        if status == "open":
            return [asdict(o) for o in engine.broker.orders.values() if o.agent == me]
        import json
        return [json.loads(r["data"]) for r in engine.db.q(
            "SELECT data FROM orders WHERE agent=? ORDER BY created DESC LIMIT 200", (me,))]

    @app.delete("/api/v1/orders/{oid}")
    async def cancel(oid: str, me: str = Depends(agent)):
        o = engine.broker.cancel(me, oid)
        if not o:
            raise HTTPException(404, "no open order with that id")
        return asdict(o)

    @app.get("/api/v1/positions")
    async def positions(me: str = Depends(agent)):
        return engine.positions(me)

    @app.get("/api/v1/account")
    async def account(me: str = Depends(agent)):
        return engine.account(me)

    @app.get("/api/v1/trades")
    async def trades(limit: int = 100, me: str = Depends(agent)):
        return engine.db.q("SELECT * FROM trades WHERE agent=? ORDER BY id DESC LIMIT ?", (me, min(limit, 1000)))

    # ---- admin
    @app.post("/api/admin/agents", dependencies=[Depends(admin)])
    async def new_agent(body: dict = Body(...)):
        name = str(body.get("name", "")).strip()[:40]
        if not name or not name.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(400, "name: letters, digits, - and _ only")
        key = engine.db.create_agent(name, body.get("kind", "api"))
        engine.db.event("ADMIN", f"API key issued for {name}", name)
        return {"name": name, "api_key": key, "note": "shown once — store it in the bot's own .env"}

    @app.get("/api/admin/agents", dependencies=[Depends(admin)])
    async def list_agents():
        return engine.db.agents()

    @app.post("/api/admin/agents/{name}/disable", dependencies=[Depends(admin)])
    async def disable(name: str):
        engine.db.set_agent_active(name, False)
        engine.broker.square_off(name, reason="agent disabled")
        return {"ok": True}

    @app.post("/api/admin/halt", dependencies=[Depends(admin)])
    async def halt(body: dict = Body(default={})):
        engine.broker.halt(body.get("reason", "halted by admin"))
        if body.get("square_off"):
            engine.broker.square_off(None, reason="admin halt")
        return {"halted": engine.broker.halted}

    @app.post("/api/admin/resume", dependencies=[Depends(admin)])
    async def resume():
        engine.broker.resume()
        return {"halted": ""}

    @app.post("/api/admin/squareoff", dependencies=[Depends(admin)])
    async def squareoff(body: dict = Body(default={})):
        return {"closed": engine.broker.square_off(body.get("agent"), reason="admin square-off")}

    @app.post("/api/admin/reset", dependencies=[Depends(admin)])
    async def reset():
        engine.broker.reset_account()
        return engine.account()

    @app.post("/api/admin/order", dependencies=[Depends(admin)])
    async def manual_order(body: dict = Body(...)):
        """Paper order from the website ticket, booked to the 'manual' agent."""
        if not engine.db.agent_for_key("") and not any(a["name"] == "manual" for a in engine.db.agents()):
            engine.db.create_agent("manual", "manual")
        o = engine.broker.place("manual", body.get("symbol", ""), body.get("side", ""), int(body.get("qty") or 0),
                                body.get("type", "MARKET"), body.get("product"), body.get("price"), body.get("trigger"),
                                "website")
        return JSONResponse(asdict(o), status_code=200 if o.status != "REJECTED" else 422)

    @app.post("/api/admin/cancel/{oid}", dependencies=[Depends(admin)])
    async def admin_cancel(oid: str):
        o = engine.broker.cancel(None, oid, "cancelled by admin")
        return asdict(o) if o else JSONResponse({"error": "no open order"}, status_code=404)

    @app.post("/api/admin/close", dependencies=[Depends(admin)])
    async def admin_close(body: dict = Body(...)):
        """Close one position at market."""
        p = engine.broker.positions.get((body.get("agent"), body.get("symbol"), body.get("product")))
        if not p or not p.qty:
            raise HTTPException(404, "no such position")
        o = engine.broker.place(p.agent, p.symbol, "SELL" if p.qty > 0 else "BUY", abs(p.qty), "MARKET", p.product,
                                tag="closed by admin")
        return JSONResponse(asdict(o), status_code=200 if o.status != "REJECTED" else 422)

    # ---- public read-only market data for the website tabs (tailnet only)
    @app.get("/api/market/chain")
    async def market_chain(expiry: str = ""):
        return await chain(expiry, "")

    @app.get("/api/market/stocks")
    async def market_stocks():
        out = []
        for i in engine.reg.stocks():
            q = engine.broker.quote(i.symbol)
            pc = q.get("prev_close")
            q["chg_pct"] = round((q["ltp"] - pc) / pc * 100, 2) if q.get("ltp") and pc else None
            out.append(q)
        return out

    # ---- dashboard (read-only, no key; served only inside the tailnet)
    @app.get("/api/dash")
    async def dash():
        return engine.dash()

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        try:
            while True:
                await sock.send_json(engine.dash())
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(sock.receive_text(), timeout=1.0)
        except (WebSocketDisconnect, RuntimeError):
            pass

    return app


async def serve(cfg: dict, mock: bool = False) -> None:
    engine = Engine(cfg, mock)
    await asyncio.to_thread(engine.setup)
    s = cfg["server"]
    server = uvicorn.Server(uvicorn.Config(make_app(engine), host=s["host"], port=s["port"], log_level="warning",
                                           access_log=False, lifespan="off", timeout_keep_alive=65))
    server.capture_signals = contextlib.nullcontext
    loops = asyncio.create_task(engine.run_loops())
    serve_t = asyncio.create_task(server.serve())
    try:
        done, _ = await asyncio.wait([loops, serve_t], return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            t.result()
    finally:
        server.should_exit = True
        await asyncio.wait([serve_t], timeout=5)
        loops.cancel()
        if engine.feed:
            await asyncio.to_thread(engine.feed.stop)
