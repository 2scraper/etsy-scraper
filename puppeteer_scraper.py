#!/usr/bin/env python3
"""
etsy-scraper — pyppeteer edition (secondary engine)
=====================================================

The same scrape as playwright_scraper.py, driven through pyppeteer. It must
agree with its twins on exit codes, run status, and whether a run crashes or
spends money — the decisions that determine all three live in page_flow.py
and output_writer.finish_run(), so this file is browser plumbing and nothing
else.

    --mode listing   (default)  category grids and search results
    --mode product              one /product/ page, with brand, EAN,
                                description and the full image list

Two things to know before choosing this engine:

  * **pyppeteer is effectively unmaintained** and its own README points at
    Playwright. It is here for parity, and for anyone who already has it.
  * **No --concurrency.** The Playwright engine is the one that fetches pages
    in parallel; the flag is accepted here and reported as ignored rather
    than silently doing nothing.

Usage
-----
    python puppeteer_scraper.py \\
        --url "https://www.etsy.com/search?q=handmade+mug" --pages 3

Requires: pip install -r requirements.txt -r requirements-puppeteer.txt
          (pyppeteer downloads its own Chromium on first run)
"""

import argparse
import asyncio
import concurrent.futures
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse, urljoin, parse_qsl

# At module level, deliberately, and not inside the launch path where it
# started out. The offline suite guards `import puppeteer_scraper` behind
# try/except ImportError and REPORTS the skip, and CI's engine-smoke job fails
# on any reported skip — that whole mechanism only works if importing this
# module actually requires the driver. With the import hidden inside
# _Session.open(), the module imported cleanly with no pyppeteer installed at
# all, the group never skipped, and CI could not have noticed a broken import.
# It also let CI install pyppeteer 0.0.25 (a stub, resolved from an unpinned
# `pip install pyppeteer`) without anything failing, because nothing ever
# imported it.
from pyppeteer import launch, connect

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            INJECT_TOKEN_JS)
from product_parser import (parse_products, parse_product_page,
                            shop_metadata, SELECTORS, detect_bot_challenge,
                            datadome_verdict, page_url, listing_kind,
                            site_host, is_supported_host, total_results,
                            unsupported_reason)
from output_writer import dedupe_by_key, finish_run, EXIT_API_ERROR
import page_flow
from page_flow import MIN_CARD_MATCHES
from proxy_pool import (from_args as proxy_pool_from_args, mask, ROTATE_MODES,
                        ProxyError, split_credentials)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("puppeteer_scraper")

ITEM_LINK_SELECTOR = SELECTORS["item_link"]

# A page holding less than this share of the fullest page in the same run is
# reported as thin. Etsy's own page size varies by a few rows (62, 60, 62 on a
# live three-page run), so the bar has to sit well below that spread or it
# fires on every healthy run.
# The lowest structured-price confirmation share that is still healthy, per
# page kind. Measured on live captures taken 2026-09-09 — search 8/64,
# category 56/64 confirmed of 61 published, shop front 36/40 — and set a few
# points below each so an ordinary page does not warn. A search page's 13% is
# not a defect: Etsy simply publishes JSON-LD for eight of its listings.
CONFIRMATION_FLOOR = {"search": 8, "market": 8, "category": 70, "shop": 80}

THIN_PAGE_SHARE = 0.6

# Every await in this file goes through the bridge below with a timeout, so a
# hung remote call ends the operation instead of the run. pyppeteer provides
# no connect timeout of its own and its page methods' `timeout` option does
# not cover a browser that has stopped answering at all.
DEFAULT_OP_TIMEOUT = 120
CONNECT_TIMEOUT = 30


