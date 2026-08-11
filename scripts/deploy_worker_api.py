#!/usr/bin/env python3
"""Deploy backend/worker.js to Cloudflare — without node or wrangler.

The blessed path is `cd backend && npx wrangler deploy`. This script exists
because this machine currently has no node at all, while the Cloudflare API
only needs HTTPS: it bundles routes.js + worker.js into one module, uploads
it, and makes sure the workers.dev route is enabled.

Auth reuses wrangler's own stored OAuth grant (~/Library/Preferences/
.wrangler/config/default.toml). Two things matter and are handled with care:

  1. The access token expires hourly, so it is refreshed on every run.
  2. Cloudflare ROTATES the refresh token on use. The new one must be
     written back, or the next `wrangler` run anywhere on this machine —
     including market-nudge's deploys — fails with a revoked grant.

Run:  python3 scripts/deploy_worker_api.py
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ACCOUNT_ID = "9499bf2dcf09e9d9299d95c6622e4366"
SCRIPT_NAME = "levels-proxy"
# wrangler's public OAuth client id — the same one the CLI itself sends.
WRANGLER_CLIENT_ID = "54d11594-84e4-41aa-b438-e81b8fa78ee7"
TOKEN_URL = "https://dash.cloudflare.com/oauth2/token"
API = "https://api.cloudflare.com/client/v4"

ROOT = Path(__file__).resolve().parent.parent
WRANGLER_TOML = Path.home() / "Library/Preferences/.wrangler/config/default.toml"


def http(url: str, data: bytes | None = None, headers: dict | None = None,
         method: str | None = None) -> tuple[int, bytes]:
    # Cloudflare's own edge 1010-blocks python-urllib's default signature —
    # the same trap NSE and Upstox spring, now on the deploy path itself.
    # Identify as wrangler, which is what this script is standing in for.
    hdrs = {"User-Agent": "wrangler/3.99.0 (deploy_worker_api.py)"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ------------------------------------------------------------------ auth

def fresh_token() -> str:
    src = WRANGLER_TOML.read_text()
    m = re.search(r'refresh_token\s*=\s*"([^"]+)"', src)
    if not m:
        sys.exit("no refresh_token in wrangler config — run `npx wrangler login` once")
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": m.group(1),
        "client_id": WRANGLER_CLIENT_ID,
    }).encode()
    status, raw = http(TOKEN_URL, body,
                       {"Content-Type": "application/x-www-form-urlencoded"}, "POST")
    if status != 200:
        sys.exit(f"token refresh failed ({status}): {raw[:200]!r} — "
                 "the grant may be revoked; re-auth with `npx wrangler login`")
    tok = json.loads(raw)

    # Write the rotated pair back in place, preserving everything else.
    expiry = (datetime.now(timezone.utc)
              + timedelta(seconds=int(tok.get("expires_in", 3600)))
              ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    out = re.sub(r'oauth_token\s*=\s*"[^"]*"', f'oauth_token = "{tok["access_token"]}"', src)
    out = re.sub(r'refresh_token\s*=\s*"[^"]*"',
                 f'refresh_token = "{tok["refresh_token"]}"', out)
    out = re.sub(r'expiration_time\s*=\s*"[^"]*"', f'expiration_time = "{expiry}"', out)
    WRANGLER_TOML.write_text(out)
    print("  token refreshed; rotated refresh_token written back")
    return tok["access_token"]


# ------------------------------------------------------------------ bundle

def bundle() -> str:
    """routes.js + worker.js as one ES module.

    Plain text surgery, not a bundler: strip `export` off routes' members and
    drop worker.js's import of them. Anything cleverer needs node, which is
    the tool this script exists to avoid.
    """
    routes = (ROOT / "backend" / "routes.js").read_text()
    worker = (ROOT / "backend" / "worker.js").read_text()
    routes = re.sub(r"^export (const|function)", r"\1", routes, flags=re.M)
    worker = re.sub(r"^import\s+\{[^}]*\}\s+from\s+'\./routes\.js';\s*\n", "", worker,
                    count=1, flags=re.M)
    out = ("// Bundled from backend/routes.js + backend/worker.js by "
           "scripts/deploy_worker_api.py — do not edit.\n\n" + routes + "\n" + worker)
    if out.count("export default") != 1 or "./routes.js" in out:
        sys.exit("bundle sanity check failed — the import/export surgery missed")
    return out


# ------------------------------------------------------------------ deploy

def deploy(token: str, code: str) -> None:
    boundary = uuid.uuid4().hex
    metadata = json.dumps({"main_module": "worker.js",
                           "compatibility_date": "2026-01-01"})
    parts = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="metadata"\r\n'
        f"Content-Type: application/json\r\n\r\n{metadata}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="worker.js"; filename="worker.js"\r\n'
        f"Content-Type: application/javascript+module\r\n\r\n{code}\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    status, raw = http(
        f"{API}/accounts/{ACCOUNT_ID}/workers/scripts/{SCRIPT_NAME}", parts,
        {"Authorization": f"Bearer {token}",
         "Content-Type": f"multipart/form-data; boundary={boundary}"}, "PUT")
    body = json.loads(raw)
    if status != 200 or not body.get("success"):
        sys.exit(f"upload failed ({status}): {json.dumps(body.get('errors'))[:400]}")
    print(f"  uploaded {SCRIPT_NAME} ({len(code)//1024} KB)")

    # workers.dev route — POST is idempotent here.
    status, raw = http(
        f"{API}/accounts/{ACCOUNT_ID}/workers/scripts/{SCRIPT_NAME}/subdomain",
        json.dumps({"enabled": True, "previews_enabled": False}).encode(),
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, "POST")
    if status != 200:
        sys.exit(f"subdomain enable failed ({status}): {raw[:200]!r}")

    status, raw = http(f"{API}/accounts/{ACCOUNT_ID}/workers/subdomain",
                       headers={"Authorization": f"Bearer {token}"})
    sub = json.loads(raw)["result"]["subdomain"]
    url = f"https://{SCRIPT_NAME}.{sub}.workers.dev"
    print(f"  live at {url}")

    # Verify with real requests; first hits can 404 while the route propagates.
    for path, expect in [("/health", "ok"), ("/api/live?symbols=RELIANCE", "RELIANCE")]:
        for attempt in range(6):
            status, raw = http(url + path)
            if status == 200 and expect in raw.decode("utf-8", "replace"):
                print(f"  verify {path}: 200 ✓")
                break
            time.sleep(5)
        else:
            sys.exit(f"verify {path}: last status {status}: {raw[:200]!r}")


if __name__ == "__main__":
    print("deploying worker via Cloudflare API (no node needed)")
    deploy(fresh_token(), bundle())
