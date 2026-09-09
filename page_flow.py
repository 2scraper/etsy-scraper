"""
page_flow.py
------------
Etsy's page-state and pagination policy, shared by all three engines.

Why this module exists, when part of this family keeps each engine
self-contained: Etsy answers a request in five different ways and four of
them want a different response.

    content     parse it
    blocked     HTTP 403 behind DataDome, whose own `t=bv` says the address
                or the browser is banned. Nothing on the page to solve —
                but DO retry, see RETRY_ON_BLOCKED below.
    captcha     the same shell with `t=fe`: a real, solvable challenge. This
                is the paid path, and the only state worth spending on.
    empty       a real page with no listings on it (an exhausted listing, a
                hub category) — report it as such, do not retry it and do
                not go looking for a proxy problem.
    unavailable a /listing/ page for an item Etsy has delisted. HTTP 200,
                170 KB of site chrome, zero product data. Detected in
                product_parser.parse_product_page, which says so by name.

Three copies of that triage would drift, and the drift would be silent — an
engine that reports a hub category as blocked exits 3 where its twin exits 4
on the same URL. The family already shares `output_writer.finish_run()` for
exactly this reason; this is the same argument applied to the decisions that
come before it.

What this module deliberately does NOT contain
----------------------------------------------
No scrolling and no lazy-load hydration. Etsy server-renders its whole grid:
measured on the live captures, `div[data-listing-id]` counts 129 nodes for 64
listings on a search page, 65 on a category page and 41 on a shop front, in
the FIRST response, with no scrolling of any kind. Porting the sibling repo's
scroll loop would be dead code that looks load-bearing.

The functions here are either pure or driven through small callables, so each
engine passes its own driver's primitives and keeps its browser plumbing to
itself:

    count(selector) -> int                how many elements match
    content() -> Optional[str]            current HTML, None if unavailable
    current_url() -> str                  the URL the browser is on
    sleep(ms) -> None                     the driver's own wait

Deliberately no `evaluate(js)`: passing JavaScript from here would decide its
dialect for every driver, and they disagree — Playwright and pyppeteer take
`() => expr`, while Selenium's execute_script takes `return expr;`.

Every value here is measured; the numbers are in the comments and in the
README, and the measurements are dated because Etsy's markup moves — two
different rating markups were live in the same hour on 2026-09-09.
"""

import logging
from typing import List, Optional
from urllib.parse import (urlparse, urlunparse, parse_qsl, urlencode, unquote,
                          urljoin)

from product_parser import (TRACKING_PARAMS, datadome_verdict,
                            detect_page_state, is_datadome_interstitial,
                            page_number_from_url, page_url, total_pages)

logger = logging.getLogger("page_flow")


# What "the page has painted" means, per mode.
#
# The listing form matches the tile DIV and not `a[href*="/listing/"]`, and
# that is not a style preference: the same live capture counted far more
# listing anchors than products, because a tile links to its product from the
# image, the title and the favourite button, and the page's own navigation
# links to listings too. Counting links would report the grid as several
# times the size it is, which matters the moment a threshold is compared
# against it.
READY_SELECTOR_LISTING = "div[data-listing-id]"

# A detail page's anchor is its buy box, which is the one element that has to
# be there for the page to be worth parsing. NOT the JSON-LD script: a CSS
# selector cannot usefully wait on script content, and a delisted listing
# renders the chrome with neither.
READY_SELECTOR_PRODUCT = ("[data-buy-box-region='price'], "
                          "[data-buy-box-region='form']")

# THE STANDARDS-BASED PAGINATION SIGNAL DOES NOT EXIST ON THIS SITE.
#
# Measured on every live capture: `link[rel=next]` and `a[rel=next]` match
# ZERO elements on a search page, a category page and a shop front alike. The
# family's most-durable layer is simply absent here, so pagination rests on
# the URL convention (layer 2) and on the data (layer 3) — see
# `next_page_selector` and `pagination_agrees` below.
#
# The two selectors are still listed FIRST in what `next_page_selector`
# builds, because they cost nothing and would start working the day Etsy
# publishes them. That is not dead code: it is the durable layer, kept ready,
# with a measurement saying it is currently unoccupied.
NEXT_PAGE_SELECTOR = 'link[rel="next"], a[rel="next"]'