class _AsyncBridge:
    """Runs pyppeteer's coroutines on a private event loop, synchronously.

    Exists so this engine can reuse page_flow.py unchanged. That module holds
    the policy all three engines must share (how long to wait for
    challenge, when to scroll, when only a fresh session helps) and it is
    written against plain synchronous callables — which is the right shape for
    two of the three drivers. Bridging here keeps the policy in one place
    rather than growing an async copy of it that would drift.

    The second benefit is the one the family's rules actually require: every
    call gets an explicit, enforced timeout. `.result(timeout)` returns
    control even when the browser never answers, which is not something
    pyppeteer's own API offers.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="pyppeteer-loop")
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        # pyppeteer leaves CDP calls in flight when a browser closes, and the
        # loop then logs each one as "Future exception was never retrieved:
        # NetworkError('Protocol error Target.sendMessageToTarget: Target
        # closed.')" — at ERROR level, AFTER a successful run has printed its
        # results. Five of those under a "Saved 48 products" line read as a
        # failed run. Only that shape is swallowed; anything else still gets
        # the default handler, because silencing the loop wholesale would hide
        # real faults.
        self.loop.set_exception_handler(self._on_loop_exception)
        self.loop.run_forever()

    @staticmethod
    def _on_loop_exception(loop, context):
        message = str(context.get("exception") or context.get("message") or "")
        if "Target closed" in message or "Connection closed" in message:
            logger.debug("Ignoring teardown noise from pyppeteer: %s", message)
            return
        loop.default_exception_handler(context)

    def run(self, coro, timeout: Optional[float] = DEFAULT_OP_TIMEOUT):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(
                f"pyppeteer call did not return within {timeout}s")

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)


@dataclass
class PageOutcome:
    """What one page produced. Mirrors playwright_scraper.PageOutcome."""
    page_num: int
    url: str
    final_url: Optional[str] = None
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    state: Optional[str] = None
    total_available: Optional[int] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching globally rather than once is the point: a driver's connection
# error can repeat the endpoint several times (the message plus a call log),
# so a masker that handled only the first occurrence would print the password
# the other times and look like it was working.
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced.

    Takes arbitrary text, not just a URL, because the strings that most need
    this are exception messages with a URL inside them. The host and port are
    KEPT — which endpoint or exit a run used is the useful half of the line
    and is not the secret.
    """
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _chrome_ua(version: str) -> str:
    """A desktop-Chrome UA naming the browser's OWN real version.

    Not a hardcoded number: it drifts the moment a newer Chromium ships, and
    claiming an older Chrome than the JS engine and TLS handshake report is
    itself a mismatch a fingerprinter can key on. pyppeteer's
    `browser.version()` returns "HeadlessChrome/115.0.0.0"; the marketing
    part is what a real Chrome would send.
    """
    number = version.split("/")[-1] if "/" in version else version
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{number} Safari/537.36")


class _Session:
    """One pyppeteer browser + page, relaunchable onto a different exit.

    Same contract as the Playwright engine's _BrowserSession, including the
    rule that a rotation means a genuinely FRESH browser: cookies a bot
    manager issued against one exit, replayed from another, are a stronger
    signal than either address alone, so the cookie jar goes with the exit.
    """

    def __init__(self, bridge: _AsyncBridge, args, pool):
        self.bridge, self.args, self.pool = bridge, args, pool
        self.remote = bool(args.cdp_endpoint)
        self.browser = self.page = None

    def open(self):
        if self.remote:
            logger.info("Connecting to an existing browser over CDP: %s",
                        _mask_credentials(self.args.cdp_endpoint))
            # pyppeteer's browserWSEndpoint takes the full ws://user:pass@host
            # form and authenticates on the WebSocket upgrade, so an
            # authenticated Scraping Browser endpoint works here — unlike
            # Selenium's debuggerAddress, which has nowhere to put a password.
            self.browser = self.bridge.run(
                connect(browserWSEndpoint=self.args.cdp_endpoint,
                        ignoreHTTPSErrors=True), timeout=CONNECT_TIMEOUT)
            self.page = self.bridge.run(self.browser.newPage())
            return self

        launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
        launch_kwargs = {}
        if self.args.chromium_path:
            launch_kwargs["executablePath"] = self.args.chromium_path
            logger.info("Using the Chromium at %s instead of pyppeteer's own.",
                        self.args.chromium_path)
        credentials = None
        if self.pool:
            exit_url = self.pool.current
            # Credentials go through page.authenticate(), never onto the
            # command line: --proxy-server= becomes part of the browser's
            # argv, readable by anything that can run `ps`.
            scrubbed, credentials = split_credentials(exit_url)
            launch_args.append(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(exit_url))

        # handleSIGINT/TERM/HUP off, and not for tidiness: pyppeteer installs
        # signal handlers inside launch(), and `signal.signal` raises
        # "signal only works in main thread of the main interpreter" because
        # the event loop here lives on a worker thread. Teardown is handled by
        # _Session.close() in scrape()'s finally block instead, so nothing is
        # lost — the browser is still closed on both success and failure.
        self.browser = self.bridge.run(
            launch(headless=self.args.headless, args=launch_args,
                   ignoreHTTPSErrors=True, handleSIGINT=False,
                   handleSIGTERM=False, handleSIGHUP=False, **launch_kwargs),
            timeout=CONNECT_TIMEOUT * 2)
        self.page = self.bridge.run(self.browser.newPage())
        version = self.bridge.run(self.browser.version())
        self.bridge.run(self.page.setUserAgent(_chrome_ua(version)))
        self.bridge.run(self.page.setViewport({"width": 1600, "height": 1000}))
        if credentials:
            self.bridge.run(self.page.authenticate(
                {"username": credentials[0], "password": credentials[1]}))
        return self

    def relaunch(self):
        if self.remote:
            return
        try:
            self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error while closing browser: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.bridge.run(self.page.close(), timeout=30)
            else:
                self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


