"""Starter bots driven through a real (mock) sandbox server over HTTP."""
import threading
import time
from datetime import datetime

import pytest
import uvicorn

from sandbox.bots.strategies import Momentum50, OrbNifty, VwapRevert
from sandbox.config import load_config
from sandbox.market.instruments import IST
from sandbox.sdk import Sandbox

PORT = 8288


@pytest.fixture(scope="module")
def sb_env(tmp_path_factory, request):
    import os
    from sandbox import server
    os.environ["SANDBOX_ADMIN_KEY"] = "adm_bots"
    e = server.Engine(load_config(), mock=True, db_path=tmp_path_factory.mktemp("b") / "bots.sqlite")
    e.setup()
    srv = uvicorn.Server(uvicorn.Config(server.make_app(e), host="127.0.0.1", port=PORT, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    def key(name):
        return e.db.create_agent(name, "bot")
    yield e, key
    srv.should_exit = True
    th.join(5)


def at(bot, hh, mm):
    bot.clock.now = lambda: datetime.now(IST).replace(hour=hh, minute=mm, second=0)


def test_orb_buys_breakout_call_then_time_exit(sb_env):
    e, key = sb_env
    bot = OrbNifty(Sandbox(f"http://127.0.0.1:{PORT}", key("orb-test")), mock=True)
    at(bot, 9, 20)
    for _ in range(3):
        bot.run_once()
    assert bot.or_hi and bot.or_lo and not bot.sb.positions()
    e.feed.spot = bot.or_hi + 60
    e.feed.step()
    at(bot, 9, 40)
    bot.run_once()
    pos = bot.sb.positions()
    assert len(pos) == 1 and pos[0]["symbol"].endswith("-CE") and pos[0]["qty"] % 65 == 0
    assert pos[0]["avg"] * pos[0]["qty"] <= OrbNifty.budget
    at(bot, 15, 1)
    bot.run_once()
    assert not [p for p in bot.sb.positions() if p["qty"]]


def test_vwap_revert_enters_below_vwap_and_exits_at_vwap(sb_env):
    e, key = sb_env
    bot = VwapRevert(Sandbox(f"http://127.0.0.1:{PORT}", key("vwap-test")), mock=True)
    at(bot, 10, 0)
    ins = e.reg.get("INFY")
    ltp = e.store.ticks[ins.sid][0]
    for s in e.reg.stocks():                                  # neutralise every stock: price == VWAP
        q = e.store.quote[s.sid]
        e.store.quote[s.sid] = (e.store.ticks[s.sid][0], q[1], q[2], q[3])
    e.store.quote[ins.sid] = (ltp * 1.02, ltp, ltp * 1.03, ltp * 0.99)   # INFY 2 % below VWAP
    bot.day = bot.clock.now().date()                         # already started today (keeps seeded history)
    now = bot.clock.now().timestamp()
    bot.history["INFY"].extend([(now - 240, ltp * 0.995), (now - 120, ltp * 0.997)])   # turning up
    bot.run_once()
    pos = bot.sb.positions()
    assert [p["symbol"] for p in pos] == ["INFY"] and pos[0]["qty"] * pos[0]["avg"] <= 10_000
    t = e.store.ticks[ins.sid]
    e.store.ticks[ins.sid] = (ltp * 1.021, t[1], t[2], ltp * 1.0209, ltp * 1.0211, t[5], time.time())
    bot.run_once()
    assert not [p for p in bot.sb.positions() if p["qty"]]
    assert bot.sb.account()["agent"]["realized"] > 0


def test_momentum_waits_for_nifty_and_time(sb_env):
    e, key = sb_env
    bot = Momentum50(Sandbox(f"http://127.0.0.1:{PORT}", key("mom-test")), mock=True)
    at(bot, 9, 45)
    bot.run_once()
    assert not bot.sb.positions()                             # before 10:00: never enters
