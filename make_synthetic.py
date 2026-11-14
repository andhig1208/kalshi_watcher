#!/usr/bin/env python3
"""
Synthetic FAIR market: no edge exists by construction.

Each asset follows a random walk. A window's target is the pre-open minute close.
Each minute's quote is the fair chance of YES given the path so far (Gaussian
approximation), rounded to cents, with a 2 to 4 cent spread. Settlement is the
minute-14 close against the target. Quotes are fair, so after fees and spread no
strategy should show an edge. A CANDIDATE here means a bug.
"""
import argparse
import csv
import math
import random
from datetime import datetime, timedelta, timezone

ap = argparse.ArgumentParser()
ap.add_argument("--n", type=int, default=20000, help="windows per asset")
ap.add_argument("--seed", type=int, default=7)
ap.add_argument("--sigma", type=float, default=0.0008, help="per-minute relative volatility")
ap.add_argument("--windows_out", default="windows.csv")
ap.add_argument("--quotes_out", default="quotes.csv")
ap.add_argument("--candles_out", default="candles.csv")
args = ap.parse_args()

rng = random.Random(args.seed)
start = datetime(2026, 1, 1, tzinfo=timezone.utc)
t0 = start - timedelta(minutes=60)
assets = {"BTC": 70000.0, "ETH": 2000.0}
total = 15 * args.n + 60


def phi(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


yes_total = 0
with open(args.windows_out, "w", newline="") as wf, \
        open(args.quotes_out, "w", newline="") as qf, \
        open(args.candles_out, "w", newline="") as cf:
    ww, qw, cw = csv.writer(wf), csv.writer(qf), csv.writer(cf)
    ww.writerow(["ticker", "asset", "window_start", "window_end", "floor_strike", "result"])
    qw.writerow(["ticker", "time", "yes_bid_c", "yes_ask_c"])
    cw.writerow(["asset", "time", "close", "volume"])
    for asset, px0 in assets.items():
        path, p = [], px0
        for m in range(total):
            p *= 1.0 + rng.gauss(0.0, args.sigma)
            path.append((t0 + timedelta(minutes=m), p, round(rng.uniform(0.5, 2.0), 4)))
        for t, c, v in path:
            cw.writerow([asset, t.isoformat(), f"{c:.2f}", f"{v:.4f}"])
        for i in range(args.n):
            ws = start + timedelta(minutes=15 * i)
            we = ws + timedelta(minutes=15)
            ticker = f"SYN-{asset}-{i:06d}"
            K = path[59 + 15 * i][1]          # target: pre-open minute close
            final = path[74 + 15 * i][1]      # settlement: minute-14 close
            result = "yes" if final >= K else "no"
            ww.writerow([ticker, asset, ws.isoformat(), we.isoformat(), f"{K:.2f}", result])
            yes_total += result == "yes"
            spread = rng.choice([2, 2, 3, 4])
            for m in range(15):
                S = path[59 + 15 * i + m][1]  # last closed bar at quote time ws+m
                z = math.log(S / K) / (args.sigma * math.sqrt(15 - m))
                mid = min(max(round(100 * phi(z)), 2), 98)
                bid = max(1, mid - spread // 2)
                ask = min(99, bid + spread)
                qw.writerow([ticker, (ws + timedelta(minutes=m)).isoformat(), bid, ask])

print(f"wrote {args.windows_out}, {args.quotes_out}, {args.candles_out}")
print(f"windows per asset {args.n}, realized YES rate {yes_total / (2 * args.n):.3f}")
