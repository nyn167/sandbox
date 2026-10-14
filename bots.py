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
from dotenv import dotenv_values

from sandbox.bots.strategies import BOTS
from sandbox.config import DATA, ROOT, admin_key, load_config
from sandbox.market.instruments import IST
from sandbox.sdk import Sandbox


def bot_key(base: str, name: str) -> str:
    var = "BOT_KEY_" + name.upper().replace("-", "_")
    env = dotenv_values(ROOT / ".env") if (ROOT / ".env").exists() else {}
    if env.get(var):
        return env[var]
    r = requests.post(base + "/api/admin/agents", json={"name": name, "kind": "bot"},
                      headers={"X-Admin-Key": admin_key()}, timeout=10)
    r.raise_for_status()
    key = r.json()["api_key"]
    with open(ROOT / ".env", "a", encoding="utf-8") as f:
        f.write(f"{var}={key}\n")
    return key


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
