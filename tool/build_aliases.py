#!/usr/bin/env python3
"""Generate the news alias table for whatever the watchlist currently names.

Two things decide whether per-stock news is useful rather than merely present:

  press_name — what the media actually calls the company. Registered names
  silently return nothing: "Sun Pharmaceutical" yields 0 items where
  "Sun Pharma" yields 16, and "Wipro Ltd" yields 0 where "Wipro" yields 6.

  block — negative keywords for names that collide with something else.
  Measured live: Titan returns Saturn's moon and an anime; Trent returns a
  cricket match and a WWII museum; Shriram returns a film star's husband.

Anything not hand-curated falls back to the registered name with the corporate
suffix stripped, which is right often enough — but a new watchlist entry with
an ambiguous name should get an entry in CURATED below.

Run:  python3 tool/build_aliases.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from symbols import load_instrument_map, load_watchlist, resolve, titlecase  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "web" / "data" / "news_aliases.json"

# symbol -> (press name, extra aliases, negative keywords)
CURATED: dict[str, tuple[str, list[str], list[str]]] = {
    "RELIANCE":   ("Reliance Industries", ["RIL"], ["reliance power", "reliance infra",
                                                    "reliance communications", "reliance capital",
                                                    "reliance home", "anil ambani"]),
    "TCS":        ("TCS", ["Tata Consultancy"], ["nashik", "harassment", "molest", "arrest"]),
    "HDFCBANK":   ("HDFC Bank", [], ["hdfc life", "hdfc amc", "hdfc ergo"]),
    "ICICIBANK":  ("ICICI Bank", [], ["icici lombard", "icici pru", "icici securities"]),
    "KOTAKBANK":  ("Kotak Mahindra Bank", ["Kotak Bank"], []),
    "AXISBANK":   ("Axis Bank", [], ["axis amc", "axis max"]),
    "SBIN":       ("SBI", ["State Bank of India"], ["sbi life", "sbi card", "sbi mutual", "sbi amc"]),
    "SBILIFE":    ("SBI Life", [], []),
    "HDFCLIFE":   ("HDFC Life", [], []),
    "BAJFINANCE": ("Bajaj Finance", [], ["bajaj auto", "bajaj finserv", "bajaj housing"]),
    "BAJAJFINSV": ("Bajaj Finserv", [], ["bajaj finance", "bajaj auto"]),
    "BAJAJ-AUTO": ("Bajaj Auto", [], ["bajaj finance", "bajaj finserv"]),
    "SUNPHARMA":  ("Sun Pharma", ["Sun Pharmaceutical"], ["sun tv"]),
    "DRREDDY":    ("Dr Reddys", ["Dr Reddy's"], []),
    "HCLTECH":    ("HCL Tech", ["HCL Technologies"], ["hcl infosystems"]),
    "LT":         ("Larsen & Toubro", ["L&T"], ["lt foods", "l&t finance", "ltimindtree"]),
    "M&M":        ("Mahindra & Mahindra", ["M&M"], ["tech mahindra", "mahindra logistics",
                                                    "mahindra lifespace", "mahindra holidays"]),
    "MARUTI":     ("Maruti Suzuki", ["Maruti"], []),
    "ULTRACEMCO": ("UltraTech Cement", ["UltraTech"], []),
    "HINDUNILVR": ("Hindustan Unilever", ["HUL"], []),
    "ASIANPAINT": ("Asian Paints", [], []),
    "TITAN":      ("Titan Company", ["Titan"], ["saturn", "moon", "methane", "attack on titan",
                                                "anime", "titanic", "titan submersible"]),
    "TRENT":      ("Trent", ["Trent Ltd", "Westside", "Zudio"], ["cricket", "the hundred",
                                                                 "trent bridge", "trent park",
                                                                 "museum", "boult", "rockets"]),
    "NESTLEIND":  ("Nestle India", [], []),
    "TATACONSUM": ("Tata Consumer", [], []),
    "TATASTEEL":  ("Tata Steel", [], []),
    "TATAMOTORS": ("Tata Motors", [], []),
    "TMPV":       ("Tata Motors", ["TMPV", "Tata Motors Passenger"], ["tmcv", "commercial vehicles"]),
    "TMCV":       ("Tata Motors Commercial", ["TMCV"], ["tmpv", "passenger vehicles"]),
    "JSWSTEEL":   ("JSW Steel", [], ["jsw energy", "jsw infra", "jsw paints"]),
    "HINDALCO":   ("Hindalco", [], []),
    "GRASIM":     ("Grasim", ["Grasim Industries"], []),
    "SHRIRAMFIN": ("Shriram Finance", [], ["shriram nene", "madhuri", "asset management",
                                           "general insurance", "properties", "tnpl"]),
    "JIOFIN":     ("Jio Financial", [], ["reliance jio", "jio platforms"]),
    "BHARTIARTL": ("Bharti Airtel", ["Airtel"], ["airtel africa"]),
    "ADANIENT":   ("Adani Enterprises", [], ["adani ports", "adani green", "adani power",
                                             "adani energy", "adani wilmar"]),
    "ADANIPORTS": ("Adani Ports", ["APSEZ"], ["adani enterprises", "adani green", "adani power"]),
    "APOLLOHOSP": ("Apollo Hospitals", [], ["apollo tyres", "apollo micro"]),
    "COALINDIA":  ("Coal India", [], []),
    "POWERGRID":  ("Power Grid", [], []),
    "WIPRO":      ("Wipro", [], ["wipro consumer care", "wipro enterprises"]),
    "TECHM":      ("Tech Mahindra", [], ["mahindra & mahindra"]),
    "INFY":       ("Infosys", [], []),
    "ITC":        ("ITC", [], ["itc hotels"]),
    "NTPC":       ("NTPC", [], ["ntpc green"]),
    "ETERNAL":    ("Eternal", ["Zomato", "Blinkit"], ["eternal sunshine", "eternals"]),
    "BEL":        ("Bharat Electronics", ["BEL"], ["bel air", "bella"]),
    "INDIGO":     ("IndiGo", ["InterGlobe Aviation"], ["indigo paints"]),
    "MAXHEALTH":  ("Max Healthcare", [], ["max financial", "max life"]),
    "CIPLA":      ("Cipla", [], []),
    "DIVISLAB":   ("Divis Labs", ["Divi's"], []),
    # Common non-index additions
    "DMART":      ("Avenue Supermarts", ["DMart", "D-Mart"], []),
    "HAL":        ("Hindustan Aeronautics", ["HAL"], ["hal 9000"]),
    "POLICYBZR":  ("PB Fintech", ["Policybazaar"], []),
    "PAYTM":      ("Paytm", ["One 97"], []),
    "NYKAA":      ("Nykaa", ["FSN E-Commerce"], []),
    "IRCTC":      ("IRCTC", [], []),
    "IRFC":       ("IRFC", ["Indian Railway Finance"], []),
    "SUZLON":     ("Suzlon", [], []),
    "TATAPOWER":  ("Tata Power", [], []),
    "DIXON":      ("Dixon Technologies", ["Dixon"], []),
    "POLYCAB":    ("Polycab", [], []),
    "LODHA":      ("Lodha", ["Macrotech"], []),
    "PERSISTENT": ("Persistent Systems", [], []),
    "COFORGE":    ("Coforge", [], []),
    "MOTHERSON":  ("Samvardhana Motherson", ["Motherson"], []),
    "TVSMOTOR":   ("TVS Motor", [], []),
    "PIDILITIND": ("Pidilite", [], []),
    "HAVELLS":    ("Havells", [], []),
}

SUFFIX = re.compile(r"\s+(LIMITED|LTD\.?|LTD|CORPORATION|CORP\.?|INDIA|COMPANY|CO\.?)\s*$", re.I)


def press_name(symbol: str, registered: str) -> str:
    name = titlecase(registered)
    prev = None
    while prev != name:                      # "X India Ltd" needs two passes
        prev = name
        name = SUFFIX.sub("", name).strip()
    return name or symbol


def main() -> int:
    wl = load_watchlist()
    rows = resolve(wl["symbols"], load_instrument_map())
    out, uncurated = [], []
    for r in rows:
        sym = r["symbol"]
        if sym in CURATED:
            press, extra, block = CURATED[sym]
        else:
            press, extra, block = press_name(sym, r["registered_name"]), [], []
            uncurated.append(sym)
        aliases = sorted({a for a in ([press, sym] + extra) if a})
        out.append({"symbol": sym, "press_name": press, "aliases": aliases, "block": block})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "note": "press_name drives the search query; aliases must appear in the headline; "
                "block drops known wrong subjects",
        "stocks": out,
    }, indent=1) + "\n")
    print(f"wrote {OUT} for {len(out)} stocks")
    if uncurated:
        print(f"  derived (no curated press name — check for name collisions): {', '.join(uncurated)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
