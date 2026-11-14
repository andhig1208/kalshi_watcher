#!/usr/bin/env python3
"""
BTC/ETH 15-minute backtest engine, version 2.

Money is integer cents and fees are Decimal, rounded up to the cent, so identical
inputs always give identical outputs. The engine walks each window minute by minute
on quote data, so every stop and halfway check uses a quote that existed at that minute.

Per window
  1. Entry at entry_minute: signal on closed one-minute candles, optional cross-asset
     agreement, spread limit, minutes-left gate, price band.
       taker: buy at the ask plus slippage, pay the taker fee.
       maker: rest a bid at the current bid. Fills only if the ask trades down to it
              before halfway. Pays the maker fee at the limit price.
  2. Monitoring at each minute in stop_check_minutes and at halfway.
       stop: exit at the bid when qty x bid <= stop_fraction x cost basis.
       halfway: exit on signal reversal or cross-asset divergence.
  3. Late entry at halfway if flat, not yet traded, and enough time remains.
  4. Settlement at the result if still open.

Account
  Stake = risk_fraction of bankroll, counting full cost including fees. At most one
  position per window. Streak pauses and the daily loss stop apply.

The engine is causal: a decision at time t uses only data available at t. So running
it on the full history and then splitting trades by time gives the same in-sample
trades as running on the in-sample period alone.

Not modeled: queue position, partial fills, order-book depth, cancellation latency,
fee schedule changes, and the CF Benchmarks index itself.
"""
import argparse
import bisect
import csv
import hashlib
import json
import math
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING

ENGINE_VERSION = "2.0"
WINDOW = timedelta(minutes=15)
ONE_MIN = timedelta(minutes=1)

TRADE_FIELDS = ["ws", "ticker", "asset", "side", "mode", "fill_minute", "exit_minute",
                "exit_reason", "qty", "entry_price", "entry_fee", "cost", "exit_price",
                "exit_fee", "pnl", "hold_pnl", "win", "bankroll_before", "bankroll_after", "signal"]
DECISION_FIELDS = ["window_start", "asset", "minute", "event", "reason"]
LEDGER_FIELDS = ["utc", "config_sha256", "windows_sha256", "oos_trades",
                 "oos_expectancy_c", "oos_ci_lo_c", "oos_ci_hi_c"]
REQUIRED = ["bankroll_start_c", "assets", "entry_minute", "halfway_minute",
            "require_cross_asset_agreement", "risk_fraction", "max_daily_loss_fraction",
            "max_consecutive_losses", "pause_windows_after_streak", "min_minutes_left_for_entry",
            "spread_max_c", "price_min_c", "price_max_c", "entry_mode", "entry_slippage_c",
            "exit_slippage_c", "taker_fee_mult", "maker_fee_mult", "late_entry_enabled",
            "stop_enabled", "stop_fraction", "stop_check_minutes", "exit_on_signal_reversal",
            "exit_on_divergence", "rsi_period", "rsi_oversold", "rsi_overbought", "vwap_bars",
            "vwap_gate", "oos_fraction", "min_oos_trades"]


# ---------- helpers ----------

def parse_ts(s):
    dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"timestamp needs a UTC offset: {s}")
    return dt.astimezone(timezone.utc)


def minute_key(t):
    return int(t.timestamp()) // 60


