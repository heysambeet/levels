#!/usr/bin/env python3
"""Local twin of backend/worker.js, plus a static server for web/.

Run:  python3 tool/dev_proxy.py           # http://localhost:8900

It parses the route table out of backend/routes.js rather than restating it,
so the allowlist and the cache TTLs cannot drift from the deployed Worker.
That drift shipped a frozen price in a previous project;
tool/test_proxy_parity.py fails if this file can no longer read the table.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import urllib.error
import urllib.parse
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
ROUTES_JS = ROOT / "backend" / "routes.js"


def load_routes() -> tuple[dict, str]:
    """Read the shared route table. Deliberately parsed, never duplicated."""
    src = ROUTES_JS.read_text()
    ua = re.search(r"BROWSER_UA\s*=\s*\n?\s*'([^']+)'", src)
    routes = {}
    for name in ("live", "indicators", "news"):
        block = re.search(rf"\b{name}:\s*\{{(.*?)\n  \}},", src, re.S)
        if not block:
            raise RuntimeError(f"could not parse route '{name}' from {ROUTES_JS}")
        b = block.group(1)
        path = re.search(r"path:\s*'([^']+)'", b)
        # (?<!_) so `index_upstream:` does not satisfy the `upstream:` match.
        upstream = re.search(r"(?<!_)\bupstream:\s*\n?\s*'([^']+)'", b)
        ttl = re.search(r"ttl:\s*(\d+)", b)
        if not (path and upstream and ttl):
            raise RuntimeError(f"route '{name}' is missing path/upstream/ttl")
        routes[name] = {"path": path.group(1), "upstream": upstream.group(1),
                        "ttl": int(ttl.group(1))}
        extra = re.search(r"index_upstream:\s*\n?\s*'([^']+)'", b)
        if extra:
            routes[name]["index_upstream"] = extra.group(1)
    if not ua:
        raise RuntimeError("could not parse BROWSER_UA")
    return routes, ua.group(1)


ROUTES, BROWSER_UA = load_routes()
_cache: dict[str, tuple[float, bytes]] = {}
_lock = threading.Lock()


def cached_get(url: str, ttl: int) -> bytes:
    # Deliberately no retry here, unlike the Worker: Google News's 5xx flap
    # is specific to Cloudflare egress. Residential egress was measured clean
    # (157 requests, zero failures), so a retry loop would only hide real
    # local errors.
    now = time.time()
    with _lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < ttl:
            return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
    with _lock:
        _cache[url] = (now, body)
    return body


def parse_rss(xml: str, limit: int = 25) -> list[dict]:
    """Mirror of parseRss in backend/routes.js."""
    def unescape(s: str) -> str:
        s = re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", s)
        for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                     ("&#39;", "'"), ("&apos;", "'"), ("&amp;", "&")):
            s = s.replace(a, b)
        return s

    out = []
    for block in re.findall(r"<item>([\s\S]*?)</item>", xml)[:limit]:
        def pick(tag: str) -> str:
            m = re.search(rf"<{tag}[^>]*>([\s\S]*?)</{tag}>", block)
            return unescape(m.group(1).strip()) if m else ""
        title = pick("title")
        if not title:
            continue
        out.append({"title": title, "source": pick("source"),
                    "published": pick("pubDate"), "link": pick("link")})
    return out


def shape_live(payload: dict, want: list[str] | None = None,
               index_payload: dict | None = None) -> dict:
    rows = (payload.get("data") or {}).get("data")
    if not isinstance(rows, list):
        raise RuntimeError("unexpected NSE shape")
    stocks = [r for r in rows if r.get("series") is not None]
    if len(stocks) < 100:
        raise RuntimeError(f"only {len(stocks)} constituents")
    universe = len(rows)

    # The header shows the NIFTY 50; constituents ride in on the 500, whose
    # own index row is the 500. Fall back to that row if the second call
    # failed, rather than blanking the header.
    idx_rows = (index_payload or {}).get("data", {}).get("data")
    index = None
    if isinstance(idx_rows, list):
        index = next((r for r in idx_rows if r.get("series") is None), None)
    if index is None:
        index = next((r for r in rows if r.get("series") is None), None)
        header = payload["data"]
    else:
        header = index_payload["data"]

    # Filter to the caller's watchlist so a phone downloads ~30 rows, not 500.
    # Symbols the index does not carry are reported rather than dropped.
    missing: list[str] = []
    if want:
        have = {s["symbol"] for s in stocks}
        missing = [s for s in want if s not in have]
        stocks = [s for s in stocks if s["symbol"] in set(want)]

    return {
        "as_of": header.get("timestamp"),
        "market_status": (header.get("marketStatus") or {}).get("marketStatus"),
        "breadth": header.get("aduCount"),
        "universe": universe,
        "missing": missing,
        "index": index and {"symbol": index["symbol"], "last": index["lastPrice"],
                            "change": index["change"], "pct": index["pChange"]},
        "stocks": [
            {"symbol": s["symbol"], "name": s.get("companyName"), "last": s["lastPrice"],
             "change": s["change"], "pct": s["pChange"], "prev_close": s["previousClose"],
             "open": s["open"], "day_high": s["dayHigh"], "day_low": s["dayLow"],
             "year_high": s["yearHigh"], "year_low": s["yearLow"],
             "near_year_high": s.get("nearWKH"), "near_year_low": s.get("nearWKL"),
             "volume": s.get("totalTradedVolume")}
            for s in stocks
        ],
    }


_ind_cache: tuple[float, dict] | None = None


def local_indicators() -> dict:
    """Recompute the daily layer locally, cached for the route's own TTL."""
    global _ind_cache
    ttl = ROUTES["indicators"]["ttl"]
    if _ind_cache and time.time() - _ind_cache[0] < ttl:
        return _ind_cache[1]

    from build_indicators import build, fetch_candles
    from symbols import load_instrument_map, load_watchlist, resolve

    wl = load_watchlist()
    rows, failed = [], []
    for t in resolve(wl["symbols"], load_instrument_map()):
        try:
            rows.append(build(t["symbol"], t, fetch_candles(t["isin"])))
        except Exception:
            failed.append(t["symbol"])
        time.sleep(0.06)          # serial; parallel fan-out earns a 573s ban
    out = {
        "generated_at": datetime.now(IST).isoformat(timespec="seconds"),
        "as_of": max((r["as_of"] for r in rows), default=None),
        "count": len(rows),
        "source": "dev_proxy",
        "watchlist_max": wl["max"],
        "placeholder_watchlist": wl["placeholder"],
        "failed": failed,
        "stocks": sorted(rows, key=lambda r: r["symbol"]),
    }
    _ind_cache = (time.time(), out)
    return out


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB), **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")

    def _send(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        try:
            if url.path == ROUTES["live"]["path"]:
                raw = cached_get(ROUTES["live"]["upstream"], ROUTES["live"]["ttl"])
                want = [s.strip().upper()
                        for s in urllib.parse.parse_qs(url.query).get("symbols", [""])[0].split(",")
                        if s.strip()]
                try:
                    idx_raw = cached_get(ROUTES["live"]["index_upstream"], ROUTES["live"]["ttl"])
                    idx = json.loads(idx_raw)
                except Exception:
                    idx = None      # header falls back to the 500's index row
                return self._send(shape_live(json.loads(raw), want, idx))
            if url.path == ROUTES["news"]["path"]:
                q = urllib.parse.parse_qs(url.query).get("q", [""])[0]
                if not q:
                    return self._send({"error": "q required"}, 400)
                raw = cached_get(ROUTES["news"]["upstream"] + urllib.parse.quote(q),
                                 ROUTES["news"]["ttl"])
                items = parse_rss(raw.decode("utf-8", "replace"))
                return self._send({"query": q, "count": len(items), "items": items})
            if url.path == ROUTES["indicators"]["path"]:
                # Served from build_indicators.py — the reference
                # implementation — while the deployed Worker recomputes the
                # same thing in JavaScript. Dev therefore exercises the same
                # page code path as production, and any disagreement between
                # the two implementations is what test_worker_indicators.py
                # exists to catch.
                return self._send(local_indicators())
            if url.path == "/health":
                return self._send({"ok": True, "routes": list(ROUTES)})
        except urllib.error.HTTPError as e:
            return self._send({"error": f"upstream {e.code}"}, 502)
        except Exception as e:
            return self._send({"error": str(e)}, 500)
        return super().do_GET()


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8900
    print(f"serving {WEB} on http://localhost:{port}")
    for name, r in ROUTES.items():
        print(f"  {r['path']:<12} -> {r['upstream'][:58]}…  (cache {r['ttl']}s)")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
