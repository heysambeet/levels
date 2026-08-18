/**
 * Cloudflare Worker — the CORS proxy.
 *
 * It exists for one measured reason: NSE returns no Access-Control-Allow-Origin
 * header, so a browser cannot call it directly however well-formed the request.
 * Upstox *does* send CORS, which is why historical candles are not proxied —
 * they are computed once a day by tool/build_indicators.py instead.
 *
 * Deploy:  cd backend && npx wrangler deploy
 */

import { ROUTES, BROWSER_UA, INDICATORS, EXPECTED_MOVE, parseRss } from './routes.js';

const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type',
};

function json(body, { status = 200, ttl = 0 } = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      ...CORS,
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': ttl ? `public, max-age=${ttl}` : 'no-store',
    },
  });
}

async function fetchUpstream(url, ttl) {
  // Google News 5xxes Workers egress in flaps — measured on this account:
  // one query 0/6 while another passes 6/6 the same minute, matching what
  // market-nudge recorded in 2026. Flapping yields to retries; a genuinely
  // blocked query still fails after three, which is the honest outcome.
  let res;
  for (let attempt = 0; attempt < 3; attempt++) {
    res = await fetch(url, {
      headers: { 'User-Agent': BROWSER_UA, Accept: '*/*' },
      // Cache only successes: 5xx must never be pinned for `ttl` seconds.
      cf: ttl ? { cacheTtl: ttl, cacheEverything: true, cacheTtlByStatus: { '500-599': 0 } } : undefined,
    });
    if (res.ok || res.status < 500) break;
    await new Promise((r) => setTimeout(r, 400 * (attempt + 1)));
  }
  if (!res.ok) {
    // Distinguish "upstream said no" from "proxy is broken". Collapsing the
    // two makes an NSE outage look like a bug in this Worker.
    throw Object.assign(new Error(`upstream ${res.status}`), { status: res.status });
  }
  return res;
}

async function handleLive(url) {
  const r = ROUTES.live;
  // Both calls in parallel — they are independent, and the header should not
  // wait on the constituent payload (which is ~10x larger).
  const [res, idxRes] = await Promise.all([
    fetchUpstream(r.upstream, r.ttl),
    fetchUpstream(r.index_upstream, r.ttl).catch(() => null),
  ]);
  const payload = await res.json();

  // NSE nests the rows one level deeper than the envelope suggests, and the
  // index row is distinguished from constituents only by a null `series`.
  const rows = payload?.data?.data;
  if (!Array.isArray(rows)) throw new Error('unexpected NSE shape');

  let stocks = rows.filter((x) => x.series != null);
  if (stocks.length < 100) throw new Error(`only ${stocks.length} constituents`);

  // Prefer the NIFTY 50 payload for the header. If that call failed, fall
  // back to the 500's own index row rather than blanking the header — but
  // never silently relabel one index with the other's number, which is why
  // the symbol travels with the figure.
  let idxPayload = null;
  if (idxRes) {
    try {
      idxPayload = await idxRes.json();
    } catch {
      idxPayload = null;
    }
  }
  const idxRows = idxPayload?.data?.data;
  const index =
    (Array.isArray(idxRows) ? idxRows.find((x) => x.series == null) : null) ||
    rows.find((x) => x.series == null) ||
    null;
  // Breadth and timestamp belong to whichever index the header is showing.
  const headerData = idxRows ? idxPayload.data : payload.data;

  // Filter to the caller's watchlist so a phone downloads ~30 rows, not 500.
  // Symbols the index does not carry are reported rather than dropped —
  // silently returning fewer stocks than were asked for is how a watchlist
  // loses a company without anyone noticing.
  const want = (url.searchParams.get('symbols') || '')
    .split(',').map((s) => s.trim().toUpperCase()).filter(Boolean);
  let missing = [];
  if (want.length) {
    const have = new Set(stocks.map((s) => s.symbol));
    missing = want.filter((s) => !have.has(s));
    const wanted = new Set(want);
    stocks = stocks.filter((s) => wanted.has(s.symbol));
  }

  return json(
    {
      as_of: headerData.timestamp ?? null,
      market_status: headerData.marketStatus?.marketStatus ?? null,
      breadth: headerData.aduCount ?? null,
      universe: payload.data.data?.length ?? null,
      missing,
      index: index && {
        symbol: index.symbol,
        last: index.lastPrice,
        change: index.change,
        pct: index.pChange,
      },
      stocks: stocks.map((s) => ({
        symbol: s.symbol,
        name: s.companyName ?? null,
        last: s.lastPrice,
        change: s.change,
        pct: s.pChange,
        prev_close: s.previousClose,
        open: s.open,
        day_high: s.dayHigh,
        day_low: s.dayLow,
        year_high: s.yearHigh,
        year_low: s.yearLow,
        near_year_high: s.nearWKH,
        near_year_low: s.nearWKL,
        volume: s.totalTradedVolume,
      })),
    },
    { ttl: ROUTES.live.ttl },
  );
}

