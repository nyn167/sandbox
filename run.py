"""NYN paper-trading sandbox — PAPER MONEY ONLY.

    python run.py            live market data from Dhan (own feed connection), paper fills
    python run.py --mock     synthetic market, works any time
"""
import argparse
import asyncio
import logging
import sys
from datetime import datetime

from sandbox.config import DATA, load_config
from sandbox.market.instruments import IST
from sandbox.server import serve


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--port", type=int)
    ap.add_argument("--capital", type=float, help="override the pot size (stress tests)")
    a = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    (DATA / "logs").mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    fmt.converter = lambda ts: datetime.fromtimestamp(ts, IST).timetuple()
    fh = logging.FileHandler(DATA / "logs" / f"{'mock-' if a.mock else ''}{datetime.now(IST):%Y-%m-%d}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, logging.StreamHandler(sys.stdout)])
    logging.getLogger("websockets").setLevel(logging.WARNING)
    cfg = load_config()
    if a.port:
        cfg["server"]["port"] = a.port
    if a.capital:
        cfg["account"]["capital"] = a.capital
        cfg["account"]["daily_loss_limit"] = a.capital
    try:
        asyncio.run(serve(cfg, mock=a.mock))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
