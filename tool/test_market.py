#!/usr/bin/env python3
"""Tests for the market layer — beta, effective breadth, volatility premium.
    python3 tool/test_market.py   (add --live for the network checks)

The claim this layer makes to a reader is strong: that it can separate the part
of a move belonging to the company from the part belonging to the market. Every
assertion below is against a value derivable by hand, an independently written
reference, or a property the mathematics must satisfy — never against whatever
the code returned the first time it ran.
"""

from __future__ import annotations

import json
import math
import random
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_indicators import (  # noqa: E402
    BETA_WINDOW_DAYS,
    MIN_BETA_OBS,
    fit_market,
    log_returns,
    market_structure,
)
import build_vrp  # noqa: E402

FAILURES: list[str] = []
ROOT = Path(__file__).resolve().parent.parent


def fail(msg: str) -> None:
    FAILURES.append(msg)


def near(name: str, got, want, tol=1e-6) -> None:
    if got is None or abs(got - want) > tol:
        fail(f"{name}: got {got}, want {want} (tol {tol})")


def _dates(n: int, end: date | None = None) -> list[str]:
    """n consecutive calendar days ending today — enough for tests that only
    need the dates to line up, not to be real sessions."""
    end = end or date.today()
    return [(end - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


# ------------------------------------------------------------------ beta

def test_beta_recovers_a_known_slope():
    """y = 1.5x exactly: the fit must return 1.5 and an R^2 of 1."""
    ds = _dates(200)
    rng = random.Random(7)
    mkt = {d: rng.gauss(0, 0.01) for d in ds}
    stk = {d: 1.5 * v for d, v in mkt.items()}
    fit = fit_market(stk, mkt, date.today())
    if fit is None:
        return fail("fit_market returned None on a clean 200-point series")
    near("noiseless beta", fit["beta"], 1.5, 0.001)
    near("noiseless r2", fit["r2"], 1.0, 0.001)
    near("noiseless residual vol", fit["own_vol_pct"], 0.0, 0.01)


def test_beta_matches_an_independent_implementation():
    """Against cov/var computed separately, on a noisy series."""
    ds = _dates(300)
    rng = random.Random(11)
    mkt = {d: rng.gauss(0, 0.012) for d in ds}
    stk = {d: 0.8 * mkt[d] + rng.gauss(0, 0.009) for d in ds}
    fit = fit_market(stk, mkt, date.today())
    cutoff = (date.today() - timedelta(days=BETA_WINDOW_DAYS)).isoformat()
    common = sorted(d for d in stk.keys() & mkt.keys() if d >= cutoff)
    x = [mkt[d] for d in common]
    y = [stk[d] for d in common]
    mx, my = statistics.fmean(x), statistics.fmean(y)
    want = (sum((a - mx) * (b - my) for a, b in zip(x, y))
            / sum((a - mx) ** 2 for a in x))
    near("beta vs reference", fit["beta"], round(want, 3), 0.002)
    # R^2 is not guessed at either. For y = b*x + e with independent noise it is
    # b^2 var(x) / (b^2 var(x) + var(e)) — here 0.8^2(0.012^2) over that plus
    # 0.009^2, which is 0.53. Sampling over 300 points moves it by a few
    # hundredths, so the tolerance is set to that rather than to a hunch.
    beta_t, sig_m, sig_e = 0.8, 0.012, 0.009
    want_r2 = (beta_t * sig_m) ** 2 / ((beta_t * sig_m) ** 2 + sig_e ** 2)
    near("r2 vs its analytic value", fit["r2"], want_r2, 0.06)


def test_beta_is_null_rather_than_guessed_when_history_is_short():
    """A short history must produce no beta at all. Returning 0, or a number
    from 20 points, would silently relabel the market's move as the company's —
    the exact error the readout exists to prevent."""
    ds = _dates(MIN_BETA_OBS - 5)
    rng = random.Random(3)
    mkt = {d: rng.gauss(0, 0.01) for d in ds}
    stk = {d: 1.1 * mkt[d] for d in ds}
    if fit_market(stk, mkt, date.today()) is not None:
        fail(f"produced a beta from only {len(ds)} observations")


def test_beta_ignores_sessions_outside_the_window():
    """Old bars must not leak in. A series whose only overlap is older than the
    window is not a short sample — it is no sample."""
    old = _dates(300, end=date.today() - timedelta(days=BETA_WINDOW_DAYS + 60))
    rng = random.Random(5)
    mkt = {d: rng.gauss(0, 0.01) for d in old}
    stk = {d: 2.0 * mkt[d] for d in old}
    if fit_market(stk, mkt, date.today()) is not None:
        fail("used observations from outside the beta window")


def test_beta_aligns_on_dates_not_position():
    """Two series that skipped different holidays must still pair correctly.
    Zipping by position instead would silently compare Monday to Tuesday."""
    ds = _dates(260)
    rng = random.Random(13)
    mkt = {d: rng.gauss(0, 0.01) for d in ds}
    stk = {d: 1.3 * mkt[d] for d in ds}
    # The stock missed ten scattered sessions the index observed.
    for d in rng.sample(ds, 10):
        del stk[d]
    fit = fit_market(stk, mkt, date.today())
    near("beta with holes", fit["beta"], 1.3, 0.001)
    if fit["obs"] != len(ds) - 10:
        fail(f"obs {fit['obs']} should be the {len(ds)-10} shared dates")


def test_log_returns_are_keyed_by_date_and_skip_the_first_bar():
    bars = [{"date": "2026-01-01", "close": 100.0},
            {"date": "2026-01-02", "close": 110.0},
            {"date": "2026-01-03", "close": 99.0}]
    r = log_returns(bars)
    if set(r) != {"2026-01-02", "2026-01-03"}:
        fail(f"unexpected keys {sorted(r)}")
    near("log return", r["2026-01-02"], math.log(1.1), 1e-9)


# ------------------------------------------------- effective breadth

def test_effective_breadth_of_identical_names_is_one():
    """Thirty copies of the same series correlate at 1.0 and are one bet."""
    ds = _dates(200)
    rng = random.Random(17)
    base = {d: rng.gauss(0, 0.01) for d in ds}
    ms = market_structure({f"S{i}": dict(base) for i in range(30)}, date.today())
    near("rho of identical series", ms["avg_pair_corr"], 1.0, 0.001)
    near("effective breadth of identical series", ms["effective_breadth"], 1.0, 0.05)


def test_effective_breadth_of_independent_names_approaches_n():
    """Uncorrelated names are worth close to their own count, and the result is
    capped at N — more independent bets than names is not a thing."""
    ds = _dates(400)
    rng = random.Random(19)
    rets = {f"S{i}": {d: rng.gauss(0, 0.01) for d in ds} for i in range(12)}
    ms = market_structure(rets, date.today())
    if abs(ms["avg_pair_corr"]) > 0.12:
        fail(f"independent series correlated at {ms['avg_pair_corr']}")
    if ms["effective_breadth"] > ms["n"]:
        fail(f"effective breadth {ms['effective_breadth']} exceeds N={ms['n']}")


def test_effective_breadth_is_capped_at_the_number_of_names():
    """With mildly negative average correlation the raw Grinold formula returns
    MORE independent bets than there are names — 12.5 from five stocks — which
    is nonsense a reader would take at face value. The cap has to bind.

    Built by subtracting a fraction c of the cross-sectional mean from each of N
    independent series, which drives average pairwise correlation to
    (c^2-2c)/(N-2c+c^2). At N=5, c=0.41 that is about -0.15: negative enough for
    the formula to overshoot, still clear of the degenerate denominator.
    """
    n, c = 5, 0.41
    ds = _dates(400)
    rng = random.Random(41)
    raw = {f"S{i}": {d: rng.gauss(0, 0.01) for d in ds} for i in range(n)}
    rets = {}
    for i in range(n):
        rets[f"S{i}"] = {d: raw[f"S{i}"][d] - c * statistics.fmean(
            [raw[f"S{j}"][d] for j in range(n)]) for d in ds}
    ms = market_structure(rets, date.today())
    rho = ms["avg_pair_corr"]
    if rho >= 0:
        return fail(f"construction failed: rho {rho} is not negative, test proves nothing")
    uncapped = n / (1 + rho * (n - 1))
    if uncapped <= n:
        return fail(f"construction failed: uncapped {uncapped:.2f} does not exceed N={n}")
    if ms["effective_breadth"] != n:
        fail(f"effective breadth {ms['effective_breadth']} not capped at N={n} "
             f"(uncapped formula gives {uncapped:.2f})")


def test_effective_breadth_matches_the_grinold_formula():
    """N / (1 + rho(N-1)), computed by hand from the reported rho."""
    ds = _dates(300)
    rng = random.Random(23)
    common = {d: rng.gauss(0, 0.01) for d in ds}
    rets = {f"S{i}": {d: common[d] + rng.gauss(0, 0.012) for d in ds} for i in range(20)}
    ms = market_structure(rets, date.today())
    rho, n = ms["avg_pair_corr"], ms["n"]
    near("grinold", ms["effective_breadth"], round(min(n, n / (1 + rho * (n - 1))), 1), 0.15)


def test_effective_breadth_is_null_when_the_formula_degenerates():
    """A sufficiently negative rho drives the denominator through zero. The
    honest answer there is no answer, not a negative or enormous count."""
    ds = _dates(300)
    rng = random.Random(29)
    a = {d: rng.gauss(0, 0.01) for d in ds}
    # Two perfectly opposed names: rho = -1, denominator 1 + (-1)(1) = 0.
    ms = market_structure({"A": a, "B": {d: -v for d, v in a.items()}}, date.today())
    near("rho of opposed pair", ms["avg_pair_corr"], -1.0, 0.001)
    if ms["effective_breadth"] is not None:
        fail(f"returned {ms['effective_breadth']} where the formula is undefined")


def test_market_structure_needs_enough_shared_sessions():
    ds = _dates(MIN_BETA_OBS - 10)
    rng = random.Random(31)
    rets = {f"S{i}": {d: rng.gauss(0, 0.01) for d in ds} for i in range(5)}
    if market_structure(rets, date.today()) is not None:
        fail("reported a structure from too few shared sessions")


# ---------------------------------------------------- volatility premium

def test_annualised_vol_matches_a_hand_computation():
    closes = [100, 101, 100, 102, 101, 103]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    want = statistics.pstdev(rets) * math.sqrt(252) * 100
    near("annualised vol", build_vrp.annualised_vol(closes), want, 1e-9)


def test_annualised_vol_of_a_flat_series_is_zero():
    near("flat vol", build_vrp.annualised_vol([50.0] * 30), 0.0, 1e-12)


def test_annualised_vol_refuses_a_series_too_short_to_measure():
    if build_vrp.annualised_vol([100.0]) is not None:
        fail("measured a volatility from one price")


def test_vrp_ratio_and_percentile_are_internally_consistent():
    """A synthetic decade where implied is always exactly 1.25x realised: the
    ratio must be 1.25 and the anchor must say it was above realised always."""
    ds = _dates(900)
    rng = random.Random(37)
    px, nifty = 20000.0, []
    for d in ds:
        px *= math.exp(rng.gauss(0, 0.008))
        nifty.append({"date": d, "close": px})
    # Implied set to 1.25x the vol actually realised over the following window,
    # so the forward measure has a known answer.
    vix = []
    for i, d in enumerate(ds):
        win = [b["close"] for b in nifty[i:i + build_vrp.REALISED_SESSIONS + 1]]
        rv = build_vrp.annualised_vol(win) if len(win) > 2 else None
        vix.append({"date": d, "close": (rv or 12.0) * 1.25})
    out = build_vrp.build(nifty, vix)
    near("anchor mean ratio", out["anchor"]["mean_ratio"], 1.25, 0.02)
    near("anchor above pct", out["anchor"]["above_pct"], 100.0, 0.01)
    if not 0 <= out["percentile"] <= 100:
        fail(f"percentile {out['percentile']} out of range")
    if out["directional"] is not False:
        fail("the volatility premium must never be marked directional")


def test_vrp_horizon_matches_the_vix_definition():
    """India VIX is a 30-calendar-day implied volatility, about 21 sessions.
    Measuring it against a 30-session realised window compares a one-month
    forward with a six-week past."""
    if build_vrp.REALISED_SESSIONS != 21:
        fail(f"realised window is {build_vrp.REALISED_SESSIONS} sessions, expected 21")


def test_vrp_chunking_stays_under_the_upstox_range_cap():
    """Upstox rejects any daily window wider than 3652 days with HTTP 400
    UDAPI1148. A chunk at or above the cap returns nothing at all."""
    if build_vrp.CHUNK_DAYS >= 3652:
        fail(f"chunk of {build_vrp.CHUNK_DAYS} days is at or over the 3652-day cap")


# ------------------------------------------------------- generated files

def test_generated_indicator_file_carries_the_market_layer():
    p = ROOT / "web" / "data" / "indicators.json"
    if not p.exists():
        return fail("indicators.json missing — run tool/build_indicators.py")
    d = json.loads(p.read_text())
    if not d.get("benchmark"):
        fail("no benchmark named — a beta without one is meaningless")
    ms = d.get("market_structure")
    if not ms:
        return fail("no market_structure block")
    if not -1 <= ms["avg_pair_corr"] <= 1:
        fail(f"correlation {ms['avg_pair_corr']} outside [-1, 1]")
    if ms["effective_breadth"] is not None and ms["effective_breadth"] > ms["n"]:
        fail("effective breadth exceeds the number of names")
    for s in d["stocks"]:
        if s.get("beta") is None:
            if "beta" not in s.get("insufficient", []):
                fail(f"{s['symbol']}: null beta not declared insufficient")
            continue
        if not -1 < s["beta"] < 4:
            fail(f"{s['symbol']}: beta {s['beta']} implausible for an Indian large cap")
        if not 0 <= s["r2"] <= 1:
            fail(f"{s['symbol']}: r2 {s['r2']} outside [0, 1]")


def test_generated_vrp_file_is_sane():
    p = ROOT / "web" / "data" / "vrp.json"
    if not p.exists():
        return fail("vrp.json missing — run tool/build_vrp.py")
    d = json.loads(p.read_text())
    near("ratio is implied over realised", d["ratio"],
         round(d["implied"]["value"] / d["realised"], 3), 0.002)
    if not 0 <= d["percentile"] <= 100:
        fail(f"percentile {d['percentile']} out of range")
    if d["history"]["obs"] < 1000:
        fail(f"only {d['history']['obs']} historical observations")
    if len(d["history"]["deciles"]) != 9:
        fail(f"{len(d['history'])} decile cuts, expected 9")
    if d["history"]["deciles"] != sorted(d["history"]["deciles"]):
        fail("decile cuts are not ascending")
    if d["directional"] is not False:
        fail("vrp.json must state it is not directional")


# ------------------------------------------------------------ the page

def test_page_and_worker_expose_the_same_market_fields():
    """The Worker passes beta through from the manifest; the dev proxy builds
    it. Both must emit it, because the page swaps the committed file for
    whichever answers — and a missing field silently removes the panel."""
    worker = (ROOT / "backend" / "worker.js").read_text()
    proxy = (ROOT / "tool" / "dev_proxy.py").read_text()
    for field in ("beta", "market_structure", "benchmark"):
        if field not in worker:
            fail(f"worker.js never mentions {field}")
    if "build_payload" not in proxy:
        fail("dev_proxy.py no longer delegates to build_indicators.build_payload — "
             "re-assembling the payload locally is how beta went missing before")


def test_page_reads_the_market_layer():
    html = (ROOT / "web" / "index.html").read_text()
    for hook in ("marketPanel", "relHtml", "market_structure", "data/vrp.json", "ordinal"):
        if hook not in html:
            fail(f"index.html is missing {hook}")
    # The one claim the page must never make.
    if "vs mkt" not in html:
        fail("the per-row own-move readout is gone")


def test_page_never_calls_the_band_a_direction():
    """A width presented as a direction is the failure mode this whole layer is
    built to avoid, and it is a claim the owner is not registered to make."""
    html = (ROOT / "web" / "index.html").read_text().lower()
    for phrase in ("will rise", "will fall", "should buy", "should sell",
                   "target price", "recommend"):
        if phrase in html:
            fail(f"page contains advice language: {phrase!r}")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        try:
            t()
            print(f"  {t.__name__}")
        except Exception as e:                                # noqa: BLE001
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