async function handleNews(url) {
  const q = url.searchParams.get('q');
  if (!q) return json({ error: 'q required' }, { status: 400 });
  if (q.length > 200) return json({ error: 'q too long' }, { status: 400 });

  const r = ROUTES.news;
  const res = await fetchUpstream(r.upstream + encodeURIComponent(q), r.ttl);
  const items = parseRss(await res.text());
  // An empty feed and a malformed query look identical from here — both are a
  // valid document with no items — so report the count rather than implying
  // the market was quiet.
  return json({ query: q, count: items.length, items }, { ttl: r.ttl });
}

/* ---------------------------------------------------------------- indicators
 *
 * This is a second implementation of tool/build_indicators.py, which is a
 * drift risk taken deliberately: the alternative is levels that are only as
 * fresh as the last manual rebuild. tool/test_worker_indicators.py compares
 * the two against live data and fails on any disagreement.
 */

/** Daily bars, oldest-first. Upstox serves them newest-first, and every
 *  indicator below is order-sensitive — reversing here, once, is what stops
 *  a caller silently averaging last year's prices. */
async function fetchCandles(isin) {
  const now = new Date();
  const ist = new Date(now.getTime() + 5.5 * 3600 * 1000);   // IST calendar day
  const to = ist.toISOString().slice(0, 10);
  const from = new Date(ist.getTime() - INDICATORS.historyDays * 86400 * 1000)
    .toISOString().slice(0, 10);
  const key = encodeURIComponent(`NSE_EQ|${isin}`);
  const res = await fetchUpstream(
    `${ROUTES.indicators.candles}/${key}/day/${to}/${from}`, ROUTES.indicators.ttl);
  const rows = (await res.json())?.data?.candles;
  if (!Array.isArray(rows) || !rows.length) throw new Error('no candles');
  return rows
    .map((r) => ({ date: r[0].slice(0, 10), high: +r[2], low: +r[3], close: +r[4] }))
    .sort((a, b) => (a.date < b.date ? -1 : 1));
}

const sma = (c, n) =>
  c.length >= n ? +(c.slice(-n).reduce((a, b) => a + b, 0) / n).toFixed(2) : null;

/** Wilder's RSI — seeded with a simple mean, then smoothed recursively.
 *  Not a plain average of the last `period` changes; that is a different
 *  number that happens to look reasonable. */
function rsiWilder(c, period = INDICATORS.rsiPeriod) {
  if (c.length < period + 1) return null;
  let g = 0, l = 0;
  for (let i = 1; i <= period; i++) {
    const d = c[i] - c[i - 1];
    g += Math.max(d, 0);
    l += Math.max(-d, 0);
  }
  let ag = g / period, al = l / period;
  for (let i = period + 1; i < c.length; i++) {
    const d = c[i] - c[i - 1];
    ag = (ag * (period - 1) + Math.max(d, 0)) / period;
    al = (al * (period - 1) + Math.max(-d, 0)) / period;
  }
  if (al === 0) return ag > 0 ? 100 : 50;
  return +(100 - 100 / (1 + ag / al)).toFixed(2);
}

/** High/low over the trailing 365 calendar days.
 *  Sufficiency is whether the series reaches back PAST the cutoff — not how
 *  many bars sit inside it. A year holds ~247 Indian sessions, so any
 *  bar-count threshold near 250 rejects every stock while looking sensible. */
function window52w(bars) {
  if (!bars.length) return null;
  const asof = new Date(bars[bars.length - 1].date + 'T00:00:00Z');
  const cutoff = new Date(asof.getTime() - 365 * 86400 * 1000).toISOString().slice(0, 10);
  if (bars[0].date > cutoff) return null;
  const win = bars.filter((b) => b.date >= cutoff);
  if (win.length < INDICATORS.minBars52w) return null;
  const hi = win.reduce((a, b) => (b.high > a.high ? b : a));
  const lo = win.reduce((a, b) => (b.low < a.low ? b : a));
  return { high: +hi.high.toFixed(2), high_date: hi.date,
           low: +lo.low.toFixed(2), low_date: lo.date };
}