def file_sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def obj_sha256(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def fee_c(mult, qty, price_c):
    """Order fee in cents: mult x qty x P x (1-P), P in dollars, rounded UP to the cent."""
    if qty <= 0:
        return 0
    raw = mult * qty * price_c * (100 - price_c) / Decimal(100)
    return int(raw.to_integral_value(rounding=ROUND_CEILING))


def size_qty(budget_c, price_c, mult):
    """Most whole contracts whose full cost plus fee fits the budget."""
    q = budget_c // price_c
    while q >= 1 and q * price_c + fee_c(mult, q, price_c) > budget_c:
        q -= 1
    return max(q, 0)


def ask_c(q, side):
    """Cents to buy this side now. q = (yes_bid_c, yes_ask_c)."""
    bid, ask = q
    return ask if side == "yes" else 100 - bid


def bid_c(q, side):
    """Cents you receive selling this side now."""
    bid, ask = q
    return bid if side == "yes" else 100 - ask


def rsi_wilder(closes, period):
    gains, losses = [], []
    for a, b in zip(closes, closes[1:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


# ---------- loaders ----------

def validate_config(cfg):
    missing = [k for k in REQUIRED if k not in cfg]
    if missing:
        raise SystemExit(f"config missing keys: {missing}")
    if not (0 <= cfg["entry_minute"] < cfg["halfway_minute"] <= 13):
        raise SystemExit("need entry_minute < halfway_minute <= 13")
    if cfg["entry_mode"] not in ("taker", "maker"):
        raise SystemExit("entry_mode must be taker or maker")
    bad = [m for m in cfg["stop_check_minutes"] if not (2 <= m <= 14)]
    if bad:
        raise SystemExit(f"stop_check_minutes must be 2..14, got {bad}")
    return cfg


def load_config(path):
    with open(path) as f:
        return validate_config(json.load(f))


def load_windows(path):
    rows, seen = [], set()
    with open(path, newline="") as f:
        for ln, r in enumerate(csv.DictReader(f), start=2):
            try:
                ws, we = parse_ts(r["window_start"]), parse_ts(r["window_end"])
                row = {"ticker": r["ticker"].strip(), "asset": r["asset"].strip().upper(),
                       "ws": ws, "we": we, "floor": float(r["floor_strike"]),
                       "result": r["result"].strip().lower()}
                if we - ws != WINDOW:
                    raise ValueError("window must be 15 minutes")
                if ws.minute % 15 or ws.second:
                    raise ValueError("window_start must sit on a 15-minute boundary")
                if row["result"] not in ("yes", "no"):
                    raise ValueError("result must be yes or no")
                if row["ticker"] in seen:
                    raise ValueError(f"duplicate ticker {row['ticker']}")
                seen.add(row["ticker"])
            except (KeyError, ValueError) as e:
                raise SystemExit(f"{path} line {ln}: {e}")
            rows.append(row)
    return rows


def group_windows(rows):
    by = defaultdict(dict)
    for r in rows:
        if r["asset"] in by[r["ws"]]:
            raise SystemExit(f"two windows for {r['asset']} at {r['ws'].isoformat()}")
        by[r["ws"]][r["asset"]] = r
    return dict(by)


def load_quotes(path, tickers):
    q = {}
    with open(path, newline="") as f:
        for ln, r in enumerate(csv.DictReader(f), start=2):
            try:
                tk = r["ticker"].strip()
                t = parse_ts(r["time"])
                bid, ask = int(r["yes_bid_c"]), int(r["yes_ask_c"])
                if t.second or t.microsecond:
                    raise ValueError("quote time must be on the minute")
                if not (1 <= bid <= ask <= 99):
                    raise ValueError("need 1 <= yes_bid_c <= yes_ask_c <= 99")
                if tk not in tickers:
                    raise ValueError(f"quote for unknown ticker {tk}")
                key = (tk, minute_key(t))
                if key in q:
                    raise ValueError("duplicate quote")
                q[key] = (bid, ask)
            except (KeyError, ValueError) as e:
                raise SystemExit(f"{path} line {ln}: {e}")
    return q


class Candles:
    def __init__(self, path):
        series = defaultdict(list)
        with open(path, newline="") as f:
            for ln, r in enumerate(csv.DictReader(f), start=2):
                try:
                    t = parse_ts(r["time"])
                    if t.second or t.microsecond:
                        raise ValueError("candle time must be on the minute")
                    series[r["asset"].strip().upper()].append(
                        (t, float(r["close"]), float(r["volume"])))
                except (KeyError, ValueError) as e:
                    raise SystemExit(f"{path} line {ln}: {e}")
        self.data = {a: sorted(v) for a, v in series.items()}
        self.times = {a: [t for t, _, _ in v] for a, v in self.data.items()}

    def closed_before(self, asset, t, n):
        """Last n one-minute bars fully closed by time t (bar stamps are bar-open times)."""
        ts = self.times.get(asset, [])
        i = bisect.bisect_right(ts, t - ONE_MIN)
        return self.data.get(asset, [])[max(0, i - n):i]


def signal(asset, t, floor, candles, cfg):
    """(side, info) or (None, reason). Uses only one-minute bars closed by time t."""
    hist = candles.closed_before(asset, t, max(60, cfg["rsi_period"] * 4))
    if len(hist) < cfg["rsi_period"] + 1:
        return None, "insufficient candles"
    if any(hist[i + 1][0] - hist[i][0] != ONE_MIN for i in range(len(hist) - 1)):
        return None, "candle gap in lookback"
    closes = [c for _, c, _ in hist]
    rsi = rsi_wilder(closes, cfg["rsi_period"])
    last = hist[-cfg["vwap_bars"]:]
    vol = sum(v for _, _, v in last)
    if vol <= 0:
        return None, "zero volume"
    vwap = sum(c * v for _, c, v in last) / vol
    price = closes[-1]
    info = {"rsi": round(rsi, 2), "price": price, "vwap": round(vwap, 2), "target": floor}
    if price < floor and rsi <= cfg["rsi_oversold"]:
        if not cfg["vwap_gate"] or price < vwap:
            return "yes", info
    if price > floor and rsi >= cfg["rsi_overbought"]:
        if not cfg["vwap_gate"] or price > vwap:
            return "no", info
    return None, "no setup"


# ---------- engine ----------

class Engine:
    def __init__(self, cfg, candles, quotes):
        self.cfg = cfg
        self.candles = candles
        self.quotes = quotes
        self.mult = Decimal(str(cfg["taker_fee_mult"]))
        self.maker_mult = Decimal(str(cfg["maker_fee_mult"]))
        self.stop_frac = Decimal(str(cfg["stop_fraction"]))
        self.risk_frac = Decimal(str(cfg["risk_fraction"]))
        self.max_daily = Decimal(str(cfg["max_daily_loss_fraction"]))
        self.bankroll = int(cfg["bankroll_start_c"])
        self.day, self.day_start, self.day_pnl = None, self.bankroll, 0
        self.streak, self.pause_until, self.cur_idx = 0, -1, -1
        self.stop_set = set(cfg["stop_check_minutes"])
        self.checks = self.stop_set | {cfg["halfway_minute"]}
        self.trades, self.baseline, self.decisions = [], [], []

    def log(self, ws, asset, event, reason="", minute=""):
        self.decisions.append({"window_start": ws, "asset": asset, "minute": minute,
                               "event": event, "reason": reason})

    def quote(self, ticker, t):
        return self.quotes.get((ticker, minute_key(t)))

    def roll_day(self, ws):
        if ws.date() != self.day:  # daily stop resets at 00:00 UTC
            self.day, self.day_start, self.day_pnl = ws.date(), self.bankroll, 0

    def daily_stop_hit(self):
        return self.day_pnl <= -(Decimal(self.day_start) * self.max_daily)

    def budget_c(self):
        return int(Decimal(self.bankroll) * self.risk_frac)

    def price_ok(self, p):
        return self.cfg["price_min_c"] <= p <= self.cfg["price_max_c"]

    def select(self, ws, cell, t):
        """Entry candidate at time t, or (None, reason)."""
        cfg = self.cfg
        we = ws + WINDOW
        found, reasons = {}, []
        for a in cfg["assets"]:
            row = cell.get(a)
            if row is None:
                reasons.append(f"{a} missing")
                continue
            side, info = signal(a, t, row["floor"], self.candles, cfg)
            if side is None:
                reasons.append(f"{a}: {info}")
                continue
            found[a] = (side, row, info)
        if not found:
            return None, "; ".join(reasons) or "no setup"
        if cfg["require_cross_asset_agreement"]:
            if len(found) < len(cfg["assets"]):
                return None, "cross-asset: only one asset has a setup"
            if len({v[0] for v in found.values()}) != 1:
                return None, "cross-asset: sides disagree"
        a = next(x for x in cfg["assets"] if x in found)
        side, row, info = found[a]
        q = self.quote(row["ticker"], t)
        if q is None:
            return None, f"{a}: no quote at {t.strftime('%H:%M')}"
        if q[1] - q[0] > cfg["spread_max_c"]:
            return None, f"{a}: spread {q[1] - q[0]}c above max"
        if we - t < timedelta(minutes=cfg["min_minutes_left_for_entry"]):
            return None, "too little time left"
        if not self.price_ok(ask_c(q, side)):
            return None, f"{a}: ask outside price band"
        return {"asset": a, "side": side, "row": row, "quote": q, "info": info}, "setup"

    def halfway_reason(self, ws, cell, pos, t):
        cfg = self.cfg
        if cfg["exit_on_signal_reversal"]:
            s, _ = signal(pos["asset"], t, cell[pos["asset"]]["floor"], self.candles, cfg)
            if s is not None and s != pos["side"]:
                return "reversal"
        if cfg["exit_on_divergence"]:
            for a in cfg["assets"]:
                if a == pos["asset"] or a not in cell:
                    continue
                s, _ = signal(a, t, cell[a]["floor"], self.candles, cfg)
                if s is not None and s != pos["side"]:
                    return "divergence"
        return None

    def new_position(self, ws, cand, mode, minute, qty, price, fee):
        cost = qty * price + fee
        row = cand["row"]
        hold = qty * 100 - cost if cand["side"] == row["result"] else -cost
        return {"ws": ws, "ticker": row["ticker"], "asset": cand["asset"], "side": cand["side"],
                "result": row["result"], "mode": mode, "fill_minute": minute, "qty": qty,
                "entry_price": price, "entry_fee": fee, "cost": cost, "hold_pnl": hold,
                "signal": json.dumps(cand["info"], sort_keys=True)}

    def open_taker(self, cand, ws, minute):
        side = cand["side"]
        p = min(ask_c(cand["quote"], side) + self.cfg["entry_slippage_c"], 99)
        if not self.price_ok(p):
            return None, f"price {p}c outside band after slippage"
        qty = size_qty(self.budget_c(), p, self.mult)
        if qty < 1:
            return None, "budget too small for one contract"
        fee = fee_c(self.mult, qty, p)
        return self.new_position(ws, cand, "taker", minute, qty, p, fee), "filled"

    def close(self, ws, pos, minute, bid, reason):
        if reason == "settled":
            pnl, exit_p, exit_fee = pos["hold_pnl"], None, 0
        else:
            exit_p = max(bid - self.cfg["exit_slippage_c"], 1)
            exit_fee = fee_c(self.mult, pos["qty"], exit_p)
            pnl = pos["qty"] * exit_p - exit_fee - pos["cost"]
        before = self.bankroll
        self.book(pnl)
        self.trades.append({
            "ws": pos["ws"], "ticker": pos["ticker"], "asset": pos["asset"], "side": pos["side"],
            "mode": pos["mode"], "fill_minute": pos["fill_minute"], "exit_minute": minute,
            "exit_reason": reason, "qty": pos["qty"], "entry_price": pos["entry_price"],
            "entry_fee": pos["entry_fee"], "cost": pos["cost"], "exit_price": exit_p,
            "exit_fee": exit_fee, "pnl": pnl, "hold_pnl": pos["hold_pnl"], "win": pnl > 0,
            "bankroll_before": before, "bankroll_after": self.bankroll, "signal": pos["signal"],
        })
        self.log(ws, pos["asset"], f"EXIT_{reason.upper()}", f"pnl {pnl}c", minute)

    def book(self, pnl):
        self.bankroll += pnl
        self.day_pnl += pnl
        if pnl < 0:
            self.streak += 1
            if self.streak >= self.cfg["max_consecutive_losses"]:
                self.pause_until = self.cur_idx + self.cfg["pause_windows_after_streak"]
                self.streak = 0
        else:
            self.streak = 0

    def record_baseline(self, ws, cell, t_e):
        """Benchmark: buy each side at the entry ask on every eligible quote."""
        for row in cell.values():
            q = self.quote(row["ticker"], t_e)
            if q is None or q[1] - q[0] > self.cfg["spread_max_c"]:
                continue
            for side in ("yes", "no"):
                p = ask_c(q, side)
                if not self.price_ok(p):
                    continue
                f = fee_c(self.mult, 1, p)
                win = side == row["result"]
                self.baseline.append((ws, (100 - p - f) if win else -(p + f)))

    def process(self, idx, ws, cell):
        run = WindowRun(self, idx, ws, cell)
        run.start()
        for m in range(self.cfg["entry_minute"] + 1, 15):
            run.minute(m)
        run.finish({a: r["result"] for a, r in cell.items()})

    def run(self, by_ws):
        for idx, ws in enumerate(sorted(by_ws)):
            self.roll_day(ws)
            cell = by_ws[ws]
            self.record_baseline(ws, cell, ws + timedelta(minutes=self.cfg["entry_minute"]))
            self.process(idx, ws, cell)
        return self


class WindowRun:
    """One window, driven minute by minute. Shared by the batch replay and the live watcher."""

    def __init__(self, eng, idx, ws, cell):
        self.eng, self.cfg, self.idx, self.ws, self.cell = eng, eng.cfg, idx, ws, cell
        self.entry_m = self.cfg["entry_minute"]
        self.half_m = self.cfg["halfway_minute"]
        self.pos = None
        self.pending = None
        self.traded = False
        self.blocked = False
        self.started = False

    def start(self):
        eng, cfg, ws, cell = self.eng, self.cfg, self.ws, self.cell
        eng.cur_idx = self.idx
        self.started = True
        t_e = ws + timedelta(minutes=self.entry_m)
        assets = sorted(cell)
        if self.idx <= eng.pause_until:
            self.blocked = True
            for a in assets:
                eng.log(ws, a, "SKIP", f"streak pause through window index {eng.pause_until}", self.entry_m)
            return
        if eng.daily_stop_hit():
            self.blocked = True
            for a in assets:
                eng.log(ws, a, "SKIP", "daily loss stop", self.entry_m)
            return
        cand, why = eng.select(ws, cell, t_e)
        if cand is None:
            for a in assets:
                eng.log(ws, a, "SKIP", why, self.entry_m)
        elif cfg["entry_mode"] == "taker":
            pos, why2 = eng.open_taker(cand, ws, self.entry_m)
            if pos is None:
                eng.log(ws, cand["asset"], "SKIP", why2, self.entry_m)
            else:
                self.pos, self.traded = pos, True
                eng.log(ws, cand["asset"], "ENTER_TAKER",
                        f"{pos['side']} x{pos['qty']} at {pos['entry_price']}c", self.entry_m)
        else:
            limit = bid_c(cand["quote"], cand["side"])
            qty = size_qty(eng.budget_c(), limit, eng.maker_mult)
            if qty < 1:
                eng.log(ws, cand["asset"], "SKIP", "budget too small for one maker contract", self.entry_m)
            else:
                self.pending = {"cand": cand, "limit": limit, "qty": qty}
                eng.log(ws, cand["asset"], "REST_MAKER", f"{cand['side']} bid {limit}c x{qty}", self.entry_m)

    def minute(self, m):
        """Handle minute m (entry_minute < m <= 14). Quotes for minute m must already be in eng.quotes."""
        if self.blocked or not self.started:
            return
        eng, cfg, ws, cell = self.eng, self.cfg, self.ws, self.cell
        t_m = ws + timedelta(minutes=m)

        if self.pending is not None and m <= self.half_m:
            c = self.pending["cand"]
            q = eng.quote(c["row"]["ticker"], t_m)
            if q is not None and ask_c(q, c["side"]) <= self.pending["limit"]:
                fee = fee_c(eng.maker_mult, self.pending["qty"], self.pending["limit"])
                self.pos = eng.new_position(ws, c, "maker", m, self.pending["qty"], self.pending["limit"], fee)
                eng.log(ws, c["asset"], "FILL_MAKER", f"{c['side']} at {self.pending['limit']}c", m)
                self.pending, self.traded = None, True
                return

        if self.pos is not None and m > self.pos["fill_minute"] and m in eng.checks:
            q = eng.quote(self.pos["ticker"], t_m)
            if q is None:
                eng.log(ws, self.pos["asset"], "CHECK_SKIPPED", "no quote at this minute", m)
            else:
                bid = bid_c(q, self.pos["side"])
                if (cfg["stop_enabled"] and m in eng.stop_set
                        and self.pos["qty"] * bid <= eng.stop_frac * self.pos["cost"]):
                    eng.close(ws, self.pos, m, bid, "stop")
                    self.pos = None
                elif m == self.half_m:
                    why3 = eng.halfway_reason(ws, cell, self.pos, t_m)
                    if why3:
                        eng.close(ws, self.pos, m, bid, why3)
                        self.pos = None

        if m == self.half_m:
            if self.pending is not None:
                eng.log(ws, self.pending["cand"]["asset"], "UNFILLED", "maker bid not reached by halfway", m)
                self.pending = None
            if self.pos is None and not self.traded and cfg["late_entry_enabled"]:
                cand2, why4 = eng.select(ws, cell, t_m)
                if cand2 is None:
                    eng.log(ws, "", "NO_LATE_ENTRY", why4, m)
                else:
                    pos2, why5 = eng.open_taker(cand2, ws, m)
                    if pos2 is None:
                        eng.log(ws, cand2["asset"], "NO_LATE_ENTRY", why5, m)
                    else:
                        self.pos, self.traded = pos2, True
                        eng.log(ws, cand2["asset"], "LATE_ENTRY",
                                f"{pos2['side']} x{pos2['qty']} at {pos2['entry_price']}c", m)

    def finish(self, results):
        """Settle an open position with its asset's result. results maps asset to yes or no."""
        if self.pos is not None:
            pos = self.pos
            result = results[pos["asset"]]
            pos["hold_pnl"] = pos["qty"] * 100 - pos["cost"] if pos["side"] == result else -pos["cost"]
            self.eng.close(self.ws, pos, 15, None, "settled")
            self.pos = None


# ---------- statistics and reporting ----------

def stats(trades):
    """Expectancy per contract in cents, with a 95% interval."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "mean": None, "lo": None, "hi": None}
    per = [t["pnl"] / t["qty"] for t in trades]
    mean = statistics.fmean(per)
    if n < 2:
        return {"n": n, "mean": mean, "lo": None, "hi": None}
    se = statistics.stdev(per) / math.sqrt(n)
    return {"n": n, "mean": mean, "lo": mean - 1.96 * se, "hi": mean + 1.96 * se}


def describe(label, trades, base):
    st = stats(trades)
    out = [f"--- {label} ---"]
    if not trades:
        out.append("no trades")
    else:
        n = len(trades)
        wins = sum(1 for t in trades if t["pnl"] > 0)
        paid = statistics.fmean(t["entry_price"] for t in trades)
        out.append(f"trades {n} | win rate {wins / n:.1%} | avg entry {paid:.1f}c")
        out.append(f"calibration gap (win rate minus avg entry): {100 * wins / n - paid:+.1f} pts")
        total = sum(t["pnl"] for t in trades)
        fees = sum(t["entry_fee"] + t["exit_fee"] for t in trades)
        peak, mdd, streak, worst = trades[0]["bankroll_before"], 0.0, 0, 0
        for t in trades:
            peak = max(peak, t["bankroll_after"])
            mdd = max(mdd, (peak - t["bankroll_after"]) / peak)
            streak = streak + 1 if t["pnl"] < 0 else 0
            worst = max(worst, streak)
        out.append(f"P&L ${total / 100:+.2f} | fees ${fees / 100:.2f} | "
                   f"max drawdown {mdd:.1%} | longest losing streak {worst}")
        if st["lo"] is not None:
            out.append(f"expectancy per contract {st['mean']:+.2f}c "
                       f"(95% CI {st['lo']:+.2f}c to {st['hi']:+.2f}c)")
        for r in sorted({t["exit_reason"] for t in trades}):
            sub = [t for t in trades if t["exit_reason"] == r]
            out.append(f"  exit {r:<10} n={len(sub):<5} avg P&L per trade {statistics.fmean(t['pnl'] for t in sub):+.1f}c"
                       f"  (held to settlement: {statistics.fmean(t['hold_pnl'] for t in sub):+.1f}c)")
        early = sum(t["pnl"] - t["hold_pnl"] for t in trades if t["exit_reason"] != "settled")
        out.append(f"early exits changed total P&L by ${early / 100:+.2f} versus holding to settlement")
    if base:
        out.append(f"baseline, buy both sides at asks ({len(base)} quotes): "
                   f"{statistics.fmean(base):+.2f}c per contract")
    return "\n".join(out), st


def verdict(st_in, st_oos, min_oos):
    if st_oos["n"] < min_oos or st_oos["lo"] is None:
        return f"INSUFFICIENT SAMPLE: need {min_oos} out-of-sample trades, have {st_oos['n']}. No conclusion."
    if st_in["lo"] is not None and st_in["lo"] > 0 and st_oos["lo"] > 0:
        return ("CANDIDATE: expectancy after fees is above zero in both periods (95% CI). "
                "Next: forward paper test with pessimistic fills. Not a trading signal yet.")
    if st_oos["lo"] > 0:
        return "MIXED: out-of-sample is positive, in-sample is not. Treat as noise until a forward test confirms it."
    return "NO EDGE DETECTED: the rules do not beat the quoted prices after fees. Do not risk real money on them."


def quote_calibration(in_ws, quotes, cfg):
    """Do the market's quoted prices match outcomes? Run this on real data before anything else."""
    buckets = defaultdict(lambda: [0, 0, 0.0])
    for ws, cell in in_ws.items():
        t = ws + timedelta(minutes=cfg["entry_minute"])
        for row in cell.values():
            q = quotes.get((row["ticker"], minute_key(t)))
            if q is None:
                continue
            mid = (q[0] + q[1]) / 2
            b = min(int(mid) // 5 * 5, 95)
            buckets[b][0] += 1
            buckets[b][1] += row["result"] == "yes"
            buckets[b][2] += mid
    lines = ["Quote calibration at entry, in-sample (implied vs realized YES rate):"]
    for b in sorted(buckets):
        n, y, s = buckets[b]
        lines.append(f"  {b:>3}-{b + 4:<3}c  n={n:<6} implied {s / n:5.1f}%  realized {100 * y / n:5.1f}%")
    return "\n".join(lines)


def write_csv(path, rows, fields):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items()})


def ledger_append(path, cfg_hash, win_hash, st_oos, n_oos):
    """Count earlier reveals of the same config on the same data, then log this one."""
    exists = os.path.exists(path)
    prior = 0
    if exists:
        with open(path, newline="") as f:
            prior = sum(1 for r in csv.DictReader(f)
                        if r["config_sha256"] == cfg_hash and r["windows_sha256"] == win_hash)
    fmt = lambda x: "" if x is None else round(x, 3)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(LEDGER_FIELDS)
        w.writerow([datetime.now(timezone.utc).isoformat(), cfg_hash, win_hash, n_oos,
                    fmt(st_oos["mean"]), fmt(st_oos["lo"]), fmt(st_oos["hi"])])
    return prior


def split_point(by_ws, frac):
    starts = sorted(by_ws)
    return starts[int(len(starts) * (1 - frac))]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--windows", required=True)
    ap.add_argument("--quotes", required=True)
    ap.add_argument("--candles", required=True)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--out_dir", default="out")
    ap.add_argument("--report-oos", action="store_true",
                    help="reveal out-of-sample results. Every reveal is logged.")
    ap.add_argument("--ledger", default="oos_ledger.csv")
    args = ap.parse_args()

    cfg = load_config(args.config)
    cfg_hash = obj_sha256(cfg)
    rows = load_windows(args.windows)
    win_hash = file_sha256(args.windows)
    quotes = load_quotes(args.quotes, {r["ticker"] for r in rows})
    candles = Candles(args.candles)
    by_ws = group_windows(rows)
    cutoff = split_point(by_ws, cfg["oos_fraction"])
    in_ws = {k: v for k, v in by_ws.items() if k < cutoff}

    eng = Engine(cfg, candles, quotes).run(by_ws)
    in_t = [t for t in eng.trades if t["ws"] < cutoff]
    in_b = [e for w, e in eng.baseline if w < cutoff]

    os.makedirs(args.out_dir, exist_ok=True)
    trades_path = os.path.join(args.out_dir, "trades.csv")
    decisions_path = os.path.join(args.out_dir, "decisions.csv")
    write_csv(trades_path, eng.trades, TRADE_FIELDS)
    write_csv(decisions_path, eng.decisions, DECISION_FIELDS)

    starts = sorted(by_ws)
    print(f"engine {ENGINE_VERSION} | config {cfg_hash[:12]} | windows {len(starts)} | "
          f"{starts[0].isoformat()} to {starts[-1].isoformat()}")
    print(f"in-sample ends {cutoff.isoformat()} | out-of-sample is the rest\n")
    print(quote_calibration(in_ws, quotes, cfg) + "\n")

    txt_in, st_in = describe("IN-SAMPLE", in_t, in_b)
    print(txt_in + "\n")

    manifest = {"engine_version": ENGINE_VERSION, "config_sha256": cfg_hash,
                "windows_sha256": win_hash, "quotes_sha256": file_sha256(args.quotes),
                "candles_sha256": file_sha256(args.candles),
                "trades_sha256": file_sha256(trades_path),
                "decisions_sha256": file_sha256(decisions_path),
                "cutoff": cutoff.isoformat(), "final_bankroll_c": eng.bankroll,
                "oos_revealed": bool(args.report_oos)}

    if not args.report_oos:
        print("OUT-OF-SAMPLE HIDDEN. Freeze config.json, then run once with --report-oos.")
    else:
        oos_t = [t for t in eng.trades if t["ws"] >= cutoff]
        oos_b = [e for w, e in eng.baseline if w >= cutoff]
        txt_oos, st_oos = describe("OUT-OF-SAMPLE", oos_t, oos_b)
        prior = ledger_append(args.ledger, cfg_hash, win_hash, st_oos, st_oos["n"])
        if prior:
            print(f"WARNING: this config on this data was already revealed {prior} time(s). "
                  "Repeat looks inflate false positives.\n")
        print(txt_oos + "\n")
        print("VERDICT:", verdict(st_in, st_oos, cfg["min_oos_trades"]))
        manifest["oos_trades"] = st_oos["n"]

    with open(os.path.join(args.out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    print(f"\nwrote {trades_path}, {decisions_path}, manifest.json")


if __name__ == "__main__":
    main()
