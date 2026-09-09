# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/) as closely
as a CLI toolkit can. In practice that means: **a patch release fixes things**
— it does not promise that every flag and every default is frozen. Where a
patch changes behaviour an existing user would notice, the release notes lead
with it, so nobody discovers it from a bill or from a diff.

---

## [0.1.0] — 2026-09-09

Rewritten from scratch as a member of the 2scraper family. The previous
contents of this repository were three standalone scripts with no tests, no
shared output contract and a data model that did not match what Etsy serves;
none of it survives.

### What this is now

Three interchangeable engines (Playwright, Selenium, pyppeteer) over one
parser and one output contract, plus a browserless client for the 2Captcha
Scraper API. Three modes — `listing` (search results, `/c/` categories,
`/market/` pages), `product` (one `/listing/<id>` page) and `shop` (a
seller's whole catalogue, with the shop's own facts in the run sidecar).
JSON and CSV output whose first sixteen columns are identical to every other
repo in this family, a `<out>.meta.json` sidecar per run, seven distinct exit
codes, and 389 offline checks that need no network and no browser.

### What Etsy turned out to need — measured 2026-09-09

The access story is the opposite of this family's usual one, so it leads.

- **Etsy is behind DataDome, and a local browser on your own address will
  almost certainly not work.** Every datacentre address tested was refused
  with HTTP 403 — including one driving a *genuine* Chrome — and a headless
  Chromium through a **residential** US exit was refused too, while plain
  `curl` from that same exit got only an interstitial. DataDome scores the
  network first and the browser second.
- **The path that works is the Scraping Browser API** (`--cdp-endpoint`), and
  it worked without paying for a single solve: HTTP 200, 1.3 MB, 64 listings,
  with `Captcha.setAutoSolve` never firing.
- **`t=bv` challenges are not paid for.** DataDome's own JS object carries a
  `t` field, and 2Captcha's documentation is explicit that a `t=bv` cookie is
  not accepted. This scraper reports those as blocked and rotates or retries
  instead of buying a solve that cannot work. Only `t=fe` reaches the solver.
- **A Scraping Browser profile warms up.** One `pid` refused the first two
  requests of a session and served the third and everything after it in full,
  so a block on the first fetch is a reason to RETRY rather than to give up.
  That retry budget is separate from `--retries`, which stays yours for
  transient faults.

### The schema, and why it differs from its siblings

- **`is_ad`.** Sponsored listings are mixed into organic results — 45 of 184
  rows on a live run — and Etsy labels them in the page's own language
  ("Anzeige" / "Ad from shop"). The column therefore rests on the tile link's
  own `ls=a` / `ls=s` parameter, which is locale-independent and agreed with
  the visible label **280 times out of 280** across four captures.
- **`rating` is the LISTING's; `shop_rating` is the SELLER's.** Etsy's
  listing pages publish no per-listing rating at all (0 of 105 JSON-LD
  product nodes across three page kinds), and the stars printed on a tile are
  the shop's — proven by every seller with more than one listing on a page
  showing the same figures on all of them, 12 shops, two page kinds, no
  exceptions. Two listings of one shop meanwhile report 825 and 375 reviews
  on their own pages while their shop reports 16,679. Three numbers, three
  columns.
- **`price_is_from` and `price_max`.** A listing with variations prints a
  MINIMUM ("ab 34,00 €"), and its structured counterpart is an
  `AggregateOffer` with `lowPrice`/`highPrice` and no `price` key at all. 33
  of 184 rows on a live run. Both are tracked by `diff_runs.py`, because a
  seller widening their range moves `price_max` while `price` holds still.
- **`shop_id`, `free_shipping`, `ships_from`, `material`, `gtin`** round out
  what Etsy actually publishes.
- **`lowest_price_30d` and `ean` are NOT here.** The first is a MediaMarkt
  disclosure with no Etsy equivalent (Etsy tiles carry exactly one kind of
  strikethrough); the second is `gtin`, which is what Etsy calls it and which
  is null on almost every handmade listing.

### The parser reads the DOM first, which inverts the family default

