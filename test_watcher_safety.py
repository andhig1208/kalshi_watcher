#!/usr/bin/env python3
"""Safety tests for the watcher: kill switch, restart continuity, and refusal to resume with an open position."""
import os
import shutil
import tempfile
from datetime import timedelta

import backtest as b
import watcher as w

rows = b.load_windows("w1500.csv")
quotes = b.load_quotes("q1500.csv", {r["ticker"] for r in rows})
candles = b.Candles("c1500.csv")
by_ws = b.group_windows(rows)
starts = sorted(by_ws)
cfg = b.load_config("config.json")
cfg["require_cross_asset_agreement"] = False
batch = b.Engine(cfg, candles, quotes).run(by_ws)
first_trade = batch.trades[0]
tws = first_trade["ws"]
print(f"first batch trade: {tws.isoformat()} {first_trade['asset']} {first_trade['side']} "
      f"exit {first_trade['exit_reason']} at minute {first_trade['exit_minute']}")


def fresh_watcher(tmp):
    return w.Watcher(cfg, w.ReplayFeed(by_ws, quotes, candles), os.path.join(tmp, "state.json"),
                     os.path.join(tmp, "logs"), os.path.join(tmp, "KILL"), mode="paper")


def feed_ticks(wt, window_list):
    for ws in window_list:
        for m in range(15):
            for off in (1, 3, 6, 9):
                wt.tick(ws + timedelta(minutes=m, seconds=off))


# 1. Kill switch: create the file while a position is open, expect a flatten and a halt.
tmp = tempfile.mkdtemp()
try:
    wt = fresh_watcher(tmp)
    kill_at = tws + timedelta(minutes=3, seconds=3)   # must match a tick offset
    for ws in starts:
        if ws > tws + timedelta(minutes=14):
            break
        for m in range(15):
            for off in (1, 3, 6, 9):
                t = ws + timedelta(minutes=m, seconds=off)
                if t == kill_at:
                    open(os.path.join(tmp, "KILL"), "w").close()
                wt.tick(t)
    kills = [t for t in wt.eng.trades if t["exit_reason"] == "kill"]
    assert kills, "kill switch did not flatten the open position"
    assert wt.halted and "kill file" in wt.halt_reason
    assert wt.run is None or wt.run.pos is None
    wt.tick(tws + timedelta(minutes=16))           # halted: nothing further happens
    print("ok   kill switch flattens the open position and halts")
finally:
    shutil.rmtree(tmp)

# 2. Restart continuity: stop cleanly after a settled window, restart, and reach the same final bankroll.
tmp = tempfile.mkdtemp()
try:
    split = len(starts) // 2
    boundary = starts[split]                       # ticks through boundary settle the window before it
    wt1 = fresh_watcher(tmp)
    feed_ticks(wt1, starts[:split])
    wt1.tick(boundary + timedelta(seconds=1))
    assert wt1.run is None or wt1.run.pos is None, "test split landed on an open position"
    wt2 = fresh_watcher(tmp)                       # new process reads the saved state
    feed_ticks(wt2, starts[split + 1:])
    wt2.tick(starts[-1] + timedelta(minutes=15, seconds=1))
    assert not wt2.halted, wt2.halt_reason
    assert wt2.eng.bankroll == batch.bankroll, (wt2.eng.bankroll, batch.bankroll)
    print(f"ok   restart resumes the same bankroll: {wt2.eng.bankroll}c equals the full run")
finally:
    shutil.rmtree(tmp)

# 3. Refuse to resume with an open position in the state file.
tmp = tempfile.mkdtemp()
try:
    wt = fresh_watcher(tmp)
    feed_ticks(wt, [ws for ws in starts if ws < tws])
    for m in range(0, 3):                          # entry at minute 1, still open at minute 2
        for off in (1, 3, 6, 9):
            wt.tick(tws + timedelta(minutes=m, seconds=off))
    assert wt.run is not None and wt.run.pos is not None, "expected an open position mid-window"
    try:
        fresh_watcher(tmp)
        raise AssertionError("restart with an open position should have refused")
    except SystemExit as e:
        assert "open position" in str(e)
    print("ok   restart refuses to resume while a position is open")
finally:
    shutil.rmtree(tmp)
print("all safety tests passed")