async function handleIndicators() {
  const r = ROUTES.indicators;
  // The committed file supplies the watchlist (symbols + ISINs); the numbers
  // below are recomputed, so changing the watchlist needs no Worker deploy.
  const manifestRes = await fetchUpstream(r.upstream, 300);
  const manifest = await manifestRes.json();
  const targets = (manifest.stocks || []).filter((s) => s.symbol && s.isin);
  if (!targets.length) throw new Error('manifest has no resolvable stocks');

  const stocks = [];
  const failed = [];
  // Strictly serial. Fanning these out in parallel earned a measured
  // 573-second rate-limit ban; serially the whole watchlist takes ~3s, and
  // this answer is cached for an hour.
  for (const t of targets) {
    try {
      const bars = await fetchCandles(t.isin);
      const closes = bars.map((b) => b.close);
      const last = bars[bars.length - 1];
      const row = {
        symbol: t.symbol,
        name: t.name ?? null,
        isin: t.isin,
        as_of: last.date,
        close: +last.close.toFixed(2),
        bars: bars.length,
        dma50: sma(closes, 50),
        dma100: sma(closes, 100),
        dma200: sma(closes, 200),
        rsi14: rsiWilder(closes),
        week52: window52w(bars),
      };
      // An indicator without the history to mean anything is null, never a
      // number computed from a short window.
      row.insufficient = ['dma50', 'dma100', 'dma200', 'rsi14']
        .filter((k) => row[k] == null)
        .concat(row.week52 == null ? ['week52'] : []);
      stocks.push(row);
    } catch {
      failed.push(t.symbol);
    }
  }

  // A watchlist is explicit, so a missing name is a failure, not a gap. Fall
  // back to the committed file rather than serving a short list.
  if (stocks.length < targets.length) {
    throw Object.assign(new Error(`${failed.join(', ')} failed`), { status: 502 });
  }
  stocks.sort((a, b) => a.symbol.localeCompare(b.symbol));
  return json(
    {
      generated_at: new Date().toISOString(),
      as_of: stocks.map((s) => s.as_of).sort().pop(),
      count: stocks.length,
      source: 'worker',
      watchlist_max: manifest.watchlist_max ?? null,
      placeholder_watchlist: manifest.placeholder_watchlist ?? null,
      failed,
      stocks,
    },
    { ttl: r.ttl },
  );
}

/* ------------------------------------------------------------ expected move
 *
 * How far the options market prices this name moving by expiry. Symmetric
 * around spot by construction: it says how far, never which way.
 */

/** Expiry stamps are dates; options stop trading at 15:30 IST that day. */
function expiryToMs(ddMonYyyy) {
  const [d, mon, y] = ddMonYyyy.split('-');
  const months = { Jan: 0, Feb: 1, Mar: 2, Apr: 3, May: 4, Jun: 5,
                   Jul: 6, Aug: 7, Sep: 8, Oct: 9, Nov: 10, Dec: 11 };
  if (!(mon in months)) return NaN;
  // 15:30 IST = 10:00 UTC on the same calendar day.
  return Date.UTC(+y, months[mon], +d, 10, 0, 0);
}

