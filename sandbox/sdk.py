"""Tiny client for the sandbox API — usable by bots, scripts and AI agents. PAPER MONEY ONLY.

    from sandbox.sdk import Sandbox
    sb = Sandbox("http://127.0.0.1:8200", api_key="sbx_...")
    q = sb.quote("RELIANCE")                     # {'ltp':…, 'bid':…, 'ask':…, 'vwap':…, 'high':…, 'low':…}
    o = sb.buy("RELIANCE", 5)                    # market, intraday (MIS)
    o = sb.buy("NIFTY-20OCT26-25000-CE", 65, type="LIMIT", price=101.5, product="NRML")
    sb.positions(); sb.account(); sb.cancel(o["id"])
"""
from __future__ import annotations

import requests


class SandboxError(RuntimeError):
    def __init__(self, status: int, body):
        self.status, self.body = status, body
        super().__init__(f"{status}: {body.get('reason') or body.get('detail') if isinstance(body, dict) else body}")


class Sandbox:
    def __init__(self, base_url: str = "http://127.0.0.1:8200", api_key: str = "", timeout: float = 5.0):
        self.base, self.timeout = base_url.rstrip("/"), timeout
        self.s = requests.Session()
        self.s.headers["X-API-Key"] = api_key

    def _req(self, method: str, path: str, **kw):
        try:
            r = self.s.request(method, self.base + path, timeout=self.timeout, **kw)
        except requests.ConnectionError:
            if method != "GET":            # never resend an order: it may have been received
                raise
            r = self.s.request(method, self.base + path, timeout=self.timeout, **kw)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        if r.status_code >= 400 and r.status_code != 422:
            raise SandboxError(r.status_code, body)
        return body

    # ---- market data
    def quotes(self, symbols) -> dict[str, dict]:
        syms = [symbols] if isinstance(symbols, str) else list(symbols)
        out = {}
        for i in range(0, len(syms), 200):
            for q in self._req("GET", "/api/v1/quote", params={"symbols": ",".join(syms[i:i + 200])}):
                out[q["symbol"]] = q
        return out

    def quote(self, symbol: str) -> dict:
        return self.quotes(symbol).get(symbol.upper(), {})

    def chain(self, expiry: str = "") -> dict:
        return self._req("GET", "/api/v1/chain", params={"expiry": expiry})

    def instruments(self, kind: str = "", expiry: str = "") -> list[dict]:
        return self._req("GET", "/api/v1/instruments", params={"kind": kind, "expiry": expiry})

    # ---- trading (a REJECTED order comes back with status 'REJECTED' and a reason, not an exception)
    def order(self, symbol: str, side: str, qty: int, type: str = "MARKET", product: str | None = None,
              price: float | None = None, trigger: float | None = None, tag: str = "") -> dict:
        return self._req("POST", "/api/v1/orders", json={"symbol": symbol, "side": side, "qty": qty, "type": type,
                                                          "product": product, "price": price, "trigger": trigger,
                                                          "tag": tag})

    def buy(self, symbol: str, qty: int, **kw) -> dict:
        return self.order(symbol, "BUY", qty, **kw)

    def sell(self, symbol: str, qty: int, **kw) -> dict:
        return self.order(symbol, "SELL", qty, **kw)

    def cancel(self, order_id: str) -> dict:
        return self._req("DELETE", f"/api/v1/orders/{order_id}")

    def orders(self, status: str = "open") -> list[dict]:
        return self._req("GET", "/api/v1/orders", params={"status": status})

    def positions(self) -> list[dict]:
        return self._req("GET", "/api/v1/positions")

    def account(self) -> dict:
        return self._req("GET", "/api/v1/account")

    def trades(self, limit: int = 100) -> list[dict]:
        return self._req("GET", "/api/v1/trades", params={"limit": limit})

    def close_all(self, tag: str = "close all") -> list[dict]:
        return [self.order(p["symbol"], "SELL" if p["qty"] > 0 else "BUY", abs(p["qty"]), product=p["product"], tag=tag)
                for p in self.positions() if p["qty"]]
