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
    // NSE republishes every ~5s. Twenty seconds keeps the page honest while
    // making the cache, not NSE, absorb a refresh loop.
    ttl: 20,
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

export const ALLOWED_HOSTS = ['www.nseindia.com', 'news.google.com'];

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
