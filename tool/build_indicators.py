#!/usr/bin/env python3
"""Compute the daily indicator layer: 50/100/200 DMA, RSI(14), 52-week range.

These derive from *daily* candles, so they change once per session. Computing
them intraday buys nothing — the page reads the live price from NSE and
compares it against these precomputed levels.

Covers whatever `tool/watchlist.json` names. That list is an arbitrary set of
NSE companies, not an index: names outside the NIFTY 50 are expected, and
NIFTY 50 members may be absent.

Sources, both free and unauthenticated:
  - Symbol -> ISIN: Upstox instrument master (see tool/symbols.py)
  - Daily candles:  Upstox historical-candle

Run after the close:  python3 tool/build_indicators.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from symbols import (  # noqa: E402
    UnresolvedSymbols,
    load_instrument_map,
    load_watchlist,
    resolve,
    titlecase,
)

IST = ZoneInfo("Asia/Kolkata")

# Upstox and NSE both reject non-browser agents. Upstox answers 403; NSE kills
# the HTTP/2 stream, which surfaces as a connection error rather than a status
# code and reads like an outage. Neither is rate limiting.
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

UPSTOX = "https://api.upstox.com/v2/historical-candle"

# 500 calendar days ≈ 338 trading bars. A 200DMA needs 200 and a 52-week range
# needs ~250; 400 days left only ~21 bars of slack, and the wider window is the
# same single request.
HISTORY_DAYS = 500
RSI_PERIOD = 14
# Sanity floor for the 52-week window. NOT a "one year of sessions" count —
# 365 calendar days holds ~247 Indian trading sessions after holidays, so
# requiring 250 rejects every stock. Coverage is tested by whether the series
# reaches back past the cutoff; this only catches a series full of holes.
MIN_BARS_52W = 200

# Serial, with a small gap. Fanning requests out in parallel trips Cloudflare
# rate limiting on Upstox and earns a measured 573-second ban; serially even
# fifty take under three seconds.
REQUEST_GAP_S = 0.06

TOOL_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = TOOL_DIR.parent / "web" / "data" / "indicators.json"


def get(url: str, tries: int = 3, timeout: int = 30) -> bytes:
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {tries} tries: {url}: {last}")


# ---------------------------------------------------------------- candles

def fetch_candles(isin: str) -> list[dict]:
    """Daily bars, returned **oldest-first**.

    Upstox serves them newest-first. Every indicator here is order-sensitive,
    and getting it wrong does not raise — it silently averages the oldest bars
    in the window instead of the newest. Measured on TCS that is a 29% error in
    the 50DMA and an RSI of 79 instead of 63, which crosses the conventional
    overbought line. So the reversal happens here, once, and no caller can skip
    it.
    """
    today = datetime.now(IST).date()
    frm = today - timedelta(days=HISTORY_DAYS)
    key = urllib.parse.quote(f"NSE_EQ|{isin}", safe="")
    url = f"{UPSTOX}/{key}/day/{today.isoformat()}/{frm.isoformat()}"
    payload = json.loads(get(url))
    if payload.get("status") != "success":
        raise RuntimeError(f"upstox: {payload}")
    bars = [
        {"date": r[0][:10], "open": float(r[1]), "high": float(r[2]),
         "low": float(r[3]), "close": float(r[4]), "volume": float(r[5] or 0)}
        for r in payload["data"]["candles"]
    ]
    bars.sort(key=lambda b: b["date"])
    for b in bars:
        if not (b["low"] <= b["close"] <= b["high"] and b["low"] <= b["open"] <= b["high"]):
            raise RuntimeError(f"malformed bar {b}")
    return bars


# ---------------------------------------------------------------- indicators

def sma(closes: list[float], n: int) -> float | None:
    return round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None


def rsi_wilder(closes: list[float], period: int = RSI_PERIOD) -> float | None:
    """Wilder's RSI — the standard. Not a plain mean of gains and losses.

    Seeds with a simple average over the first `period` changes, then smooths
    recursively. Because it is recursive the value depends slightly on how far
    back the series starts; it converges to four decimal places by ~150 bars,
    and we feed it far more than that.
    """
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 2)


def window_52w(bars: list[dict], asof: date) -> dict | None:
    """High/low over the trailing 365 calendar days.

    Filtered by date rather than by slicing a fixed number of bars, because
    that is the actual definition and it self-corrects for however many
    trading holidays fell in the year.

    Sufficiency is judged by whether the series *reaches back past* the cutoff,
    not by counting bars inside it. Counting is the tempting test and it is
    wrong: a full year holds ~247 Indian sessions, so any threshold near 250
    rejects every stock while looking like a reasonable guard.
    """
    cutoff = (asof - timedelta(days=365)).isoformat()
    if not bars or bars[0]["date"] > cutoff:
        return None                      # history starts inside the window
    win = [b for b in bars if b["date"] >= cutoff]
    if len(win) < MIN_BARS_52W:
        return None                      # window covered, but too gappy to trust
    hi = max(win, key=lambda b: b["high"])
    lo = min(win, key=lambda b: b["low"])
    return {"high": round(hi["high"], 2), "high_date": hi["date"],
            "low": round(lo["low"], 2), "low_date": lo["date"]}


def build(sym: str, meta: dict, bars: list[dict]) -> dict:
    closes = [b["close"] for b in bars]
    last = bars[-1]
    asof = date.fromisoformat(last["date"])
    row = {
        "symbol": sym,
        "name": meta.get("name") or titlecase(meta.get("registered_name", "")),
        "isin": meta["isin"],
        "as_of": last["date"],
        "close": round(last["close"], 2),
        "bars": len(bars),
        "dma50": sma(closes, 50),
        "dma100": sma(closes, 100),
        "dma200": sma(closes, 200),
        "rsi14": rsi_wilder(closes),
        "week52": window_52w(bars, asof),
    }
    # An indicator that lacks the history to be meaningful is null, never a
    # number computed from a short window. A freshly relisted entity (the
    # post-demerger Tata Motors CV had 183 sessions) would otherwise show a
    # confident, wrong 200-day average.
    row["insufficient"] = [k for k in ("dma50", "dma100", "dma200", "rsi14") if row[k] is None]
    if row["week52"] is None:
        row["insufficient"].append("week52")
    return row


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, help="only the first N (for a quick check)")
    args = ap.parse_args()

    started = time.time()
    wl = load_watchlist()
    print(f"watchlist: {len(wl['symbols'])} symbols (max {wl['max']})"
          f"{'  [PLACEHOLDER — awaiting the real list]' if wl['placeholder'] else ''}")
    try:
        targets = resolve(wl["symbols"], load_instrument_map())
    except UnresolvedSymbols as e:
        # Stopping is the point. Skipping would hand back a watchlist quietly
        # shorter than the one that was asked for.
        print(f"ERROR {e}", file=sys.stderr)
        return 2
    if args.limit:
        targets = targets[: args.limit]

    rows, failed = [], []
    for c in targets:
        try:
            bars = fetch_candles(c["isin"])
            rows.append(build(c["symbol"], c, bars))
        except Exception as e:
            failed.append(c["symbol"])
            print(f"  WARN {c['symbol']}: {e}", file=sys.stderr)
        time.sleep(REQUEST_GAP_S)

    # Every named company must appear. A watchlist is explicit, so a missing
    # name is a failure rather than an acceptable gap.
    if failed:
        print(f"{len(failed)} of {len(targets)} failed to fetch: {', '.join(failed)}",
              file=sys.stderr)
        return 1

    rows.sort(key=lambda r: r["symbol"])
    thin = [r["symbol"] for r in rows if r["insufficient"]]
    out = {
        "generated_at": datetime.now(IST).isoformat(timespec="seconds"),
        "as_of": max(r["as_of"] for r in rows),
        "watchlist_max": wl["max"],
        "placeholder_watchlist": wl["placeholder"],
        "count": len(rows),
        "failed": failed,
        "stocks": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, separators=(",", ":")) + "\n")

    kb = args.out.stat().st_size / 1024
    print(f"wrote {args.out} ({kb:.0f} KB) — {len(rows)} stocks, session {out['as_of']}, "
          f"{time.time()-started:.1f}s")
    if failed:
        print(f"  failed: {', '.join(failed)}")
    if thin:
        print(f"  partial history (some indicators null): {', '.join(thin)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
