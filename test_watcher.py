#!/usr/bin/env python3
"""
Equivalence test: replay historical windows through the live watcher on a fake clock and
check it makes exactly the same trades and decision events as the batch backtest.

Setup (run once):
  python3 make_synthetic.py --n 1500 --seed 11 --windows_out w1500.csv --quotes_out q1500.csv --candles_out c1500.csv
"""
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


def run_both(cfg):
    batch = b.Engine(cfg, candles, quotes).run(by_ws)
    tmp = tempfile.mkdtemp()
    try:
        wt = w.Watcher(cfg, w.ReplayFeed(by_ws, quotes, candles),
                       os.path.join(tmp, "state.json"), os.path.join(tmp, "logs"),
                       os.path.join(tmp, "KILL"), mode="paper")
        for ws in starts:
            for m in range(15):
                for off in (1, 3, 6, 9):
                    wt.tick(ws + timedelta(minutes=m, seconds=off))
        wt.tick(starts[-1] + timedelta(minutes=15, seconds=1))   # settle the last window
    finally:
        shutil.rmtree(tmp)
    return batch, wt


def key(t):
    return (t["ws"].isoformat(), t["asset"], t["side"], t["mode"], t["fill_minute"],
            t["exit_minute"], t["exit_reason"], t["qty"], t["entry_price"], t["cost"], t["pnl"])


def dec(d):
    return (d["window_start"].isoformat(), d["asset"], d["minute"], d["event"])


for label, over in [("cross-asset on (config.json)", {}),
                    ("cross-asset off", {"require_cross_asset_agreement": False})]:
    cfg = b.load_config("config.json")
    cfg.update(over)
    batch, wt = run_both(cfg)
    bt = [key(t) for t in batch.trades]
    wtr = [key(t) for t in wt.eng.trades]
    # The final settle tick opens a window past the data, which logs NO_MARKETS. That is expected.
    wd = [dec(d) for d in wt.eng.decisions if d["event"] != "NO_MARKETS"]
    bd = [dec(d) for d in batch.decisions]
    print(f"[{label}] batch trades {len(bt)} | watcher trades {len(wtr)} | "
          f"decision events {len(bd)} vs {len(wd)}")
    assert bt == wtr, "watcher and backtest disagree on trades"
    assert bd == wd, "watcher and backtest disagree on decision events"
    assert batch.bankroll == wt.eng.bankroll, (batch.bankroll, wt.eng.bankroll)
    assert not wt.halted, wt.halt_reason
    print(f"  identical: trades, decision events, final bankroll {batch.bankroll}c")
print("watcher equivalence test passed")
