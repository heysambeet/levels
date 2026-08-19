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
import math
import statistics
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

def fetch_candles(instrument_key: str) -> list[dict]:
    """Daily bars, returned **oldest-first**.

    `instrument_key` is the full Upstox key — `NSE_EQ|<isin>` for a company,
    `NSE_INDEX|Nifty 50` for the index. It is passed whole rather than built
    from an ISIN because the index has no ISIN and beta needs both series to
    come through exactly the same path.

    Upstox serves them newest-first. Every indicator here is order-sensitive,
    and getting it wrong does not raise — it silently averages the oldest bars
    in the window instead of the newest. Measured on TCS that is a 29% error in
    the 50DMA and an RSI of 79 instead of 63, which crosses the conventional
    overbought line. So the reversal happens here, once, and no caller can skip
    it.
    """
    today = datetime.now(IST).date()
    frm = today - timedelta(days=HISTORY_DAYS)
    key = urllib.parse.quote(instrument_key, safe="")
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


# ---------------------------------------------------------------- market fit
#
# Beta exists here to answer the question the product is actually for: "why is
# this down 1%?" Very often the answer is "it isn't, particularly — the whole
# market is down and this name is behaving normally." Without a beta the page
# cannot tell those two cases apart, and every market-wide dip sends the reader
# hunting for company news that does not exist.
#
# Measured on the current watchlist: the market explains a median 33% of a
# name's daily variance, so two thirds of a typical move really is its own —
# which is exactly why the third that is not needs separating out.
#
# The check that this decomposition is sound: average pairwise correlation
# across the watchlist is 0.273, and after subtracting beta x market it falls
# to -0.013. Removing one common factor leaves residuals that are essentially
# uncorrelated, which is what a correct market model should do.

BETA_WINDOW_DAYS = 365
# A beta from a short window is noise wearing a decimal point. Six months of
# sessions is the floor; below it the field is null, like every other indicator
# here that lacks the history to mean anything.
MIN_BETA_OBS = 120

NIFTY_KEY = "NSE_INDEX|Nifty 50"


def log_returns(bars: list[dict]) -> dict[str, float]:
    """date -> log return. Keyed by date so two series align on trading days
    rather than on position, which silently misaligns after any holiday one
    series observed and the other did not."""
    out = {}
    for i in range(1, len(bars)):
        prev, cur = bars[i - 1]["close"], bars[i]["close"]
        if prev > 0 and cur > 0:
            out[bars[i]["date"]] = math.log(cur / prev)
    return out


def fit_market(stock: dict[str, float], market: dict[str, float],
               asof: date) -> dict | None:
    """OLS of the stock's daily returns on the index's, over one year.

    Returns beta, the R^2 that says how much of the move the market explains at
    all, and the annualised volatility of what is left over.
    """
    cutoff = (asof - timedelta(days=BETA_WINDOW_DAYS)).isoformat()
    common = sorted(d for d in stock.keys() & market.keys() if d >= cutoff)
    if len(common) < MIN_BETA_OBS:
        return None
    x = [market[d] for d in common]
    y = [stock[d] for d in common]
    mx, my = statistics.fmean(x), statistics.fmean(y)
    sxx = sum((a - mx) ** 2 for a in x)
    if sxx <= 0:
        return None
    beta = sum((a - mx) * (b - my) for a, b in zip(x, y)) / sxx
    syy = sum((b - my) ** 2 for b in y)
    resid = [b - my - beta * (a - mx) for a, b in zip(x, y)]
    return {
        "beta": round(beta, 3),
        "r2": round(1 - sum(r * r for r in resid) / syy, 3) if syy > 0 else None,
        "own_vol_pct": round(statistics.pstdev(resid) * math.sqrt(252) * 100, 1),
        "obs": len(common),
    }


def market_structure(returns: dict[str, dict[str, float]], asof: date) -> dict | None:
    """Average pairwise correlation across the watchlist, and what it implies
    about how many genuinely independent names are in it.

    Grinold's effective breadth, N / (1 + rho(N-1)), is the standard way to say
    that thirty correlated holdings are not thirty separate bets. On the
    current list rho = 0.27 puts it near three.
    """
    cutoff = (asof - timedelta(days=BETA_WINDOW_DAYS)).isoformat()
    syms = sorted(returns)
    if len(syms) < 2:
        return None
    shared = set.intersection(*[set(returns[s]) for s in syms])
    common = sorted(d for d in shared if d >= cutoff)
    if len(common) < MIN_BETA_OBS:
        return None
    series = {s: [returns[s][d] for d in common] for s in syms}
    stats = {}
    for s in syms:
        m = statistics.fmean(series[s])
        sd = statistics.pstdev(series[s])
        stats[s] = (m, sd)
    pairs = []
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            a, b = syms[i], syms[j]
            (ma, sa), (mb, sb) = stats[a], stats[b]
            if sa <= 0 or sb <= 0:
                continue
            cov = sum((p - ma) * (q - mb) for p, q in zip(series[a], series[b])) / len(common)
            pairs.append(cov / (sa * sb))
    if not pairs:
        return None
    rho = statistics.fmean(pairs)
    n = len(syms)
    denom = 1 + rho * (n - 1)
    return {
        "avg_pair_corr": round(rho, 3),
        # Guarded: a rho at or below -1/(N-1) drives the denominator to zero or
        # negative and the formula stops meaning anything. It cannot exceed N
        # either — that would claim more independent bets than there are names.
        "effective_breadth": round(min(n, n / denom), 1) if denom > 0.05 else None,
        "n": n,
        "obs": len(common),
    }


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


