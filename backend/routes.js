/**
 * The proxy's route table — the single definition shared by the deployed
 * Worker and the local dev proxy.
 *
 * It exists because these two drifted in a previous project and shipped a
 * frozen price and a broken calendar: the same allowlist and the same cache
 * TTLs have to hold in both, and the only way to guarantee that is to have
 * one copy. `tool/dev_proxy.py` parses this file rather than restating it,
 * and `tool/test_proxy_parity.py` fails if it ever cannot.
 */

// NSE kills non-browser HTTP/2 streams — the failure surfaces as a connection
// error rather than a status code, which reads like an outage instead of a
// rejection. Upstox answers 403 to the same. Always send a browser UA.
export const BROWSER_UA =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36';

export const ROUTES = {
  // Live price, change% and 52-week range for a whole index in one request.
  // No cookie warm-up and no Referer needed — only the UA.
  //
  // NIFTY 500, not NIFTY 50, because the watchlist is no longer an index: it
  // may name any NSE company. The 500 covers every NIFTY 50 member plus the
  // large- and mid-caps a watchlist realistically reaches for, still in a
  // single ~300 KB call. The Worker filters to the requested symbols before
  // replying, so the phone receives ~30 rows rather than 500.
  live: {
    path: '/api/live',
    upstream:
      'https://www.nseindia.com/api/NextApi/apiClient/marketWatchApi?functionName=getIndicesData&symbol=NIFTY%20500',
    // The header displays the NIFTY 50 — the number everyone means by "the
    // market" (owner's call, 2026-08-11) — but the 500's own index row is
    // the 500. So the index, breadth and timestamp come from a second call
    // to the same proven endpoint, while constituents stay on the 500 for
    // watchlist coverage.
    index_upstream:
      'https://www.nseindia.com/api/NextApi/apiClient/marketWatchApi?functionName=getIndicesData&symbol=NIFTY%2050',
    // NSE republishes roughly every 5s. Matching that means the cache absorbs
    // a refresh loop without ever being the reason the screen lags — anything
    // longer and the page waits on us rather than on the exchange.
    ttl: 5,
    type: 'application/json',
  },

  // The daily layer — 50/100/200 DMA, RSI(14), 52-week range — computed here
  // from Upstox candles instead of being read from a file someone remembered
  // to rebuild. These move only when a new daily bar prints, so the answer is
  // cached for hours; the point is that it always reflects the latest close
  // rather than the last deploy.
  indicators: {
    path: '/api/indicators',
    // The committed file is the watchlist manifest (symbols + ISINs) and the
    // page's fallback. Reading it here means changing the watchlist needs no
    // Worker redeploy.
    upstream: 'https://heysambeet.github.io/levels/data/indicators.json',
    candles: 'https://api.upstox.com/v2/historical-candle',
    ttl: 3600,
    type: 'application/json',
  },
  // The option-implied expected move — how far the options market prices this
  // name moving by expiry. Fetched per stock on demand, like news, because it
  // costs two upstream calls each and thirty of those would blow the Worker's
  // 50-subrequest ceiling on a single page load.
  //
  // It answers "how far", never "which way": the band is symmetric around
  // spot by construction and carries no directional information at all.
  expectedMove: {
    path: '/api/expected-move',
    // Two hops. The chain REQUIRES an explicit &expiry= — without it NSE
    // returns HTTP 200 and a literal `{}`. A cookie warm-up changes nothing;
    // a browser User-Agent is the only requirement.
    upstream: 'https://www.nseindia.com/api/option-chain-v3',
    contracts: 'https://www.nseindia.com/api/option-chain-contract-info?symbol=',
    // NSE caches the chain 60–90s; polling faster returns an identical
    // timestamp.
    ttl: 90,
    type: 'application/json',
  },

  // Per-stock headlines. Fetched only for stocks that actually moved, so this
  // is a handful of requests a day, not fifty.
  news: {
    path: '/api/news',
    // `q` is appended by the handler. Google News sorts by RELEVANCE, so the
    // when: operator is what makes the result recent rather than merely
    // matching — without it the top hit can be 400 hours old.
    upstream: 'https://news.google.com/rss/search?hl=en-IN&gl=IN&ceid=IN:en&q=',
    // Median freshest item measured at ~4h, so polling faster buys nothing.
    ttl: 1800,
    type: 'application/json',
  },
};

// Indicator windows and history depth — kept beside the routes so the Worker
// and tool/build_indicators.py can be checked against each other.
export const INDICATORS = {
  historyDays: 500,   // ≈338 trading bars; a 200DMA needs 200, a 52w range ~250
  rsiPeriod: 14,
  minBars52w: 200,
};

export const EXPECTED_MOVE = {
  // An ATM straddle prices E[|move|] = S·σ·√T·√(2/π), so one sigma is the
  // straddle divided by 0.7979 — i.e. × √(π/2) = 1.2533.
  //
  // The widely repeated "straddle × 0.85 ≈ 1σ" rule is simply wrong: it
  // yields 0.68σ, a ~50% band, and shipping it labelled 68% would understate
  // the range by nearly half. Measured on eight live chains, × 1.2533 agrees
  // with S·IV·√(T/365) at 0.993 ± 0.011.
  straddleToSigma: Math.SQRT2 * Math.sqrt(Math.PI) / 2,   // √(π/2)
  // Inside ~12h of expiry the ATM approximation degenerates — CE and PE IV
  // diverge and the straddle-implied sigma blew out 2.3× in testing.
  minDaysToExpiry: 0.5,
  // If the two independent routes to sigma disagree by more than this, the
  // chain is telling us something is off (stale print, illiquid strike) and
  // the answer is flagged rather than shown as fact.
  maxMethodDisagreement: 0.15,
  // Measured, not assumed: over 2,471 overlapping 7-day windows on NIFTY the
  // 1σ band contained the actual move 76.6% of the time against a theoretical
  // 68.3%, because implied vol runs ~21% above subsequently realised vol.
  // The band is therefore a conservative ceiling, not a forecast.
  measuredCoverage7d: 0.766,
};

export const ALLOWED_HOSTS = [
  'www.nseindia.com',
  'news.google.com',
  'api.upstox.com',
  'heysambeet.github.io',
];

/** Parse an RSS document without a DOM — Workers have no DOMParser. */
export function parseRss(xml, limit = 25) {
  const items = [];
  const re = /<item>([\s\S]*?)<\/item>/g;
  let m;
  while ((m = re.exec(xml)) !== null && items.length < limit) {
    const block = m[1];
    const pick = (tag) => {
      const r = new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`).exec(block);
      return r ? decodeEntities(r[1].trim()) : '';
    };
    const title = pick('title');
    if (!title) continue;
    items.push({
      title,
      source: pick('source'),
      published: pick('pubDate'),
      link: pick('link'),
    });
  }
  return items;
}

function decodeEntities(s) {
  return s
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&apos;/g, "'")
    .replace(/&amp;/g, '&');
}
