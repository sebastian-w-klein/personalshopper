// ==UserScript==
// @name         TRR Suit Scout
// @namespace    personal-shopper
// @version      0.1.0
// @description  Collects men's suit listings from The RealReal in your own signed-in browser and exports them for analysis.
// @match        https://www.therealreal.com/*
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_deleteValue
// @run-at       document-idle
// @noframes
// ==/UserScript==

/*
 * How it works (see README.md):
 *  1. Search phase: the tab itself navigates through /products?keywords=…&price_to=…
 *     pages, following the "Next page" link (cursor `after=` param), and queues
 *     product URLs that match `scrape.url_filter`.
 *  2. Product phase: each queued product page is loaded (same-origin fetch by
 *     default, or real navigation with product_mode "navigate") and parsed.
 *  3. Export: downloads all collected items (with price history) as JSON for
 *     analysis/analyze.py.
 *
 * If TRR serves its bot check ("Access to this page has been denied"), the
 * scan pauses and asks you to handle it yourself. The script never tries to
 * solve or get around it.
 */

(function () {
  'use strict';

  // ---------------------------------------------------------------- parsing
  // Pure functions (no GM / DOM-global access) so they can be unit tested in Node.

  const BLOCK_TAGS = new Set(('ADDRESS ARTICLE ASIDE BLOCKQUOTE BR BUTTON DD DETAILS DIV DL DT FIELDSET ' +
    'FIGCAPTION FIGURE FOOTER FORM H1 H2 H3 H4 H5 H6 HEADER HR LI MAIN NAV OL P PRE SECTION SUMMARY ' +
    'TABLE TBODY TD TH THEAD TR UL').split(' '));
  const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'SVG', 'IFRAME']);

  /** Block-aware text extraction that works on DOMParser documents (no layout, so no innerText). */
  function docLines(root) {
    const out = [];
    let cur = '';
    const flush = () => { const t = cur.replace(/\s+/g, ' ').trim(); if (t) out.push(t); cur = ''; };
    (function walk(node) {
      if (node.nodeType === 3) { cur += node.nodeValue; return; }
      if (node.nodeType !== 1 && node.nodeType !== 9 && node.nodeType !== 11) return;
      const tag = node.nodeName.toUpperCase();
      if (SKIP_TAGS.has(tag)) return;
      const block = BLOCK_TAGS.has(tag);
      if (block) flush();
      for (let c = node.firstChild; c; c = c.nextSibling) walk(c);
      if (block) flush();
    })(root);
    flush();
    return out;
  }

  const money = (s) => {
    if (s == null) return null;
    const m = String(s).replace(/,/g, '').match(/(\d+(?:\.\d+)?)/);
    return m ? parseFloat(m[1]) : null;
  };

  /** Parse 21, 21.5, 21 1/2, 21½ → number. */
  function inches(s) {
    if (s == null) return null;
    s = String(s).replace('½', ' 1/2').replace('¼', ' 1/4').replace('¾', ' 3/4');
    const m = s.match(/(\d+(?:\.\d+)?)(?:\s+(\d+)\/(\d+))?/);
    if (!m) return null;
    let v = parseFloat(m[1]);
    if (m[2]) v += parseInt(m[2], 10) / parseInt(m[3], 10);
    return v;
  }

  const SECTION_END = /^(condition|details|description|shipping|returns|item\s*#|size|you may also like|recently viewed|similar items|authenticity|payment|klarna|afterpay|add to (bag|cart)|questions\??)\b/i;
  const CONDITIONS = ['Pristine', 'Excellent', 'Very Good', 'Good', 'Fair'];
  const WEAR_WORDS = /\b(wear|worn|stain|staining|pilling|discolou?ration|fading|faded|alteration|altered|hemmed|odor|missing|hole|snag|scuff|tear|loose|creas|spot|marks?|tarnish)\w*/i;
  const FABRICS = ['cashmere', 'vicuna', 'vicuña', 'silk', 'linen', 'mohair', 'alpaca', 'camel', 'cotton', 'flannel',
    'tweed', 'seersucker', 'velvet', 'corduroy', 'polyester', 'viscose', 'rayon', 'nylon', 'elastane', 'spandex',
    'acetate', 'cupro', 'lyocell', 'virgin wool', 'wool', 'super 1[0-9]0s?', 'super 2[0-9]0s?'];

  /**
   * Parse product-page lines (+ optional meta / DOM hints) into a listing record.
   * `hints`: { url, ogImage, ogTitle, h1, designer, metaPrice, strikePrice }
   */
  function parseProductLines(lines, hints) {
    hints = hints || {};
    const rec = { url: hints.url || null, image: hints.ogImage || null };

    // Restrict to the product region: from the title (h1) to the recommendation rail.
    let start = 0;
    if (hints.h1) {
      const i = lines.findIndex((l) => l === hints.h1 || l.includes(hints.h1));
      if (i >= 0) start = Math.max(0, i - 1); // include a designer line just above the title
    }
    let end = lines.findIndex((l, i) => i > start && /^(you may also like|recently viewed|similar items|more from this designer|shop similar)/i.test(l));
    if (end < 0) end = lines.length;
    const region = lines.slice(start, end);
    const text = region.join('\n');

    rec.title = hints.h1 || hints.ogTitle || null;
    rec.designer = hints.designer || null;

    // Price
    const estM = text.match(/Est(?:imated)?\.?\s*Retail\s*:?\s*\$\s*([\d,]+(?:\.\d+)?)/i);
    rec.est_retail = estM ? money(estM[1]) : null;
    const priceLines = [];
    region.forEach((l, i) => {
      if (/est(?:imated)?\.?\s*retail/i.test(l)) return;
      const m = l.match(/^(?:(?:sale\s+)?price:?\s*)?\$\s*([\d,]+(?:\.\d{2})?)$/i);
      if (m) priceLines.push({ i, v: money(m[1]) });
    });
    let price = hints.metaPrice != null ? money(hints.metaPrice) : null;
    let original = hints.strikePrice != null ? money(hints.strikePrice) : null;
    const top = priceLines.filter((p) => p.i < 60);
    if (price == null && top.length) price = top[0].v;
    if (original == null && top.length >= 2 && /\d+\s*%\s*off/i.test(region.slice(0, 60).join(' '))) {
      original = Math.max(top[0].v, top[1].v);
      price = Math.min(top[0].v, top[1].v);
    }
    rec.price = price;
    rec.original_price = original && price && original > price ? original : null;
    const pctOff = text.match(/(\d{1,2})\s*%\s*off/i);
    rec.pct_off_shown = pctOff ? parseInt(pctOff[1], 10) : null;

    // Item number
    const itemM = text.match(/Item\s*(?:#|No\.?|Number)\s*:?\s*([A-Z0-9][A-Z0-9-]{4,})/i);
    rec.item_id = itemM ? itemM[1].toUpperCase() : null;

    // Size (skip "Size Guide"/"Size Chart")
    const sizeM = text.match(/(?:^|\n)\s*(?:Designer\s+)?Size\s*:?\s*(?!guide|chart)([^\n]{1,40})/i);
    rec.size_label = sizeM ? sizeM[1].trim() : null;

    // Measurements
    rec.measurements = {};
    const mi = region.findIndex((l) => /estimated\s+item\s+measurements/i.test(l));
    if (mi >= 0) {
      const after = [];
      // Same line may already contain measurements after the heading.
      const headTail = region[mi].replace(/.*estimated\s+item\s+measurements\s*:?/i, '').trim();
      if (headTail) after.push(headTail);
      for (let j = mi + 1; j < Math.min(region.length, mi + 25); j++) {
        if (SECTION_END.test(region[j]) && !/^size\s*:?\s*[\d.]+\s*("|in|”)/i.test(region[j])) break;
        after.push(region[j]);
      }
      const block = after.join('\n');
      const re = /([A-Za-z][A-Za-z \-\/]{1,30}?)\s*:\s*(\d+(?:\.\d+)?(?:\s+\d+\/\d+)?|\d+\s*[½¼¾])\s*(?:"|”|in\.?|inches)?/g;
      let m;
      while ((m = re.exec(block))) rec.measurements[m[1].trim().toLowerCase()] = inches(m[2]);
      // Label-on-one-line, value-on-next variant.
      for (let j = 0; j < after.length - 1; j++) {
        if (/^[A-Za-z][A-Za-z \-\/]{1,30}:?$/.test(after[j]) && /^\d/.test(after[j + 1])) {
          const k = after[j].replace(/:$/, '').trim().toLowerCase();
          if (!(k in rec.measurements)) rec.measurements[k] = inches(after[j + 1]);
        }
      }
    }

    // Condition + notes
    const condRe = new RegExp('Condition\\s*:?\\s*(' + CONDITIONS.join('|') + ')', 'i');
    const condM = text.match(condRe);
    rec.condition = condM ? CONDITIONS.find((c) => c.toLowerCase() === condM[1].toLowerCase()) : null;
    if (!rec.condition) {
      const ci = region.findIndex((l) => /^condition:?$/i.test(l));
      if (ci >= 0 && region[ci + 1]) {
        const c = CONDITIONS.find((c) => region[ci + 1].toLowerCase().startsWith(c.toLowerCase()));
        if (c) rec.condition = c;
      }
    }
    const notes = [];
    const ci = region.findIndex((l) => condRe.test(l) || /^condition:?$/i.test(l));
    if (ci >= 0) {
      for (let j = ci; j < Math.min(region.length, ci + 6); j++) {
        if (j > ci && SECTION_END.test(region[j])) break;
        const l = region[j].replace(condRe, '').replace(/^condition:?/i, '').trim();
        if (l && l.length > 3 && !CONDITIONS.includes(l)) notes.push(l);
      }
    }
    region.forEach((l) => { if (WEAR_WORDS.test(l) && l.length < 300 && !notes.includes(l) && !/return|shipping/i.test(l)) notes.push(l); });
    rec.condition_notes = notes.join(' | ') || null;

    // Final sale / sold
    rec.final_sale = /\bfinal\s+sale\b/i.test(text);
    rec.sold = /\b(sold out|this item has sold|item (is )?sold|no longer available)\b/i.test(text);

    // Description (Details/Description block before measurements)
    const di = region.findIndex((l) => /^(description|details|item details|product details)$/i.test(l));
    if (di >= 0) {
      const desc = [];
      for (let j = di + 1; j < Math.min(region.length, di + 30); j++) {
        if (/estimated\s+item\s+measurements/i.test(region[j]) || /^(condition|shipping|returns)\b/i.test(region[j])) break;
        desc.push(region[j]);
      }
      rec.description = desc.join('\n') || null;
    } else {
      rec.description = null;
    }

    // Fabric
    const fabricSrc = ((rec.description || '') + '\n' + (rec.title || '')).toLowerCase();
    const comp = [];
    const pctRe = /(\d{1,3})\s*%\s*([a-zñ ]+?)(?=[,\/\n.;]|\s+\d|$)/g;
    let pm;
    while ((pm = pctRe.exec(fabricSrc))) comp.push({ pct: parseInt(pm[1], 10), fiber: pm[2].trim() });
    rec.composition = comp.length ? comp : null;
    rec.fabrics = [];
    FABRICS.forEach((f) => {
      const m = fabricSrc.match(new RegExp('\\b' + f + '\\b', 'i'));
      if (m && !rec.fabrics.includes(m[0])) rec.fabrics.push(m[0]);
    });

    return rec;
  }

  /** Extract hints from a product Document (live or DOMParser). */
  function productHints(doc, url) {
    const meta = (p) => {
      const el = doc.querySelector(`meta[property="${p}"], meta[name="${p}"]`);
      return el ? el.getAttribute('content') : null;
    };
    const h1 = doc.querySelector('h1');
    const designerLink = doc.querySelector('main a[href*="/designers/"], h1 a[href*="/designers/"], a[href*="/designers/"][class*="designer" i], a[href*="/designers/"]');
    const strike = doc.querySelector('main s, main del, [class*="strike" i], [class*="original-price" i], [class*="originalPrice" i]');
    let ld = null;
    doc.querySelectorAll('script[type="application/ld+json"]').forEach((s) => {
      try {
        const j = JSON.parse(s.textContent);
        const arr = Array.isArray(j) ? j : (j['@graph'] || [j]);
        arr.forEach((o) => { if (o && /Product/i.test(o['@type'])) ld = o; });
      } catch (e) { /* ignore */ }
    });
    const ldOffer = ld && (Array.isArray(ld.offers) ? ld.offers[0] : ld.offers);
    return {
      url,
      ogImage: meta('og:image') || (ld && (Array.isArray(ld.image) ? ld.image[0] : ld.image)) || null,
      ogTitle: meta('og:title'),
      h1: h1 ? h1.textContent.replace(/\s+/g, ' ').trim() : null,
      designer: (ld && ld.brand && (ld.brand.name || ld.brand)) || (designerLink ? designerLink.textContent.replace(/\s+/g, ' ').trim() : null),
      metaPrice: meta('product:price:amount') || meta('og:price:amount') || (ldOffer && ldOffer.price) || null,
      strikePrice: strike ? strike.textContent : null,
      ldSku: ld && ld.sku,
    };
  }

  function parseProductDoc(doc, url) {
    const hints = productHints(doc, url);
    const rec = parseProductLines(docLines(doc.body || doc), hints);
    if (!rec.item_id && hints.ldSku) rec.item_id = String(hints.ldSku).toUpperCase();
    return rec;
  }

  /** Search results page → { links, next } */
  function parseSearchDoc(doc, baseUrl, urlFilter) {
    const filt = new RegExp(urlFilter || '/products/');
    const links = [];
    doc.querySelectorAll('a[href*="/products/"]').forEach((a) => {
      const u = new URL(a.getAttribute('href'), baseUrl);
      u.search = ''; u.hash = '';
      if (filt.test(u.pathname) && !links.includes(u.href)) links.push(u.href);
    });
    let nextEl = doc.querySelector('a[aria-label="Next page"], a[rel="next"]');
    if (!nextEl) nextEl = Array.from(doc.querySelectorAll('a')).find((a) => /^next( page)?$/i.test(a.textContent.trim()));
    const next = nextEl && nextEl.getAttribute('href') ? new URL(nextEl.getAttribute('href'), baseUrl).href : null;
    return { links, next };
  }

  function isBlockPage(doc) {
    const t = (doc.title || '').toLowerCase();
    const d = doc.querySelector('meta[name="description"]');
    return t.includes('access to this page has been denied') || (d && /px-captcha/i.test(d.getAttribute('content') || ''));
  }

  function itemKey(rec) {
    if (rec.item_id) return rec.item_id;
    const m = (rec.url || '').match(/-([a-z0-9]{4,6})\/?$/i);
    return m ? 'SLUG-' + m[1].toLowerCase() : rec.url;
  }

  /** Merge a fresh parse into the stored item, appending to price history when price changes. */
  function mergeItem(prev, rec, now) {
    const item = Object.assign({}, prev || {}, rec);
    item.first_seen = (prev && prev.first_seen) || now;
    item.last_seen = now;
    const hist = (prev && prev.price_history) ? prev.price_history.slice() : [];
    const last = hist[hist.length - 1];
    if (rec.price != null && (!last || last.price !== rec.price)) hist.push({ t: now, price: rec.price });
    item.price_history = hist;
    if (prev && prev.sold === false && rec.sold) item.sold_at = now;
    return item;
  }

  const api = { docLines, parseProductLines, productHints, parseProductDoc, parseSearchDoc, isBlockPage, itemKey, mergeItem, inches, money };
  if (typeof module !== 'undefined' && module.exports) { module.exports = api; return; }
  if (typeof GM_getValue === 'undefined') return;

  // ---------------------------------------------------------------- storage

  const DEFAULT_CONFIG = {
    scrape: {
      searches: ['mens suit'],
      add_designer_searches: true,
      price_to: 2500,
      url_filter: '/products/men/clothing/suiting/',
      max_pages_per_search: 10,
      product_mode: 'fetch',
      refetch_after_hours: 24,
      delay_ms: [2500, 6000],
    },
    designers: [],
  };

  const S = {
    get config() { return GM_getValue('config', DEFAULT_CONFIG); },
    set config(v) { GM_setValue('config', v); },
    get state() { return GM_getValue('state', { phase: 'idle' }); },
    set state(v) { GM_setValue('state', v); },
    get items() { return GM_getValue('items', {}); },
    set items(v) { GM_setValue('items', v); },
  };
  const cfg = () => Object.assign({}, DEFAULT_CONFIG.scrape, (S.config || {}).scrape || {});
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const jitter = () => { const [a, b] = cfg().delay_ms; return a + Math.random() * (b - a); };
  const nowIso = () => new Date().toISOString();

  function saveRecord(rec) {
    if (!rec || (!rec.price && !rec.title)) return null;
    const items = S.items;
    const k = itemKey(rec);
    items[k] = mergeItem(items[k], rec, nowIso());
    items[k].key = k;
    S.items = items;
    return items[k];
  }

  function buildSearchUrls() {
    const c = cfg();
    const kws = (c.searches || []).slice();
    const suffix = c.designer_search_suffix != null ? c.designer_search_suffix : 'suit';
    if (c.add_designer_searches) (S.config.designers || []).forEach((d) => kws.push(`${d.name || d} ${suffix}`.trim()));
    return [...new Set(kws)].map((k) => {
      const u = new URL('/products', location.origin);
      u.searchParams.set('keywords', k);
      if (c.price_to) u.searchParams.set('price_to', c.price_to);
      return u.href;
    });
  }

  // ---------------------------------------------------------------- UI

  let ui;
  function mountUI() {
    const box = document.createElement('div');
    box.id = 'trr-suit-scout';
    box.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:2147483647;background:#111;color:#eee;font:12px/1.4 system-ui,sans-serif;padding:10px;border-radius:8px;width:290px;box-shadow:0 4px 16px rgba(0,0,0,.4)';
    box.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <b>Suit Scout</b><a href="#" data-a="min" style="color:#aaa;text-decoration:none">–</a></div>
      <div data-body>
        <div data-status style="margin-bottom:6px;white-space:pre-wrap"></div>
        <div style="display:flex;flex-wrap:wrap;gap:4px">
          <button data-a="start">Start scan</button><button data-a="pause">Pause</button><button data-a="resume">Resume</button>
          <button data-a="here">Grab this page</button><button data-a="export">Export JSON</button>
          <button data-a="settings">Settings</button><button data-a="debug">Copy page lines</button><button data-a="clear">Clear data</button>
        </div>
        <div data-settings style="display:none;margin-top:6px">
          <textarea style="width:100%;height:180px;font:11px monospace"></textarea>
          <button data-a="save">Save config</button>
        </div>
      </div>`;
    box.querySelectorAll('button').forEach((b) => { b.style.cssText = 'background:#333;color:#eee;border:1px solid #555;border-radius:4px;padding:2px 6px;cursor:pointer;font-size:11px'; });
    document.body.appendChild(box);
    box.addEventListener('click', onClick);
    ui = box;
    render();
  }

  function render(msg) {
    if (!ui) return;
    const st = S.state;
    const n = Object.keys(S.items).length;
    let s = `Items stored: ${n}\nPhase: ${st.phase}${st.paused ? ' (paused)' : ''}`;
    if (st.phase === 'search') s += `\nSearch ${st.searchIdx + 1}/${st.searches.length}, page ${st.page}, queued ${st.queue.length}`;
    if (st.phase === 'products') s += `\nProducts ${st.done}/${st.queue.length + st.done}`;
    if (st.message) s += `\n⚠ ${st.message}`;
    if (msg) s += `\n${msg}`;
    ui.querySelector('[data-status]').textContent = s;
  }

  async function onClick(e) {
    const a = e.target.getAttribute && e.target.getAttribute('data-a');
    if (!a) return;
    e.preventDefault();
    if (a === 'min') { const b = ui.querySelector('[data-body]'); b.style.display = b.style.display === 'none' ? '' : 'none'; }
    if (a === 'start') startScan();
    if (a === 'pause') { S.state = Object.assign(S.state, { paused: true }); render(); }
    if (a === 'resume') { S.state = Object.assign(S.state, { paused: false, message: null }); render(); tick(); }
    if (a === 'here') { const r = grabCurrent(); render(r ? `Saved ${r.key}: ${r.title} $${r.price}` : 'Not a product page (or nothing parsed).'); }
    if (a === 'export') exportJson();
    if (a === 'debug') {
      const dump = { url: location.href, hints: productHints(document, location.href), lines: docLines(document.body), parsed: parseProductDoc(document, location.href) };
      try { await navigator.clipboard.writeText(JSON.stringify(dump, null, 1)); render('Page lines + parse copied to clipboard.'); } catch (err) { console.log(dump); render('Clipboard blocked; dump logged to console.'); }
    }
    if (a === 'settings') {
      const p = ui.querySelector('[data-settings]');
      p.style.display = p.style.display === 'none' ? '' : 'none';
      p.querySelector('textarea').value = JSON.stringify(S.config, null, 2);
    }
    if (a === 'save') {
      try { S.config = JSON.parse(ui.querySelector('textarea').value); render('Config saved.'); } catch (err) { render('Invalid JSON: ' + err.message); }
    }
    if (a === 'clear' && confirm('Delete all stored items and scan state? Export first if you want to keep them.')) {
      S.items = {}; S.state = { phase: 'idle' }; render('Cleared.');
    }
  }

  function exportJson() {
    const payload = { exported_at: nowIso(), source: 'trr-suit-scout', version: 1, items: Object.values(S.items) };
    const blob = new Blob([JSON.stringify(payload, null, 1)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `trr-suits-${nowIso().slice(0, 19).replace(/[:T]/g, '-')}.json`;
    document.body.appendChild(a); a.click(); a.remove();
    render(`Exported ${payload.items.length} items.`);
  }

  // ---------------------------------------------------------------- scan state machine

  function startScan() {
    const searches = buildSearchUrls();
    S.state = { phase: 'search', searches, searchIdx: 0, page: 1, queue: [], done: 0, paused: false, started: nowIso() };
    location.href = searches[0];
  }

  function pauseBlocked() {
    S.state = Object.assign(S.state, {
      paused: true,
      message: 'TRR showed a bot check. Complete it yourself in this tab (or browse normally for a while), then click Resume.',
    });
    render();
  }

  function grabCurrent() {
    if (!/^\/products\/.+/.test(location.pathname)) return null;
    return saveRecord(parseProductDoc(document, location.href.split('?')[0]));
  }

  function isFresh(url) {
    const hrs = cfg().refetch_after_hours;
    const it = Object.values(S.items).find((i) => i.url === url);
    return it && it.last_seen && (Date.now() - Date.parse(it.last_seen)) / 3.6e6 < hrs;
  }

  async function tick() {
    let st = S.state;
    if (isBlockPage(document)) { if (st.phase !== 'idle') pauseBlocked(); return; }
    if (st.paused || st.phase === 'idle') return;

    if (st.phase === 'search') {
      if (location.pathname === '/products') {
        const { links, next } = parseSearchDoc(document, location.href, cfg().url_filter);
        st.queue = [...new Set(st.queue.concat(links.filter((l) => !isFresh(l))))];
        render();
        await sleep(jitter());
        st = Object.assign(S.state, { queue: st.queue });
        if (st.paused) { S.state = st; return; }
        if (next && st.page < cfg().max_pages_per_search) {
          st.page += 1; S.state = st; location.href = next; return;
        }
        st.searchIdx += 1; st.page = 1;
        if (st.searchIdx < st.searches.length) { S.state = st; location.href = st.searches[st.searchIdx]; return; }
        st.phase = 'products'; S.state = st; render();
      } else {
        location.href = st.searches[st.searchIdx]; return;
      }
    }

    if (st.phase === 'products') {
      if (cfg().product_mode === 'navigate') return navigateLoop();
      return fetchLoop();
    }
  }

  async function fetchLoop() {
    while (true) {
      const st = S.state;
      if (st.paused || st.phase !== 'products') return;
      if (!st.queue.length) { finish(); return; }
      const url = st.queue[0];
      let resp;
      try { resp = await fetch(url, { credentials: 'include' }); } catch (err) { st.message = 'Network error: ' + err.message; st.paused = true; S.state = st; render(); return; }
      const html = await resp.text();
      const doc = new DOMParser().parseFromString(html, 'text/html');
      if (resp.status === 403 || isBlockPage(doc)) {
        S.state = Object.assign(st, { message: 'Product fetch got a bot check (403). Open any product page, complete the check yourself if shown, then Resume. Or set scrape.product_mode to "navigate".' , paused: true });
        render(); return;
      }
      if (resp.ok) saveRecord(parseProductDoc(doc, url));
      const s2 = S.state; s2.queue.shift(); s2.done += 1; S.state = s2; render();
      await sleep(jitter());
    }
  }

  async function navigateLoop() {
    const st = S.state;
    const here = location.href.split('?')[0];
    if (st.queue[0] === here) {
      grabCurrent();
      st.queue.shift(); st.done += 1; S.state = st; render();
      await sleep(jitter());
    }
    const s2 = S.state;
    if (s2.paused) return;
    if (!s2.queue.length) { finish(); return; }
    location.href = s2.queue[0];
  }

  function finish() {
    S.state = { phase: 'idle', message: null, lastRun: nowIso() };
    render('Scan finished. Click Export JSON.');
  }

  // ---------------------------------------------------------------- boot

  mountUI();
  // Passive capture: any suit product page you view gets recorded.
  if (new RegExp(cfg().url_filter).test(location.pathname) && !isBlockPage(document) && S.state.phase !== 'products') grabCurrent();
  tick();
})();
