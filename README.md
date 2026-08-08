# Levels

A watchlist of up to **30 Indian companies** on one page, each showing where
it sits against its 50/100/200-day averages and its 52-week range, with RSI —
and the news that explains the move.

**The watchlist is not an index.** Any NSE-listed company may appear, and
NIFTY 50 members may be absent. Membership lives in
[`tool/watchlist.json`](tool/watchlist.json) and nothing else may assume it.

The question it answers is narrow on purpose: **something moved, why?**
See the move, see the levels it crossed, see the reason. Anything that does
not serve that loop stays out.

Personal, mobile-web, ₹0/month. Not SEBI-registered, and it reports
measurements rather than conclusions — see [Not advice](#not-advice).

> Working title. The repository name is not final, and neither is this one.

---

## Run it

```bash
python3 tool/build_aliases.py        # after any watchlist change
python3 tool/build_indicators.py     # once after each close — 30 stocks, ~5s
python3 tool/dev_proxy.py            # http://localhost:8900
```

### Changing the watchlist

Edit `symbols` in `tool/watchlist.json` (max 30), then re-run the two build
steps above. Symbols are NSE trading symbols. If one does not resolve the
build **stops and names it** rather than quietly returning a shorter list —
that is the whole point, since a watchlist losing a company unnoticed is the
failure that matters. Companies with ambiguous names should get an entry in
`CURATED` in `tool/build_aliases.py`, or their news will be noisy.

The proxy also serves `web/`, so that one command is the whole local setup.

```bash
python3 tool/test_indicators.py --live    # --live also cross-checks against NSE
python3 tool/test_proxy_parity.py
```

## How it is put together

| Piece | What it does |
|---|---|
| `tool/watchlist.json` | **The companies tracked.** The one place membership is defined |
| `tool/symbols.py` | Watchlist loading and symbol → ISIN resolution |
| `tool/build_aliases.py` | Press-style names and negative keywords for the news query |
| `tool/build_indicators.py` | Daily job. Pulls candles, computes DMA/RSI/52-week, writes `web/data/indicators.json` |
| `backend/routes.js` | The proxy's route table — **the one definition**, shared by both proxies |
| `backend/worker.js` | Deployed Cloudflare Worker |
| `tool/dev_proxy.py` | Local twin of the Worker, plus a static server |
| `web/index.html` | The page |

Two data paths, because they change at different rates:

- **Once a day** — the averages, RSI and the 52-week range all derive from
  daily closes, so they move only when a new daily bar prints. Computed after
  the close and stored.
- **Live** — price and day change come from NSE on each refresh. One request
  covers the whole watchlist.

### Why there is a proxy

**NSE sends no `Access-Control-Allow-Origin` header**, so a browser cannot call
it directly however well-formed the request. That is the Worker's entire job.
Upstox *does* send CORS, which is why historical candles are not proxied.

The live feed reads **NIFTY 500**, not NIFTY 50, because the watchlist may name
any company. The 500 covers every NIFTY 50 member plus the large- and mid-caps
a watchlist realistically reaches for, still in one ~300 KB call. The proxy
filters to the requested symbols before replying, so a phone receives ~30 rows
rather than 500 — and any symbol the index does not carry is **reported**, not
dropped.

---

## Data sources

All free, all unauthenticated. No API key exists anywhere in this repo, so
there is nothing to rotate, leak or have expire.

| Need | Source |
|---|---|
| Symbol → ISIN | Upstox instrument master (~2,400 NSE equities) |
| Live price, change, 52-week range | NSE market-watch route, **NIFTY 500** — one call |
| Daily candles | Upstox `historical-candle` |
| Per-stock news | Google News RSS, filtered |

The 52-week range arrives from NSE *and* is computed independently from
Upstox candles. They agreed on every symbol checked, which is what says the
data layer is sound rather than merely self-consistent —
`test_indicators.py --live` re-runs the comparison against the live feed.

---

## Traps

Every one of these was hit while building, and every one fails **silently** —
a wrong number, not an error.

- **Upstox returns candles newest-first.** Averaging without reversing uses
  last summer's prices. Measured on TCS: the 50-day average lands 29% off and
  RSI reads 79 instead of 63, crossing the conventional overbought line. The
  reversal happens once, inside `fetch_candles`, so no caller can skip it.
- **Never fan candle requests out in parallel.** Fifty at once tripped rate
  limiting and a measured **573-second** block. Serially the same fifty take
  under three seconds, so there is no upside to trying.
- **Send a browser User-Agent everywhere.** Upstox answers 403 to a default
  script agent; NSE kills the HTTP/2 stream, which surfaces as a *connection
  error* rather than a status code and reads like an outage.
- **A 365-day window holds ~247 Indian trading sessions, not 250+.** A
  bar-count threshold near 250 rejects every stock while looking like a
  sensible guard. Coverage is tested by whether the series reaches back past
  the cutoff. (Shipped this bug; `test_52w_accepts_a_real_trading_year` pins it.)
- **Symbols disappear.** `TATAMOTORS` no longer exists — it demerged into
  TMPV (full history) and TMCV (too short for a 200-day average). `LTIM`
  resolves to nothing at all today. Resolution goes through Upstox's live
  instrument master and **raises** on anything unresolved, because a
  hardcoded table fails by silently shrinking the watchlist.
- **A watchlist company outside NIFTY 500 has no live price.** The page says
  so on the row and in the footer rather than showing a stale close as if it
  were current.
- **NSE nests the rows at `data.data`**, and the index row is distinguished
  from constituents only by a null `series`.
- **Google News sorts by relevance, not date.** Without `when:2d` the top hit
  can be weeks old. Quote the company name, and use press-style names —
  `"Sun Pharma"` returns results where `"Sun Pharmaceutical"` returns none.
- **Roughly 60% of news results are unrelated backfill**, because the search
  pads thin queries rather than returning fewer items. Requiring the company
  name in the *headline* is the feature, not polish. Titan otherwise returns
  Saturn's moon; Shriram returns a film star's husband.

---

## Decisions

- **News is shown near a move, never as its cause.** Most 1% moves have no
  story, and a product that always produces an explanation will invent one.
  When nothing relevant exists the page says so.
- **An indicator without the history to be meaningful is `null`, never a
  number from a short window.** A newly relisted company would otherwise show
  a confident, wrong 200-day average.
- **One route table, two proxies.** `test_proxy_parity.py` fails if they drift.
- **Work Sans for body, Fraunces for headings.** Work Sans replaced Inter on
  the owner's instruction (2026-08-08). It was checked for `tnum` and `zero`
  first: this page is columns of numbers, and a proportional-figure face makes
  prices reflow on every refresh. Work Sans has both. Inter's `cv05`/`ss03`
  were dropped rather than carried over — those tags address different
  glyphs in Work Sans.
- **Lime is for calls to action only** — never for selected chips or
  highlighted figures. Selection is carried by surface lightness and weight.
- **Green = up, red = down** (Indian convention), as a data layer kept
  separate from the brand accent.

## Still open

- **The watchlist itself.** `tool/watchlist.json` currently holds a
  placeholder — the 30 largest NIFTY 50 names — so the product runs end to
  end. It is flagged `"placeholder": true` and is waiting on the real list.
- **The name.** "Nifty 50 Levels" no longer describes a product whose
  watchlist is neither the Nifty 50 nor fifty companies. The page reads
  "Levels" for now.
- Alert delivery and the rules that fire one. Confirmed crossings on a
  chosen shortlist is the agreed shape; notifications are the chosen channel,
  which on iPhone means the page must be added to the Home Screen.
- **Nothing about live intraday behaviour is measured yet** — everything here
  was validated with the market closed. How fast prices update mid-session,
  and how long after a move an explainer appears, both need a weekday.

## Not advice

This reports measurements: where a price sits relative to its own history, and
what was published around it. It does not identify direction, generate
signals, or recommend any position. Not registered with SEBI.
