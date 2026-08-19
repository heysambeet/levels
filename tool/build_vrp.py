#!/usr/bin/env python3
"""Build the volatility-premium layer: what options charge versus what the
market has actually delivered.

Why this exists. The page already shows an option-implied expected range on
each stock, and that band is systematically wide — implied volatility runs
above what subsequently gets realised. A reader has no way to know *how* wide
today's bands are unless the page says so. This measures it.

What it is NOT: a direction, a signal, or a reason to trade anything. It is one
ratio and where that ratio sits in its own ten-year history.

Only the index layer is built here. Per-stock volatility premia would need a
history of each name's implied volatility, and no free source publishes one —
it would have to accumulate from daily snapshots. See README.

Run after the close:  python3 tool/build_vrp.py
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

IST = ZoneInfo("Asia/Kolkata")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
UPSTOX = "https://api.upstox.com/v2/historical-candle"

NIFTY_KEY = "NSE_INDEX|Nifty 50"
VIX_KEY = "NSE_INDEX|India VIX"

# India VIX is a 30-CALENDAR-day implied volatility, which is about 21 trading
# sessions. Comparing it against a 30-*session* realised window would be
# measuring a one-month forward against a six-week past — matching the horizon
# is what makes the ratio mean what it claims to.
REALISED_SESSIONS = 21

HISTORY_YEARS = 10
# Upstox rejects any daily window wider than 3652 days (exactly 10.00 years)
# with HTTP 400 UDAPI1148 "Invalid date range". Ten years therefore has to be
# assembled from chunks — asking for 3800 days in one call returns nothing at
# all, which reads like the index having no history rather than a range limit.
CHUNK_DAYS = 1000
REQUEST_GAP_S = 0.12

TOOL_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = TOOL_DIR.parent / "web" / "data" / "vrp.json"


def get(url: str, tries: int = 3, timeout: int = 40) -> bytes:
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


def fetch_history(instrument_key: str, years: int = HISTORY_YEARS) -> list[dict]:
    """Daily closes, oldest-first, assembled in chunks under the range cap.

    Deduplicated by date because adjacent chunks share a boundary day, and a
    duplicated session would quietly bias every volatility window that spans it.
    """
    today = datetime.now(IST).date()
    earliest = today - timedelta(days=int(years * 365.25))
    key = urllib.parse.quote(instrument_key, safe="")
    seen: dict[str, dict] = {}
    to = today
    while to > earliest:
        frm = max(earliest, to - timedelta(days=CHUNK_DAYS))
        url = f"{UPSTOX}/{key}/day/{to.isoformat()}/{frm.isoformat()}"
        payload = json.loads(get(url))
        if payload.get("status") != "success":
            raise RuntimeError(f"upstox: {json.dumps(payload)[:200]}")
        for r in payload["data"]["candles"]:
            d = r[0][:10]
            seen[d] = {"date": d, "close": float(r[4])}
        to = frm - timedelta(days=1)
        time.sleep(REQUEST_GAP_S)
    bars = sorted(seen.values(), key=lambda b: b["date"])
    if not bars:
        raise RuntimeError(f"no history for {instrument_key}")
    return bars


def annualised_vol(closes: list[float]) -> float | None:
    """Close-to-close volatility, annualised, in percent — the same units the
    VIX is quoted in, so the two are directly comparable."""
    if len(closes) < 3:
        return None
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes)) if closes[i - 1] > 0 and closes[i] > 0]
    if len(rets) < 2:
        return None
    return statistics.pstdev(rets) * math.sqrt(252) * 100


def build(nifty: list[dict], vix: list[dict]) -> dict:
    ncl = {b["date"]: b["close"] for b in nifty}
    vcl = {b["date"]: b["close"] for b in vix}
    days = [b["date"] for b in nifty if b["date"] in vcl]
    if len(days) < 250:
        raise RuntimeError(f"only {len(days)} overlapping sessions")

    # --- the honest historical measure -------------------------------------
    # For each past day: what the VIX charged, against what the index then went
    # on to deliver over the matching horizon. This needs a future, so it can
    # only ever be computed about a month behind — which is the point. It is a
    # measurement of what happened, never a claim about what comes next.
    forward = []
    for i in range(len(days) - REALISED_SESSIONS):
        win = days[i:i + REALISED_SESSIONS + 1]
        rv = annualised_vol([ncl[d] for d in win])
        iv = vcl[days[i]]
        if rv and rv > 0.5 and iv > 0:
            forward.append(iv / rv)

    # --- the measure that is computable today ------------------------------
    # Today's VIX against the volatility of the sessions just gone. This is what
    # the page shows, and its ten-year distribution is what makes today's number
    # readable as high or low rather than merely a decimal.
    trailing = []
    for i in range(REALISED_SESSIONS, len(days)):
        win = days[i - REALISED_SESSIONS:i + 1]
        rv = annualised_vol([ncl[d] for d in win])
        iv = vcl[days[i]]
        if rv and rv > 0.5 and iv > 0:
            trailing.append({"date": days[i], "vix": iv, "realised": rv, "ratio": iv / rv})
    if not trailing or not forward:
        raise RuntimeError("not enough history to measure a premium")

    cur = trailing[-1]
    ratios = [t["ratio"] for t in trailing]
    pctile = sum(1 for r in ratios if r <= cur["ratio"]) / len(ratios) * 100

    return {
        "generated_at": datetime.now(IST).isoformat(timespec="seconds"),
        "as_of": cur["date"],
        "horizon_sessions": REALISED_SESSIONS,
        "benchmark": "NIFTY 50",
        "implied": {"symbol": "India VIX", "value": round(cur["vix"], 2)},
        "realised": round(cur["realised"], 2),
        "ratio": round(cur["ratio"], 3),
        "percentile": round(pctile),
        "history": {
            "years": HISTORY_YEARS,
            "obs": len(ratios),
            "from": trailing[0]["date"],
            "median_ratio": round(statistics.median(ratios), 3),
            "deciles": [round(q, 3) for q in statistics.quantiles(ratios, n=10)],
        },
        # The anchor figures the page quotes as "historically". Forward-measured,
        # so they describe what implied volatility was actually worth rather than
        # how it compared to the past.
        "anchor": {
            "mean_ratio": round(statistics.fmean(forward), 3),
            "median_ratio": round(statistics.median(forward), 3),
            "above_pct": round(sum(1 for r in forward if r > 1) / len(forward) * 100, 1),
            "obs": len(forward),
        },
        # Said plainly in the data itself so no reader of this file, and no
        # future caller, mistakes it for a direction.
        "directional": False,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    started = time.time()
    try:
        nifty = fetch_history(NIFTY_KEY)
        vix = fetch_history(VIX_KEY)
    except Exception as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 1
    print(f"NIFTY 50  {len(nifty):5d} bars  {nifty[0]['date']} -> {nifty[-1]['date']}")
    print(f"India VIX {len(vix):5d} bars  {vix[0]['date']} -> {vix[-1]['date']}")

    try:
        out = build(nifty, vix)
    except Exception as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, separators=(",", ":")) + "\n")

    a = out["anchor"]
    print(f"\nwrote {args.out} ({args.out.stat().st_size/1024:.1f} KB) in {time.time()-started:.1f}s")
    print(f"  today {out['as_of']}: VIX {out['implied']['value']} vs "
          f"{out['realised']} realised ({out['horizon_sessions']}d) "
          f"= {out['ratio']}x, {out['percentile']}th percentile of {out['history']['obs']}")
    print(f"  anchor: implied ran {(a['mean_ratio']-1)*100:+.1f}% above subsequently "
          f"realised, above in {a['above_pct']}% of {a['obs']} windows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
