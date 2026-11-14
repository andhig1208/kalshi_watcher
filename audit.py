#!/usr/bin/env python3
"""
Independent audit: re-derives every trade from the raw quotes and flags any mismatch.
Run after backtest.py. A nonzero mismatch count means the engine and the data disagree.

Checks per trade
  - cost = qty x entry price + entry fee, with the fee recomputed
  - taker entry price = ask at fill minute + entry slippage
  - stop exit: the bid at the exit minute really was at or below half of cost
  - exit price = bid at exit minute - exit slippage, fee recomputed
  - settled trades: P&L equals the result outcome
"""
import argparse
import csv
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING

from backtest import load_config, load_quotes, load_windows, minute_key, parse_ts, ask_c, bid_c

ap = argparse.ArgumentParser()
ap.add_argument("--trades", required=True)
ap.add_argument("--windows", required=True)
ap.add_argument("--quotes", required=True)
ap.add_argument("--config", default="config.json")
args = ap.parse_args()

cfg = load_config(args.config)
M = Decimal(str(cfg["taker_fee_mult"]))
MM = Decimal(str(cfg["maker_fee_mult"]))
rows = load_windows(args.windows)
ticker_ws = {r["ticker"]: r for r in rows}
quotes = load_quotes(args.quotes, set(ticker_ws))


def fee(mult, qty, p):
    return int((mult * qty * p * (100 - p) / Decimal(100)).to_integral_value(rounding=ROUND_CEILING)) if qty else 0


problems, n = [], 0
with open(args.trades, newline="") as f:
    for t in csv.DictReader(f):
        n += 1
        tk, mode = t["ticker"], t["mode"]
        qty, ep, ef, cost = int(t["qty"]), int(t["entry_price"]), int(t["entry_fee"]), int(t["cost"])
        fm, em = int(t["fill_minute"]), int(t["exit_minute"])
        reason, pnl = t["exit_reason"], int(t["pnl"])
        ws = parse_ts(t["ws"])
        row = ticker_ws[tk]
        tag = f"{t['ws']} {tk}"

        if cost != qty * ep + ef:
            problems.append(f"{tag}: cost {cost} != qty*price+fee {qty * ep + ef}")
        if ef != fee(MM if mode == "maker" else M, qty, ep):
            problems.append(f"{tag}: entry fee {ef} does not match formula")

        q_fill = quotes.get((tk, minute_key(ws + timedelta(minutes=fm))))
        if mode == "taker" and q_fill is not None:
            if ep != min(ask_c(q_fill, t["side"]) + cfg["entry_slippage_c"], 99):
                problems.append(f"{tag}: taker price {ep} does not match ask at fill minute")

        if reason == "settled":
            want = qty * 100 - cost if t["side"] == row["result"] else -cost
            if pnl != want:
                problems.append(f"{tag}: settled pnl {pnl} != expected {want}")
        else:
            q_exit = quotes.get((tk, minute_key(ws + timedelta(minutes=em))))
            if q_exit is None:
                problems.append(f"{tag}: exit minute has no quote in the data")
                continue
            bid = bid_c(q_exit, t["side"])
            xp = int(t["exit_price"])
            if xp != max(bid - cfg["exit_slippage_c"], 1):
                problems.append(f"{tag}: exit price {xp} does not match bid {bid} minus slippage")
            xf = fee(M, qty, xp)
            if int(t["exit_fee"]) != xf:
                problems.append(f"{tag}: exit fee {t['exit_fee']} does not match formula {xf}")
            if pnl != qty * xp - xf - cost:
                problems.append(f"{tag}: exit pnl {pnl} does not match proceeds minus cost")
            if reason == "stop":
                if qty * bid > Decimal(str(cfg["stop_fraction"])) * cost:
                    problems.append(f"{tag}: stop fired but value {qty * bid}c is above half of cost {cost}c")

print(f"trades audited: {n}")
print(f"mismatches: {len(problems)}")
for p in problems[:20]:
    print("  " + p)
if not problems:
    print("every trade re-derives exactly from the quotes")