# ---------------------------------------------------------------------------
# page_flow, bound to pyppeteer
# ---------------------------------------------------------------------------
# Only "how to ask this driver" lives here; every decision about what to do
# with the answer is in page_flow.py so all three engines make it the same way.
def _driver(session):
    bridge, page = session.bridge, session.page

    def count(selector):
        return len(bridge.run(page.querySelectorAll(selector)))

    def sleep(ms):
        time.sleep(ms / 1000.0)

    def content():
        try:
            return bridge.run(page.content())
        except Exception as e:  # noqa: BLE001
            # A geo-redirect or the consent layer can navigate, so a
            # snapshot can land exactly on the document swap. None tells the
            # caller to skip a check rather than fail the run.
            logger.debug("content() unavailable (page navigating?): %s", e)
            return None

    def current_url():
        return page.url

    # No scroll primitives, deliberately. Etsy server-renders its whole
    # grid — see page_flow's docstring for the measurement — so a
    # scroll_to_bottom() no engine calls would be one more thing the three
    # could drift on for no benefit.
    return {"count": count, "sleep": sleep, "content": content,
            "current_url": current_url}


def _content(session) -> Optional[str]:
    return _driver(session)["content"]()


def _parse_for_mode(html: str, url: str, args) -> List:
    if args.mode == "product":
        return parse_product_page(html, url, category=args.category)
    return parse_products(html, url, category=args.category)


def _same_url(a: str, b: str) -> bool:
    """Whether two URLs address the same page.

    Delegates to page_flow rather than reimplementing the comparison, so all
    three engines cannot drift on it. In particular Etsy writes some of its
    next-links percent-DECODED (".../kühlen-gefrieren-32.html") while a
    pasted URL is encoded (".../k%C3%BChlen-gefrieren-32.html"); an engine
    with its own copy of this got that wrong and silently fell back to
    sequential fetching on every accented category.
    """
    return page_flow.comparable(a) == page_flow.comparable(b)


def _next_page_candidates(session, page_num: int) -> List[str]:
    """The site's own next-page link, resolved, or None.

    Returns EVERY candidate, filtered by page_flow to the ones that really do
    paginate this listing: a shop front advertises its REVIEWS pagination
    alongside its items, and following that one returns rows from the wrong
    listing while reporting success.

    Reads the DOM's `.href` property rather than the raw attribute, which the
    browser has already resolved — the opposite of Playwright's
    get_attribute("href"). Kept explicit because the two engines differ here
    and a hand-rolled join got it wrong once.
    """
    bridge, page = session.bridge, session.page
    hrefs = bridge.run(page.evaluate(
        "(selector) => Array.from(document.querySelectorAll(selector))"
        ".map(a => a.href || a.getAttribute('href')).filter(Boolean)",
        page_flow.next_page_selector(page_num)))
    return page_flow.next_page_candidates(page.url, hrefs or [])