def next_page_selector(page_num: int) -> str:
    """A selector matching only the link to the page AFTER `page_num`.

    Etsy renders its pagination as a row of numbered links plus a previous
    and a next button, and the two buttons are told apart only by their
    screen-reader text — "Next" / "Weiter" — which is written in the page's
    own language. So a fixed selector for "the next button" would either be
    locale-dependent or match the previous button too: on page 3 of a search
    listing, `a.wt-btn--icon[href*="page="]` matches the link BACK to page 2
    first.

    Deriving the selector from the page number instead sidesteps the whole
    problem and is exact. Verified on the live captures: on search page 3 it
    matches three nodes, all pointing at `page=4` and nothing else; on
    category page 1, three nodes, all `page=2`.

    The `page=N&` / `page=N`-at-the-end pair matters: a bare
    `[href*="page=4"]` would also match `page=40`, which is a real page on a
    20-page listing.
    """
    nxt = page_num + 1
    return (NEXT_PAGE_SELECTOR
            + ', a[href*="page=%d&"], a[href$="page=%d"]' % (nxt, nxt))


# How many tiles must appear before a LISTING counts as loaded rather than as
# a lucky single match. Must be > 1: waiting for one resolves on an unrelated
# element long before the grid paints.
#
# Five, against a measured floor of 41 tile nodes on every listing page
# captured (41 on a shop front, 65 on a category page, 129 on a search page).
# Five leaves room for a short final page — a shop with three listings is an
# ordinary thing on this marketplace — without waiting out the timeout on it.
MIN_CARD_MATCHES = 5

# A listing gets the full 20s: a 2.4 MB grid can genuinely be slow to paint
# through a remote browser. A detail page gets 10s, because its content is
# either in the markup that arrived or it is not.
CONTENT_TIMEOUT_MS = {"listing": 20000, "product": 10000, "shop": 20000}

# Retried, and this is measured rather than optimistic. On the Scraping
# Browser API a fresh profile answered the first TWO requests of a session
# with the DataDome 403 shell and served the third and every one after it, in
# full, from the same profile:
#
#     search page 1   3,414 B      t=bv
#     search page 2   3,457 B      t=bv
#     search page 3   1,331,088 B  64 listings
#     category        2,417,686 B  65 listings
#     shop front        790,893 B  40 listings
#
# So a `t=bv` on the first fetch of a run is NOT a reason to abandon the
# profile — retrying the same one is what cleared it. A rotation is still the
# answer if the retries run out, which is what `--proxy-block-retries` is
# for.
RETRY_ON_BLOCKED = True

# How many times a blocked page may be re-fetched when there is NO proxy pool
# to rotate through — which is the ordinary case on this site, because the
# access path that works is `--cdp-endpoint` and a Scraping Browser session
# brings its own exit.
#
# This is a SEPARATE budget from `--retries`, deliberately. `--retries` is
# the user's allowance for transient faults (a navigation timeout, a flap);
# spending it on a bot-manager decision would leave nothing for the faults it
# was meant for, and the two want different pauses. It is also separate from
# `--proxy-block-retries`, which counts EXITS and is meaningless with no pool
# — that is the gap that made the first live run of this engine give up on
# the first block without retrying once, on a site where retrying is what
# clears it.
#
# Three, from the measurement in RETRY_ON_BLOCKED above: the profile that
# refused two requests served the third. A fourth attempt has never been
# observed to help where three did not.
BLOCK_RETRIES_WITHOUT_POOL = 3

# How long to let a DataDome INTERSTITIAL make up its mind, and how often to
# look. `rt:'i'` is a device check in progress, not a verdict: measured
# 2026-09-09, its iframe moved from `/interstitial/` to `/captcha/?…&t=fe`
# about six seconds after load, and on other attempts the page cleared to
# content instead. Classifying at `domcontentloaded` — 1.6 s in on the run
# that found this — sees only the interstitial and calls a page blocked that
# was about to become either readable or solvable.
#
# 12 rounds of 1.5 s is 18 s, which covers the ~6 s transition with room for
# a slow exit while staying under the 20 s a listing gets to paint.
INTERSTITIAL_ROUNDS = 12
INTERSTITIAL_PAUSE_MS = 1500


