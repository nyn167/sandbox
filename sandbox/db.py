"""SQLite storage: agents, orders, trades, positions, events, equity curve, key/value state."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (name TEXT PRIMARY KEY, key_hash TEXT NOT NULL, kind TEXT, created REAL, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, agent TEXT, data TEXT, status TEXT, created REAL);
CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, agent TEXT, order_id TEXT, symbol TEXT,
  side TEXT, qty INTEGER, price REAL, product TEXT, charges REAL, pnl REAL);
CREATE TABLE IF NOT EXISTS positions (agent TEXT, symbol TEXT, product TEXT, data TEXT, PRIMARY KEY (agent, symbol, product));
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, agent TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS equity (ts REAL PRIMARY KEY, equity REAL, cash REAL);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS trades_ts ON trades(ts);
"""


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.executescript(SCHEMA)

    def q(self, sql: str, args=()) -> list[dict]:
        return [dict(r) for r in self.c.execute(sql, args).fetchall()]

    def x(self, sql: str, args=()) -> None:
        self.c.execute(sql, args)

    # ---- kv
    def get(self, k: str, default=None):
        r = self.c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set(self, k: str, v) -> None:
        self.x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

    # ---- agents
    def create_agent(self, name: str, kind: str = "api") -> str:
        key = "sbx_" + secrets.token_urlsafe(24)
        self.x("INSERT INTO agents(name,key_hash,kind,created,active) VALUES(?,?,?,?,1) ON CONFLICT(name) DO UPDATE SET "
               "key_hash=excluded.key_hash, active=1", (name, hash_key(key), kind, time.time()))
        return key

    def agent_for_key(self, key: str) -> str | None:
        r = self.c.execute("SELECT name FROM agents WHERE key_hash=? AND active=1", (hash_key(key or ""),)).fetchone()
        return r[0] if r else None

    def agents(self) -> list[dict]:
        return self.q("SELECT name, kind, created, active FROM agents ORDER BY created")

    def set_agent_active(self, name: str, active: bool) -> None:
        self.x("UPDATE agents SET active=? WHERE name=?", (1 if active else 0, name))

    # ---- trading records
    def save_order(self, o: dict) -> None:
        self.x("INSERT INTO orders(id,agent,data,status,created) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
               "data=excluded.data, status=excluded.status", (o["id"], o["agent"], json.dumps(o), o["status"], o["created"]))

    def save_position(self, p: dict) -> None:
        if p["qty"] == 0 and p["margin"] == 0:
            self.x("DELETE FROM positions WHERE agent=? AND symbol=? AND product=?", (p["agent"], p["symbol"], p["product"]))
        else:
            self.x("INSERT INTO positions(agent,symbol,product,data) VALUES(?,?,?,?) ON CONFLICT(agent,symbol,product) "
                   "DO UPDATE SET data=excluded.data", (p["agent"], p["symbol"], p["product"], json.dumps(p)))

    def add_trade(self, t: dict) -> None:
        self.x("INSERT INTO trades(ts,agent,order_id,symbol,side,qty,price,product,charges,pnl) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (t["ts"], t["agent"], t["order_id"], t["symbol"], t["side"], t["qty"], t["price"], t["product"],
                t["charges"], t["pnl"]))

    def event(self, level: str, text: str, agent: str = "") -> None:
        self.x("INSERT INTO events(ts,level,agent,text) VALUES(?,?,?,?)", (time.time(), level, agent, text))

    def equity_point(self, ts: float, equity: float, cash: float) -> None:
        self.x("INSERT OR REPLACE INTO equity(ts,equity,cash) VALUES(?,?,?)", (round(ts), round(equity, 2), round(cash, 2)))
