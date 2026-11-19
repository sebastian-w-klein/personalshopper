# Personal Shopper: TRR Suit Scout

A personal tool for finding mispriced men's suits on The RealReal.

The RealReal blocks server-side requests (PerimeterX), so collection happens **in your own signed-in browser** through a userscript. A small stdlib-only Python script then filters the listings to your size and taste and ranks them by how underpriced they look.

```
userscript/trr-suit-scout.user.js   collects listings in the browser and exports JSON
analysis/analyze.py                 filters, scores, writes report.html + report.csv
config.example.json                 your size, designers, budget, fabrics, scrape settings
tests/                              node --test tests/parser.test.js ; python -m unittest discover tests
```

## Setup

1. Install [Tampermonkey](https://www.tampermonkey.net/) or Violentmonkey in Chrome. On recent Chrome, also turn on **Allow User Scripts** for the extension (chrome://extensions → Tampermonkey → Details).
2. Create a new script and paste in `userscript/trr-suit-scout.user.js`.
3. `cp config.example.json config.json`, then replace the EXAMPLE values with your measurements, designers and max price.
4. On therealreal.com, open the **Suit Scout** panel (bottom right) → **Settings**, paste your `config.json`, and click **Save config**.

## Collecting

- **Start scan**: for each search (`scrape.searches`, plus `"<designer> suit"` for every designer when `add_designer_searches` is on), the tab loads `/products?keywords=…&price_to=…` and follows the **Next page** link (cursor `after=` param) up to `max_pages_per_search` pages. It queues product URLs that match `url_filter` (`/products/men/clothing/suiting/`). It then loads each product page, either by same-origin `fetch` (`product_mode: "fetch"`) or by navigating the tab (`"navigate"`), waiting a random `delay_ms` between requests.
- **Passive capture**: any suit page you open normally is recorded too.
- **Grab this page**: records the product page you are on.
- **Export JSON**: save the downloaded file into `data/`. The script keeps everything in Tampermonkey storage and builds a price history each time it sees an item again, so it's worth scanning every few days to catch markdowns.

Each product page is parsed for: designer, title, price, original/struck price and "% off", Est. Retail, size, the "Estimated item measurements" block, condition grade and notes, the final-sale flag, sold state, Item #, description, fabric composition and og:image.

### Bot checks

If TRR shows "Access to this page has been denied", or `fetch` gets a 403, the scan **pauses** and tells you. Complete any check yourself in that tab, then click **Resume**. If fetches keep getting 403, switch `product_mode` to `"navigate"`, which loads pages exactly as you would. The script never tries to solve or get around the check. Keep the delays generous.

## Analysing

```bash
python analysis/analyze.py --config config.json data/*.json
```

This writes `report.html` (sortable, with thumbnails and links) and `report.csv`, and prints the top candidates. Add `--all` to show excluded listings, greyed out with the reason.

**Filters:** `max_price`, `designers` (with aliases), `fabrics.include_any`/`exclude`, `min_condition`, `exclude_final_sale`, and fit.

**Fit:** each measurement in `profile` has `target`, `min` and `max` in inches. Build tailoring room into min/max. Sleeves and trousers can be shortened easily but only lengthened a little, and shoulders can barely be changed. Flat measurements (chest < 30", waist < 24") are doubled automatically. Listings without measurements fall back to the size label (EU 50 → US 40) and are marked *unverified*, which costs a score penalty.

**Mispricing score:**

| Component | How |
|---|---|
| Condition-normalised price | price ÷ condition factor (Pristine 1.0, Excellent 0.92, Very Good 0.82, Good 0.68, Fair 0.5) |
| Comp estimate | median normalised price for the same designer and size (≥ `min_comps`); falls back to the designer across all sizes. Sold items count as comps. |
| Retail estimate | Est. Retail × the designer's median price/retail ratio (falls back to the all-designer ratio) |
| Expected price | weighted geometric mean of the two estimates |
| Score | 100 × (1 − price/expected) + `markdown_weight` × markdown % − penalties (final sale, wear notes, unverified fit) |

Markdown % comes from the item's own price history, or from the struck-through original price shown on the page.

> Comps are only as good as the data you collect. That's why `scrape.price_to` is set well above your `max_price`: if you only collect items under budget, every designer median gets dragged down and nothing looks cheap.

## Handbags

[`handbags.example.json`](handbags.example.json) is a spec for searching handbags instead of suits (`"category": "handbags"`). Copy it to `handbags.json` and edit:

| Spec | What it does |
|---|---|
| `bag.styles` | shoulder, crossbody, tote, top handle, flap, clutch, … (`include_any` / `exclude`) |
| `bag.colors` | read from the title first, then the description (hardware and lining wording is ignored) |
| `bag.materials` | lambskin, calfskin (incl. Togo/Epsom/…), caviar, suede, canvas, nylon, exotic, … |
| `bag.hardware` | gold, silver, palladium, ruthenium, … |
| `bag.size_words` | nano / mini / small / medium / PM / MM / GM… in the title |
| `bag.dimensions` | width, height, depth, handle_drop, strap_drop in inches. These are hard limits because bags can't be tailored. `require_dimensions` decides whether listings without measurements are dropped or marked unverified |
| `designers[].models` | e.g. Lady Dior, Saddle, Classic Flap. Comps are grouped by designer + model (or by style if no model is found). `models_only` keeps only listed models |
| `exclude_keywords`, `prefer_includes` | drop listings mentioning e.g. "repair"; small bonus for dust bag / box / authenticity card |

The pricing score is the same as for suits. To scan for bags, paste `handbags.json` into the userscript's Settings box and click Start scan. Designer searches use `designer_search_suffix` (e.g. "Chanel bag"). Paste `config.json` back in for suits. Export as usual, then:

```bash
python analysis/analyze.py --config handbags.json --out bags data/*.json
```

Each config only analyses listings whose URL matches its `scrape.url_filter`, so suit and bag exports can sit together in `data/`. The bag URL pattern hasn't been checked against a live listing, so look at one bag's URL and adjust the regex if needed.

## Caveats

- The product-page parser is text-based and was written without a live page to calibrate against. If a field comes back empty, open a listing, click **Copy page lines**, and fix the matching regex in `parseProductLines` (add the real lines as a test case in `tests/parser.test.js`).
- This is for personal use at a human pace. Respect TRR's terms and don't run it as a high-volume crawler.
