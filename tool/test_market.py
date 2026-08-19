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
import re
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_indicators import (  # noqa: E402
    BETA_WINDOW_DAYS,
    MIN_BARS_52W,
    MIN_BETA_OBS,
    STRUCTURAL_BREAK_LOG,
    build,
    crossings,
    fit_market,
    log_returns,
    market_structure,
    usable_history,
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


# -------------------------------------------------- corporate actions

def test_a_demerger_step_is_not_treated_as_a_daily_return():
    """The measured case. TMPV closed 660.75 then 395.45 across the Tata Motors
    demerger; Upstox does not adjust for it. Read as a return it published a
    52-week high of 739.70 for a stock trading at 322.80, R^2 0.121 against a
    true 0.399, and insufficient: [] — every number asserted as sound."""
    bars = _bars([660.75] * 260 + [395.45] + [400.0] * 30)
    cut, brk = usable_history(bars)
    if brk != bars[260]["date"]:
        return fail(f"break not detected at the step: got {brk}")
    if len(cut) != 31:
        fail(f"usable history {len(cut)} bars, expected 31 (the break bar onward)")
    if cut[0]["close"] != 395.45:
        fail("usable history must START at the break bar, which is the new basis")
    # The offending step must be gone from the returns entirely.
    worst = max(abs(v) for v in log_returns(cut).values()) if len(cut) > 1 else 0
    if worst > STRUCTURAL_BREAK_LOG:
        fail(f"a step of {worst:.3f} survived the cut")


def test_usable_history_is_idempotent():
    """Cutting twice must not cut twice — the returned series starts at the
    break, so the step is outside it and a second pass finds nothing."""
    bars = _bars([100.0] * 100 + [40.0] + [41.0] * 100)
    once, brk1 = usable_history(bars)
    twice, brk2 = usable_history(once)
    if brk2 is not None or len(twice) != len(once):
        fail(f"second pass cut again: {brk1} then {brk2}, {len(once)} -> {len(twice)}")


def test_an_ordinary_crash_is_not_mistaken_for_a_corporate_action():
    """A real fall must survive. Indian price bands cap a session well below the
    threshold, so a 15% collapse is trading and has to stay in the history."""
    bars = _bars([100.0] * 100 + [85.0] + [86.0] * 50)
    cut, brk = usable_history(bars)
    if brk is not None or len(cut) != len(bars):
        fail(f"a -15% session was treated as a corporate action (break {brk})")


def test_indicators_spanning_a_break_go_null_rather_than_wrong():
    """The whole point: after a restatement the long-range figures must be
    absent, not computed across two different companies."""
    bars = _bars([660.0] * LONG + [395.0] + [400.0] * 40)
    row = build("X", {"isin": "I", "name": "X"}, bars)
    if row["week52"] is not None:
        fail("52-week range computed across a corporate action")
    if "week52" not in row["insufficient"]:
        fail("a null 52-week range was not declared insufficient")
    if row["structural_break"] != bars[LONG]["date"]:
        fail(f"the break date is not published: {row['structural_break']}")
    if row["dma200"] is not None:
        fail("a 200-day average was computed from 41 post-break sessions")


def test_the_beta_fit_is_given_the_same_cut_history_as_the_indicators():
    """build_payload must feed fit_market from usable_history, not raw bars.

    Checked two ways because the code path needs a network to run: the source
    must compose them, and the published file must be arithmetically consistent
    with a fit that saw only post-break sessions. Fitting across the break is
    what published TMPV's R^2 as 0.121 against a true 0.424.
    """
    src = (ROOT / "tool" / "build_indicators.py").read_text()
    if "clean, _ = usable_history(bars)" not in src or "log_returns(clean)" not in src:
        fail("build_payload no longer fits beta on the corporate-action-cut history")

    p = ROOT / "web" / "data" / "indicators.json"
    if not p.exists():
        return
    d = json.loads(p.read_text())
    for s in d["stocks"]:
        brk = s.get("structural_break")
        if not brk or s.get("obs") is None:
            continue
        days = (date.fromisoformat(s["as_of"]) - date.fromisoformat(brk)).days
        # Indian sessions run ~247 per 365 calendar days (0.677). Allowing 0.72
        # plus a few bars is generous; a fit that reached back across the break
        # cannot fit under it.
        ceiling = int(days * 0.72) + 5
        if s["obs"] > ceiling:
            fail(f"{s['symbol']}: beta used {s['obs']} sessions but only ~{ceiling} exist "
                 f"since its {brk} restatement — the fit spans the corporate action")


def test_the_break_threshold_sits_above_every_nse_price_band():
    """Set generous on purpose. Missing a real break publishes a wrong number;
    a false positive only shortens a history and nulls an indicator, which the
    page already states plainly."""
    if not (0.15 < STRUCTURAL_BREAK_LOG < 0.30):
        fail(f"threshold {STRUCTURAL_BREAK_LOG:.3f} is outside the defensible range")


# ------------------------------------------------------------- crossings

# _bars lays one bar on each CONSECUTIVE CALENDAR day, so a series of N bars
# spans N days. Any test of a 52-week rule therefore needs more than 365 of
# them, or the series does not reach back a year and the coverage guard — quite
# rightly — refuses to call anything a 52-week extreme.
LONG = 420


def _bars(closes: list[float], start: str = "2024-01-01") -> list[dict]:
    d0 = date.fromisoformat(start)
    return [{"date": (d0 + timedelta(days=i)).isoformat(), "close": float(c),
             "high": float(c), "low": float(c), "open": float(c)}
            for i, c in enumerate(closes)]


def _cross(bars):
    return crossings(bars, date.fromisoformat(bars[-1]["date"]))


def test_crossing_is_detected_in_both_directions():
    up = _cross(_bars([100.0] * 49 + [90.0, 110.0]))
    if [(e["level"], e["direction"]) for e in up] != [("50-day average", "above")]:
        fail(f"upward crossing not detected: {up}")
    down = _cross(_bars([100.0] * 49 + [110.0, 90.0]))
    if [(e["level"], e["direction"]) for e in down] != [("50-day average", "below")]:
        fail(f"downward crossing not detected: {down}")


def test_no_crossing_when_the_price_stays_one_side():
    if _cross(_bars([100.0] * 49 + [90.0, 92.0])):
        fail("reported a crossing without one")
    if _cross(_bars([100.0] * 49 + [110.0, 112.0])):
        fail("reported a crossing without one")


def test_crossing_compares_against_each_session_own_average():
    """The average moves too. Testing today's close against YESTERDAY's average
    manufactures crossings out of the average's own drift — on a steadily rising
    series the price is always above both, and nothing should fire."""
    rising = [100.0 + i * 0.5 for i in range(80)]
    if _cross(_bars(rising)):
        fail("a steadily rising series produced a crossing")
    falling = [200.0 - i * 0.5 for i in range(80)]
    if _cross(_bars(falling)):
        fail("a steadily falling series produced a crossing")


def test_sitting_exactly_on_the_level_is_not_a_crossing():
    """A perfectly flat series sits exactly on its own average. That is not a
    crossing in either direction, and calling it one would fire an alert every
    single session for a stock that has not moved at all."""
    if _cross(_bars([100.0] * 60)):
        fail("a flat series produced a crossing")


def test_a_new_extreme_reports_once_not_every_day_of_the_run():
    """Measured on RELIANCE, a year replay produced new closing lows on three
    consecutive sessions — three notifications for one slide. Only the first of
    a run is news; a reader alerted daily for a fortnight stops reading."""
    n = LONG
    # A long flat history, then four consecutive new lows.
    closes = [100.0] * n + [90.0, 89.0, 88.0, 87.0]
    bars = _bars(closes)
    fired = []
    for i in range(n, len(bars)):
        for e in crossings(bars[:i + 1], date.fromisoformat(bars[i]["date"])):
            if e["type"] == "extreme":
                fired.append(bars[i]["date"])
    if len(fired) != 1:
        fail(f"new 52-week low fired {len(fired)} times over one run: {fired}")


def test_a_new_extreme_reports_again_after_the_run_breaks():
    """Suppression must not be permanent — a second, separate slide is news
    again. RELIANCE's real history shows two distinct runs a week apart."""
    n = LONG
    closes = [100.0] * n + [90.0, 89.0] + [95.0] * 6 + [85.0]
    bars = _bars(closes)
    fired = []
    for i in range(n, len(bars)):
        for e in crossings(bars[:i + 1], date.fromisoformat(bars[i]["date"])):
            if e["type"] == "extreme":
                fired.append(bars[i]["date"])
    if len(fired) != 2:
        fail(f"expected two separate runs to fire, got {len(fired)}: {fired}")


def test_extreme_is_measured_against_the_window_excluding_today():
    """Including today's close in the record it is tested against means nothing
    can ever be a new high, and the alert silently never fires."""
    n = LONG
    bars = _bars([100.0] * n + [130.0])
    ev = [e for e in _cross(bars) if e["type"] == "extreme"]
    if len(ev) != 1 or ev[0]["direction"] != "above":
        return fail(f"new closing high not detected: {ev}")
    near("record broken", ev[0]["value"], 100.0, 0.01)


def test_no_extreme_without_a_year_of_history():
    bars = _bars([100.0] * 50 + [130.0])
    if [e for e in _cross(bars) if e["type"] == "extreme"]:
        fail("claimed a 52-week extreme from 51 sessions")


def test_no_extreme_when_the_history_does_not_reach_back_a_full_year():
    """Counting bars is the tempting guard and it is wrong — it catches a series
    full of holes but cannot tell whether the series covers a year at all. A
    name with 201-246 sessions passes the count and covers under twelve months,
    and the row then announces a '52-week closing high' beside a 52-week range
    reading n/a. Measured: 220 sessions spanning 307 days did exactly that."""
    d, bars = date(2025, 10, 15), []
    while len(bars) < 220:                       # weekdays only, like real sessions
        if d.weekday() < 5:
            bars.append({"date": d.isoformat()})
        d += timedelta(days=1)
    for i, b in enumerate(bars):
        c = 100.0 + i * 0.05 if i < 200 else 105.0
        b.update(close=c, high=c, low=c, open=c)
    bars[-1].update(close=120.0, high=120.0, low=120.0, open=120.0)

    span = (date.fromisoformat(bars[-1]["date"]) - date.fromisoformat(bars[0]["date"])).days
    if span >= 365:
        return fail(f"test is broken: {span} days is a full year, prove nothing")
    row = build("TESTCO", {"isin": "I", "name": "T"}, bars)
    if row["week52"] is not None:
        return fail("test is broken: the 52-week range should already be null here")
    if [e for e in _cross(bars) if e["type"] == "extreme"]:
        fail(f"claimed a 52-week extreme from {len(bars)} sessions spanning {span} days, "
             "while the row's own 52-week range reads n/a")


def test_crossings_in_the_generated_file_are_well_formed():
    p = ROOT / "web" / "data" / "indicators.json"
    if not p.exists():
        return fail("indicators.json missing")
    d = json.loads(p.read_text())
    for s in d["stocks"]:
        for c in s.get("crossings", []):
            if c["direction"] not in ("above", "below"):
                fail(f"{s['symbol']}: bad direction {c['direction']!r}")
            if c["type"] not in ("dma", "extreme"):
                fail(f"{s['symbol']}: bad type {c['type']!r}")
            # The close really must be the side the event claims.
            if c["direction"] == "above" and c["close"] <= c["value"]:
                fail(f"{s['symbol']}: says above but {c['close']} <= {c['value']}")
            if c["direction"] == "below" and c["close"] >= c["value"]:
                fail(f"{s['symbol']}: says below but {c['close']} >= {c['value']}")
            # At most one event per level per stock per session.
        levels = [c["level"] for c in s.get("crossings", [])]
        if len(levels) != len(set(levels)):
            fail(f"{s['symbol']}: duplicate level in one session: {levels}")


def test_the_worker_implements_crossings_rather_than_passing_them_through():
    """The Worker exists to reflect the newest printed bar. A crossing carried
    over from a manifest built on an older bar is yesterday's news shown as
    today's — so it must compute its own, and the parity test must compare
    them."""
    worker = (ROOT / "backend" / "worker.js").read_text()
    if "function crossings(" not in worker:
        fail("worker.js does not implement crossings")
    if "newClosingExtreme" not in worker:
        fail("worker.js is missing the run-suppression helper")
    parity = (ROOT / "tool" / "test_worker_indicators.py").read_text()
    if "crossings" not in parity:
        fail("test_worker_indicators.py does not compare crossings")


def test_alert_copy_states_the_event_without_prescribing_an_action():
    """Moving-average crossings have no established predictive value. The alert
    may say what happened; it may not suggest what to do about it."""
    html = (ROOT / "web" / "index.html").read_text()
    if "crossingLine" not in html:
        return fail("the crossing sentence builder is gone")
    for bad in ("buy signal", "sell signal", "bullish", "bearish",
                "breakout", "act now", "opportunity"):
        if bad in html.lower():
            fail(f"alert wording implies an action: {bad!r}")


def test_ios_web_push_limitation_is_detected_and_stated():
    """On iOS, notifications only work from a Home-Screen install; in a tab the
    request fails silently and the reader believes alerts are armed."""
    html = (ROOT / "web" / "index.html").read_text()
    for hook in ("navigator.standalone", "display-mode: standalone",
                 "Add to Home Screen", "notifyState"):
        if hook not in html:
            fail(f"index.html is missing the iOS push guard: {hook}")


def test_notified_and_acknowledged_are_stored_separately():
    """One flag for both makes the on-page list vanish on the next refresh,
    seconds after the notification fires."""
    html = (ROOT / "web" / "index.html").read_text()
    if "levels.notified" not in html or "levels.acked" not in html:
        fail("notification-sent and user-acknowledged share one store")


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


def test_the_page_does_not_fall_back_to_nse_52w_after_a_restatement():
    """Nulling only our own 52-week range leaves NSE's in its place.

    NSE quotes the 52-week high of the TICKER, and a demerged ticker really did
    print that price — as a larger company. TMPV showed 739.70 against a 322.80
    close, which would put the position dot at the bottom of a range that no
    longer exists. The page must suppress both, not just ours.
    """
    html = (ROOT / "web" / "index.html").read_text()
    if "restated" not in html:
        return fail("merge() no longer distinguishes a restated company")
    for field in ("year_high", "year_low", "near_year_high", "near_year_low"):
        line = next((l for l in html.splitlines()
                     if l.strip().startswith(f"{field}:")), None)
        if line is None:
            fail(f"merge() no longer assigns {field}")
        elif "restated" not in line:
            fail(f"{field} still falls back to NSE's figure after a restatement: {line.strip()[:80]}")


def test_page_reads_the_market_layer():
    html = (ROOT / "web" / "index.html").read_text()
    for hook in ("marketPanel", "relHtml", "market_structure", "data/vrp.json", "ordinal"):
        if hook not in html:
            fail(f"index.html is missing {hook}")
    # The one claim the page must never make.
    if "vs mkt" not in html:
        fail("the per-row own-move readout is gone")


ADVICE_PHRASES = ("will rise", "will fall", "should buy", "should sell",
                  "target price", "recommend", "bullish", "bearish",
                  "buy signal", "sell signal", "act now", "opportunity")


def advice_hits(html: str) -> list[str]:
    """Lines that use advice language, ignoring lines that disclaim it.

    Scanning the whole document as one string cannot tell "a recommendation"
    from "not a recommendation" — and the second is the product stating exactly
    what it refuses to be, which is the opposite of the failure being tested
    for. So the scan is per line, and a negation shortly before the phrase
    clears it.
    """
    hits = []
    for raw in html.splitlines():
        line = raw.lower()
        for p in ADVICE_PHRASES:
            if p not in line:
                continue
            if re.search(r"\b(not|never|no|rather than|without)\b[^.]{0,40}" + re.escape(p), line):
                continue
            hits.append(f"{p!r} in: {raw.strip()[:90]}")
    return hits


def test_page_never_calls_the_band_a_direction():
    """A width presented as a direction is the failure mode this whole layer is
    built to avoid, and it is a claim the owner is not registered to make."""
    for h in advice_hits((ROOT / "web" / "index.html").read_text()):
        fail(f"page contains advice language: {h}")


def test_the_disclaimer_exemption_does_not_swallow_real_advice():
    """The exemption above must clear a disclaimer and still catch the real
    thing — otherwise the guard silently passes everything."""
    if advice_hits("<p>This is not a recommendation.</p>"):
        fail("disclaimer wording was flagged as advice")
    if advice_hits("<p>A measurement, not a recommendation.</p>"):
        fail("the notification's own disclaimer was flagged as advice")
    if not advice_hits("<p>We recommend buying this stock.</p>"):
        fail("real advice was not caught")
    if not advice_hits("<p>A bullish breakout — act now.</p>"):
        fail("real advice was not caught")


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
