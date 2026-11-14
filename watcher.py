#!/usr/bin/env python3
"""
Autonomous PAPER watcher for the BTC/ETH 15-minute rules.

It polls quotes and one-minute candles and runs the same WindowRun code the backtest
uses, minute by minute. Fills are simulated. No real orders are placed. This version
has no live execution path, and any mode other than paper stops at startup.

Safety
- Kill file: if the kill file exists, any open paper position is flattened and the watcher halts.
- Halts need a human to clear them. Triggers: kill file, no settlement result within the
  timeout, an open position found at startup, or a halted flag in the state file.
- Stale quotes: a minute is traded only on quotes fetched within SNAPSHOT_MAX_AGE_S of it.
  Missing quotes are logged, never guessed.
- A new window does not start until the previous window has settled.
- Bankroll, streak, pause, and daily-loss counters are saved every tick and restored on restart.

Decisions are deterministic functions of the data and config.json. Nothing here learns or
adapts. Changing a rule means editing config.json and re-running validation.
"""
import argparse
import csv
import json
import os
import time
import urllib.error
import urllib.request
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone

import backtest as b
import live_gate

WINDOW = timedelta(minutes=15)
SNAPSHOT_MAX_AGE_S = 10
SETTLE_TIMEOUT_MIN = 10
POLL_SECONDS = 5


def floor_window(t):
    return t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)


def idx_of(ws):
    return int(ws.timestamp()) // 900


class LiveCandles(b.Candles):
    KEEP = 500

    def __init__(self):
        self.data, self.times = {}, {}

    def add(self, asset, t, close, vol):
        d = self.data.setdefault(asset, [])
        ts = self.times.setdefault(asset, [])
        if ts and t <= ts[-1]:
            return
        d.append((t, close, vol))
        ts.append(t)
        if len(ts) > self.KEEP:
            del d[:-self.KEEP]
            del ts[:-self.KEEP]


# ---------- data feeds ----------

class Feed:
    """windows(ws) -> {asset: {ticker, floor}}; quotes(tickers, now) -> {ticker: (bid_c, ask_c, fetched_at)};
    new_bars(asset, since, now) -> [(time, close, volume)] closed by now;
    results(pairs, now) -> {asset: 'yes'|'no'} for settled markets, pairs = {asset: ticker}."""


class ReplayFeed(Feed):
    """Historical data replayed on a fake clock. Used for tests."""

    def __init__(self, by_ws, quotes, candles):
        self.by_ws, self.quotes_map, self.candles = by_ws, quotes, candles
        self.row_by_ticker = {r["ticker"]: r for cell in by_ws.values() for r in cell.values()}

    def windows(self, ws):
        return {a: {"ticker": r["ticker"], "floor": r["floor"]} for a, r in self.by_ws.get(ws, {}).items()}

    def quotes(self, tickers, now):
        out = {}
        for tk in tickers:
            q = self.quotes_map.get((tk, b.minute_key(now)))
            if q is not None:
                out[tk] = (q[0], q[1], now)
        return out

    def new_bars(self, asset, since, now):
        ts = self.candles.times.get(asset, [])
        data = self.candles.data.get(asset, [])
        start = bisect_right(ts, since) if since is not None else 0
        end = bisect_right(ts, now - b.ONE_MIN)
        return data[start:end]

    def results(self, pairs, now):
        out = {}
        for a, tk in pairs.items():
            r = self.row_by_ticker.get(tk)
            if r is not None and now >= r["we"]:
                out[a] = r["result"]
        return out


