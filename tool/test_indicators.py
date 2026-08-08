#!/usr/bin/env python3
"""Tests for the indicator math.  python3 tool/test_indicators.py

Every assertion here is checked against a hand-derivable value, an
independently written reference implementation, or a second data source —
never against whatever the code happens to return today.

Add --live to also cross-check the generated file against NSE's own published
52-week figures. That one needs a network and is skipped by default.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_indicators import (  # noqa: E402
    MIN_BARS_52W,
    RSI_PERIOD,
    UA,
    build,
    rsi_wilder,
    sma,
    window_52w,
)

FAILURES: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)


def near(name: str, got, want, tol=0.01) -> None:
    if got is None or abs(got - want) > tol:
        fail(f"{name}: got {got}, want {want}")


def eq(name: str, got, want) -> None:
    if got != want:
        fail(f"{name}: got {got!r}, want {want!r}")


def bars_from(closes: list[float], start=date(2025, 1, 1)) -> list[dict]:
    """Synthetic daily bars on consecutive calendar days."""
    from datetime import timedelta
    out = []
    d = start
    for c in closes:
        out.append({"date": d.isoformat(), "open": c, "high": c + 1, "low": c - 1,
                    "close": c, "volume": 1000})
        d += timedelta(days=1)
    return out


# ------------------------------------------------------------------ SMA

def test_sma_is_the_last_n_closes():
    closes = [float(i) for i in range(1, 101)]        # 1..100
    near("sma(10) of 1..100", sma(closes, 10), sum(range(91, 101)) / 10)   # 95.5
    near("sma(100)", sma(closes, 100), 50.5)
    eq("sma returns None when short", sma(closes, 101), None)


def test_sma_uses_newest_not_oldest():
    """The ordering trap, pinned. A reversed series must not give the same answer."""
    closes = [float(i) for i in range(1, 101)]
    forward = sma(closes, 10)
    backward = sma(list(reversed(closes)), 10)
    if forward == backward:
        fail("sma is order-insensitive — the newest-first bug would not be caught")
    near("sma on reversed picks the oldest", backward, sum(range(1, 11)) / 10)  # 5.5


# ------------------------------------------------------------------ RSI

def rsi_reference(closes: list[float], period: int = RSI_PERIOD):
    """Independent, deliberately naive Wilder implementation.

    Written differently from the one under test (explicit gain/loss lists,
    no running accumulator) so that a shared mistake is unlikely.
    """
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return round(100 - 100 / (1 + ag / al), 2)


def test_rsi_matches_an_independent_implementation():
    import math
    series = [100 + 12 * math.sin(i / 3.0) + i * 0.4 for i in range(260)]
    near("rsi vs reference", rsi_wilder(series), rsi_reference(series), tol=0.011)


def test_rsi_extremes():
    eq("all gains -> 100", rsi_wilder([float(i) for i in range(1, 60)]), 100.0)
    eq("all losses -> 0", rsi_wilder([float(i) for i in range(60, 1, -1)]), 0.0)
    eq("flat -> 50", rsi_wilder([50.0] * 60), 50.0)
    eq("too short -> None", rsi_wilder([1.0, 2.0, 3.0]), None)


def test_rsi_is_wilder_not_a_simple_mean():
    """Wilder smoothing carries the whole history; a plain mean of the last
    `period` changes forgets everything older. They must disagree.

    The series needs BOTH gains and losses in the trailing window — with
    losses absent, every method returns 100 and the comparison proves nothing.
    """
    series = [200.0 - i for i in range(100)]                 # long decline …
    series += [100.0 + (4 if i % 2 else -1) for i in range(20)]   # … then a mixed patch
    wilder = rsi_wilder(series)
    deltas = [series[i] - series[i - 1] for i in range(1, len(series))][-RSI_PERIOD:]
    g = sum(d for d in deltas if d > 0) / RSI_PERIOD
    l = sum(-d for d in deltas if d < 0) / RSI_PERIOD
    simple = 100.0 if l == 0 else round(100 - 100 / (1 + g / l), 2)
    if wilder == simple:
        fail("RSI matches a simple-mean RSI — Wilder smoothing may not be applied")


def test_rsi_is_order_sensitive():
    import math
    series = [100 + 10 * math.sin(i / 4.0) + i * 0.3 for i in range(200)]
    if rsi_wilder(series) == rsi_wilder(list(reversed(series))):
        fail("RSI is order-insensitive — the newest-first bug would not be caught")


# ------------------------------------------------------------------ 52 week

def test_52w_accepts_a_real_trading_year():
    """The bug this pins: a 365-day window holds ~247 Indian sessions, so a
    bar-count threshold near 250 rejected every single stock."""
    from datetime import timedelta
    bars, d, weekday_no = [], date(2025, 3, 26), 0
    while d <= date(2026, 8, 7):
        if d.weekday() < 5:
            # ~1 holiday a month. Counted off the weekday index, not off the
            # number of bars appended — keying it to the appended count makes
            # the condition latch permanently once it first trips.
            if weekday_no % 21 != 20:
                bars.append({"date": d.isoformat(), "open": 100,
                             "high": 100 + (weekday_no % 37),
                             "low": 100 - (weekday_no % 29), "close": 100, "volume": 1})
            weekday_no += 1
        d += timedelta(days=1)
    got = window_52w(bars, date(2026, 8, 7))
    if got is None:
        in_win = sum(1 for b in bars if b["date"] >= "2025-08-07")
        fail(f"52w rejected a full trading year ({in_win} sessions in window, {len(bars)} total)")


def test_52w_rejects_history_that_starts_inside_the_window():
    bars = bars_from([100.0] * 300, start=date(2026, 1, 1))   # begins well after the cutoff
    eq("short history -> None", window_52w(bars, date(2026, 8, 7)), None)


def test_52w_finds_the_true_extremes_and_dates():
    from datetime import timedelta
    bars, d = [], date(2025, 1, 1)
    for i in range(500):
        bars.append({"date": d.isoformat(), "open": 100, "high": 100.0, "low": 100.0,
                     "close": 100.0, "volume": 1})
        d += timedelta(days=1)
    bars[400]["high"] = 999.0        # inside the trailing year
    bars[410]["low"] = 1.0
    bars[10]["high"] = 5000.0        # older than the window — must be ignored
    got = window_52w(bars, date.fromisoformat(bars[-1]["date"]))
    if got is None:
        fail("52w returned None on a dense series")
    else:
        near("52w high", got["high"], 999.0)
        near("52w low", got["low"], 1.0)
        eq("52w high date", got["high_date"], bars[400]["date"])


# ------------------------------------------------------------------ build()

def test_build_nulls_rather_than_guesses():
    """A short series must yield null indicators, never a number computed
    from a window that isn't there."""
    meta = {"name": "Newly Listed", "industry": "X", "isin": "INE000A00000"}
    row = build("NEW", meta, bars_from([100.0 + i for i in range(120)]))
    eq("dma50 present", row["dma50"] is not None, True)
    eq("dma200 absent", row["dma200"], None)
    eq("dma200 flagged", "dma200" in row["insufficient"], True)
    eq("week52 absent", row["week52"], None)