def settle_datadome(content, sleep, rounds: int = INTERSTITIAL_ROUNDS,
                    pause_ms: int = INTERSTITIAL_PAUSE_MS, frames=None):
    """Let an unresolved DataDome check finish, and return the final HTML.

    Returns as soon as the page is anything OTHER than an interstitial — a
    served page, a `t=fe` challenge, or a `t=bv` refusal — so a page that was
    never an interstitial in the first place costs nothing.

    An EMPTY or unavailable read counts as "not settled yet", not as
    resolved, and that distinction was earned on a live run: an interstitial
    resolves by NAVIGATING, and a driver asked for the document mid-navigation
    answers with nothing (Playwright raises outright). Treating that as the
    final answer would hand the caller the interstitial it started with and
    report a block on a page that had just cleared itself.

    Driven through the caller's own `content()`, `sleep(ms)` and — where a
    live browser exists — `frames()` primitives, so this stays free of any
    driver's dialect; each engine passes its own and all three then wait
    exactly the same way. `content()` must return None rather than raise when
    the document cannot be read.

    `frames()` IS WHAT MAKES THIS TERMINATE. The hand-off from the device
    check to the solvable slider happens in the iframe and NOT in the
    top-level markup, which was measured holding `rt='i'` for two solid
    minutes while the iframe had moved on at seven seconds. Without the frame
    list this function waits out its whole budget on a page that resolved
    almost immediately, and then reports a block.
    """
    def look():
        html = content()
        urls = frames() if frames else None
        return html, urls

    html, urls = look()
    if html and not is_datadome_interstitial(html, urls):
        return html
    last_seen = html
    for attempt in range(1, rounds + 1):
        sleep(pause_ms)
        html, urls = look()
        if not html:
            # Mid-navigation. Keep the last readable markup so the caller is
            # never handed None, and keep waiting.
            continue
        last_seen = html
        if not is_datadome_interstitial(html, urls):
            verdict = datadome_verdict(html, urls)
            logger.info("DataDome interstitial resolved after %.1fs into %s.",
                        attempt * pause_ms / 1000.0,
                        "content" if verdict is None else "t=%s" % verdict)
            return html
    logger.info("DataDome interstitial did not resolve within %.0fs; "
                "treating it as a block.", rounds * pause_ms / 1000.0)
    return last_seen


def ready_selector(mode: str) -> str:
    return READY_SELECTOR_PRODUCT if mode == "product" else READY_SELECTOR_LISTING


def min_matches(mode: str) -> int:
    """How many readiness anchors mean "loaded", for this mode.

    A listing needs several. A detail page has exactly one buy box, so
    requiring more than one would time out on every successful fetch — the
    threshold has to follow the mode or it silently inverts.
    """
    return 0 if mode == "product" else MIN_CARD_MATCHES


def content_timeout_ms(mode: str) -> int:
    return CONTENT_TIMEOUT_MS.get(mode, 20000)


def classify(html: Optional[str], status: Optional[int] = None,
             url: Optional[str] = None, frame_urls=None) -> str:
    """The page's state, as the engines see it.

    A thin wrapper over `product_parser.detect_page_state` so that every
    engine reaches the policy through one name, and so that a future change
    to how a state is decided lands in one place rather than three.

    `frame_urls` is not optional in spirit. A DataDome challenge hands off to
    its solvable form by NAVIGATING ITS IFRAME, and the top-level markup does
    not follow — measured holding `rt='i'` for 120 seconds while the iframe
    sat on `t=fe`. An engine that omits its frame list therefore reports
    "blocked" on pages that were solvable, which is how the paid path became
    unreachable in the first place. Pass it wherever a live browser exists;
    a `--dump-html` file read back has no frames and honestly cannot.
    """
    return detect_page_state(html or "", status=status, url=url,
                             frame_urls=frame_urls)


# What each state means for the run. Kept as data rather than as three copies
# of an if-chain, so an engine cannot quietly disagree with its twins about
# whether a page is worth retrying or worth paying for.
#
#   retry     fetch it again, possibly from another exit
#   solve     hand it to the captcha solver, if one is configured
#   blocked   count it towards the blocked-page tally that decides exit 3
STATE_POLICY = {
    "content": {"retry": False, "solve": False, "blocked": False},
    # Retried but NOT solved, and the distinction is the whole reason this
    # site needs its own policy. A `t=bv` DataDome page carries no solvable
    # challenge — 2Captcha's own documentation says the cookie will not be
    # accepted — so paying for it spends money to learn nothing. It is still
    # worth retrying, because on a Scraping Browser profile that is exactly
    # what cleared it (see RETRY_ON_BLOCKED).
    "blocked": {"retry": RETRY_ON_BLOCKED, "solve": False, "blocked": True},
    # `t=fe`: a real slider, and the one state where a solve is worth buying.
    "captcha": {"retry": True, "solve": True, "blocked": True},
    # NOT retried. An empty page is a correct answer to the question that was
    # asked — a hub category has no listings, and page 21 of a 20-page
    # listing has none either. Retrying it would spend the user's budget on
    # getting the same right answer again.
    "empty": {"retry": False, "solve": False, "blocked": False},
}


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, {}).get("retry", False)


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, {}).get("solve", False)


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, {}).get("blocked", False)