def handle_captcha_if_present(session, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Same two families, same order, same "detected is not blocking" rule as
    the Playwright engine — see its docstring for why the anchor count is
    checked here rather than after the readiness wait.
    """
    bridge, page = session.bridge, session.page
    html = _content(session)
    if html is None:
        return False

    selector = page_flow.ready_selector(args.mode)
    already_rendered = len(bridge.run(page.querySelectorAll(selector)))
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, page.url)
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: bridge.run(page.evaluate(js)), page_url=page.url)
    challenge = reconcile_detections(html_challenge, runtime_challenge)
    if not challenge:
        return False
    if when_blocked and already_rendered > MIN_CARD_MATCHES:
        logger.info("%s detected via %s, but %d anchors are already on the "
                    "page — not solving it.", challenge.kind, challenge.source,
                    already_rendered)
        return False
    logger.warning("%s detected via %s (sitekey=%s) — attempting to solve.",
                   challenge.kind, challenge.source, challenge.sitekey)
    if not args.twocaptcha_key:
        logger.warning("No 2captcha API key, so this challenge cannot be solved.")
        return False
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                api_version=args.captcha_api,
                                min_score=args.min_score)
    except Exception as e:  # noqa: BLE001
        logger.error("Solving the challenge failed (%s).", e)
        return False
    bridge.run(page.evaluate(INJECT_TOKEN_JS, token))
    logger.info("Token injected. Reloading page to continue.")
    time.sleep(1.5)
    bridge.run(page.reload({"waitUntil": "domcontentloaded", "timeout": 60000}))
    return True


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Mirrors playwright_scraper._fetch_one_page.

    The retry/rotate/wait policy is page_flow's and finish_run's; what differs
    here is only the driver calls. Kept structurally parallel on purpose —
    the two files are meant to be diffable, because "all three engines agree"
    is checked by reading them side by side as well as by the smoke suite.
    """
    outcome = PageOutcome(page_num=page_num, url=url)
    bridge, page = session.bridge, session.page
    d = _driver(session)

    # See the Playwright engine for the measurement: without a pool there is
    # no exit to rotate to, but a plain re-fetch is what clears a block on a
    # Scraping Browser profile, so the budget is not zero.
    has_pool = bool(pool and len(pool) > 1)
    block_retries = (args.proxy_block_retries if has_pool
                     else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    html, state, load_failed = None, "ok", False

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
        load_failed = False
        for attempt in range(1, args.retries + 1):
            try:
                bridge.run(page.goto(url, {"waitUntil": "domcontentloaded",
                                           "timeout": 60000}))
                load_failed = False
                break
            except Exception as e:  # noqa: BLE001 — pyppeteer raises many types
                load_failed = True
                # pyppeteer surfaces a dead proxy as a page error whose text
                # carries Chromium's own name for it, exactly as Playwright
                # does; a timeout and an unusable exit want opposite
                # responses, so they are told apart by that text.
                text = str(e)
                if any(marker in text for marker in _PROXY_ERROR_MARKERS):
                    logger.warning("Exit %s is unusable (%s).",
                                   mask(pool.current) if pool else "(none)", text[:120])
                    break
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Failed to load %s (attempt %d/%d: %s) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, text[:120], pause)
                    time.sleep(pause)

        if load_failed and block_attempt < block_retries:
            pool.advance("unusable exit or repeated load failure")
            session.relaunch()
            bridge, page = session.bridge, session.page
            d = _driver(session)
            continue
        if load_failed:
            break


        if handle_captcha_if_present(session, args):
            time.sleep(1)

        html = _content(session) or ""
        # A DataDome interstitial has not decided yet — wait it out before
        # classifying, exactly as the Playwright engine does.
        html = page_flow.settle_datadome(
            lambda: _content(session),
            lambda ms: bridge.run(page.waitFor(ms))) or html
        state = page_flow.classify(html, url=page.url)


        if not page_flow.should_retry(state):
            # "content" and "empty" are both final answers. An empty page is
            # a CORRECT one — a hub category has no grid — so retrying it
            # would re-confirm the same right answer, and rotating the exit
            # would blame an address for the URL it was given.
            break

        # Blocked or challenged. The ADDRESS is what was scored, not the URL,
        # so a different exit is the only thing that plausibly changes the
        # outcome.
        if block_attempt < block_retries:
            logger.warning("Page %d came back as %s from %s — retrying from "
                           "another exit (%d/%d).", page_num, state,
                           mask(pool.current), block_attempt + 1, block_retries)
            pool.advance(f"{state} on page {page_num}")
            session.relaunch()
            bridge, page = session.bridge, session.page
            d = _driver(session)

    if load_failed:
        logger.error("Gave up loading %s after %d attempt(s).", url, args.retries)
        outcome.load_failed = True
        return outcome

    outcome.state = state

    if state == "blocked":
        # DataDome's `t=bv` says the challenge is unsolvable, so the generic
        # challenge check below to find — it is the shop's own error page
        # under a 403. Saying so plainly, and saying what actually clears it,
        # is more use than a captcha hint that does not apply.
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        logger.error("Etsy refused this request (DataDome under HTTP 403 "
                     "under HTTP 403) — saved to %s. There is no challenge on "
                     "that page to solve, so a 2Captcha key does not help; a "
                     "residential exit does. This is exit 3, distinct from a "
                     "genuinely empty result (exit 4).", debug_html)
        outcome.blocked_by = "datadome-%s" % (datadome_verdict(html) or "403")
        outcome.final_url = d["current_url"]()
        return outcome


    if state == "content":
        # No scrolling and no session re-rolling, and both omissions are
        # measured rather than assumed: eight scroll rounds on a live
        # category page left the card count at 12 and the document height at
        # 13648px. See page_flow.py. The only thing worth waiting for is
        # paint.
        selector = page_flow.ready_selector(args.mode)
        threshold = page_flow.min_matches(args.mode)
        try:
            bridge.run(page.waitForFunction(
                f"() => document.querySelectorAll({selector!r}).length > {threshold}",
                {"timeout": page_flow.content_timeout_ms(args.mode)}))
            time.sleep(0.5)
        except Exception:  # noqa: BLE001 — a timeout here is often the right answer
            # Not an error on its own: a hub category renders no cards and
            # never will, and one page past the end of a listing is the same.
            logger.info("No product cards appeared in time. If this URL is a "
                        "hub category rather than a product grid, that is the "
                        "expected answer and the run will report 0 rows "
                        "(exit 4).")
        html = d["content"]() or html

    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    vendor = detect_bot_challenge(html, url=page.url)
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            bridge.run(page.screenshot({"path": f"{args.out}_page{page_num}_debug.png",
                                        "fullPage": True}))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s. "
                     "This is exit 3, distinct from a genuinely empty result "
                     "(exit 4).", vendor, len(html), debug_html)
        outcome.blocked_by = vendor
        return outcome

    products = _parse_for_mode(html, page.url, args)
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    if args.mode in ("listing", "shop") and page_num == 1:
        # Etsy publishes its own result-set size in the listing's JSON-LD,
        # turns "did we get everything?" into arithmetic instead of a guess.
        outcome.total_available = total_results(html, shown=len(products))

    if products and args.mode in ("listing", "shop"):
        priced = sum(1 for p in products if p.price is not None)
        logger.info("Price coverage on page %d: %d/%d (%.0f%%).", page_num,
                    priced, len(products), 100.0 * priced / len(products))
        if priced < len(products):
            logger.warning("%d row(s) on page %d carry no price. All 166 rows "
                           "across the live captures had one, so this is worth "
                           "a look.", len(products) - priced, page_num)
        # The share of rows whose rendered price was CONFIRMED against the
        # structured data.
        #
        # THE ACHIEVABLE SHARE DEPENDS ENTIRELY ON THE PAGE KIND, and a
        # single threshold across all of them would be useless. Etsy's own
        # JSON-LD covers a different fraction of each:
        #
        #     search page    8 of 64 listings   -> ~13% is HEALTHY
        #     category page 61 of 65 listings   -> ~88%
        #     shop front    36 of 40 listings   -> ~92%
        #
        # So the bar is set per kind, from those measurements, with room for
        # the handful of listings on which Etsy publishes a price it does not
        # display (5 of 64 on the category capture — see
        # product_parser._ld_list_price). A run that drops well below its
        # kind's floor has lost the join between tiles and structured data,
        # which is what empties `in_stock` and the JSON-LD half of `brand`
        # while the row count and the prices still look fine.
        confirmed = sum(1 for p in products if p.price_source == "jsonld+dom")
        share = 100.0 * confirmed / len(products)
        kind = listing_kind(page.url)
        floor = CONFIRMATION_FLOOR.get(kind, 0)
        logger.info("Structured-price confirmation on page %d (%s page): "
                    "%d/%d (%.0f%%); the floor for this page kind is %d%%.",
                    page_num, kind, confirmed, len(products), share, floor)
        if share < floor:
            logger.warning(
                "Only %.0f%% of page %d was confirmed against Etsy's own "
                "structured data, against a measured floor of %d%% for a %s "
                "page. `in_stock` and the structured half of `brand` come "
                "from that join, so they are the columns to check first.",
                share, page_num, floor, kind)

    if not products:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            bridge.run(page.screenshot({"path": f"{args.out}_page{page_num}_debug.png",
                                        "fullPage": True}))
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw to "
                       "%s.", debug_html)

    outcome.products = products
    outcome.final_url = page.url
    return outcome