def test_build_reads_the_newest_bar_as_close():
    meta = {"name": "T", "industry": "X", "isin": "I"}
    rows = bars_from([float(i) for i in range(1, 301)])
    row = build("T", meta, rows)
    near("close is the last bar", row["close"], 300.0)
    eq("as_of is the last date", row["as_of"], rows[-1]["date"])


# ------------------------------------------------------------------ artefact

def test_generated_file():
    p = Path(__file__).resolve().parent.parent / "web" / "data" / "indicators.json"
    if not p.exists():
        print("  (skipping generated-file checks — run build_indicators.py first)")
        return
    d = json.loads(p.read_text())
    from symbols import load_watchlist
    wl = load_watchlist()
    if d["count"] != len(wl["symbols"]):
        fail(f"generated file has {d['count']} stocks, watchlist names {len(wl['symbols'])}")
    if d["count"] > d.get("watchlist_max", 30):
        fail(f"{d['count']} stocks exceeds the watchlist max {d.get('watchlist_max')}")
    got = {s["symbol"] for s in d["stocks"]}
    missing = [s for s in wl["symbols"] if s not in got]
    if missing:
        fail(f"watchlist symbols absent from the generated file: {missing}")
    for s in d["stocks"]:
        if s["insufficient"]:
            fail(f"{s['symbol']}: unexpected missing indicators {s['insufficient']}")
            continue
        w = s["week52"]
        if not (w["low"] <= s["close"] <= w["high"]):
            fail(f"{s['symbol']}: close {s['close']} outside 52w {w['low']}–{w['high']}")
        if not (0 <= s["rsi14"] <= 100):
            fail(f"{s['symbol']}: RSI {s['rsi14']} out of range")
        if s["bars"] < 250:
            fail(f"{s['symbol']}: only {s['bars']} bars fetched")


def test_live_cross_check_against_nse():
    """NSE publishes its own 52-week high/low. Ours is computed from a
    different provider's candles, so agreement is real evidence."""
    if "--live" not in sys.argv:
        print("  (skipping NSE cross-check — pass --live to run)")
        return
    p = Path(__file__).resolve().parent.parent / "web" / "data" / "indicators.json"
    if not p.exists():
        return
    ours = {s["symbol"]: s for s in json.loads(p.read_text())["stocks"]}
    # NIFTY 500, because the watchlist may name companies outside the NIFTY 50.
    url = ("https://www.nseindia.com/api/NextApi/apiClient/marketWatchApi"
           "?functionName=getIndicesData&symbol=NIFTY%20500")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    rows = json.loads(urllib.request.urlopen(req, timeout=30).read())["data"]["data"]
    checked = mismatched = 0
    for r in rows:
        if r.get("series") is None:
            continue
        mine = ours.get(r["symbol"])
        if not mine or not mine["week52"]:
            continue
        checked += 1
        for ourk, nsek in (("high", "yearHigh"), ("low", "yearLow")):
            a, b = mine["week52"][ourk], r.get(nsek)
            if b and abs(a - b) / b > 0.005:          # 0.5% tolerance
                mismatched += 1
                fail(f"{r['symbol']} 52w {ourk}: ours {a} vs NSE {b}")
    print(f"  cross-checked {checked} symbols against NSE, {mismatched} mismatches")
    if checked < len(ours) * 0.8:
        fail(f"only cross-checked {checked} of {len(ours)} — is the watchlist outside NIFTY 500?")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        try:
            t()
            print(f"  {t.__name__}")
        except Exception as e:
            fail(f"{t.__name__}: raised {type(e).__name__}: {e}")
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"all {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
