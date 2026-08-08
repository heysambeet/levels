# Nifty 50 Levels

All fifty NIFTY constituents on one page, each showing where it sits against
its 50/100/200-day averages and its 52-week range, with RSI — and the news
that explains the move.

The question it answers is narrow on purpose: **something moved, why?**
See the move, see the levels it crossed, see the reason. Anything that does
not serve that loop stays out.

Personal, mobile-web, ₹0/month. Not SEBI-registered, and it reports
measurements rather than conclusions — see [Not advice](#not-advice).

> Working title. The repository name is not final, and neither is this one.

---

## Run it

```bash
python3 tool/build_indicators.py     # once after each close — 50 stocks, ~7s
python3 tool/dev_proxy.py            # http://localhost:8900
```

The proxy also serves `web/`, so that one command is the whole local setup.

```bash
python3 tool/test_indicators.py --live    # --live also cross-checks against NSE
python3 tool/test_proxy_parity.py
```

## How it is put together

| Piece | What it does |
|---|---|
| `tool/build_indicators.py` | Daily job. Pulls candles, computes DMA/RSI/52-week, writes `web/data/indicators.json` |
| `backend/routes.js` | The proxy's route table — **the one definition**, shared by both proxies |
| `backend/worker.js` | Deployed Cloudflare Worker |
| `tool/dev_proxy.py` | Local twin of the Worker, plus a static server |
| `web/index.html` | The page |
| `tool/news_aliases.json` | Press-style names and negative keywords per stock |

Two data paths, because they change at different rates:

- **Once a day** — the averages, RSI and the 52-week range all derive from
  daily closes, so they move only when a new daily bar prints. Computed after
  the close and stored.
- **Live** — price and day change come from NSE on each refresh. One request
  returns all fifty.

### Why there is a proxy

**NSE sends no `Access-Control-Allow-Origin` header**, so a browser cannot call
it directly however well-formed the request. That is the Worker's entire job.
Upstox *does* send CORS, which is why historical candles are not proxied.

---

## Data sources

All free, all unauthenticated. No API key exists anywhere in this repo, so
there is nothing to rotate, leak or have expire.

| Need | Source |
|---|---|
| Constituents + ISINs | NSE published NIFTY 50 list (archives CSV) |
| Live price, change, 52-week range | NSE market-watch route — all 50 in one call |
| Daily candles | Upstox `historical-candle` |
| Per-stock news | Google News RSS, filtered |

The 52-week range arrives from NSE *and* is computed independently from
Upstox candles. They agreed on all 50 symbols, which is the check that says
the data layer is sound — `test_indicators.py --live` re-runs it.

---

## Traps

Every one of these was hit while building, and every one fails **silently** —
a wrong number, not an error.

- **Upstox returns candles newest-first.** Averaging without reversing uses
  last summer's prices. Measured on TCS: the 50-day average lands 29% off and
  RSI reads 79 instead of 63, crossing the conventional overbought line. The
  reversal happens once, inside `fetch_candles`, so no caller can skip it.
- **Never fan out all 50 candle requests at once.** Fifty in parallel tripped
  rate limiting and a measured **573-second** block. Serially they take under
  three seconds. There is no upside to parallelising.
- **Send a browser User-Agent everywhere.** Upstox answers 403 to a default
  script agent; NSE kills the HTTP/2 stream, which surfaces as a *connection
  error* rather than a status code and reads like an outage.
- **A 365-day window holds ~247 Indian trading sessions, not 250+.** A
  bar-count threshold near 250 rejects every stock while looking like a
  sensible guard. Coverage is tested by whether the series reaches back past
  the cutoff. (Shipped this bug; `test_52w_accepts_a_real_trading_year` pins it.)
- **Take the constituent list from NSE every run.** `TATAMOTORS` no longer
  exists — it demerged, and the index holds **TMPV**. The other successor,
  TMCV, has too little history for a 200-day average and is *not* in the index.
  A hardcoded list would have quietly dropped a constituent.
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
- **Lime is for calls to action only** — never for selected chips or
  highlighted figures. Selection is carried by surface lightness and weight.
- **Green = up, red = down** (Indian convention), as a data layer kept
  separate from the brand accent.

## Still open

- Alert delivery and the rules that fire one. Confirmed crossings on a
  chosen shortlist is the agreed shape; the channel is undecided.
- **Nothing about live intraday behaviour is measured yet** — everything here
  was validated with the market closed. How fast prices update mid-session,
  and how long after a move an explainer appears, both need a weekday.

## Not advice

This reports measurements: where a price sits relative to its own history, and
what was published around it. It does not identify direction, generate
signals, or recommend any position. Not registered with SEBI.
