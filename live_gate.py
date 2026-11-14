"""
Gate for live mode. Live mode is refused unless every check below passes.

This version has no live order code, so the last check always fails and live mode always refuses.
The gate file is a checklist with an audit trail, not a security control: whoever can edit it can
also edit this code. The real control is a human reading the evidence before approving.

Gate file (live_gate.json), written by a human after reviewing the evidence:
{
  "backtest": {"verdict": "CANDIDATE", "config_sha256": "...", "oos_revealed_once": true},
  "forward_paper": {"trades": 200, "within_backtest_ci": true},
  "approval": {"approved_by": "name", "date": "YYYY-MM-DD"},
  "max_stake_c": 100,
  "daily_loss_c": 300
}
"""
import json
import os


def evaluate_gate(cfg, cfg_hash, gate_path):
    if not os.path.exists(gate_path):
        return [f"no gate file at {gate_path}"]
    with open(gate_path) as f:
        g = json.load(f)
    failures = []
    bt = g.get("backtest", {})
    if bt.get("verdict") != "CANDIDATE":
        failures.append("backtest verdict on real data is not CANDIDATE")
    if bt.get("config_sha256") != cfg_hash:
        failures.append("validated config differs from the current config.json")
    if not bt.get("oos_revealed_once"):
        failures.append("out-of-sample result was not revealed exactly once")
    fwd = g.get("forward_paper", {})
    if fwd.get("trades", 0) < 200:
        failures.append(f"forward paper trades {fwd.get('trades', 0)} below 200")
    if not fwd.get("within_backtest_ci"):
        failures.append("forward paper results are outside the backtest confidence range")
    ap = g.get("approval", {})
    if not ap.get("approved_by") or not ap.get("date"):
        failures.append("no written human approval")
    if g.get("max_stake_c") is None or g.get("daily_loss_c") is None:
        failures.append("stake and daily loss limits are not set")
    failures.append("live order execution is not implemented in this version")
    return failures
