#!/usr/bin/env python3
"""Catch a page that cannot run at all.

Every other test here checks numbers. None of them opened the page, so a
plain JavaScript syntax error shipped to production and left the site stuck
on "Loading…" while all six proxy tests passed — the redeclared `const` that
prompted this file.

Deliberately not a browser test: no headless Chrome to install, no CI to
configure. It parses the page's own script for the mistakes that stop a
classic script dead before the first line executes.

Run: python3 tool/test_page_loads.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "web" / "index.html"

FAILURES: list[str] = []


def fail(m: str) -> None:
    FAILURES.append(m)


def script_body(src: str) -> str:
    m = re.search(r"<script>(.*?)</script>", src, re.S)
    if not m:
        fail("no <script> block found in the page")
        return ""
    return m.group(1)


def strip_noise(js: str) -> str:
    """Blank out strings, template literals and comments so declarations
    inside them are not mistaken for real ones.

    Order matters and is the whole subtlety: strings go FIRST. Stripping
    comments first treats the `//` in every `https://` URL as the start of a
    line comment and eats the rest of the line — which collapsed an 18,000
    character script to 1,000 and made this file's own balance check
    nonsense before it was corrected.

    Template literals are replaced by a token of matching brace count, since
    `${...}` interpolations legitimately contain braces that must not count
    toward the balance of the surrounding code.
    """
    js = re.sub(r"`(?:\\.|[^`\\])*`", "TEMPLATE", js, flags=re.S)
    js = re.sub(r"'(?:\\.|[^'\n\\])*'", "STR", js)
    js = re.sub(r'"(?:\\.|[^"\n\\])*"', "STR", js)
    js = re.sub(r"/\*.*?\*/", " ", js, flags=re.S)
    js = re.sub(r"(?m)//.*$", " ", js)
    return js


def top_level_blocks(js: str):
    """Yield (name, body) for each function declared at depth 0, plus the
    module top level itself as ('<top>', ...). Enough to catch a redeclaration
    inside one function scope, which is the failure that actually bit."""
    depth = 0
    i = 0
    starts: list[tuple[str, int, int]] = []
    out = []
    top_chunks = []
    last_top = 0
    while i < len(js):
        c = js[i]
        if c == "{":
            if depth == 0:
                m = re.search(r"function\s+([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*$", js[:i])
                starts.append((m.group(1) if m else "<block>", i, len(out)))
                top_chunks.append(js[last_top:i])
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and starts:
                name, start, _ = starts.pop()
                out.append((name, js[start + 1:i]))
                last_top = i + 1
        i += 1
    top_chunks.append(js[last_top:])
    out.append(("<top level>", "".join(top_chunks)))
    return out


def test_no_redeclared_binding_in_a_scope():
    js = strip_noise(script_body(PAGE.read_text()))
    if not js:
        return
    for name, body in top_level_blocks(js):
        # Only depth-0 declarations within this block; nested braces are
        # separate scopes and may legitimately shadow.
        seen: dict[str, int] = {}
        depth = 0
        for line in body.splitlines():
            stripped = line.strip()
            if depth == 0:
                for m in re.finditer(r"\b(?:const|let)\s+([A-Za-z_$][\w$]*)", stripped):
                    ident = m.group(1)
                    seen[ident] = seen.get(ident, 0) + 1
                    if seen[ident] > 1:
                        fail(f"{name}: '{ident}' declared more than once in the same scope "
                             f"— this throws before any code runs")
            depth += line.count("{") - line.count("}")
            depth = max(depth, 0)


def test_braces_and_parens_balance():
    js = strip_noise(script_body(PAGE.read_text()))
    for open_c, close_c in (("{", "}"), ("(", ")"), ("[", "]")):
        if js.count(open_c) != js.count(close_c):
            fail(f"unbalanced {open_c}{close_c}: {js.count(open_c)} vs {js.count(close_c)}")


def test_required_hooks_exist():
    """The ids and handlers the script writes into must be present in the
    markup, or the page renders to nowhere without erroring."""
    src = PAGE.read_text()
    for hook in ('id="list"', 'id="status"', 'id="idx"', 'id="chips"', 'id="foot"', 'id="q"'):
        if hook not in src:
            fail(f"markup is missing {hook}, which the script writes into")


def test_api_base_is_origin_aware():
    """Hosted, the page and the proxy are different origins; on localhost they
    are the same. Hardcoding either one breaks the other."""
    src = PAGE.read_text()
    if "location.hostname" not in src or "workers.dev" not in src:
        fail("the API base no longer switches between localhost and the Worker")


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