# Chromium's own names for "the proxy is the problem, not the site".
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH", "ERR_PROXY_CERTIFICATE_INVALID",
)


def scrape(args) -> int:
    outcomes: List[PageOutcome] = []
    seen_keys = set()
    blocked = False
    # Both modes are one row per product, so `sku` is the key for both.
    dedupe_key = "sku"
    # Only --mode product is single-page. A SHOP FRONT paginates exactly like
    # a category listing — ?page=N, the same tiles — and treating it as
    # single-page made `--mode shop --pages 2` fetch one page and report
    # "complete", which is the silent-success failure this family exists to
    # avoid. Found on the first live shop run.
    stop_reason = "single_page_mode" if args.mode == "product" else "completed"

    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None
    if args.concurrency > 1:
        logger.warning("--concurrency is ignored in this engine: parallel page "
                       "fetching is implemented in playwright_scraper.py, "
                       "which is the primary engine. Running one page at a "
                       "time.")

    bridge = _AsyncBridge()
    session = None
    try:
        session = _Session(bridge, args, pool).open()
        first = _fetch_one_page(session, args, pool, 1, args.url)
        outcomes.append(first)

        if not first.ok:
            stop_reason = ("page_load_timeout" if first.load_failed
                           else f"blocked_{first.blocked_by}")
            blocked = first.blocked_by is not None
        elif args.mode in ("listing", "shop"):
            seen_keys.update(p.sku for p in first.products if p.sku is not None)

            # Same check as the Playwright engine: the ?page=N convention is
            # only used when the site's own link agrees with it, so a cursor
            # or token in pagination cannot be silently papered over.
            planned = None
            if args.pages > 1:
                page_one = first.final_url or args.url
                constructed = page_url(page_one, 2)
                candidates = _next_page_candidates(session, 1)
                if candidates and not page_flow.pagination_agrees(
                        page_one, 1, candidates):
                    logger.info("The site's own next-page link (%s) is not "
                                "what the page convention would build (%s) — "
                                "following its links one page at a time.",
                                candidates[0], constructed)
                else:
                    if not candidates:
                        logger.info(
                            "No pagination link for page 2 was found on page "
                            "1 — using the URL convention. That is EXPECTED "
                            "on this site: Etsy publishes no rel=next "
                            "anywhere, and ?page=N addresses every page "
                            "correctly.")
                    planned = [page_url(page_one, n)
                               for n in range(2, args.pages + 1)]

            first_candidates = _next_page_candidates(session, 1)
            url = (planned[0] if planned else
                   (first_candidates[0] if first_candidates
                    else page_url(session.page.url, 2)))
            for page_num in range(2, args.pages + 1):
                if pool and pool.rotates_per_page():
                    pool.advance(f"per-page rotation, page {page_num}")
                    session.relaunch()

                outcome = _fetch_one_page(session, args, pool, page_num, url)
                outcomes.append(outcome)
                if not outcome.ok:
                    stop_reason = ("page_load_timeout" if outcome.load_failed
                                   else f"blocked_{outcome.blocked_by}")
                    blocked = outcome.blocked_by is not None
                    break

                fresh_count = sum(1 for p in outcome.products
                                  if p.sku is None or p.sku not in seen_keys)
                seen_keys.update(p.sku for p in outcome.products
                                 if p.sku is not None)
                if not fresh_count:
                    logger.info("Page %d added no rows not already seen — "
                                "treating that as the end of the listing.",
                                page_num)
                    stop_reason = "no_new_products"
                    break

                if page_num < args.pages:
                    nxt = _next_page_candidates(session, page_num)
                    url = (planned[page_num - 1] if planned else
                           (nxt[0] if nxt
                            else page_url(session.page.url, page_num + 1)))
                    time.sleep(args.delay)
    finally:
        if session is not None:
            session.close()
        bridge.close()

    all_rows = []
    merged_seen = set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, merged_seen, key=dedupe_key)
        if len(fresh) < len(oc.products):
            logger.info("Page %d: dropped %d duplicate row(s).",
                        oc.page_num, len(oc.products) - len(fresh))
        all_rows.extend(fresh)

    # Completeness, checked over the MERGED result rather than per page — a
    # per-page check cannot see a gap BETWEEN two pages, which is exactly
    # where a short page hides.
    #
    # NOT "pages x rows-per-page", which is what the sibling repo does and
    # what fired on the first healthy live run here. Etsy's page size VARIES:
    # a three-page run returned 62, 60 and 62 rows, and multiplying the
    # largest page by the page count then declared the run short by 8. A
    # threshold that warns on every healthy run teaches the reader to ignore
    # it.
    #
    # What is worth warning about is a page that came back materially THIN
    # against its siblings — that is what a truncated response or a
    # half-painted grid looks like. A page holding less than 60% of the
    # fullest page is well outside the +-3% spread that the varying page size
    # accounts for.
    total_available = next((o.total_available for o in outcomes
                            if o.total_available is not None), None)
    if args.mode in ("listing", "shop") and all_rows:
        counts = [(o.page_num, len(o.products)) for o in outcomes if o.ok]
        fullest = max((n for _, n in counts), default=0)
        thin = [(p, n) for p, n in counts
                if fullest and n < THIN_PAGE_SHARE * fullest]
        # The LAST page of a listing is legitimately short — the catalogue
        # simply ran out — so it is excluded unless there are pages after it.
        last_page = max((p for p, _ in counts), default=0)
        thin = [(p, n) for p, n in thin if p != last_page]
        if thin:
            logger.warning(
                "Page(s) %s came back much thinner than the fullest page "
                "(%d rows): %s. A truncated response or a half-painted grid "
                "looks like this — re-run with --dump-html to check the "
                "snapshot for those pages.",
                ", ".join(str(p) for p, _ in thin), fullest,
                ", ".join("page %d: %d" % (p, n) for p, n in thin))
        if total_available:
            logger.info("This listing holds %d product(s) in total; this run "
                        "took %d (%.1f%%).", total_available, len(all_rows),
                        100.0 * len(all_rows) / total_available)


    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = [o.page_num for o in outcomes if not o.ok]
    final_url = (max(ok_pages, key=lambda o: o.page_num).final_url
                 if ok_pages else args.url)

    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages, mode=args.mode,
                      source=site_host(final_url),
                      start_url=args.url, final_url=final_url)