class KalshiFeed(Feed):
    """
    UNVERIFIED. Endpoints and field names were taken from earlier transcripts and must be checked
    against one real response before this is trusted. Public market data only: no account access,
    no orders.
    """
    BASE = "https://api.elections.kalshi.com/trade-api/v2"
    SERIES = {"BTC": "KXBTC15M", "ETH": "KXETH15M"}
    COINBASE = {"BTC": "BTC-USD", "ETH": "ETH-USD"}

    def _get(self, url):
        req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "paper-watcher/2.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    @staticmethod
    def _cents(m, key):
        if key + "_dollars" in m:
            return int(round(float(m[key + "_dollars"]) * 100))
        return int(m[key])

    def _markets(self, asset):
        return self._get(f"{self.BASE}/markets?series_ticker={self.SERIES[asset]}&status=open").get("markets", [])

    def windows(self, ws):
        out = {}
        for a in self.SERIES:
            for m in self._markets(a):
                if b.parse_ts(m["close_time"]) == ws + WINDOW:
                    out[a] = {"ticker": m["ticker"], "floor": float(m["floor_strike"])}
        return out

    def quotes(self, tickers, now):
        out, wanted = {}, set(tickers)
        for a in self.SERIES:
            for m in self._markets(a):
                if m["ticker"] in wanted:
                    out[m["ticker"]] = (self._cents(m, "yes_bid"), self._cents(m, "yes_ask"),
                                        datetime.now(timezone.utc))
        return out

    def new_bars(self, asset, since, now):
        rows = self._get(f"https://api.exchange.coinbase.com/products/{self.COINBASE[asset]}/candles?granularity=60")
        out = []
        for t_s, _low, _high, _open, close, vol in sorted(rows):
            t = datetime.fromtimestamp(t_s, timezone.utc)
            if t + b.ONE_MIN > now:
                continue  # bar still forming
            if since is not None and t <= since:
                continue
            out.append((t, float(close), float(vol)))
        return out

    def results(self, pairs, now):
        out = {}
        for a, tk in pairs.items():
            m = self._get(f"{self.BASE}/markets/{tk}").get("market", {})
            if m.get("status") in ("settled", "finalized") and m.get("result") in ("yes", "no"):
                out[a] = m["result"]
        return out


# ---------- watcher ----------