# How many times a DataDome solve may be BOUGHT for one page.
#
# One, and that is a deliberate ceiling rather than a placeholder. The
# vendor's own remedy for a failed solve is a different exit, so a second
# purchase from the same address is money spent to be told the same thing —
# and the engines' block-retry loop already rotates and reloads, which
# produces a fresh challenge to solve from somewhere else. Measured
# 2026-09-09: two `t=fe` pages, two ERROR_CAPTCHA_UNSOLVABLE.
#
# `captcha_solver.solve_datadome` retries TRANSIENT failures internally (a
# task that would not create, a poll that errored) and raises
# `CaptchaUnsolvable` on the first "not from here", so this number counts
# purchases and not attempts.
SOLVES_PER_PAGE = 1


# The DataDome solve is OPT-IN on this site, and that is a measurement rather
# than caution. Six frame-aware attempts on 2026-09-10, each from a fresh
# residential exit:
#
#     2 reached t=fe, both solved (billed $0.00145 each) — and BOTH cookies
#       were REJECTED: the page came back t=bv afterwards, 0 rows
#     2 were interstitials whose challenge iframe never appeared at all
#     2 exits were dead
#
# Zero for two on the purchases that completed. A default that spends a
# user's money at that rate is not defensible, so the path requires
# `--solve-captcha always` — and the log says why when it declines.
#
# Worse, the vendor's "ready" is not evidence of anything: a task built from
# a FABRICATED captchaUrl (invented cid and hash, a challenge that never
# existed) came back ready with a billable cookie. So there is no way to tell
# a good solve from a worthless one except by reloading the page, which is
# what the engines do.
DATADOME_SOLVE_IS_OPT_IN = True


def should_pay_for(verdict: Optional[str],
                   solve_mode: str = "when-blocked") -> bool:
    """Whether a DataDome page is worth buying a solve for.

    One place, so the three engines cannot disagree about when money is
    spent. `verdict` is `product_parser.datadome_verdict`'s answer:

        "fe"  a real slider              -> yes, IF opted in (see above)
        "bv"  address or browser banned  -> NO; the cookie is not accepted
        "i"   still deciding             -> no; wait it out first
        ""    DataDome, `t` unstated     -> no; nothing says it is solvable
        None  not a DataDome page        -> no

    `solve_mode` is the `--solve-captcha` value. `always` is the opt-in;
    the default `when-blocked` declines, because on this site a DataDome
    block is the ordinary case and paying for it by default would bill a
    user per blocked page for a cookie measured not to work.
    """
    if verdict != "fe":
        return False
    if DATADOME_SOLVE_IS_OPT_IN and solve_mode != "always":
        return False
    return True


def datadome_cookie_domain(url: str) -> str:
    """The domain to set a solved DataDome cookie on.

    Derived from the URL the browser is actually on rather than taken from
    the API's own `Domain` attribute, because a cookie set on the wrong
    domain is silently ignored — which looks exactly like a solve that did
    not work, and costs another one to "fix".

    Leading dot, so it covers the locale-prefixed paths and any subdomain the
    site redirects to.
    """
    host = urlparse(url or "").hostname or "www.etsy.com"
    if host.startswith("www."):
        host = host[4:]
    return "." + host


def comparable(url: str) -> str:
    """`url` reduced to the parts that decide WHICH PAGE it addresses.

    Etsy hangs a dozen per-impression parameters on its own links
    (`click_key`, `ref`, `sr_prefetch`, `ls`, …), so a strict string
    comparison would declare a disagreement on every run and every run would
    refuse `--concurrency` for no reason. They are dropped here, using the
    one list `product_parser` also uses for cleaning row URLs — two lists
    that can disagree is how the engines and the parser end up with
    different ideas of which page a URL is.

    The path is percent-DECODED as well, because a category or shop name
    with a non-ASCII character is written encoded in a URL a user pastes and
    decoded in some of Etsy's own links.
    """
    parts = urlparse(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.split("[")[0] not in TRACKING_PARAMS]
    return urlunparse(parts._replace(path=unquote(parts.path),
                                     query=urlencode(sorted(kept)),
                                     fragment=""))


