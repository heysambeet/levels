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
import urllib.error
import urllib.parse
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
ROUTES_JS = ROOT / "backend" / "routes.js"


def load_routes() -> tuple[dict, str]:
    """Read the shared route table. Deliberately parsed, never duplicated."""
    src = ROUTES_JS.read_text()
    ua = re.search(r"BROWSER_UA\s*=\s*\n?\s*'([^']+)'", src)
    routes = {}
    for name in ("live", "news"):
        block = re.search(rf"\b{name}:\s*\{{(.*?)\n  \}},", src, re.S)
        if not block:
            raise RuntimeError(f"could not parse route '{name}' from {ROUTES_JS}")
        b = block.group(1)
        path = re.search(r"path:\s*'([^']+)'", b)
        upstream = re.search(r"upstream:\s*\n?\s*'([^']+)'", b)
        ttl = re.search(r"ttl:\s*(\d+)", b)
        if not (path and upstream and ttl):
            raise RuntimeError(f"route '{name}' is missing path/upstream/ttl")
        routes[name] = {"path": path.group(1), "upstream": upstream.group(1),
                        "ttl": int(ttl.group(1))}
    if not ua:
        raise RuntimeError("could not parse BROWSER_UA")
    return routes, ua.group(1)


ROUTES, BROWSER_UA = load_routes()
_cache: dict[str, tuple[float, bytes]] = {}
_lock = threading.Lock()


def cached_get(url: str, ttl: int) -> bytes:
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


def shape_live(payload: dict) -> dict:
    rows = (payload.get("data") or {}).get("data")
    if not isinstance(rows, list):
        raise RuntimeError("unexpected NSE shape")
    stocks = [r for r in rows if r.get("series") is not None]
    index = next((r for r in rows if r.get("series") is None), None)
    if len(stocks) < 45:
        raise RuntimeError(f"only {len(stocks)} constituents")
    return {
        "as_of": payload["data"].get("timestamp"),
        "market_status": (payload["data"].get("marketStatus") or {}).get("marketStatus"),
        "breadth": payload["data"].get("aduCount"),
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
                return self._send(shape_live(json.loads(raw)))
            if url.path == ROUTES["news"]["path"]:
                q = urllib.parse.parse_qs(url.query).get("q", [""])[0]
                if not q:
                    return self._send({"error": "q required"}, 400)
                raw = cached_get(ROUTES["news"]["upstream"] + urllib.parse.quote(q),
                                 ROUTES["news"]["ttl"])
                items = parse_rss(raw.decode("utf-8", "replace"))
                return self._send({"query": q, "count": len(items), "items": items})
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
