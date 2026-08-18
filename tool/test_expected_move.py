#!/usr/bin/env python3
"""Tests for the expected-move band and the round-trip cost.

The band is the closest thing to an "approximate buy/sell price" that survives
scrutiny, which makes it the most dangerous thing in the app to get subtly
wrong: it will look plausible either way. Two things are pinned hardest —
the straddle-to-sigma multiplier, and the fact that the band is symmetric and
therefore carries no direction.

Run:            python3 tool/test_expected_move.py
With --live it also hits the deployed Worker and the dev proxy and compares.
"""

from __future__ import annotations

import json
import math
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROUTES_JS = ROOT / "backend" / "routes.js"
WORKER = "https://levels-proxy.marketnudge.workers.dev/api/expected-move"
DEV = "http://localhost:8900/api/expected-move"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

FAILURES: list[str] = []


def fail(m: str) -> None:
    FAILURES.append(m)


def near(name, got, want, tol=0.01):
    if got is None or abs(got - want) > tol:
        fail(f"{name}: got {got}, want {want}")


# ------------------------------------------------------------ the multiplier

def test_straddle_multiplier_is_sqrt_pi_over_2():
    """An ATM straddle prices E[|move|] = S·σ·√T·√(2/π), so 1σ = straddle ÷
    0.7979 = × 1.2533. The popular "× 0.85" rule gives 0.68σ — a ~50% band.
    Shipping that labelled 68% understates the range by nearly half."""
    correct = math.sqrt(math.pi / 2)
    near("√(π/2)", correct, 1.2533, tol=0.0001)
    # What the wrong rule actually produces, in sigmas:
    wrong_in_sigmas = 0.85 * math.sqrt(2 / math.pi)
    near("0.85 rule expressed in sigmas", wrong_in_sigmas, 0.678, tol=0.001)
    if wrong_in_sigmas > 0.9:
        fail("the 0.85 rule should be well under 1σ — check the algebra")


def test_routes_js_uses_the_correct_multiplier():
    src = ROUTES_JS.read_text()
    m = re.search(r"straddleToSigma:\s*([^,]+),", src)
    if not m:
        fail("straddleToSigma missing from routes.js")
        return
    expr = m.group(1)
    if "0.85" in expr:
        fail("routes.js uses the 0.85 rule — that is 0.68σ, not 1σ")
    if "Math.PI" not in expr:
        fail(f"straddleToSigma does not look like √(π/2): {expr!r}")


def test_dev_proxy_reads_the_multiplier_rather_than_restating_it():
    sys.path.insert(0, str(ROOT / "tool"))
    import dev_proxy
    near("dev_proxy STRADDLE_TO_SIGMA", dev_proxy.STRADDLE_TO_SIGMA,
         math.sqrt(math.pi / 2), tol=0.0001)


# ------------------------------------------------------------ cost arithmetic

def test_round_trip_cost_matches_the_statutory_rates():
    """Delivery equity, discount broker. STT dominates: 0.1% each side."""
    notional = 100_000
    stt = notional * 0.001 * 2          # 200
    stamp = notional * 0.00015          # 15
    exch = notional * 0.0000375 * 2     # 7.50
    dp = 15.34
    total = stt + stamp + exch + dp
    near("round trip on ₹1L", total, 237.84, tol=0.5)
    near("in bps", total / notional * 10000, 23.78, tol=0.05)
    # The number that actually matters to a user.
    near("breakeven move %", total / notional * 100, 0.2378, tol=0.005)


def test_page_cost_constants_agree():
    src = (ROOT / "web" / "index.html").read_text()
    for name, want in (("sttPct", 0.10), ("stampPct", 0.015),
                       ("exchGstPct", 0.00375), ("dpFlat", 15.34)):
        m = re.search(rf"{name}:\s*([0-9.]+)", src)
        if not m:
            fail(f"page COSTS.{name} missing")
        else:
            near(f"page COSTS.{name}", float(m.group(1)), want, tol=1e-6)


# ------------------------------------------------------------ live behaviour

def fetch(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def test_live_band_is_symmetric_and_consistent():
    if "--live" not in sys.argv:
        print("  (skipping live checks — pass --live)")
        return
    for sym in ("RELIANCE", "TCS", "HDFCBANK"):
        d = fetch(f"{WORKER}?symbol={sym}")
        if not d.get("available"):
            fail(f"{sym}: unavailable — {d.get('reason')}")
            continue
        # Symmetry is the whole ethical point: no direction is implied.
        near(f"{sym} low", d["low"], d["spot"] - d["sigma"], tol=0.02)
        near(f"{sym} high", d["high"], d["spot"] + d["sigma"], tol=0.02)
        if d.get("directional") is not False:
            fail(f"{sym}: directional flag should be False")
        # Two independent derivations of sigma must agree closely.
        if abs(d["method_ratio"] - 1) > 0.15:
            fail(f"{sym}: straddle and IV methods disagree by "
                 f"{abs(d['method_ratio']-1)*100:.0f}%")
        if not (0 < d["sigma_pct"] < 25):
            fail(f"{sym}: sigma_pct {d['sigma_pct']} is implausible")
        if d["days_to_expiry"] < 0.5:
            fail(f"{sym}: returned an expiry inside the 12h guard")


def test_live_failure_modes_are_reported_not_faked():
    """Every upstream failure answers HTTP 200 with an empty array, so a naive
    implementation would show a confident band for a company with no options."""
    if "--live" not in sys.argv:
        return
    for sym in ("FAKESYM", "HFCL"):
        d = fetch(f"{WORKER}?symbol={sym}")
        if d.get("available"):
            fail(f"{sym}: reported available, but it has no listed options")
        if not d.get("reason"):
            fail(f"{sym}: unavailable without saying why")


def test_worker_and_dev_proxy_agree():
    if "--live" not in sys.argv:
        return
    try:
        w = fetch(f"{WORKER}?symbol=RELIANCE")
        d = fetch(f"{DEV}?symbol=RELIANCE")
    except Exception as e:
        print(f"  (dev proxy not running — skipping parity: {e})")
        return
    if not (w.get("available") and d.get("available")):
        fail("one side reported unavailable")
        return
    # Spot moves between the two calls, so compare the ratio, not the level.
    drift = abs(w["sigma"] - d["sigma"]) / w["sigma"]
    if drift > 0.05:
        fail(f"worker σ {w['sigma']} vs dev σ {d['sigma']} — {drift*100:.1f}% apart")
    print(f"  worker vs dev σ: {w['sigma']} / {d['sigma']} ({drift*100:.2f}% apart)")


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
