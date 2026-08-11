#!/usr/bin/env python3
"""The Worker's indicator math must agree with build_indicators.py.

There are deliberately two implementations. The Python one is the reference
and runs the tests; the JavaScript one runs in the Worker so the page's levels
reflect the latest close rather than the last time someone remembered to
rebuild the file. That is a real drift risk, accepted for a real gain — and
this test is the thing that makes it safe.

It compares the deployed Worker's /api/indicators against a fresh local
computation, symbol by symbol, and fails on any disagreement beyond rounding.

Run: python3 tool/test_worker_indicators.py
"""

from __future__ import annotations

import json
import sys
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_indicators import UA, build, fetch_candles  # noqa: E402
from symbols import load_instrument_map, load_watchlist, resolve  # noqa: E402

WORKER = "https://levels-proxy.marketnudge.workers.dev/api/indicators"
# Rounding differences between float64 in two languages are legitimate; a
# genuine logic divergence (wrong window, unreversed series, simple-mean RSI)
# is never this small.
TOL = 0.02

FAILURES: list[str] = []


def fail(m: str) -> None:
    FAILURES.append(m)


def main() -> int:
    # Cache-bust, or this test can pass against a Worker that is already
    # broken: /api/indicators is cached for an hour, so a fresh deploy keeps
    # being answered from the edge. Caught the first time this suite was
    # mutation-checked — the reversed-candles mutation sailed through on a
    # cached good response while the live Worker really was returning the
    # oldest bar as `as_of`.
    url = f"{WORKER}?nocache={uuid.uuid4().hex}"
    print(f"fetching {url}")
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Cache-Control": "no-cache", "Pragma": "no-cache"})
    worker = json.loads(urllib.request.urlopen(req, timeout=120).read())
    wrows = {s["symbol"]: s for s in worker["stocks"]}
    print(f"  worker: {worker['count']} stocks, as_of {worker['as_of']}")

    wl = load_watchlist()
    targets = resolve(wl["symbols"], load_instrument_map())
    if set(wrows) != {t["symbol"] for t in targets}:
        fail(f"symbol sets differ: worker-only {set(wrows) - {t['symbol'] for t in targets}}, "
             f"local-only {{t['symbol'] for t in targets}} - set(wrows)")

    checked = 0
    for t in targets:
        sym = t["symbol"]
        w = wrows.get(sym)
        if not w:
            fail(f"{sym}: missing from worker response")
            continue
        local = build(sym, t, fetch_candles(t["isin"]))
        checked += 1

        # A different as_of means one side saw a bar the other did not — real,
        # and it makes every other comparison meaningless, so say so and move on.
        if local["as_of"] != w["as_of"]:
            fail(f"{sym}: as_of differs — local {local['as_of']} vs worker {w['as_of']}")
            continue

        for k in ("dma50", "dma100", "dma200", "rsi14", "close"):
            a, b = local[k], w[k]
            if (a is None) != (b is None):
                fail(f"{sym}.{k}: local {a!r} vs worker {b!r}")
            elif a is not None and abs(a - b) > TOL:
                fail(f"{sym}.{k}: local {a} vs worker {b} (diff {abs(a-b):.4f})")

        lw, ww = local["week52"], w["week52"]
        if (lw is None) != (ww is None):
            fail(f"{sym}.week52: local {lw!r} vs worker {ww!r}")
        elif lw:
            for k in ("high", "low"):
                if abs(lw[k] - ww[k]) > TOL:
                    fail(f"{sym}.week52.{k}: local {lw[k]} vs worker {ww[k]}")
            if lw["high_date"] != ww["high_date"]:
                fail(f"{sym}.week52.high_date: local {lw['high_date']} vs worker {ww['high_date']}")

        if local["insufficient"] != w["insufficient"]:
            fail(f"{sym}.insufficient: local {local['insufficient']} vs worker {w['insufficient']}")

    print(f"  compared {checked} symbols against a fresh local computation")
    if FAILURES:
        print(f"\nFAILED ({len(FAILURES)}):")
        for f in FAILURES[:20]:
            print(f"  - {f}")
        return 1
    print("\nworker and python agree on every symbol")
    return 0


if __name__ == "__main__":
    sys.exit(main())
