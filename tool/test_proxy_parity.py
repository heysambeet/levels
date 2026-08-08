#!/usr/bin/env python3
"""The deployed Worker and the local dev proxy must not drift.

They serve the same routes from the same upstreams with the same cache TTLs.
In a previous project the equivalent pair drifted and shipped a frozen price
and a broken calendar — the failure is silent, because each half works
perfectly on its own.

Both read backend/routes.js, so this test's real job is to prove that sharing
still holds: that dev_proxy can parse the table, that worker.js references it
rather than restating it, and that neither has grown a hardcoded upstream.

Run: python3 tool/test_proxy_parity.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
ROUTES_JS = ROOT / "backend" / "routes.js"
WORKER_JS = ROOT / "backend" / "worker.js"

FAILURES: list[str] = []


def fail(m: str) -> None:
    FAILURES.append(m)


def test_dev_proxy_parses_the_shared_table():
    from dev_proxy import ROUTES, BROWSER_UA
    for name in ("live", "news"):
        if name not in ROUTES:
            fail(f"dev_proxy did not parse route '{name}'")
            continue
        r = ROUTES[name]
        if not r["upstream"].startswith("https://"):
            fail(f"{name}: upstream is not https ({r['upstream']!r})")
        if r["ttl"] <= 0:
            fail(f"{name}: non-positive ttl {r['ttl']}")
        if not r["path"].startswith("/api/"):
            fail(f"{name}: path {r['path']!r} is not under /api/")
    if "Mozilla/" not in BROWSER_UA:
        fail(f"BROWSER_UA is not browser-like: {BROWSER_UA!r}")


def test_worker_imports_rather_than_restates():
    src = WORKER_JS.read_text()
    if "from './routes.js'" not in src:
        fail("worker.js does not import the shared route table")
    for token in ("ROUTES", "BROWSER_UA"):
        if token not in src:
            fail(f"worker.js does not use {token}")


def test_no_upstream_url_is_hardcoded_outside_the_table():
    """An upstream literal anywhere but routes.js is exactly how drift starts."""
    table = ROUTES_JS.read_text()
    hosts = set(re.findall(r"https://([a-z0-9.-]+)", table))
    for path in (WORKER_JS, ROOT / "tool" / "dev_proxy.py"):
        src = path.read_text()
        for host in re.findall(r"https://([a-z0-9.-]+)", src):
            if host in hosts:
                fail(f"{path.name}: hardcodes upstream host {host!r} — it belongs in routes.js")


def test_dev_proxy_and_worker_shape_the_same_response():
    """Both must expose the same field names, or the page works against one
    and silently shows blanks against the other."""
    worker = WORKER_JS.read_text()
    dev = (ROOT / "tool" / "dev_proxy.py").read_text()
    fields = ["symbol", "last", "change", "pct", "prev_close", "open", "day_high",
              "day_low", "year_high", "year_low", "near_year_high", "near_year_low", "volume"]
    for f in fields:
        in_worker = re.search(rf"\b{f}:", worker) is not None
        in_dev = f'"{f}"' in dev
        if in_worker != in_dev:
            fail(f"field '{f}' present in {'worker' if in_worker else 'dev_proxy'} only")
    for env in ("as_of", "market_status", "breadth", "index", "stocks"):
        if env not in worker or env not in dev:
            fail(f"envelope key '{env}' missing from one side")


def test_rss_parsers_agree_on_a_real_document():
    """The two parseRss implementations are in different languages, so the
    only way to know they agree is to run one on a document with the shapes
    that actually appear: CDATA, entities, and a trailing source element."""
    from dev_proxy import parse_rss
    xml = """<rss><channel>
      <item><title>Why &amp; how ABC fell 5%</title><source url="x">CNBC TV18</source>
        <pubDate>Fri, 07 Aug 2026 10:01:00 GMT</pubDate><link>https://e.com/a</link></item>
      <item><title><![CDATA[Bajaj Finance's ₹33K cr fall]]></title><source>Business Standard</source>
        <pubDate>Fri, 07 Aug 2026 05:44:00 GMT</pubDate><link>https://e.com/b</link></item>
      <item><description>no title</description></item>
    </channel></rss>"""
    got = parse_rss(xml)
    if len(got) != 2:
        fail(f"parse_rss returned {len(got)} items, want 2 (the untitled one must be dropped)")
        return
    if got[0]["title"] != "Why & how ABC fell 5%":
        fail(f"entity decoding wrong: {got[0]['title']!r}")
    if got[1]["title"] != "Bajaj Finance's ₹33K cr fall":
        fail(f"CDATA handling wrong: {got[1]['title']!r}")
    if got[0]["source"] != "CNBC TV18":
        fail(f"source with attributes not parsed: {got[0]['source']!r}")


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