Etsy's JSON-LD is partial, and how partial depends on the page kind: 8 of 64
listings on a search page, 61 of 65 on a category page, 36 of 40 on a shop
front. A structured-data-first parser would silently drop most of a search
page, so the tiles (`data-listing-id`) are primary and JSON-LD enriches and
cross-checks them. The consequence is that a `jsonld+dom` share of ~12% on a
search page is **correct**, and the confirmation floors are set per page kind
rather than as one number.

### Traps found and handled

- **Two rating markups were live in the same hour** — a custom element
  carrying its numbers as attributes on search pages, the older sprite
  classes on category pages. A parser written against a three-week-old
  archive snapshot would have read null ratings on every live search row.
  Both are read, and from the CLASS rather than the localised aria-label.
- **A shop front advertises two "page 2" links**, and one of them paginates
  its **reviews**. Following it would return rows from the wrong listing while
  reporting success, so pagination candidates are filtered by path.
- **`rel="next"` does not exist anywhere on this site** (0 matches on every
  page kind), so pagination rests on `?page=N` cross-checked against Etsy's
  own numbered anchors, with the site's embedded `"initial_total_pages"` as
  the bound where it publishes one.
- **The DataDome interstitial has to be waited out.** `rt='i'` is a check in
  progress with no `t` at all; its iframe was measured moving to
  `/captcha/?…&t=fe` about six seconds later. Classifying at
  `domcontentloaded` reported a block on a page that was about to become
  readable.
- **A delisted listing answers HTTP 200** with 170 KB of chrome and zero
  product data. `--mode product` names that rather than reporting an empty
  parse.
- **A handful of listings publish a price Etsy does not print** — 5 of 64 on
  a live category page, 15–20% below the tile, with the tile's figure
  repeated as a `priceSpecification` ListPrice. The run keeps what the page
  showed the visitor and logs every sku where the two disagreed.
- **The locale is a path prefix and the exit IP overrides it.** A request for
  `/fr/…` from a German exit came back as `/de/…`, canonical and all, so
  `currency` is read from the page and never defaulted.

### Fixed in the family core while doing this

These were in files copied verbatim from the sibling repos, and every one of
them is fixed there too or reported:

- **`_GROUP_SPACES` held four plain spaces** instead of space, NBSP, narrow
  NBSP and thin space, so a price written `1 234 €` with a no-break separator
  parsed as **234**. The constant is now written with explicit escapes, and
  the suite pins all four codepoints — an editor or a `sed` normalising the
  characters is what broke it, and escapes survive both.
- **A blocked page was never retried without a proxy pool.** The budget came
  from `--proxy-block-retries`, which counts EXITS and is zero when there is
  no pool — which is the ordinary case over `--cdp-endpoint`. The first live
  run of this engine abandoned page 1 on its first block without retrying
  once, on a site where retrying is what clears it.
- **`page.content()` raises mid-navigation** in Playwright, and a DataDome
  interstitial resolves by navigating — so the one moment the engine needed
  to read the page was the one moment it could crash the run.
- **`--mode shop --pages 2` fetched one page and reported `complete`.** The
  loop treated everything but `listing` as single-page. A shop front
  paginates exactly like a category listing.
- **A locked Scraping Browser profile exited 1, not 5.** `profile_locked`
  means another run holds the `pid`; reporting it as a crash sends a harness
  looking for a bug in the scraper. `EXIT_API_ERROR` is now defined once, in
  `output_writer.py`, rather than separately in the browserless client.
- **The completeness check warned on every healthy run.** It multiplied the
  fullest page by the page count, which is right on a site with a fixed page
  size and wrong here — Etsy served 62, 60 and 62 rows and the run was
  declared "short by 8". It now reports a page that is materially thinner
  than its siblings instead.
- **The `.env` check asked the filesystem, not git.** A developer's own
  `.env` beside the scripts is expected; `.gitignore` is what keeps it out of
  the repo. The check was red on exactly the machines most likely to be
  running it.

[0.1.0]: https://github.com/2scraper/etsy-scraper/releases/tag/v0.1.0
