#!/usr/bin/env python3
"""
Grid sweep on IN-SAMPLE data only. Out-of-sample data is never read here.

Ranks configs by the lower 95% bound on expectancy per contract, which penalizes small
samples. The winner is still optimistic because many configs were tried. Pick one by
the rule written in the README BEFORE running the sweep, then run backtest.py
--report-oos exactly once on it.
"""
import argparse
import copy
import csv
import itertools
import json
import os
from datetime import datetime, timezone

import backtest as b

ap = argparse.ArgumentParser()
ap.add_argument("--windows", required=True)
ap.add_argument("--quotes", required=True)
ap.add_argument("--candles", required=True)
ap.add_argument("--base", default="config.json")
ap.add_argument("--grid", default="sweep_grid.json")
ap.add_argument("--min_trades", type=int, default=100)
ap.add_argument("--out", default="sweep_results.csv")
args = ap.parse_args()

base = b.load_config(args.base)
rows = b.load_windows(args.windows)
quotes = b.load_quotes(args.quotes, {r["ticker"] for r in rows})
candles = b.Candles(args.candles)
by_ws = b.group_windows(rows)
cutoff = b.split_point(by_ws, base["oos_fraction"])
in_ws = {k: v for k, v in by_ws.items() if k < cutoff}   # out-of-sample never enters this file

with open(args.grid) as f:
    grid = json.load(f)
keys = sorted(grid)
combos = list(itertools.product(*(grid[k] for k in keys)))

results = []
for i, combo in enumerate(combos, 1):
    over = dict(zip(keys, combo))
    cfg = copy.deepcopy(base)
    cfg.update(over)
    b.validate_config(cfg)
    eng = b.Engine(cfg, candles, quotes).run(in_ws)
    st = b.stats(eng.trades)
    row = {k: json.dumps(v) for k, v in over.items()}
    row.update({"n": st["n"], "expectancy_c": st["mean"], "ci_lo_c": st["lo"],
                "ci_hi_c": st["hi"], "final_bankroll_c": eng.bankroll})
    results.append(row)
    print(f"[{i}/{len(combos)}] n={st['n']} expectancy={st['mean'] if st['mean'] is None else round(st['mean'], 2)}c")

fields = keys + ["n", "expectancy_c", "ci_lo_c", "ci_hi_c", "final_bankroll_c"]
with open(args.out, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    for r in results:
        w.writerow({k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()})

ranked = sorted([r for r in results if r["n"] >= args.min_trades and r["ci_lo_c"] is not None],
                key=lambda r: r["ci_lo_c"], reverse=True)
print(f"\nconfigs tried: {len(combos)} | with >= {args.min_trades} in-sample trades: {len(ranked)}")
for r in ranked[:10]:
    print(f"  ci_lo {r['ci_lo_c']:+.2f}c  expectancy {r['expectancy_c']:+.2f}c  n={r['n']}  " +
          "  ".join(f"{k}={r[k]}" for k in keys))
print("\nThe top row is optimistic: many configs were tried. Confirm with backtest.py --report-oos, once.")

with open("sweep_log.csv", "a", newline="") as f:
    fresh = not os.path.exists("sweep_log.csv") or os.path.getsize("sweep_log.csv") == 0
    w = csv.writer(f)
    if fresh:
        w.writerow(["utc", "configs_tried", "min_trades", "grid"])
    w.writerow([datetime.now(timezone.utc).isoformat(), len(combos), args.min_trades, json.dumps(grid, sort_keys=True)])