class Watcher:
    def __init__(self, cfg, feed, state_path, log_dir, kill_path, mode="paper", gate_path="live_gate.json"):
        if mode == "live":
            failures = live_gate.evaluate_gate(cfg, b.obj_sha256(cfg), gate_path)
            raise SystemExit("LIVE MODE REFUSED. Failed checks:\n  - " + "\n  - ".join(failures))
        if mode != "paper":
            raise SystemExit(f"unknown mode {mode!r}")
        self.cfg, self.feed = cfg, feed
        self.state_path, self.kill_path = state_path, kill_path
        os.makedirs(log_dir, exist_ok=True)
        self.trades_path = os.path.join(log_dir, "trades.csv")
        self.decisions_path = os.path.join(log_dir, "decisions.csv")
        self.candles = LiveCandles()
        self.eng = b.Engine(cfg, self.candles, {})
        self.cur_ws, self.cur_cell, self.run = None, None, None
        self.last_minute = -1
        self.last_bar = {}
        self.halted, self.halt_reason = False, ""
        self.n_trades_written = 0
        self.n_decisions_written = 0
        self._load()

    # state
    def _load(self):
        if not os.path.exists(self.state_path):
            return
        with open(self.state_path) as f:
            st = json.load(f)
        e = self.eng
        e.bankroll = st["bankroll"]
        e.day = date.fromisoformat(st["day"]) if st["day"] else None
        e.day_start, e.day_pnl = st["day_start"], st["day_pnl"]
        e.streak, e.pause_until = st["streak"], st["pause_until"]
        self.last_bar = {a: b.parse_ts(t) for a, t in st["last_bar"].items()}
        self.halted, self.halt_reason = st["halted"], st["halt_reason"]
        if st["open_position"]:
            self.halted = True
            self.halt_reason = "open position found at startup; inspect the logs and clear the state file by hand"
        if self.halted:
            raise SystemExit(f"halted: {self.halt_reason}")

    def _save(self):
        e = self.eng
        st = {"bankroll": e.bankroll, "day": e.day.isoformat() if e.day else None,
              "day_start": e.day_start, "day_pnl": e.day_pnl, "streak": e.streak,
              "pause_until": e.pause_until,
              "last_bar": {a: t.isoformat() for a, t in self.last_bar.items()},
              "halted": self.halted, "halt_reason": self.halt_reason,
              "open_position": bool(self.run and self.run.pos)}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, indent=2)
        os.replace(tmp, self.state_path)

    def _append(self, path, rows, fields):
        new = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            if new:
                w.writeheader()
            for r in rows:
                w.writerow({k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items()})

    def _flush(self):
        t_new = self.eng.trades[self.n_trades_written:]
        if t_new:
            self._append(self.trades_path, t_new, b.TRADE_FIELDS)
            self.n_trades_written = len(self.eng.trades)
        d_new = self.eng.decisions[self.n_decisions_written:]
        if d_new:
            self._append(self.decisions_path, d_new, b.DECISION_FIELDS)
            self.n_decisions_written = len(self.eng.decisions)

    # actions
    def halt(self, reason, now):
        self.halted, self.halt_reason = True, reason
        self.eng.log(now, "", "HALT", reason)

    def flatten(self, now, reason):
        if self.run is None or self.run.pos is None:
            return
        pos = self.run.pos
        minute = int((now - self.cur_ws).total_seconds() // 60)
        q = self.feed.quotes([pos["ticker"]], now).get(pos["ticker"])
        if q is None:
            self.eng.log(self.cur_ws, pos["asset"], "FLATTEN_FAILED", "no quote available", minute)
            return
        self.eng.close(self.cur_ws, pos, minute, b.bid_c((q[0], q[1]), pos["side"]), reason)
        self.run.pos = None

    def try_settle(self, now):
        run = self.run
        if run is None or run.pos is None:
            self.run = None
            return True
        asset = run.pos["asset"]
        res = self.feed.results({asset: run.pos["ticker"]}, now)
        if asset in res:
            run.finish({asset: res[asset]})
            self.run = None
            return True
        if now - (self.cur_ws + WINDOW) > timedelta(minutes=SETTLE_TIMEOUT_MIN):
            self.halt(f"no settlement result for {run.pos['ticker']} within {SETTLE_TIMEOUT_MIN} min", now)
        return False

    def _build_cell(self, ws):
        info = self.feed.windows(ws)
        return {a: {"ticker": d["ticker"], "asset": a, "floor": d["floor"], "result": None,
                    "ws": ws, "we": ws + WINDOW} for a, d in info.items()}

    def _pull_bars(self, asset, now):
        for t, c, v in self.feed.new_bars(asset, self.last_bar.get(asset), now):
            self.candles.add(asset, t, c, v)
            self.last_bar[asset] = t

    def refresh(self, ws, m, now):
        """Load fresh quotes and new bars for minute m. True when every market has a fresh quote."""
        t_m = ws + timedelta(minutes=m)
        tickers = {r["ticker"] for r in self.cur_cell.values()}
        if tickers:
            for tk, (bid, ask, fetched) in self.feed.quotes(sorted(tickers), now).items():
                age = (fetched - t_m).total_seconds()
                if 0 <= age <= SNAPSHOT_MAX_AGE_S:
                    self.eng.quotes[(tk, b.minute_key(t_m))] = (bid, ask)
        for a in self.cur_cell:
            self._pull_bars(a, now)
        return all((tk, b.minute_key(t_m)) in self.eng.quotes for tk in tickers)

    def tick(self, now):
        if self.halted:
            return
        if os.path.exists(self.kill_path):
            self.flatten(now, "kill")
            self.halt("kill file present", now)
            self._save()
            self._flush()
            return

        ws = floor_window(now)
        m = int((now - ws).total_seconds() // 60)

        if self.cur_ws is not None and ws != self.cur_ws:
            if not self.try_settle(now):
                self._save()
                self._flush()
                return
        if ws != self.cur_ws:
            self.cur_ws = ws
            self.cur_cell = self._build_cell(ws)
            self.run = None
            self.last_minute = -1
            self.eng.roll_day(ws)
            if not self.cur_cell:
                self.eng.log(ws, "", "NO_MARKETS", "no open market matched this window")

        entry_m = self.cfg["entry_minute"]
        if entry_m <= m <= 14 and m > self.last_minute and self.cur_cell:
            t_m = ws + timedelta(minutes=m)
            ready = self.refresh(ws, m, now)
            if ready or (now - t_m).total_seconds() >= SNAPSHOT_MAX_AGE_S:
                if m == entry_m:
                    self.run = b.WindowRun(self.eng, idx_of(ws), ws, self.cur_cell)
                    self.run.start()
                elif self.run is not None:
                    if m > self.last_minute + 1:
                        self.eng.log(ws, "", "MISSED_MINUTES", f"minutes {self.last_minute + 1}..{m - 1} not processed", m)
                    self.run.minute(m)
                self.last_minute = m

        self._save()
        self._flush()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--mode", default="paper", choices=["paper", "live"],
                    help="paper (default). live is refused unless live_gate.json passes every check")
    ap.add_argument("--gate", default="live_gate.json")
    ap.add_argument("--state", default="watcher_state.json")
    ap.add_argument("--log_dir", default="watcher_logs")
    ap.add_argument("--kill", default="KILL")
    args = ap.parse_args()

    cfg = b.load_config(args.config)
    w = Watcher(cfg, KalshiFeed(), args.state, args.log_dir, args.kill, mode=args.mode, gate_path=args.gate)
    print(f"paper watcher running. Create a file named {args.kill} to flatten and halt.")
    try:
        while not w.halted:
            try:
                w.tick(datetime.now(timezone.utc))
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
                w.eng.log(datetime.now(timezone.utc), "", "FEED_ERROR", str(e)[:200])
                w._flush()
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("stopped by user; state saved")
    if w.halted:
        print("halted:", w.halt_reason)


if __name__ == "__main__":
    main()