async function handleExpectedMove(url) {
  const symbol = (url.searchParams.get('symbol') || '').trim().toUpperCase();
  if (!symbol || !/^[A-Z0-9&_-]{1,20}$/.test(symbol)) {
    return json({ error: 'symbol required' }, { status: 400 });
  }
  const kind = url.searchParams.get('type') === 'Indices' ? 'Indices' : 'Equity';
  const r = ROUTES.expectedMove;

  // Hop 1 — the expiry list. The chain is unusable without one.
  const ciRes = await fetchUpstream(r.contracts + encodeURIComponent(symbol), r.ttl);
  const expiries = (await ciRes.json())?.expiryDates;
  if (!Array.isArray(expiries) || !expiries.length) {
    return json({ symbol, available: false, reason: 'no listed options' }, { ttl: r.ttl });
  }

  // Nearest expiry that is not about to degenerate. Inside ~12h the ATM
  // approximation breaks down — CE and PE implied vols diverge and the
  // straddle-implied sigma blew out 2.3× in testing.
  const now = Date.now();
  const dated = expiries
    .map((e) => ({ e, days: (expiryToMs(e) - now) / 86400000 }))
    .filter((x) => isFinite(x.days) && x.days >= EXPECTED_MOVE.minDaysToExpiry)
    .sort((a, b) => a.days - b.days);
  if (!dated.length) {
    return json({ symbol, available: false, reason: 'nearest expiry is inside 12 hours' },
                { ttl: r.ttl });
  }
  const { e: expiry, days: T } = dated[0];

  // Hop 2 — the chain itself.
  const chainRes = await fetchUpstream(
    `${r.upstream}?type=${kind}&symbol=${encodeURIComponent(symbol)}&expiry=${encodeURIComponent(expiry)}`,
    r.ttl);
  const rec = (await chainRes.json())?.records;
  const rows = rec?.data;
  const spot = rec?.underlyingValue;

  // Every failure mode here answers HTTP 200 — a bad symbol, a past expiry
  // and a non-F&O stock all return an empty array with underlyingValue 0.
  // The status code is worthless as a health check; the row count is not.
  if (!Array.isArray(rows) || !rows.length || !spot) {
    return json({ symbol, available: false, reason: 'no option chain for this name' },
                { ttl: r.ttl });
  }

  const atm = rows.reduce((best, row) =>
    Math.abs(row.strikePrice - spot) < Math.abs(best.strikePrice - spot) ? row : best);
  const ce = atm.CE || {};
  const pe = atm.PE || {};
  const straddle = (ce.lastPrice || 0) + (pe.lastPrice || 0);
  const ivPct = ((ce.impliedVolatility || 0) + (pe.impliedVolatility || 0)) / 2;

  if (straddle <= 0 || ivPct <= 0) {
    return json({ symbol, available: false, reason: 'ATM strike has no live quotes' },
                { ttl: r.ttl });
  }

  const sigmaStraddle = straddle * EXPECTED_MOVE.straddleToSigma;
  const sigmaIv = spot * (ivPct / 100) * Math.sqrt(T / 365);
  // Two independent routes to the same quantity. Agreement is the check that
  // the chain is internally consistent; disagreement means a stale or thin
  // ATM print, and is surfaced rather than averaged away.
  const ratio = sigmaIv > 0 ? sigmaStraddle / sigmaIv : 0;
  const disagrees = Math.abs(ratio - 1) > EXPECTED_MOVE.maxMethodDisagreement;
  const sigma = (sigmaStraddle + sigmaIv) / 2;

  return json(
    {
      symbol,
      available: true,
      expiry,
      days_to_expiry: +T.toFixed(2),
      spot,
      atm_strike: atm.strikePrice,
      straddle: +straddle.toFixed(2),
      iv_pct: +ivPct.toFixed(2),
      sigma: +sigma.toFixed(2),
      sigma_pct: +((sigma / spot) * 100).toFixed(2),
      low: +(spot - sigma).toFixed(2),
      high: +(spot + sigma).toFixed(2),
      method_ratio: +ratio.toFixed(3),
      method_disagreement: disagrees,
      // Stated so the page never has to imply a probability it did not measure.
      measured_coverage_7d: EXPECTED_MOVE.measuredCoverage7d,
      chain_timestamp: rec.timestamp ?? null,
      directional: false,
    },
    { ttl: r.ttl },
  );
}

export default {
  async fetch(request) {
    if (request.method === 'OPTIONS') return new Response(null, { headers: CORS });
    if (request.method !== 'GET') return json({ error: 'GET only' }, { status: 405 });

    const url = new URL(request.url);
    try {
      if (url.pathname === ROUTES.live.path) return await handleLive(url);
      if (url.pathname === ROUTES.indicators.path) return await handleIndicators();
      if (url.pathname === ROUTES.expectedMove.path) return await handleExpectedMove(url);
      if (url.pathname === ROUTES.news.path) return await handleNews(url);
      if (url.pathname === '/health') return json({ ok: true });
      return json({ error: 'not found' }, { status: 404 });
    } catch (err) {
      return json(
        { error: String(err.message || err), upstream_status: err.status ?? null },
        { status: err.status && err.status >= 400 && err.status < 600 ? 502 : 500 },
      );
    }
  },
};
