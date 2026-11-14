#!/usr/bin/env python3
"""Hand-checked tests for the engine. Run: python3 test_engine.py (from this folder)."""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import backtest as b

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
TICKER = "BTC-T1"


def write(path, header, rows):
    with open(path, "w", newline="") as f:
        f.write(",".join(header) + "\n")
        for r in rows:
            f.write(",".join(str(x) for x in r) + "\n")


def build(d, result="yes", bids=None, asks=None):
    """One BTC window. Target 95. Candles make the signal fire yes at entry and stay yes at halfway."""
    bids, asks = bids or {}, asks or {}
    ws = T0
    write(os.path.join(d, "w.csv"),
          ["ticker", "asset", "window_start", "window_end", "floor_strike", "result"],
          [[TICKER, "BTC", ws.isoformat(), (ws + timedelta(minutes=15)).isoformat(), "95.00", result]])
    q = []
    for m in range(15):
        q.append([TICKER, (ws + timedelta(minutes=m)).isoformat(),
                  bids.get(m, 49), asks.get(m, 51)])
    write(os.path.join(d, "q.csv"), ["ticker", "time", "yes_bid_c", "yes_ask_c"], q)
    c = []
    for i in range(60):   # pre-window: steady decline, so RSI is 0
        t = ws - timedelta(minutes=60 - i)
        c.append(["BTC", t.isoformat(), f"{100 - 10 * i / 59:.4f}", "1.0"])
    for k in range(15):   # window: keeps falling, below target 95
        t = ws + timedelta(minutes=k)
        c.append(["BTC", t.isoformat(), f"{90 - 0.1 * k:.4f}", "1.0"])
    write(os.path.join(d, "c.csv"), ["asset", "time", "close", "volume"], c)


def run(cfg_over, result="yes", bids=None, asks=None):
    with tempfile.TemporaryDirectory() as d:
        build(d, result, bids, asks)
        cfg = b.load_config("config.json")
        cfg["assets"] = ["BTC"]
        cfg["require_cross_asset_agreement"] = False
        cfg["stop_check_minutes"] = [8]
        cfg.update(cfg_over)
        b.validate_config(cfg)
        rows = b.load_windows(os.path.join(d, "w.csv"))
        q = b.load_quotes(os.path.join(d, "q.csv"), {TICKER})
        cd = b.Candles(os.path.join(d, "c.csv"))
        eng = b.Engine(cfg, cd, q).run(b.group_windows(rows))
        return eng


def test_fees_and_sizing():
    m = Decimal("0.07")
    assert b.fee_c(m, 1, 50) == 2, "1 contract at 50c: 1.75c rounds up to 2c"
    assert b.fee_c(m, 1, 90) == 1, "1 contract at 90c: 0.63c rounds up to 1c"
    assert b.fee_c(m, 10, 50) == 18, "10 contracts at 50c: 17.5c rounds up to 18c"
    assert b.size_qty(100, 52, m) == 1, "100c budget, 52c contract plus 2c fee: one contract"
    assert b.size_qty(200, 52, m) == 3, "200c budget: 3 contracts cost 156c + 6c fee"


def test_stop_triggers_and_pnl():
    # Entry: ask 51 + 1c slippage = 52c, fee 2c, cost 54c. At minute 8 bid is 25c, value 25c <= 27c.
    # Exit: bid 25 - 1 slippage = 24c, exit fee 2c, proceeds 22c. P&L = 22 - 54 = -32c.
    eng = run({}, "yes", bids={8: 25}, asks={8: 27})
    assert len(eng.trades) == 1
    t = eng.trades[0]
    assert t["exit_reason"] == "stop", t["exit_reason"]
    assert t["cost"] == 54 and t["entry_fee"] == 2
    assert t["pnl"] == -32, t["pnl"]


def test_stop_boundary_is_inclusive():
    # Bid 27c: value 27c equals 50% of cost 54c, so the stop fires.
    # Exit 26c, fee 2c, proceeds 24c. P&L = 24 - 54 = -30c.
    eng = run({}, "yes", bids={8: 27}, asks={8: 29})
    assert eng.trades[0]["exit_reason"] == "stop"
    assert eng.trades[0]["pnl"] == -30, eng.trades[0]["pnl"]


def test_no_stop_above_half_then_settles():
    eng = run({}, "no", bids={8: 28}, asks={8: 30})   # value 28c > 27c, no stop
    t = eng.trades[0]
    assert t["exit_reason"] == "settled"
    assert t["pnl"] == -54, t["pnl"]                   # result no, held: lose full cost


def test_settlement_wins():
    eng = run({"stop_enabled": False}, "yes")
    assert eng.trades[0]["exit_reason"] == "settled" and eng.trades[0]["pnl"] == 46  # 100 - 54
    eng = run({"stop_enabled": False}, "no")
    assert eng.trades[0]["pnl"] == -54


def test_maker_fill_and_pnl():
    # Rest a bid at 49c. Ask falls to 49c at minute 2, so it fills at 49c.
    # Maker fee on 2 contracts at 49c: 0.0175*2*49*51/100 = 0.875c, rounds up to 1c.
    # Budget 100c: 2 contracts cost 98c + 1c = 99c. Result yes: pnl = 200 - 99 = 101c.
    eng = run({"entry_mode": "maker", "stop_enabled": False}, "yes", asks={2: 49})
    t = eng.trades[0]
    assert t["mode"] == "maker" and t["fill_minute"] == 2
    assert t["qty"] == 2 and t["cost"] == 99 and t["pnl"] == 101, (t["qty"], t["cost"], t["pnl"])


def test_maker_unfilled_then_late_entry():
    # Ask never trades to 49c. Rest unfilled, cancel at halfway, then take a late entry at minute 8.
    eng = run({"entry_mode": "maker", "stop_enabled": False}, "yes")
    events = [d["event"] for d in eng.decisions]
    assert "UNFILLED" in events and "LATE_ENTRY" in events, events
    t = eng.trades[0]
    assert t["mode"] == "taker" and t["fill_minute"] == 8 and t["pnl"] == 46


def test_deterministic():
    a = json.dumps(run({}, "yes", bids={8: 25}, asks={8: 27}).trades, default=str, sort_keys=True)
    c = json.dumps(run({}, "yes", bids={8: 25}, asks={8: 27}).trades, default=str, sort_keys=True)
    assert a == c


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
        print("ok  ", t.__name__)
    print(f"all {len(tests)} tests passed")