def parse_args():
    p = argparse.ArgumentParser(
        description="Etsy scraper (pyppeteer edition). pyppeteer is "
                    "effectively unmaintained — playwright_scraper.py is the "
                    "primary engine.")
    p.add_argument("--url", default=None,
                   help="Etsy URL. Required, unless ETSY_URL is set in the "
                        "environment or in .env.")
    p.add_argument("--mode", choices=["listing", "product", "shop"],
                   default="listing",
                   help="listing (default) or product. product reads one "
                        "/product/ page and adds brand, EAN, description and "
                        "the full image list — the columns a listing row "
                        "cannot carry. No --pages in product mode.")
    p.add_argument("--category", default=None, help="Label to tag output rows with.")
    p.add_argument("--pages", type=int, default=1, help="Listing pages to crawl")
    p.add_argument("--delay", type=float, default=2.0, help="Delay between pages, seconds")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Accepted for flag parity and IGNORED here: parallel "
                        "page fetching lives in playwright_scraper.py.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page load before giving up (default 3). "
                        "A page that comes back EMPTY is not retried: an empty "
                        "hub category is a correct answer, not a fault.")
    p.add_argument("--retry-delay", type=float, default=2.0,
                   help="Seconds before the first retry, doubling thereafter")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="etsy_products", help="Output file prefix")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999. "
                        "Credentials are sent over CDP (page.authenticate), "
                        "never on the browser's command line.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=2)
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked")
    p.add_argument("--min-score", type=float, default=0.7)
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to a running browser over CDP, e.g. "
                        "ws://user:pass@host:port. pyppeteer authenticates on "
                        "the WebSocket upgrade, so a credentialed Scraping "
                        "Browser endpoint works here.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success "
                        "as well as failure.")
    p.add_argument("--chromium-path", default=None, metavar="PATH",
                   help="Browser executable to drive, instead of the Chromium "
                        "pyppeteer downloads for itself. Needed where that "
                        "build will not start: on an Apple Silicon Mac "
                        "pyppeteer fetches an x86_64 Chromium 117, which runs "
                        "under Rosetta far enough to print --version and then "
                        "fails to open its DevTools socket (measured "
                        "2026-09-08; the same failure occurs with no wrapper "
                        "code at all, so it is the build, not this engine). "
                        "Point it at a Chrome or Chromium of your own — "
                        "Playwright's, if you have it installed.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    args = p.parse_args()
    env_config.apply(args)
    if not args.url:
        p.error("no --url given, and ETSY_URL is not set in the environment "
                "or in .env.")
    if args.mode == "product" and args.pages != 1:
        logger.warning("--pages %d is ignored in --mode %s: there is one page "
                       "to read.", args.pages, args.mode)
        args.pages = 1
    if not is_supported_host(args.url):
        # Refused rather than attempted: the selectors, the sku pattern and
        # the pagination convention are all Etsy's, so another marketplace
        # would not fail loudly — it would return zero rows and read as an
        # empty category.
        why = unsupported_reason(args.url)
        if why:
            p.error(f"{site_host(args.url)} {why}.")
        p.error(f"{site_host(args.url) or args.url!r} is not Etsy. This "
                f"scraper reads www.etsy.com; every market is a path prefix "
                f"on that one host (/de/, /uk/, /ca-fr/, ...), so there is no "
                f"per-country hostname to pass.")
    if args.mode == "shop" and listing_kind(args.url) != "shop":
        p.error(f"--mode shop expects a /shop/<name> URL; "
                f"{args.url!r} is a {listing_kind(args.url)} page.")
    if args.mode == "product" and listing_kind(args.url) != "listing":
        p.error(f"--mode product expects a /listing/<id> URL; "
                f"{args.url!r} is a {listing_kind(args.url)} page.")
    if args.mode == "listing" and listing_kind(args.url) == "listing":
        p.error(f"{args.url!r} is a single listing page. Use --mode product "
                f"for it, or pass a /search, /c/ or /market/ URL.")
    return args


if __name__ == "__main__":
    args = parse_args()
    try:
        sys.exit(scrape(args))
    except ProxyError as e:
        logger.error("%s", e)
        sys.exit(2)
    except Exception as e:
        # A remote browser that will not accept the connection is a REMOTE
        # API failure (exit 5), not a crash in this code (exit 1) and not bad
        # usage (exit 2). The distinction earns its keep on the commonest
        # one: `profile_locked` means another run still holds this `pid`, and
        # a harness that sees exit 1 goes looking for a bug in the scraper
        # instead of waiting or passing a different pid.
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
