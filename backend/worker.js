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

import { ROUTES, BROWSER_UA, parseRss } from './routes.js';

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
  const res = await fetch(url, {
    headers: { 'User-Agent': BROWSER_UA, Accept: '*/*' },
    cf: ttl ? { cacheTtl: ttl, cacheEverything: true } : undefined,
  });
  if (!res.ok) {
    // Distinguish "upstream said no" from "proxy is broken". Collapsing the
    // two makes an NSE outage look like a bug in this Worker.
    throw Object.assign(new Error(`upstream ${res.status}`), { status: res.status });
  }
  return res;
}

async function handleLive(url) {
  const r = ROUTES.live;
  const res = await fetchUpstream(r.upstream, r.ttl);
  const payload = await res.json();

  // NSE nests the rows one level deeper than the envelope suggests, and the
  // index row is distinguished from constituents only by a null `series`.
  const rows = payload?.data?.data;
  if (!Array.isArray(rows)) throw new Error('unexpected NSE shape');

  let stocks = rows.filter((x) => x.series != null);
  const index = rows.find((x) => x.series == null) || null;
  if (stocks.length < 100) throw new Error(`only ${stocks.length} constituents`);

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
      as_of: payload.data.timestamp ?? null,
      market_status: payload.data.marketStatus?.marketStatus ?? null,
      breadth: payload.data.aduCount ?? null,
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

export default {
  async fetch(request) {
    if (request.method === 'OPTIONS') return new Response(null, { headers: CORS });
    if (request.method !== 'GET') return json({ error: 'GET only' }, { status: 405 });

    const url = new URL(request.url);
    try {
      if (url.pathname === ROUTES.live.path) return await handleLive(url);
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
