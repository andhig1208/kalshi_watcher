#!/usr/bin/env python3
"""Live mode must refuse in every case this version can reach. Paper mode must still start."""
import json
import os
import shutil
import tempfile

import backtest as b
import live_gate
import watcher as w

cfg = b.load_config("config.json")
h = b.obj_sha256(cfg)
tmp = tempfile.mkdtemp()
try:
    gate = os.path.join(tmp, "live_gate.json")

    # 1. No gate file: refused, and the reason is listed.
    fails = live_gate.evaluate_gate(cfg, h, gate)
    assert fails == [f"no gate file at {gate}"], fails
    try:
        w.Watcher(cfg, w.ReplayFeed({}, {}, b.Candles.__new__(b.Candles)), os.path.join(tmp, "s.json"),
                  os.path.join(tmp, "l"), os.path.join(tmp, "KILL"), mode="live", gate_path=gate)
        raise AssertionError("live mode must refuse")
    except SystemExit as e:
        assert "LIVE MODE REFUSED" in str(e)
    print("ok   live refused with no gate file")

    # 2. A complete, correct-looking gate file still refuses: live order code does not exist yet.
    full = {"backtest": {"verdict": "CANDIDATE", "config_sha256": h, "oos_revealed_once": True},
            "forward_paper": {"trades": 250, "within_backtest_ci": True},
            "approval": {"approved_by": "tester", "date": "2026-12-01"},
            "max_stake_c": 100, "daily_loss_c": 300}
    with open(gate, "w") as f:
        json.dump(full, f)
    fails = live_gate.evaluate_gate(cfg, h, gate)
    assert fails == ["live order execution is not implemented in this version"], fails
    print("ok   a fully passing gate still refuses: live order code is not built")

    # 3. A changed config is caught.
    fails = live_gate.evaluate_gate(dict(cfg, risk_fraction="0.02"), b.obj_sha256(dict(cfg, risk_fraction="0.02")), gate)
    assert any("config differs" in f for f in fails), fails
    print("ok   a config change after validation is caught")

    # 4. Paper mode still starts normally.
    wp = w.Watcher(cfg, w.ReplayFeed({}, {}, b.Candles.__new__(b.Candles)), os.path.join(tmp, "s2.json"),
                   os.path.join(tmp, "l2"), os.path.join(tmp, "KILL2"), mode="paper")
    assert not wp.halted
    print("ok   paper mode starts")
finally:
    shutil.rmtree(tmp)
print("all live-gate tests passed")