def _same_listing(current_url: str, candidate: str) -> bool:
    """Whether `candidate` paginates the SAME listing as `current_url`.

    A hard requirement, and not a tidiness one. A shop front advertises TWO
    links to "page 2":

        /de/shop/StonehousePotteryOH?page=2&sort_order=custom   its items
        /de/shop/StonehousePotteryOH/reviews?page=2             its REVIEWS

    Both match a page-number selector. Following the second one fetches the
    shop's review pages instead of its catalogue — a run that reports success
    and returns rows from the wrong listing entirely, which is the "a
    selector matching the WRONG element is worse than one matching nothing"
    failure in its purest form.

    Comparing paths is what tells them apart, and it costs one line.
    """
    return (unquote(urlparse(urljoin(current_url, candidate)).path).rstrip("/")
            == unquote(urlparse(current_url).path).rstrip("/"))


def next_page_candidates(current_url: str,
                         advertised_hrefs: List[str]) -> List[str]:
    """The advertised links that really do paginate this listing, absolute.

    Every engine filters through this before believing a next-link, so none
    of them can follow the reviews pagination on its own. Order is preserved,
    duplicates dropped.
    """
    out, seen = [], set()
    for href in advertised_hrefs or []:
        if not href or not _same_listing(current_url, href):
            continue
        absolute = urljoin(current_url, href)
        key = comparable(absolute)
        if key not in seen:
            seen.add(key)
            out.append(absolute)
    return out


def pagination_agrees(current_url: str, page_num: int,
                      advertised_hrefs: List[str]) -> bool:
    """Whether ANY link the page advertises is the URL we would construct.

    Any, not the first, and that is measured: a shop front advertises page 2
    TWICE, once as `?ref=pagination&page=2` and once as
    `?ref=items-pagination&page=2&sort_order=custom`. The second carries a
    content-affecting parameter, so comparing only the first match would
    report a disagreement and drop the run to sequential fetching on every
    shop front — while the site plainly does advertise the plain form too.

    Each href is RESOLVED against `current_url` before comparison. Etsy
    writes some of its pagination links relative ("?ref=pagination&page=2")
    and some absolute, and comparing a relative one against a constructed
    absolute URL fails on the scheme and host rather than on the page — which
    read as a cursor-style disagreement and silently cost every shop front
    its concurrency.
    """
    constructed = comparable(page_url(current_url, page_num + 1))
    return any(comparable(href) == constructed
               for href in next_page_candidates(current_url, advertised_hrefs))


def pagination_is_addressable(page1_url: str,
                              advertised_hrefs: Optional[List[str]]) -> bool:
    """Whether page N can be fetched without first fetching page N-1.

    Concurrency depends entirely on this. Constructing `?page=N` removes the
    strictly sequential chain of following the site's own next-link — but
    only when the site's own link AGREES with what the convention would
    build. If Etsy ever starts issuing a cursor or a signed token this
    function cannot reproduce, the honest answer is to chain link-to-link and
    say the listing cannot be fetched independently, rather than to fetch a
    set of URLs that quietly return the wrong pages.

    No advertised link is not a disagreement: there is nothing to contradict.
    Returns True there, and the caller's own "this page added no new skus"
    condition ends the run. That default has to be True rather than a
    cautious False on this site, because `rel=next` is absent everywhere and
    `?page=N` nonetheless addresses every page correctly — verified against
    Etsy's own numbered anchors, which is what `pagination_agrees` checks.
    """
    if not next_page_candidates(page1_url, advertised_hrefs or []):
        return True
    return pagination_agrees(page1_url,
                             page_number_from_url(page1_url) or 1,
                             advertised_hrefs)


def page_bound(html: Optional[str], requested: int) -> int:
    """How many pages to actually fetch, given what the site says it has.

    Etsy embeds its own page count in the search page's markup
    (`"initial_total_pages":20`), which is a stronger terminating signal than
    any selector: a missing next-link is a property of markup, an exhausted
    listing is a property of the catalogue. Where the site states a smaller
    number than was asked for, asking for the rest would spend a fetch per
    page to be told the same thing.

    Only `/search` publishes it — category pages and shop fronts do not — so
    a None answer means "the site did not say", not "one page".
    """
    stated = total_pages(html)
    if stated is None or stated <= 0:
        return requested
    if stated < requested:
        logger.info("Etsy states this listing has %d page(s); fetching that "
                    "many instead of the %d requested.", stated, requested)
        return stated
    return requested
