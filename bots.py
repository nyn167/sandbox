"""Run the built-in bots against the sandbox API (PAPER MONEY ONLY).

    python bots.py                    all bots, live market hours
    python bots.py --mock             all bots against a --mock sandbox (20× clock)
    python bots.py --bots orb-nifty   just one

Each bot gets its own API key on first run (created with the admin key, saved to .env as BOT_KEY_<NAME>).
"""
import argparse
import logging
import sys
import threading
import time
from datetime import datetime

import requests

if hasattr(sys.stdout, "reconfigure"):      # UTF-8 logs (₹) even when Windows redirects output to a cp1252 file
    for _s in (sys.stdout, sys.stderr):
        _s.reconfigure(encoding="utf-8", errors="replace")
from dotenv import dotenv_values

from sandbox.bots.strategies import BOTS
from sandbox.config import DATA, ROOT, admin_key, load_config
from sandbox.market.instruments import IST
from sandbox.sdk import Sandbox


def bot_key(base: str, name: str) -> str:
    var = "BOT_KEY_" + name.upper().replace("-", "_")
    env = dotenv_values(ROOT / ".env") if (ROOT / ".env").exists() else {}
    if env.get(var) and requests.get(base + "/api/v1/account", headers={"X-API-Key": env[var]}, timeout=10).status_code == 200:
        return env[var]
    r = requests.post(base + "/api/admin/agents", json={"name": name, "kind": "bot"},
                      headers={"X-Admin-Key": admin_key()}, timeout=10)
    r.raise_for_status()
    key = r.json()["api_key"]
    lines = [l for l in ((ROOT / ".env").read_text(encoding="utf-8").splitlines() if (ROOT / ".env").exists() else [])
             if not l.startswith(var + "=")]
    (ROOT / ".env").write_text("\n".join(lines + [f"{var}={key}"]) + "\n", encoding="utf-8")
    return key


def wait_for_server(base: str) -> None:
    while True:
        try:
            if requests.get(base + "/api/dash", timeout=5).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(10)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--bots", default=",".join(BOTS))
    ap.add_argument("--port", type=int)
    ap.add_argument("--duration", type=float, help="stop after N seconds (testing)")
    a = ap.parse_args()
    (DATA / "logs").mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(name)s: %(message)s", "%H:%M:%S")
    fmt.converter = lambda ts: datetime.fromtimestamp(ts, IST).timetuple()
    fh = logging.FileHandler(DATA / "logs" / f"bots-{'mock-' if a.mock else ''}{datetime.now(IST):%Y-%m-%d}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh])
    cfg = load_config()
    base = f"http://127.0.0.1:{a.port or cfg['server']['port']}"
    wait_for_server(base)
    stop = threading.Event()
    threads = []
    for name in [b.strip() for b in a.bots.split(",") if b.strip()]:
        bot = BOTS[name](Sandbox(base, bot_key(base, name)), mock=a.mock)
        t = threading.Thread(target=bot.run_forever, args=(stop.is_set,), name=name, daemon=True)
        t.start()
        threads.append(t)
    try:
        t_end = time.time() + a.duration if a.duration else None
        while not t_end or time.time() < t_end:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    stop.set()


if __name__ == "__main__":
    main()
