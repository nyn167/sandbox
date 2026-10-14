"""Load test on the laptop: a separate MOCK sandbox (own port + throwaway DB) hammered by N concurrent agents.

    python tools/stress.py --agents 50 --seconds 60 --viewers 10
Runs at below-normal CPU priority so the live NIFTY trader always wins. Never touches the real sandbox DB.
"""
import argparse
import asyncio
import os
import random
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import psutil

ROOT = Path(__file__).resolve().parent.parent
PORT = 8299


def low_priority(p: psutil.Process):
    try:
        p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 10)
    except Exception:
        pass


async def agent(i, key, until, stats, syms, opts):
    h = {"X-API-Key": key}
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}", headers=h, timeout=10) as c:
        while time.time() < until:
            r = random.random()
            t0 = time.perf_counter()
            try:
                if r < 0.35:
                    op, resp = "quote", await c.get("/api/v1/quote", params={"symbols": ",".join(random.sample(syms, 10))})
                elif r < 0.60:
                    s = random.choice(syms)
                    op, resp = "market order", await c.post("/api/v1/orders", json={"symbol": s, "side": random.choice(["BUY", "SELL"]), "qty": random.randint(1, 3)})
                elif r < 0.75:
                    s = random.choice(opts)
                    op, resp = "option order", await c.post("/api/v1/orders", json={"symbol": s, "side": "BUY", "qty": 65})
                elif r < 0.90:
                    s = random.choice(syms)
                    resp = await c.post("/api/v1/orders", json={"symbol": s, "side": "BUY", "qty": 1, "type": "LIMIT", "price": 1})
                    op = "limit+cancel"
                    if resp.status_code == 200:
                        await c.delete(f"/api/v1/orders/{resp.json()['id']}")
                else:
                    op, resp = "positions", await c.get(random.choice(["/api/v1/positions", "/api/v1/account", "/api/v1/chain"]))
                ok = resp.status_code in (200, 422)
            except Exception as e:
                op, ok = "error", False
            dt = (time.perf_counter() - t0) * 1000
            stats.setdefault(op, []).append((dt, ok))
            await asyncio.sleep(random.uniform(0.0, 0.05))


async def viewer(until, frames):
    import websockets
    try:
        async with websockets.connect(f"ws://127.0.0.1:{PORT}/ws", max_size=None) as w:
            while time.time() < until:
                await asyncio.wait_for(w.recv(), 10)
                frames.append(time.time())
    except Exception:
        pass


async def main():
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=50)
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--viewers", type=int, default=10)
    a = ap.parse_args()
    low_priority(psutil.Process())
    db = Path(tempfile.mkdtemp()) / "stress.sqlite"
    env = {**os.environ, "SANDBOX_DB": str(db), "PYTHONIOENCODING": "utf-8"}
    srv = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "--mock", "--port", str(PORT), "--capital", "100000000"],
                           cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc = psutil.Process(srv.pid)
    low_priority(proc)
    try:
        sys.path.insert(0, str(ROOT))
        from sandbox.config import admin_key
        adm = {"X-Admin-Key": admin_key()}
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}", timeout=10) as c:
            for _ in range(60):
                try:
                    if (await c.get("/api/dash")).status_code == 200:
                        break
                except Exception:
                    await asyncio.sleep(0.5)
            keys = [(await c.post("/api/admin/agents", json={"name": f"stress-{i}"}, headers=adm)).json()["api_key"]
                    for i in range(a.agents)]
            ins = (await c.get("/api/v1/instruments", headers={"X-API-Key": keys[0]})).json()
        syms = [i["symbol"] for i in ins if i["kind"] == "EQ"]
        opts = [i["symbol"] for i in ins if i["kind"] == "OPT"]
        stats, frames, cpu = {}, [], []
        until = time.time() + a.seconds

        procs = [proc] + proc.children(recursive=True)          # Windows venv: launcher + real python child
        for p_ in procs:
            low_priority(p_)
            p_.cpu_percent(None)

        async def sample():
            while time.time() < until:
                await asyncio.sleep(2)
                live = [p_ for p_ in procs if p_.is_running()]
                cpu.append((sum(p_.cpu_percent(None) for p_ in live),
                            sum(p_.memory_info().rss for p_ in live) / 2**20, psutil.cpu_percent(None)))
        await asyncio.gather(sample(), *(viewer(until, frames) for _ in range(a.viewers)),
                             *(agent(i, k, until, stats, syms, opts) for i, k in enumerate(keys)))
        total = sum(len(v) for v in stats.values())
        print(f"\n{a.agents} agents + {a.viewers} dashboard viewers for {a.seconds}s → {total:,} requests "
              f"({total / a.seconds:,.0f}/s)")
        print(f"{'operation':<14}{'count':>8}{'ok %':>8}{'p50 ms':>9}{'p95 ms':>9}{'p99 ms':>9}")
        for op, v in sorted(stats.items()):
            d = sorted(x for x, _ in v)
            q = lambda p: d[min(len(d) - 1, int(p * len(d)))]
            print(f"{op:<14}{len(v):>8}{100 * sum(ok for _, ok in v) / len(v):>8.1f}{q(.5):>9.1f}{q(.95):>9.1f}{q(.99):>9.1f}")
        if cpu:
            print(f"server CPU avg {statistics.mean(c[0] for c in cpu):.0f}% of one core (peak {max(c[0] for c in cpu):.0f}%), "
                  f"RAM peak {max(c[1] for c in cpu):.0f} MB, whole laptop CPU avg {statistics.mean(c[2] for c in cpu):.0f}%")
        print(f"dashboard frames received: {len(frames)} ({len(frames) / max(1, a.viewers) / a.seconds:.2f}/s per viewer)")
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except Exception:
            srv.kill()


if __name__ == "__main__":
    asyncio.run(main())
