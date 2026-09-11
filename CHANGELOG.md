# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/) as closely
as a CLI toolkit can. In practice that means: **a patch release fixes things**
— it does not promise that every flag and every default is frozen. Where a
patch changes behaviour an existing user would notice, the release notes lead
with it, so nobody discovers it from a bill or from a diff.

---

## [0.2.2] — 2026-09-11

### Fixed

- **`fingerprint_client.py` could not read the key from `.env`.** `--key`
  defaulted to `os.environ.get("TWOCAPTCHA_KEY")` and only that, so a key put
  in `.env` — exactly as §3, the README and `.env.example` instruct — worked
  for every engine and failed HERE with "No API key". A documented mechanism
  not applied on one path, which is the shape of half the defects §16 lists.

  It now reads through `env_config.env_value`, calling `load_env()` itself
  because this is a standalone entry point that no engine has necessarily run
  first. Going through the loader rather than `os.environ` is measured rather
  than stylistic: with `TWOCAPTCHA_KEY=your_2captcha_api_key_here` exported,
  the old path sent the placeholder to the API and reported "Fingerprint API
  rejected the key (401) — note this is a separate subscription", sending the
  reader off to check a subscription they never needed; the loader says
  "still set to the placeholder from .env.example" instead.

  Found on a sibling repo's first live `--fingerprint` run, then checked
  across the family before patching, per §16: five repos had it and one had
  already fixed it. Pinned by a check verified to fail on the old code —
  including that the help string does not interpolate its default, which is
  one substring away from printing a live credential to anyone who types
  `--help`.

---

## [0.2.1] — 2026-09-11

### Fixed

- **`--fingerprint` dropped `deviceScaleFactor`, so the identity
  contradicted itself.** `playwright_context_kwargs` mapped the user agent,
  the locale, the timezone and the screen onto the browser context and
  ignored the scale factor the fingerprint API returns beside them. Measured
  2026-09-11 against the live API and a live browser: a fingerprint stating
  `deviceScaleFactor: 1.25` produced a browser reporting
  `window.devicePixelRatio === 1` — the paid identity saying one thing and
  the browser another, on every run, silently, on an axis any fingerprinter
  reads for free. Playwright takes it as its own context option, so the fix
  is to pass it; verified in a live browser both ways and pinned in the
  offline suite.

  Found while auditing a new sibling repo against the family notes. All five
  repos in this family had it.

---

## [0.2.0] — 2026-09-10

> **Two changes an existing user will notice.** `diff_runs.py` now REFUSES to
> compare two runs whose rows carry different currencies, where before it
> compared them and reported every row as changed — if you diff a US run
> against a European one in a pipeline, that pipeline now stops with a
> message instead of producing a false alarm; `--force` restores the old
> behaviour. And the canary no longer runs on a schedule: it is
> `workflow_dispatch` only, so its badge reports the last run someone asked
> for rather than last night's.

### The canary runs on demand, not on a schedule

The workflow needs a Scraping Browser endpoint, and the credential in one
does not survive a day — so a nightly run would have gone red every night
once it expired, and a check that is always red teaches everyone to ignore
checks. The schedule is off, the badge says "on demand", and the file
carries the two lines to uncomment the day a long-lived credential exists.

Its exit-code handling was wrong in a way the schedule would have made
loud: the job failed on ANY non-zero exit, so a blocked run (exit 3) or a
refused endpoint (exit 5) — access conditions that test nothing about this
code — reported the same red as a real defect. Those are now warnings that
write "the canary did NOT test anything" into the run summary. Red is
reserved for a crash, bad usage, or the case worth having a canary for at
all: it got in, and parsed zero rows.

### Fixed — checks that reported more than they did

- **The fixture privacy check examined an empty string.** It collected its
  corpus by a `FIX_` prefix while every fixture in the suite is named by
  suffix, so twelve patterns — JWTs, session ids, click-tracking keys,
  DataDome blobs — ran against `""` and all twelve passed over 150 KB of
  committed captures that nothing had read. The captures are clean; with the
  collection fixed all twelve still pass, now over 150,487 characters. What
  changed is that we know it. An assertion underneath now fails if the
  corpus is ever empty again.
- **The banned-wording scan reached only the repo root**, leaving both
  Claude workflows, `tests.yml`, `canary.yml` and the four issue templates
  unscanned — the files most likely to attract product wording. It asks git
  for the tracked list now.
- **The canary's header described a schedule** its own body, forty lines
  down, explains it does not have.
- **The README did not say pyppeteer is effectively unmaintained**, though
  the engine table recommends it as one of the two that reach Etsy.

---


### The second storefront, verified — and a guard the family's own one cannot provide

§15 of the family notes asks for a SECOND country site, and the first
Scraping Browser zone could not give one: three fresh `pid`s with
`country-us` all returned German exits. A second zone honoured it, so the US
storefront is now captured and pinned.

What that turned up:

| | US exit | German exit |
|---|---|---|
| `lang` | `en-US` | `de` |
| `currency` | USD 39/39, from a bare `$` | EUR 39/39 |
| the same mug | $33.50 | €35.84 |
| `shop_location` | `Wooster, Ohio` | `Ohio, Vereinigte Staaten` |
| `material` (detail) | `Ceramic` | `Keramik` |
| `free_shipping` (detail) | `null` — no shipping rate published | `true` |

`sku`, `brand`, `shop_id`, `ships_from`, `rating` and `in_stock` were
identical, as they must be. Titles matched on 35 of 39: **Etsy translates
some listing titles.**

