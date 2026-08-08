#!/usr/bin/env python3
"""Watchlist loading and symbol resolution.

The watchlist is an arbitrary set of NSE-listed companies. It is deliberately
*not* an index: names outside the NIFTY 50 belong here, and NIFTY 50 members
may be absent. Nothing may assume index membership.

Resolution goes through Upstox's published instrument master rather than a
hardcoded table, because symbols genuinely disappear — TATAMOTORS ceased to
exist after its demerger, and LTIM resolves to nothing today. A stale table
fails by silently dropping a company, which is the one failure a watchlist
must never have.
"""

from __future__ import annotations

import gzip
import json
import sys
import time
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

TOOL_DIR = Path(__file__).resolve().parent
WATCHLIST_PATH = TOOL_DIR / "watchlist.json"
INSTRUMENT_CACHE = TOOL_DIR / ".instruments_cache.json"
CACHE_MAX_AGE_S = 20 * 3600          # a trading day; new listings appear overnight


class UnresolvedSymbols(Exception):
    """Raised rather than skipping. A watchlist that quietly shrinks is worse
    than one that refuses to build."""


def load_watchlist(path: Path = WATCHLIST_PATH) -> dict:
    wl = json.loads(path.read_text())
    symbols = [s.strip().upper() for s in wl.get("symbols", []) if s and s.strip()]
    seen, ordered = set(), []
    for s in symbols:
        if s in seen:
            print(f"  WARN duplicate symbol in watchlist: {s}", file=sys.stderr)
            continue
        seen.add(s)
        ordered.append(s)
    cap = int(wl.get("max", 30))
    if len(ordered) > cap:
        raise ValueError(f"watchlist has {len(ordered)} symbols, over its own max of {cap}")
    if not ordered:
        raise ValueError("watchlist is empty")
    return {"symbols": ordered, "max": cap, "placeholder": bool(wl.get("placeholder"))}


def _fetch_instruments() -> list[dict]:
    req = urllib.request.Request(INSTRUMENTS_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(gzip.decompress(resp.read()))


def load_instrument_map(force: bool = False) -> dict[str, dict]:
    """symbol -> {isin, name}. Cached; the file is ~2 MB and rarely changes."""
    if not force and INSTRUMENT_CACHE.exists():
        age = time.time() - INSTRUMENT_CACHE.stat().st_mtime
        if age < CACHE_MAX_AGE_S:
            return json.loads(INSTRUMENT_CACHE.read_text())
    try:
        rows = _fetch_instruments()
    except Exception as e:
        if INSTRUMENT_CACHE.exists():
            print(f"  WARN instrument master fetch failed ({e}); using stale cache", file=sys.stderr)
            return json.loads(INSTRUMENT_CACHE.read_text())
        raise
    out = {}
    for r in rows:
        if r.get("segment") == "NSE_EQ" and r.get("instrument_type") == "EQ" and r.get("isin"):
            out[r["trading_symbol"]] = {"isin": r["isin"], "name": r.get("name", "")}
    if len(out) < 1000:
        raise RuntimeError(f"instrument master looks wrong: only {len(out)} equities")
    INSTRUMENT_CACHE.write_text(json.dumps(out))
    return out


def resolve(symbols: list[str], instruments: dict[str, dict] | None = None) -> list[dict]:
    """Attach ISIN and registered name to each symbol.

    Raises on any unresolved symbol. Typos and retired listings look identical
    from here, and both need a human — so the build stops and names them
    instead of producing a shorter watchlist than the one that was asked for.
    """
    instruments = instruments if instruments is not None else load_instrument_map()
    resolved, missing = [], []
    for s in symbols:
        hit = instruments.get(s)
        if not hit:
            missing.append(s)
            continue
        resolved.append({"symbol": s, "isin": hit["isin"], "registered_name": hit["name"]})
    if missing:
        raise UnresolvedSymbols(
            f"{len(missing)} symbol(s) not listed on NSE as tradable equities: {', '.join(missing)}. "
            "Check for a rename, a demerger, or a typo — e.g. TATAMOTORS is now TMPV, "
            "and LTIM no longer resolves."
        )
    return resolved


def titlecase(name: str) -> str:
    """NSE and Upstox both SHOUT. Keep short tokens that are genuinely acronyms."""
    keep = {"NSE", "BSE", "SBI", "TCS", "HDFC", "ICICI", "ITC", "L&T", "NTPC", "ONGC",
            "BPCL", "IOC", "HCL", "IT", "PVR", "GAIL", "IDFC", "IRCTC", "IRFC", "HAL",
            "TVS", "MRF", "UPL", "ABB", "PB", "AU", "RBL", "IIFL", "JSW", "LTD", "LIC"}
    out = []
    for w in (name or "").split():
        bare = w.strip(".,").upper()
        if bare in keep:
            out.append(bare if bare != "LTD" else "Ltd")
        elif len(w) <= 2 and w.isupper():
            out.append(w)
        else:
            out.append(w.capitalize())
    return " ".join(out)


if __name__ == "__main__":
    wl = load_watchlist()
    print(f"watchlist: {len(wl['symbols'])} symbols (max {wl['max']})"
          f"{'  [PLACEHOLDER]' if wl['placeholder'] else ''}")
    try:
        rows = resolve(wl["symbols"])
    except UnresolvedSymbols as e:
        print(f"UNRESOLVED: {e}", file=sys.stderr)
        sys.exit(1)
    for r in rows:
        print(f"  {r['symbol']:12s} {r['isin']:14s} {titlecase(r['registered_name'])}")
