# etsy-scraper

Etsy listing scraper — search results, category listings, shop fronts and
listing pages — with three interchangeable browser engines, DataDome-aware
blocking, JSON/CSV output and a run-metadata sidecar.

[![tests](https://github.com/2scraper/etsy-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/etsy-scraper/actions/workflows/tests.yml)
[![canary (on demand)](https://github.com/2scraper/etsy-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/etsy-scraper/actions/workflows/canary.yml)
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

One practical note on the endpoint: **check that `country-` is honoured on
your zone.** On the first zone tested it was not — three fresh `pid`s with
`country-us` all returned German exits, so every capture was the German
storefront. A second zone honoured it (a Comcast residential address in the
US). The row's own `url` and `currency` are what tell you which storefront
answered; do not trust the login string.

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

**And expect this to vary through the day.** Measured across 2026-09-09/10 on
one account: in the morning a fresh profile served three full pages of search
results; by late evening two fresh profiles were refused with `t=bv` through
all three retries while a third served a shop front in full, twice. Same code,
same zone, same URLs. So a red run is not by itself evidence of a code
change — try another `pid` and check the log's `t` value before going looking
for one. The numbers in this README are from the runs that got through, and
they are what the canary thresholds; the intermittency is the site, and it is
the reason the canary retries four times.

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

### Which engine can actually reach Etsy

All three take the same flags, but on THIS site they are not interchangeable
in practice, and that follows from the access table above rather than from
anything about the code.

| engine | reaches Etsy? | why |
|---|---|---|
| **`playwright_scraper.py`** | **yes** | authenticates a Scraping Browser endpoint on the WebSocket upgrade. Verified live: 184 rows over 3 pages |
| **`puppeteer_scraper.py`** | **yes** | same, via `browserWSEndpoint`. Verified live: 123 rows over 2 pages, 100% priced |
| `selenium_scraper.py` | **not with a credentialed endpoint** | chromedriver's `debuggerAddress` is a bare `host:port` with nowhere to put a password, so it cannot use the Scraping Browser API — and `--proxy-server` cannot authenticate a proxy either. It refuses up front with that reason rather than failing somewhere further in |
| `scraper_api_client.py` | no | browserless, and its own exit is a datacentre address. Measured: upstream HTTP 403 |

One caveat on the second engine, since the table above recommends it as a
working path: **pyppeteer is effectively unmaintained** — its own README
points readers at Playwright. It works here and is covered by the same
suite, but Playwright is the one to reach for unless you have a reason.

So Selenium is here for parity of behaviour — it makes the same decisions,
reports the same exit codes and is checked by the same suite — but on a site
whose only working access path is an authenticated remote browser, it has no
way in. If you need Selenium specifically, you need an Etsy that accepts a
local browser from your address, and this repo's own measurements say not to
count on that.

The two working engines were cross-checked against each other on 121 listings
they both saw: three fields differed, all three on rows where the live page
had moved between the runs (Etsy converts prices at a live rate, and ad
placement varies per impression). They share one parser, so a parsing
difference between them is not possible by construction — what the comparison
verifies is that both browsers reach the same content.

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
* **The locale is a path prefix, and your exit IP decides it.** Every market
  lives on `www.etsy.com` (`/de/`, `/uk/`, `/ca-fr/`, 27 of them), so there is
  no `--country` flag. A request for `/fr/…` from a German exit came back as
  `/de/…`, canonical and all. The currency follows the storefront you actually
  got, which is why `currency` is read from the page and never defaulted.

  **Verified on two storefronts.** The same shop, captured from a US exit and
  a German one on 2026-09-10, 39 listings present in both:

  | | US exit | German exit |
  |---|---|---|
  | `lang` | `en-US` | `de` |
  | `currency` | USD 39/39 | EUR 39/39 |
  | the same mug | $33.50 | €35.84 |
  | `shop_location` | `Wooster, Ohio` | `Ohio, Vereinigte Staaten` |
  | `material` (detail) | `Ceramic` | `Keramik` |
  | `free_shipping` (detail) | `null` — the page publishes no shipping rate | `true` |

  `sku`, `brand`, `shop_id` and `ships_from` were identical, as they must be.
  Titles matched on 35 of 39 — **Etsy translates some listing titles.**

  Prices differed by a constant 0.935 ratio, which is the exchange rate
  rather than the seller. **So a diff across two storefronts is meaningless,
  and `diff_runs.py` now refuses one** — it cannot use `source` for that the
  way the sibling repos do, because `source` is `etsy.com` on both sides. It
  compares the rows' currencies instead, and `--force` remains the escape
  hatch.
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
* **Captcha solving** (`--twocaptcha-key`) — wired, opt-in, and **measured 0
  for 2**. It is the fallback, not the plan.

  Six frame-aware attempts on 2026-09-10, each from a fresh residential exit:

  | outcome | count |
  |---|---|
  | reached `t=fe`, solved, **cookie rejected** (page came back `t=bv`, 0 rows) | 2 |
  | interstitial whose challenge iframe never appeared | 2 |
  | the exit itself was dead | 2 |

  Both purchases completed and cost $0.00145 each. Neither got in. So the
  DataDome solve requires **`--solve-captcha always`** — it does not run by
  default, because a default that bills per blocked page for a cookie that
  does not work is not a default worth having. The run says so when it
  declines.

  Worse, and worth knowing before you trust any solver's own report: a task
  built from a **fabricated** `captchaUrl` — invented `cid` and `hash`, a
  challenge that never existed — came back `status: ready` with a billable
  cookie. So "solved" is not evidence of anything. This scraper treats the
  RELOAD as the only success signal and logs whether the cookie was accepted.
  The result's `ip` field is no help either: it reported this machine's own
  egress on every task, including ones that passed a residential proxy.

  All three engines drive `DataDomeSliderTask`, but a solve is bought only
  when every one of these holds:

  | condition | why |
  |---|---|
  | the page is `t=fe` | a `t=fe` cookie is the only kind Etsy accepts |
  | a key is configured | otherwise the run says so and continues |
  | **a proxy is configured** | the cookie is bound to the address that solved it, so the solve must leave from the browser's exit — and 2Captcha's API requires the fields |
  | fewer than one solve has been bought for this page | the vendor's remedy for a failed solve is a *different exit*, which the rotation already provides |

  **Over `--cdp-endpoint` this path cannot run at all**, and the refusal says
  so: the remote browser owns its exit and does not disclose it, so there is
  no proxy to pass. That is not a gap to work around — on the measured runs
  the Scraping Browser needed no solve in the first place.

  `ERROR_CAPTCHA_UNSOLVABLE` (and the proxy-banned codes) raise a distinct
  exception from a transient failure, because they call for opposite actions:
  rotate versus retry. A transient failure is retried inside the solver; an
  unsolvable one is not retried at all.
* **Fingerprints** (`--fingerprint`) — for a local browser only; ignored with
  `--cdp-endpoint`, because the remote browser brings its own and stacking a
  second creates a mismatch rather than better cover.

All four are separate 2Captcha products behind one key.

## What the canary badge means on this site

`tests.yml` is offline and green means green, on every push.

The **canary** is a real run against etsy.com, and it runs **on demand
rather than on a schedule** — dispatch it from the Actions tab with a fresh
`ETSY_CDP_ENDPOINT` secret when the answer matters. The reason is the
credential: a Scraping Browser endpoint on this account does not survive a
day, and a daily cron against it would give either a permanently red badge or
a permanently green one that had tested nothing. The second is worse, because
green reads as "the parser still works".

When it does run, it has to distinguish two things that look the same from
the outside:

| the run | the badge | why |
|---|---|---|
| got in, data is right | green | |
| got in, **parsed 0 rows** from a search that returns tens of thousands | **red** | the tile anchor moved. This is the regression the canary exists to catch |
| got in, a column collapsed below its floor | **red** | same reason |
| crashed, or the workflow's own arguments are wrong | **red** | |
| **blocked** before parsing (`t=bv`) | green, with a **warning** | access, not code — measured intermittent per profile |
| the **endpoint refused the connection** | green, with a **warning** | usually an expired secret |
| no `ETSY_CDP_ENDPOINT` secret at all | green, with a **notice** | nothing to run |

The three warning rows also write to the run's step summary in words: *the
canary did not test anything this time.* That matters — a warning must not
read as a pass, and a green badge from a blocked run is not evidence the
parser still works.

**Why not fail on a block?** Because it would be red most days for reasons
that are nobody's bug. Measured 2026-09-09/10 on one account: a fresh profile
served three full pages in the morning; that evening two fresh profiles were
refused through all their retries while a third served a shop front in full,
twice. Same code, same zone, same URLs. And a credential expires eventually,
which would pin the badge red until someone noticed. A check that is always
red teaches everyone to ignore checks.

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