**`diff_runs.py` now refuses a cross-storefront comparison.** The sibling
repos guard this with `source` — eleven country hostnames — but Etsy is ONE
host, so `source` is `etsy.com` on both sides and the family's protection
silently did not apply. Measured before the fix: diffing the same shop's two
storefronts reported **39 of 39 listings as changed**, at a constant 0.935
ratio that is the exchange rate rather than the seller, and exited 0.
`--fail-on-change` would have fired on a catalogue that had not moved.

The guard compares the rows' currencies, refuses a run that holds more than
one (a run redirected mid-way is not even comparable with itself), and keeps
`--force` as the escape hatch. All three directions are pinned by tests,
including that a same-storefront diff still finds a real price change —
refusing everything would pass a naive check and break the tool.

### Verified, not assumed

* **The ad signal now holds across two languages.** `ls=a` agreed with the
  visible label 120/120 against English "Ad from shop", on top of 280/280
  against German "Anzeige" — 400 for 400 in total. That is what makes it a
  structural signal rather than one that happens to work on one locale.
* **A bare `$` resolves to USD through the locale table**, and to `null` with
  no locale — it is seven different currencies across Etsy's storefronts, so
  it cannot name itself.
* **Both decimal conventions are live**: `$34.87` on the US storefront,
  `34,87 €` on the German one.
* The two rating markups are a per-PAGE-KIND difference, not a per-locale
  one: both storefronts' search pages use the custom element.

### Added

- US fixtures (a search page and a detail page, both scrubbed and both
  verified to parse identically to their untrimmed originals) so the second
  locale is covered offline, not only in a live run.
- 35 checks over the above.

---



### The DataDome fallback, wired — and measured

`v0.1.0` shipped with the DataDome solve documented as "not covered". It is
now implemented in all three engines, and implementing it turned up a defect
that made the whole path unreachable.

**The challenge hand-off is invisible in the markup.** DataDome's device check
becomes a solvable slider by NAVIGATING ITS IFRAME, and neither the top-level
`dd` object nor the iframe's `src` attribute follows. Measured over 120
seconds on one live page:

    +3.1s   markup rt='i'   no challenge iframe yet
    +5.1s   markup rt='i'   iframe on /interstitial/
    +7.1s   markup rt='i'   iframe on /captcha/?t=fe   <- solvable
    +120s   markup rt='i'   STILL

So a detector reading only the HTML reports "interstitial, do not pay"
forever. Six live attempts through this repo's own code did exactly that on
four pages — the paid path could not fire at all. Detection now reads the
LIVE FRAME URLS, which every engine supplies through its own primitive
(`page.frames` for two of them; Selenium has to switch into each iframe and
ask it where it is, then switch back).

**And then the solve was measured, six attempts from six fresh exits:**

| outcome | count |
|---|---|
| reached `t=fe`, solved, **cookie rejected** (`t=bv` after the reload, 0 rows) | 2 |
| interstitial whose challenge iframe never appeared | 2 |
| dead exit | 2 |

Zero for two on the purchases that completed, at $0.00145 each. So the solve
is **opt-in** (`--solve-captcha always`) rather than on by default, and the
run explains itself when it declines.

**Two things the vendor's own answer does not tell you**, both measured and
both now stated in the code:

* `status: "ready"` is not "solved". A task built from a FABRICATED
  `captchaUrl` — invented `cid` and `hash` — came back ready with a billable
  cookie. The only evidence a solve worked is the page loading afterwards, so
  the engines verify by reloading and log whether the cookie was accepted.
* `ip` in the result is the REQUESTER's address, not the proxy exit. It
  reported this machine's own egress on every task.

### Added

- `captcha_solver.solve_datadome`, `DataDomeChallenge` and
  `CaptchaUnsolvable`. The unsolvable case is a distinct exception because it
  calls for a different action: the vendor's documented remedy is a different
  exit, so it is never retried from the same one, while transient failures
  are.
- `page_flow.should_pay_for`, `SOLVES_PER_PAGE` (one) and
  `datadome_cookie_domain`, so the three engines cannot disagree about when
  money is spent or where the cookie goes. The domain comes from the page the
  browser is on, not from the API's own `Domain` attribute — a cookie set on
  the wrong domain is silently ignored, which looks exactly like a solve that
  did not work and costs another one to "fix".
- 40 checks covering the paid path, including that the default does NOT spend
  money and that a real exception still gets through the noise filter.

### Fixed

- **A proxy password could reach a log.** `captcha_solver._redact` masked
  `key=`/`token=` query parameters but not credentials in a URL's userinfo —
  and this module now takes a proxy, so its errors can quote one. A
  portless-proxy refusal printed the whole URL. Masking is global and keeps
  the host and port, because which exit failed is the useful half.
- **`claude.yml` did not pin the Claude CLI to the stable channel** while its
  twin `claude-code-review.yml` did. On 2026-09-08 `latest` left no binary
  where the action looks and every run died; one of a pair of workflows
  carrying the fix and the other not is how one of them goes red for a reason
  nobody can see in the other.

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

[0.2.2]: https://github.com/2scraper/etsy-scraper/releases/tag/v0.2.2
[0.2.1]: https://github.com/2scraper/etsy-scraper/releases/tag/v0.2.1
[0.2.0]: https://github.com/2scraper/etsy-scraper/releases/tag/v0.2.0
[0.1.0]: https://github.com/2scraper/etsy-scraper/releases/tag/v0.1.0
