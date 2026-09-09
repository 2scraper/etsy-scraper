# etsy-scraper

Etsy listing scraper — search results, category listings, shop fronts and
listing pages — with three interchangeable browser engines, DataDome-aware
blocking, JSON/CSV output and a run-metadata sidecar.

[![tests](https://github.com/2scraper/etsy-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/etsy-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/etsy-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/etsy-scraper/actions/workflows/canary.yml)
![Python](https://img.shields.io/badge/python-3.9%2B-blue?logo=python&logoColor=white)
![licence](https://img.shields.io/badge/licence-MIT-green)
![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-blueviolet)
![access](https://img.shields.io/badge/needs-a%20residential%20exit-orange)

---

## Read this before anything else: Etsy needs a real exit

**Etsy is behind DataDome, and it refuses datacentre addresses outright.**
Everything below was measured on 2026-09-09; the numbers and the dates are
here so you can tell what has moved since.

| What was tried | What came back |
|---|---|
| `curl` with a browser user-agent, hosting ASN | HTTP 403, `t=bv` |
| Playwright Chromium, headless, hosting ASN | HTTP 403, `t=bv` |
| **Real Chrome**, headful, hosting ASN | HTTP 403, `t=bv` |
| Playwright Chromium via a residential US exit | HTTP 403, `t=bv` |
| **Real Chrome via the same residential US exit** | interstitial → a solvable `t=fe` challenge |
| Browserless HTTP fetch (2Captcha Scraper API) | upstream HTTP 403 |
| **2Captcha Scraping Browser API, fresh profile** | **HTTP 200, 1.3 MB, 64 listings** |

Two things follow, and they are the opposite of what most repos in this
family need:

1. **A local browser on your own address will almost certainly not work.**
   Unlike [farfetch-scraper](https://github.com/2scraper/farfetch-scraper),
   where a plain local Chromium on a clean residential IP returned 96
   products with no key and no proxy, here even a genuine Chrome was refused
   from a hosting network — and a headless Chromium was refused from a
   *residential* one. DataDome scores the network first and the browser
   second, and an automated browser fails the second test.
2. **The path that works is the Scraping Browser API**, and it worked
   *without paying for a single captcha solve*: `Captcha.setAutoSolve` never
   fired on any successful run.

### The `t` parameter decides whether money can help

A refused request is a ~1.5 KB shell carrying DataDome's own JS object. The
field that matters is `t`:

```
var dd={'rt':'c', ..., 't':'bv', 'host':'geo.captcha-delivery.com', ...}
```

* **`t=bv`** — the address or the browser is banned. 2Captcha's own
  documentation is explicit: *"If `t=bv`, it means that your ip is banned by
  the captcha and you need to change the ip address."* A solve bought here
  returns a cookie Etsy rejects. **This scraper does not pay for those.**
* **`t=fe`** — a real, solvable slider. This is the one state worth buying,
  and the only one `--solve-captcha` acts on.
* **`rt='i'` with no `t`** — an interstitial that has not decided yet. Its
  iframe was measured moving to `/captcha/?…&t=fe` about six seconds later,
  and on other attempts the page simply cleared. All three engines wait it
  out before classifying (`page_flow.settle_datadome`); deciding at
  `domcontentloaded` — 1.6 s in — reported a block on a page that was about
  to become readable.

### A Scraping Browser profile warms up. Retry before you rotate.

Measured on one `pid` in one session:

```
search page 1     3,414 B   t=bv
search page 2     3,457 B   t=bv
search page 3 1,331,088 B   64 listings
category      2,417,686 B   65 listings
shop front      790,893 B   40 listings
```

The first two requests were refused and the third and everything after it was
served in full **from the same profile**. So a block on the first fetch is a
reason to retry, not to give up: the engines carry a retry budget for exactly
this (`page_flow.BLOCK_RETRIES_WITHOUT_POOL`, three attempts) that is
separate from `--retries`, which stays yours for transient faults.

A `pid` that has been refused repeatedly does stay burnt, though. If three
retries do not clear it, use a different `pid` — reuse a handful rather than
minting one per run, since they are capped per account.

---

## What a healthy run looks like

Measured 2026-09-09 over the Scraping Browser API. Every number here is from
a real run, and these are what the canary thresholds.

**`--mode listing`, `/search?q=handmade+mug`, 3 pages:**

| | |
|---|---|
| rows | **184** (62 + 61 + 64, deduped), 184 distinct listing ids |
| price populated | **184/184 (100%)** |
| currency populated | 184/184 — all EUR, because the exit was German |
| sponsored rows | 45 flagged `is_ad`, 139 organic |
| discounted rows | 49 with an `original_price`, and **0** where it was at or below `price` |
| from-price rows | 33 (`price_is_from` — a listing with variations) |
| shop rating | 182/184 |
| `price_source` | 21 `jsonld+dom`, 163 `dom` |
| status | `complete`, exit 0 |

**`--mode shop`, `/shop/StonehousePotteryOH`, 3 pages:** 108 rows of a stated
110 (98.2%), 100% priced, 105/108 confirmed against structured data, and the
seller's own facts in the sidecar (rating 5.0 from 16,679 reviews, Ohio).

**`--mode product`, one listing:** every detail column populated — price
33.70 EUR confirmed against the buy box, the listing's own rating (5.0 from
827 reviews), 8 images, the breadcrumb category, `free_shipping`, `ships_from`
US, `material` "Keramik".

### That ~12% `jsonld+dom` share on a search page is CORRECT

Etsy publishes JSON-LD for only a fraction of the listings it renders, and
the fraction depends on the page kind:

| page | listings rendered | in Etsy's `ItemList` |
|---|---|---|
| search | 64 | **8** |
| category | 65 | 61 |
| shop front | 40 | 36 |

So this repo **inverts the family's usual order**: the DOM tiles are the
primary source and JSON-LD enriches and cross-checks them. A confirmation
share of ~12% on a search page is the maximum achievable, not a defect, which
is why the floors are set per page kind (`CONFIRMATION_FLOOR` in each engine).

---

## Install

```bash
git clone https://github.com/2scraper/etsy-scraper.git
cd etsy-scraper
pip install -r requirements.txt          # core: beautifulsoup4, requests
pip install -r requirements-playwright.txt && playwright install chromium
```

**Install exactly one engine.** Playwright and pyppeteer pin incompatible
`pyee` versions, and pyppeteer and selenium collide on `urllib3`. They do run
side by side in practice, but `pip check` will report the conflict and pip may
resolve it by downgrading something you wanted. Use a virtualenv per engine if
you need more than one.

```bash
pip install -r requirements-selenium.txt     # or
pip install -r requirements-puppeteer.txt
```

## Credentials go in `.env`, never on the command line

A secret in `argv` is readable by anything that can run `ps`, and it lands in
your shell history. Copy `.env.example` to `.env` and fill it in:

```bash
cp .env.example .env
python3 env_config.py          # prints what was picked up, WITHOUT the secrets
```

```
TWOCAPTCHA_KEY=...
ETSY_CDP_ENDPOINT=ws://{login}-zone-scraping_browser-country-us-pid-{profile}:{password}@cb.2captcha.com:9222
ETSY_PROXY=http://{user}:{password}@na.proxy.2captcha.com:2334
ETSY_URL=https://www.etsy.com/search?q=handmade+mug
```

Precedence, highest first: **an explicit flag → an exported environment
variable → `.env` → the default.** A `.env` never overrides something you
typed, and it never clobbers a CI secret or a `direnv` value.

## Run

```bash
# search results, three pages, JSON and CSV
python3 playwright_scraper.py \
    --url "https://www.etsy.com/search?q=handmade+mug" \
    --pages 3 --format both --out mugs

# a category listing
python3 playwright_scraper.py \
    --url "https://www.etsy.com/c/home-and-living/kitchen-and-dining/drink-and-barware/drinkware/mugs" \
    --pages 5

# one listing, with description, materials, shipping origin and every image
python3 playwright_scraper.py --mode product \
    --url "https://www.etsy.com/listing/519688604/handthrown-pottery-mug"

# a seller's whole catalogue, with the shop's own rating in the sidecar
python3 playwright_scraper.py --mode shop \
    --url "https://www.etsy.com/shop/StonehousePotteryOH" --pages 3
```

The other two engines take the same flags: `selenium_scraper.py`,
`puppeteer_scraper.py`. A fourth, browserless client
(`scraper_api_client.py`) fetches through the 2Captcha Scraper API with no
local browser at all — but see the access table above: Etsy refused it.

### Modes

| `--mode` | URL it expects | What it adds |
|---|---|---|
| `listing` (default) | `/search?q=…`, `/c/…`, `/market/…` | paginated, ~62 rows per page |
| `product` | `/listing/<id>/…` | description, all images, material, `gtin`, `ships_from`, `free_shipping`, and the LISTING's own rating |
| `shop` | `/shop/<name>` | that seller's catalogue as listing rows, plus the shop's name, location, rating and review count in the sidecar |

## Output

`<out>.json`, `<out>.csv` and `<out>.meta.json`. The row schema is shared
across this scraper family — the first sixteen columns are identical in every
repo in it — with Etsy's own columns appended. See
[`sample_output.json`](sample_output.json), which is cut from a real run.

| column | notes |
|---|---|
| `source` `scraped_at` `url` `sku` `title` | `sku` is Etsy's listing id |
| `brand` | the SHOP name — on a marketplace of makers the seller is the brand, and Etsy's own `brand.name` says so |
| `price` `currency` `original_price` `discount_pct` | `discount_pct` is COMPUTED from the two prices, never read from the printed badge |
| `rating` `review_count` | the LISTING's own — null on listing rows, see below |
| `in_stock` `image_url` `category` `price_source` | |
| `page` `position` | where in the listing this row was |
| **`is_ad`** | **sponsored placement.** 45 of 184 rows on a live search run |
| `shop_id` `shop_rating` `shop_review_count` | the SELLER's, see below |
| `price_is_from` `price_max` | the listing has variations and `price` is a MINIMUM |
| `free_shipping` `ships_from` `material` `gtin` | `--mode product` only |
| `description` `images` | `--mode product` only |

### Exit codes

`0` ok · `1` crash · `2` bad usage · `3` blocked · `4` zero products ·
`5` remote API error (a locked Scraping Browser profile is the common one) ·
`6` partial

**A run that finds nothing writes nothing** — last night's good output is not
replaced with `[]`. Pass `--allow-empty` if an empty result is what you want
recorded.

## Traps that look like bugs

Read these before filing an issue. Each one is measured, and each one is the
site rather than the scraper.

* **`rating` and `review_count` are null on every listing row.** Etsy's
  listing pages publish no per-listing rating at all — 0 of 105 JSON-LD
  product nodes across a search page, a category page and a shop front carry
  an `aggregateRating`. The stars printed on a tile are the **SHOP's**, and
  they land in `shop_rating` / `shop_review_count`. That is not a guess: on
  every page captured, every seller with more than one listing on it showed
  the *same* rating and count on all of them — 12 shops, two page kinds, no
  exceptions. Meanwhile two listings of one shop report 825 and 375 reviews
  on their own detail pages, while their shop reports 16,679. Three different
  numbers; three different columns.
* **Sponsored listings are mixed into organic results** and Etsy labels them
  in the page's own language ("Anzeige" on a German page, "Ad from shop" on
  an English one). `is_ad` therefore comes from the tile link's own `ls=a` /
  `ls=s` parameter, which is locale-independent — verified against the text
  label across four captures with **280 agreements and zero disagreements**.
  A listing can appear both as an ad and organically on one page, so a dedupe
  on `sku` collapses the pair.
* **`price` can be a minimum.** 33 of 184 rows on a live run carry
  `price_is_from`, because the listing has variations and Etsy prints
  "ab 34,00 €" / "from $34.00". Its structured counterpart is an
  `AggregateOffer` with `lowPrice`/`highPrice` and **no `price` key at all**.
* **The locale is a path prefix, and your exit IP can override it.** Every
  market lives on `www.etsy.com` (`/de/`, `/uk/`, `/ca-fr/`, 27 of them), so
  there is no `--country` flag. But a request for `/fr/…` from a German exit
  was **redirected to `/de/…`**, canonical and all. The currency follows the
  storefront you actually got, which is why `currency` is read from the page
  and never defaulted.
* **A handful of listings publish a price Etsy does not print.** 5 of 64 rows
  on a live category page carry `offers.price` 15–20% below the figure on the
  tile, with the tile's figure repeated as a `priceSpecification` ListPrice
  and no sale label anywhere. This scraper keeps **what the page showed the
  visitor** and logs the sku of every row where the two disagreed.
* **`gtin` is null on almost every row.** Most handmade listings have no
  barcode. It is kept because where it *is* present it is the only field that
  makes an Etsy row joinable against another retailer's data.
* **A delisted listing answers HTTP 200** with 170 KB of site chrome and zero
  product data. `--mode product` says so by name rather than reporting an
  empty parse.
* **`rel="next"` does not exist on this site** — 0 matches on every page kind.
  Pagination runs on `?page=N`, cross-checked against Etsy's own numbered
  anchors, with the site's own `"initial_total_pages"` as the bound where it
  publishes one.
* **A shop front advertises two "page 2" links**, and one of them paginates
  its **reviews**. Following it would return rows from the wrong listing while
  reporting success, so every engine filters candidates by path first.

## What the paid products buy here

* **Scraping Browser API** (`--cdp-endpoint`) — the access path that works.
  A remote browser with a residential exit and a persistent profile. **One
  live connection per `pid`**, so `--concurrency` is refused with it; use
  several `pid`s, one run each.
* **Proxies** (`--proxy`, `--proxy-file`) — for a local browser. Note that a
  residential exit was **not sufficient** on its own here: a headless
  Chromium through one was still refused. Note also that the 2Captcha
  residential gateway answered **SOCKS5 only** on the ports tested
  (`na.proxy.2captcha.com:2333` and `:2334`), and Chromium cannot
  authenticate a SOCKS5 proxy — so these flags need the HTTP endpoint.
* **Captcha solving** (`--twocaptcha-key`) — the **fallback**, and only for a
  `t=fe` page. Measured over five attempts on five fresh residential exits:
  two reached `t=fe` and both returned `ERROR_CAPTCHA_UNSOLVABLE`, two never
  reached `t=fe`, and one exit was dead. Budget accordingly.
* **Fingerprints** (`--fingerprint`) — for a local browser only; ignored with
  `--cdp-endpoint`, because the remote browser brings its own and stacking a
  second creates a mismatch rather than better cover.

All four are separate 2Captcha products behind one key.

## Comparing two runs

```bash
python3 diff_runs.py yesterday.json today.json --fail-on-change
```

Refuses to compare runs whose `status` is not `complete`, or whose `mode`
differs: a partial run's un-fetched pages would otherwise read as delisted
listings. A price difference that comes with a `price_source` difference is
reported as `source_changed` rather than `changed`, because that says
something about our two snapshots and not about Etsy.

## Tests

```bash
python3 smoke_test.py     # offline: no network, no browser
pytest                    # the same checks, through pytest
```

The fixtures are real captures, trimmed and then **verified to parse
identically to the untrimmed original**. The detail fixture's review list is
scrubbed of the customer's name and words; the checks need the structure of a
review, not the person.

## Not supported

`etsy.me` (the URL shortener — resolve it first), `help.etsy.com`,
`community.etsy.com`, `partners.etsy.com`. Each is refused **with the reason**
rather than as "not an Etsy site", which would be false and send you looking
for a typo.

## Licence and scope

MIT. This scrapes **public** Etsy pages — search results, category listings,
shop fronts and listing pages — the same pages a visitor sees without signing
in. It does not touch an account, a cart or a checkout. See
[SECURITY.md](SECURITY.md) and [CONTRIBUTING.md](CONTRIBUTING.md).
