# BTC/ETH 15-minute backtest, v2

## Files
- test_engine.py: hand-checked tests. Run first.
- backtest.py: engine and report. Out-of-sample results stay hidden unless you pass --report-oos, and every reveal is logged.
- sweep.py and sweep_grid.json: grid search on in-sample data only.
- audit.py: recomputes every trade from the raw quotes and reports mismatches.
- make_synthetic.py: fair-market test data, with no edge by construction.
- config.json: the rules. Freeze them before testing.

Needs Python 3.8 or newer. No packages.

## The 50% rule as implemented
- Stop: exit at the bid when qty x bid is at or below 50% of the cost basis (entry price x qty, plus entry fee). The exit pays the taker fee and 1 cent of slippage.
- Checked at every minute listed in stop_check_minutes (default 2 to 14).
- The scheduled chat automation checks only at minutes 1 and 8, so it cannot enforce this rule. A stop needs a process that runs continuously and reads quotes at least once a minute, with its own kill switch.

## Order of work
1. python3 test_engine.py must print "all 8 tests passed".
2. Fair-market check: python3 make_synthetic.py, then python3 backtest.py with the synthetic files. Expect NO EDGE DETECTED or INSUFFICIENT SAMPLE. Then run audit.py on the trades file.
3. Real data: build the three files described below and run backtest.py. Read the quote calibration first. If realized YES rates do not track implied rates, stop. Your quotes or their timestamps are wrong.
4. Sweep in-sample: python3 sweep.py --windows ... --quotes ... --candles ...
5. Pick ONE config with this rule, written down before the sweep: highest ci_lo_c among configs with at least 100 in-sample trades. Ties go to the config listed first.
6. Reveal once: python3 backtest.py ... --report-oos. Each reveal is logged to oos_ledger.csv. A second reveal prints a warning.
7. Forward paper test on live quotes with the same rules and pessimistic fills, at least 200 trades, before any real money.

## Data formats (times are UTC ISO 8601 with an offset)
- windows.csv: ticker, asset, window_start, window_end, floor_strike, result. Windows are 15 minutes and start on the quarter hour. result is yes or no.
- quotes.csv: ticker, time, yes_bid_c, yes_ask_c. One row per market per minute, integer cents, time on the minute.
- candles.csv: asset, time, close, volume. One-minute bars, time is the bar open. Source is Coinbase, a proxy for the CF Benchmarks index.

Target (floor_strike) must be the pre-open 60-second average. Using the entry bar's close will silently break the signal.

## Reading the output
- Quote calibration: the market check. Run it on real data first.
- Calibration gap: win rate minus average entry price. Negative means the rules picked worse prices than the market gave.
- Expectancy per contract with a 95% CI: the lower bound must be above zero to count as evidence.
- Exit breakdown: for each exit reason, the realized P&L and what holding to settlement would have given. This is how you judge the 50% rule directly.
- Baseline: buying both sides at the entry ask on every quote. The rules must beat this.
- CANDIDATE is the best verdict a backtest can give. It is not a trading signal.

## Known limits
- Taker fills assume the ask plus 1 cent.
- Maker fills assume the limit fills whenever the ask reaches it. This is optimistic, because orders fill more often when price moves against you.
- Stops and halfway checks see only minute quotes. Moves within a minute are invisible.
- Not modeled: queue position, partial fills, order depth, cancellation delay, fee changes, the CF Benchmarks index.
- The maker fee default (0.0175 multiplier) is the pessimistic assumption. Confirm it against Kalshi's current fee schedule.

## What a stop can and cannot do
On a fair market, a stop trades a little expected value for smaller losses on the trades it exits. It can reduce drawdowns, but it cannot make an unprofitable strategy profitable, and exit costs push it slightly the wrong way. The fair-market test shows this: the stop moved results by a few dollars in either direction, which is noise.

## Autonomous paper watcher (watcher.py)

What it does: polls quotes and one-minute candles, then makes every entry, stop, halfway, and
exit decision with the same WindowRun code the backtest uses. Fills are simulated. It records
every decision and trade to CSV, and saves its counters after every tick, so a restart resumes
the streak, pause, and daily-loss state.

What it does not do: place real orders. This version has no live execution path. Any mode
other than paper stops at startup. It also does not learn or adapt. Its decisions are plain
code applied to data and config.json.

Safety behavior
- Kill file (default name KILL): flattens any open paper position at the bid, then halts.
- Halts need a human. Triggers: kill file, no settlement result within 10 minutes of close,
  an open position found at startup, or a halted flag in watcher_state.json.
- A minute is traded only on quotes fetched within 10 seconds of it. Missing quotes are logged.
- A new window does not start until the previous window has settled.

Verify before running live data
- KalshiFeed was written from earlier transcripts. Its endpoints and field names (close_time,
  floor_strike, yes_bid, yes_ask or *_dollars, status, result) are UNVERIFIED. Check one real
  response from each endpoint, and fix the parser if a field name differs, before trusting it.
- Coinbase candles: the newest bar may still be forming. The feed drops it.

Tests
- python3 test_watcher.py: replays 1,500 historical windows through the watcher on a fake clock.
  It must match the batch backtest exactly: trades, decision events, and final bankroll.
- python3 test_watcher_safety.py: kill switch, restart continuity, refusal to resume with an open position.

Conditions before any live order code is written (not built here)
1. backtest.py returns CANDIDATE on real data, with the out-of-sample revealed once.
2. At least 200 forward paper trades with results inside the backtest's confidence range.
3. A human has read the decision log and the audit output, and approved the config in writing.
4. Live order code is a separate module, reviewed, with its own limits: maximum stake, daily loss
   stop, and kill switch, all enforced in code.

Why rules and not a model deciding each trade: a model choosing trades in real time cannot be
replayed or audited against the rules, its output can change between runs, and text inside market
data can influence it. Deterministic code can be tested, replayed, and reviewed. An AI reviewer
is useful for reading the daily logs and proposing config changes, with a human approving each one.

## Live mode switch (live_gate.py)

- Default is paper. `python3 watcher.py --mode live` runs the gate check and refuses to start.
- The gate needs live_gate.json: a validated CANDIDATE on real data, matching config hash, one out-of-sample
  reveal, at least 200 forward paper trades inside the confidence range, a named human approval,
  and stake and daily-loss limits.
- Even a fully passing gate refuses in this version, because live order code is not written.
  That code is a separate module to be reviewed before it exists.
- The gate is a checklist with an audit trail. It does not stop someone who edits the code.