# ---------------------------------------------------------------- payload
#
# One implementation, two callers: this CLI and tool/dev_proxy.py. The proxy
# used to re-assemble the payload itself, which is how it came to serve rows
# without a beta while the Worker served rows with one — the page then silently
# dropped its market panel the moment live levels replaced the committed file.
# Nothing here may be restated by a caller.

def build_payload(targets: list[dict], wl: dict, source: str,
                  gap_s: float = REQUEST_GAP_S) -> tuple[dict, list[str]]:
    """Fetch candles for every target and assemble the whole daily layer.

    Returns (payload, failed). Callers decide what a failure means: the CLI
    refuses to write a short watchlist, the dev proxy reports it inline.
    """
    # The benchmark, fetched once, is what every beta is measured against. If
    # it fails the betas are simply absent and the page falls back to raw
    # moves — the honest degradation, and the reason this is not fatal.
    try:
        market_rets = log_returns(fetch_candles(NIFTY_KEY))
    except Exception as e:                                    # noqa: BLE001
        market_rets = {}
        print(f"  WARN benchmark fetch failed, betas will be null: {e}", file=sys.stderr)
    time.sleep(gap_s)

    rows, failed, returns = [], [], {}
    for c in targets:
        try:
            bars = fetch_candles(f"NSE_EQ|{c['isin']}")
            row = build(c["symbol"], c, bars)
            rets = log_returns(bars)
            returns[c["symbol"]] = rets
            fit = fit_market(rets, market_rets, date.fromisoformat(row["as_of"])) \
                if market_rets else None
            if fit:
                row.update(fit)
            else:
                row["beta"] = row["r2"] = row["own_vol_pct"] = None
                row["insufficient"].append("beta")
            rows.append(row)
        except Exception as e:                                # noqa: BLE001
            failed.append(c["symbol"])
            print(f"  WARN {c['symbol']}: {e}", file=sys.stderr)
        time.sleep(gap_s)

    rows.sort(key=lambda r: r["symbol"])
    as_of = max((r["as_of"] for r in rows), default=None)
    structure = market_structure(returns, date.fromisoformat(as_of)) if rows else None
    payload = {
        "generated_at": datetime.now(IST).isoformat(timespec="seconds"),
        "as_of": as_of,
        "source": source,
        "watchlist_max": wl["max"],
        "placeholder_watchlist": wl["placeholder"],
        "count": len(rows),
        "failed": failed,
        # The benchmark is named in the file rather than assumed by the reader:
        # a beta is meaningless without saying beta to what.
        "benchmark": {"symbol": "NIFTY 50", "window_days": BETA_WINDOW_DAYS} if market_rets else None,
        "market_structure": structure,
        "stocks": rows,
    }
    return payload, failed


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

    out, failed = build_payload(targets, wl, source="build_indicators.py")
    rows = out["stocks"]
    structure = out["market_structure"]

    # Every named company must appear. A watchlist is explicit, so a missing
    # name is a failure rather than an acceptable gap.
    if failed:
        print(f"{len(failed)} of {len(targets)} failed to fetch: {', '.join(failed)}",
              file=sys.stderr)
        return 1

    thin = [r["symbol"] for r in rows if r["insufficient"]]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, separators=(",", ":")) + "\n")

    kb = args.out.stat().st_size / 1024
    print(f"wrote {args.out} ({kb:.0f} KB) — {len(rows)} stocks, session {out['as_of']}, "
          f"{time.time()-started:.1f}s")
    if structure:
        print(f"  market: avg pairwise corr {structure['avg_pair_corr']}, "
              f"{structure['n']} names behave like {structure['effective_breadth']} "
              f"independent bets ({structure['obs']} sessions)")
    betas = [r["beta"] for r in rows if r.get("beta") is not None]
    if betas:
        print(f"  beta vs NIFTY 50: {min(betas):.2f}–{max(betas):.2f}, "
              f"median {statistics.median(betas):.2f}")
    if failed:
        print(f"  failed: {', '.join(failed)}")
    if thin:
        print(f"  partial history (some indicators null): {', '.join(thin)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
