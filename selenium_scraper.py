#!/usr/bin/env python3
"""
etsy-scraper — Selenium edition (secondary engine)
====================================================

The same scrape as playwright_scraper.py, driven through Selenium. It must
agree with its twins on exit codes, run status, and whether a run crashes or
spends money — the decisions that determine all three live in page_flow.py
and output_writer.finish_run(), so this file is browser plumbing and nothing
else.

    --mode listing   (default)  category grids and search results
    --mode product              one /product/ page, with brand, EAN,
                                description and the full image list

Two limits of this engine, stated here rather than left to be discovered.
Neither is a bug in this code and neither can be fixed from here:

  * **Selenium cannot use an authenticated remote CDP endpoint.** Playwright's
    `connect_over_cdp` and pyppeteer's `browserWSEndpoint` take a full
    `ws://user:pass@host:port` and authenticate on the WebSocket upgrade.
    chromedriver's `debuggerAddress` takes a bare `host:port` and has nowhere
    to put a password. So --cdp-endpoint here works only for an endpoint that
    needs no credentials; a credentialed one is refused with exit 2 rather
    than connected to and silently failing.
  * **Selenium cannot authenticate a proxy at all.** `--proxy-server=` accepts
    no credentials, and there is no equivalent of pyppeteer's
    `page.authenticate`. Credentials are stripped and a warning says so, so
    nobody believes a `user:pass` URL is doing something.

There is no --concurrency here either: parallel page fetching lives in the
Playwright engine.

Usage
-----
    python selenium_scraper.py \\
        --url "https://www.etsy.com/search?q=handmade+mug" --pages 3

Requires: pip install -r requirements.txt -r requirements-selenium.txt
          Selenium 4 fetches a matching chromedriver itself; a local Chrome
          or Chromium must be installed.
"""

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import urlparse, urlsplit, parse_qsl

from selenium import webdriver
from selenium.common.exceptions import (TimeoutException, WebDriverException,
                                        JavascriptException)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            reconcile_detections, solve_recaptcha,
                            solve_datadome, CaptchaUnsolvable,
                            DataDomeChallenge, INJECT_TOKEN_JS)
from product_parser import (parse_products, parse_product_page,
                            shop_metadata, SELECTORS, detect_bot_challenge,
                            datadome_verdict, datadome_captcha_url,
                            page_url, listing_kind,
                            site_host, is_supported_host, total_results,
                            unsupported_reason)
from output_writer import dedupe_by_key, finish_run, EXIT_API_ERROR
import page_flow
from page_flow import MIN_CARD_MATCHES
from proxy_pool import (from_args as proxy_pool_from_args, mask, ROTATE_MODES,
                        ProxyError, split_credentials)
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("selenium_scraper")

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

PAGE_LOAD_TIMEOUT = 60
SCRIPT_TIMEOUT = 30

# Chromium's own names for "the proxy is the problem, not the site". A dead
# proxy and a slow page want opposite responses — a different exit versus
# another try at the same one — so they are told apart by the error text.
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH", "ERR_PROXY_CERTIFICATE_INVALID",
)


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
    # In --mode shop, the seller's own name/location/rating, read off page 1.
    # Stored as the small dict rather than by keeping the page's HTML around:
    # a shop front is 790 KB and a listing page 2.4 MB, and holding those for
    # the length of a run to re-read six fields at the end would cost more
    # memory than the whole result set.
    shop_facts: Optional[dict] = None

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

    `driver.capabilities["browserVersion"]` is the installed Chrome's version,
    so the claim matches what the JS engine and the TLS handshake report. A
    hardcoded number drifts the moment Chrome updates, and claiming an older
    Chrome than everything else reports is itself a signal.
    """
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{version} Safari/537.36")


def _cdp_host_port(endpoint: str) -> str:
    """`host:port` for chromedriver's debuggerAddress, or exit 2 with a reason.

    chromedriver takes a bare address here and cannot send credentials, so an
    endpoint that carries them cannot work through this engine. Refused up
    front: connecting anyway would fail somewhere further in with an error
    that names none of this.
    """
    parts = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
    if parts.username or parts.password:
        logger.error(
            "This --cdp-endpoint carries credentials (%s), and Selenium cannot "
            "send them: chromedriver's debuggerAddress is a bare host:port. "
            "Use playwright_scraper.py or puppeteer_scraper.py for a "
            "credentialed endpoint such as the Scraping Browser API — both "
            "authenticate on the WebSocket upgrade.",
            _mask_credentials(endpoint))
        sys.exit(2)
    host = parts.hostname or endpoint
    port = f":{parts.port}" if parts.port else ""
    return f"{host}{port}"


class _Session:
    """One Chrome driver, relaunchable onto a different exit.

    Same contract as the Playwright engine's _BrowserSession, including the
    rule that a rotation means a genuinely FRESH browser — and a
    fresh browser is also the only thing that re-rolls the served page
    fresh cookie jar is what an ordinary user on another network looks like.
    """

    def __init__(self, args, pool):
        self.args, self.pool = args, pool
        self.remote = bool(args.cdp_endpoint)
        self.driver = None

    def open(self):
        options = Options()
        if self.remote:
            options.debugger_address = _cdp_host_port(self.args.cdp_endpoint)
            logger.info("Attaching to an existing browser at %s.",
                        options.debugger_address)
            # No UA, no proxy, no fingerprint on this path: the remote browser
            # brings its own, and stacking a second creates a contradiction
            # rather than better cover.
            self.driver = webdriver.Chrome(options=options)
            self._apply_timeouts()
            return self

        if self.args.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1600,1000")
        # Not a fingerprint measure, a correctness one: without it Chrome
        # advertises "HeadlessChrome", which is a giveaway on any site with
        # a bot manager in front of it.
        options.add_argument("--disable-blink-features=AutomationControlled")

        if self.pool:
            scrubbed, credentials = split_credentials(self.pool.current)
            options.add_argument(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(self.pool.current))
            if credentials:
                logger.warning(
                    "This proxy has credentials and SELENIUM CANNOT SEND "
                    "THEM: --proxy-server accepts an address only, and there "
                    "is no Selenium equivalent of pyppeteer's "
                    "page.authenticate. They have been stripped, so requests "
                    "will go out unauthenticated and the exit will most "
                    "likely refuse them. Use playwright_scraper.py or "
                    "puppeteer_scraper.py for an authenticated proxy.")

        self.driver = webdriver.Chrome(options=options)
        self._apply_timeouts()

        version = self.driver.capabilities.get("browserVersion", "")
        if version:
            # Set over CDP rather than as a launch switch, so it can use the
            # version the driver actually reports.
            try:
                self.driver.execute_cdp_cmd(
                    "Network.setUserAgentOverride",
                    {"userAgent": _chrome_ua(version)})
            except WebDriverException as e:
                logger.debug("Could not override the user agent: %s", e)

        if self.args.fingerprint:
            self._apply_fingerprint()
        return self

    def _apply_timeouts(self):
        # Explicit, because a driver that stops answering otherwise hangs the
        # run: "every remote call is bounded" applies to this engine too.
        self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        self.driver.set_script_timeout(SCRIPT_TIMEOUT)

    def _apply_fingerprint(self):
        from fingerprint_client import get_fingerprint, playwright_init_script
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = (fp.get("userAgent") or {}).get("value")
        script = playwright_init_script(fp)
        try:
            if ua:
                self.driver.execute_cdp_cmd("Network.setUserAgentOverride",
                                            {"userAgent": ua})
            # The same patch script the Playwright engine installs on its
            # context. Shared deliberately: two engines applying different
            # halves of one fingerprint would be a contradiction of exactly
            # the kind a fingerprint is meant to avoid.
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument", {"source": script})
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"),
                        fp.get("country"))
        except WebDriverException as e:
            logger.warning("Could not apply the fingerprint over CDP (%s) — "
                           "continuing without it.", e)

    def relaunch(self):
        if self.remote:
            return
        self.close()
        self.open()

    def close(self):
        try:
            if self.driver is not None:
                # quit(), not close(): close() ends one window and leaves the
                # driver process running, which on a per-page rotation would
                # leak a chromedriver per page.
                self.driver.quit()
        except Exception as e:  # noqa: BLE001 — teardown must not mask the reason we're here
            logger.debug("Ignoring error during driver teardown: %s", e)


# ---------------------------------------------------------------------------
# page_flow, bound to Selenium
# ---------------------------------------------------------------------------
# Only "how to ask this driver" lives here. Note the JS dialect: Selenium's
# execute_script runs a function BODY and needs an explicit `return`, unlike
# the `() => expr` both other engines take — which is why page_flow names
# operations instead of passing JavaScript.
def _driver(session):
    driver = session.driver

    def count(selector):
        try:
            return len(driver.find_elements(By.CSS_SELECTOR, selector))
        except WebDriverException as e:
            logger.debug("count(%s) failed: %s", selector, e)
            return 0

    def sleep(ms):
        time.sleep(ms / 1000.0)

    def content():
        try:
            return driver.page_source
        except WebDriverException as e:
            # A geo-redirect or the consent layer can navigate, so a
            # snapshot can land on the document swap. None tells the caller to
            # skip a check rather than fail the run.
            logger.debug("page_source unavailable (page navigating?): %s", e)
            return None

    def current_url():
        try:
            return driver.current_url
        except WebDriverException:
            return ""

    # No scroll primitives, deliberately. Etsy server-renders its whole
    # grid — see page_flow's docstring for the measurement — so a
    # scroll_to_bottom() no engine calls would be one more thing the three
    # could drift on for no benefit.
    return {"count": count, "sleep": sleep, "content": content,
            "current_url": current_url}


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


def _frame_urls(session) -> List[str]:
    """Every frame's CURRENT url, which is not what the markup says.

    The one primitive that makes the DataDome hand-off visible: a challenge
    moves from the device check to its solvable slider by navigating this
    iframe, and neither the `dd` object nor the iframe's `src` attribute in
    the top-level document follows. Measured: the markup said `rt='i'` for
    120 seconds while this list said `t=fe` from second seven.

    Never raises — a frame can detach between the enumeration and the read,
    and losing the whole list to that would put the run back where it
    started.

    Selenium has no "list the frames' URLs" call: `driver.current_url` stays
    on the top document however many frames you switch into. So this switches
    into each iframe, asks IT where it is, and switches back — and the switch
    back is in a `finally`, because leaving the driver parked inside a frame
    would make every later `find_element` search the wrong document.
    """
    driver = session.driver
    urls = []
    try:
        frames = driver.find_elements(By.TAG_NAME, "iframe")
    except WebDriverException:
        return []
    for frame in frames:
        try:
            driver.switch_to.frame(frame)
            got = driver.execute_script("return window.location.href;")
            if got:
                urls.append(got)
        except WebDriverException:
            continue
        finally:
            try:
                driver.switch_to.default_content()
            except WebDriverException:
                pass
    return urls


def _next_page_candidates(session, page_num: int) -> List[str]:
    """The site's own next-page link, resolved by the browser, or None.

    Returns EVERY candidate, filtered by page_flow to the ones that really do
    paginate this listing: a shop front advertises its REVIEWS pagination
    alongside its items, and following that one returns rows from the wrong
    listing while reporting success.

    Reads the DOM's `.href` property, which is already absolute — the
    opposite of Playwright's get_attribute("href"), which returns the raw
    attribute. Kept explicit because the engines differ here.

    Note the JS is a function BODY with an explicit `return`, not the arrow
    expression the other two engines pass. That difference is exactly why no
    JavaScript crosses the page_flow boundary.
    """
    try:
        hrefs = session.driver.execute_script(
            "return Array.from(document.querySelectorAll(arguments[0]))"
            ".map(a => a.href || a.getAttribute('href')).filter(Boolean);",
            page_flow.next_page_selector(page_num))
    except WebDriverException:
        return []
    return page_flow.next_page_candidates(session.driver.current_url,
                                          hrefs or [])


def handle_datadome_if_present(session, args, html: str) -> bool:
    """Buy a DataDome solve when — and only when — one can work. True if solved.

    Mirrors playwright_scraper.handle_datadome_if_present exactly: the
    preconditions, their order, the purchase cap and the reload are the same,
    because all three engines must agree on when a run spends money. What
    differs is only how this driver reads its user agent and sets a cookie.
    """
    driver = session.driver
    frame_urls = _frame_urls(session)
    verdict = datadome_verdict(html, frame_urls)
    solve_mode = getattr(args, "solve_captcha", "when-blocked")
    if not page_flow.should_pay_for(verdict, solve_mode):
        if verdict == "fe" and args.twocaptcha_key:
            # Worth saying out loud: the one solvable state on this site is
            # in front of us, a key is configured, and the run is still not
            # spending. Silence here would read as "nothing to solve".
            logger.info(
                "A solvable DataDome challenge (t=fe) is on this page and "
                "was NOT paid for: on this site the solve is opt-in, because "
                "two of two purchases were measured returning cookies Etsy "
                "rejected. Pass --solve-captcha always to try it anyway.")
        return False

    captcha_url = datadome_captcha_url(html, frame_urls)
    if not captcha_url:
        logger.warning("DataDome reports a solvable challenge (t=fe) but its "
                       "iframe carries no src to hand to the solver — not "
                       "paying for a task that cannot be scoped.")
        return False
    if not args.twocaptcha_key:
        logger.warning("A SOLVABLE DataDome challenge (t=fe) is on this page "
                       "and no 2captcha key is configured — continuing with "
                       "whatever the page holds.")
        return False

    challenge = DataDomeChallenge(
        captcha_url=captcha_url, page_url=driver.current_url,
        # A function BODY with an explicit return, not an arrow expression —
        # the dialect difference that keeps JavaScript out of page_flow.
        user_agent=driver.execute_script("return navigator.userAgent;"))
    try:
        name, value = solve_datadome(challenge, args.twocaptcha_key,
                                     args.proxy, attempts=2)
    except CaptchaUnsolvable as e:
        logger.warning("DataDome will not solve from this exit: %s", e)
        return False
    except Exception as e:  # noqa: BLE001 — a solver failure is not a crash
        logger.error("The DataDome solve failed (%s) — continuing with "
                     "whatever the page holds.", e)
        return False

    try:
        # Selenium refuses a cookie whose domain does not match the page the
        # driver is currently on, so this has to happen after a navigation to
        # the site — which it always is, since it is reached from a fetch.
        driver.add_cookie({
            "name": name, "value": value,
            "domain": page_flow.datadome_cookie_domain(driver.current_url),
            "path": "/",
        })
    except WebDriverException as e:
        logger.error("Selenium refused the solved cookie (%s) — the solve is "
                     "paid for but cannot be used.", e)
        return False
    logger.info("DataDome cookie set; reloading to use it.")
    driver.refresh()
    return True


def handle_captcha_if_present(session, args) -> bool:
    """Detect and solve a challenge. True if something was solved.

    Same detectors, same reconciliation and the same "detected is not
    blocking" rule as the Playwright engine — the three must agree about
    when a run spends money.

    NOTE what this cannot help with: Etsy's `t=bv` refusal is an HTTP
    403 carrying its own error page with no challenge on it, so no solve
    applies there and none is attempted. See product_parser.detect_page_state.
    """
    driver = session.driver
    d = _driver(session)
    html = d["content"]()
    if html is None:
        return False

    selector = page_flow.ready_selector(args.mode)
    already_rendered = d["count"](selector)
    when_blocked = getattr(args, "solve_captcha", "when-blocked") == "when-blocked"

    html_challenge = detect_recaptcha_v3(html, d["current_url"]())
    runtime_challenge = detect_recaptcha_in_page(
        lambda js: driver.execute_script(f"return ({js})();"),
        page_url=d["current_url"]())
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
    try:
        driver.execute_script(f"return ({INJECT_TOKEN_JS})(arguments[0]);", token)
    except WebDriverException as e:
        logger.error("Could not inject the token (%s).", e)
        return False
    logger.info("Token injected. Reloading page to continue.")
    time.sleep(1.5)
    driver.refresh()
    return True


def _fetch_one_page(session, args, pool, page_num: int, url: str) -> PageOutcome:
    """Fetch and parse one page. Mirrors playwright_scraper._fetch_one_page.

    Kept structurally parallel to its twins on purpose — "all three engines
    agree" is checked by reading them side by side as well as by the smoke
    suite.
    """
    outcome = PageOutcome(page_num=page_num, url=url)
    d = _driver(session)
    html, state, load_failed = None, "ok", False

    # See the Playwright engine for the measurement: without a pool there is
    # no exit to rotate to, but a plain re-fetch is what clears a block on a
    # Scraping Browser profile, so the budget is not zero.
    has_pool = bool(pool and len(pool) > 1)
    # `RETRY_ON_BLOCKED` is CONSULTED, not just documented. It was a
    # constant with a paragraph of justification that no engine read — a
    # policy statement nothing enforced, which is the same defect as dead
    # code that looks load-bearing. Setting it False now really does stop
    # the retry loop.
    block_retries = 0 if not page_flow.RETRY_ON_BLOCKED else (
        args.proxy_block_retries if has_pool
        else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
    # Counted across the whole block-retry loop, not per attempt: a page that
    # keeps coming back as a challenge would otherwise buy one solve per
    # rotation, which is how a run quietly turns into a bill.
    solves_bought = 0

    for block_attempt in range(block_retries + 1):
        logger.info("Fetching page %d/%d: %s", page_num, args.pages, url)
        load_failed, exit_failed = False, None
        for attempt in range(1, args.retries + 1):
            try:
                session.driver.get(url)
                load_failed = False
                break
            except (TimeoutException, WebDriverException) as e:
                text = str(e)
                reason = next((m for m in _PROXY_ERROR_MARKERS if m in text), "")
                load_failed = True
                if reason:
                    exit_failed = reason
                    break  # a different exit is the only thing that helps
                if attempt < args.retries:
                    pause = args.retry_delay * (2 ** (attempt - 1))
                    logger.warning("Failed to load %s (attempt %d/%d: %s) — "
                                   "retrying in %.1fs.", url, attempt,
                                   args.retries, text[:120], pause)
                    time.sleep(pause)

        if exit_failed and has_pool and block_attempt < block_retries:
            logger.warning("Exit %s is unusable (%s) — rotating to another "
                           "one (%d/%d).", mask(pool.current), exit_failed,
                           block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            session.relaunch()
            d = _driver(session)
            continue
        if load_failed:
            break


        if handle_captcha_if_present(session, args):
            time.sleep(1)

        html = d["content"]() or ""
        # A DataDome interstitial has not decided yet — wait it out before
        # classifying, exactly as the other two engines do.
        html = page_flow.settle_datadome(
            d["content"], d["sleep"],
            frames=lambda: _frame_urls(session)) or html
        state = page_flow.classify(html, url=d["current_url"](),
                                   frame_urls=_frame_urls(session))

        # The paid path, and the only state that reaches it. `should_solve` is
        # True for "captcha" and False for "blocked", which is where the
        # t=fe / t=bv distinction turns into a decision about money.
        if (page_flow.should_solve(state)
                and solves_bought < page_flow.SOLVES_PER_PAGE):
            solves_bought += 1
            if handle_datadome_if_present(session, args, html):
                html = d["content"]() or html
                state = page_flow.classify(html, url=d["current_url"](),
                                           frame_urls=_frame_urls(session))
                # The VERIFIED outcome, and the only one worth reporting: a
                # "ready" task result is not evidence the cookie works (one
                # was measured coming back ready, and billed, for a
                # fabricated challenge URL). This line is what says whether
                # the money bought anything.
                if state == "content":
                    logger.info("The solved cookie was accepted — page %d is "
                                "content now.", page_num)
                else:
                    logger.warning(
                        "The solved cookie was NOT accepted: page %d is still "
                        "%s. The purchase is spent. On this site that usually "
                        "means the solve left from a different exit than the "
                        "browser — a sticky-session proxy whose exit is keyed "
                        "to the client cannot share one with 2captcha's "
                        "servers.", page_num, state)


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
        timeout_s = page_flow.content_timeout_ms(args.mode) / 1000.0
        try:
            WebDriverWait(session.driver, timeout_s).until(
                lambda _: d["count"](selector) > threshold)
            time.sleep(0.5)
        except TimeoutException:
            # Not an error on its own, and what it MEANS depends on the
            # mode — which is why the message does too. A listing page with
            # no grid is a correct answer (a taxonomy hub, or one page past
            # the end); a detail page whose buy box never painted is a
            # different thing entirely, and on this site it is usually just
            # slow rather than absent, because the row is parsed out of the
            # page's JSON-LD and not out of the buy box.
            if args.mode == "product":
                logger.info("The buy box did not paint within %.0fs. That is "
                            "not fatal: a detail row is read from the page's "
                            "structured data, and the parse below decides. If "
                            "it returns nothing, the listing is probably "
                            "unavailable — the parser will say so.", timeout_s)
            else:
                logger.info("No listing tiles appeared within %.0fs. If this "
                            "URL is a taxonomy hub or one page past the end "
                            "of a listing, that is the expected answer and "
                            "the run will report 0 rows (exit 4).", timeout_s)
        html = d["content"]() or html

    if args.dump_html:
        dump_path = (args.dump_html if args.pages == 1
                     else f"{args.dump_html}.page{page_num}")
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info("Saved the snapshot the parser sees to %s (%d bytes).",
                    dump_path, len(html))

    # Only when the page is NOT already content. A challenge marker on a
    # page whose products have rendered guards nothing — and over
    # --cdp-endpoint the Scraping Browser's own auto-solve extension injects
    # such markers into every page it loads.
    vendor = (detect_bot_challenge(html, url=d["current_url"]())
              if state != "content" else None)
    if vendor:
        debug_html = f"{args.out}_page{page_num}_debug.html"
        with open(debug_html, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            session.driver.save_screenshot(f"{args.out}_page{page_num}_debug.png")
        except WebDriverException as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.error("Blocked by %s before parsing (%d bytes) — saved to %s. "
                     "This is exit 3, distinct from a genuinely empty result "
                     "(exit 4).", vendor, len(html), debug_html)
        outcome.blocked_by = vendor
        return outcome

    final_url = d["current_url"]() or url
    products = _parse_for_mode(html, final_url, args)
    logger.info("Parsed %d row(s) from page %d.", len(products), page_num)

    if args.mode == "shop" and page_num == 1:
        outcome.shop_facts = shop_metadata(html, d["current_url"]())

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
        kind = listing_kind(d["current_url"]())
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
            session.driver.save_screenshot(f"{args.out}_page{page_num}_debug.png")
        except WebDriverException as e:
            logger.warning("Could not capture screenshot: %s", e)
        logger.warning("0 rows parsed — saved what the browser actually saw to "
                       "%s.", debug_html)

    outcome.products = products
    outcome.final_url = final_url
    return outcome


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

    session = None
    try:
        session = _Session(args, pool).open()
        first = _fetch_one_page(session, args, pool, 1, args.url)
        outcomes.append(first)

        if not first.ok:
            stop_reason = ("page_load_timeout" if first.load_failed
                           else f"blocked_{first.blocked_by}")
            blocked = first.blocked_by is not None
        elif args.mode in ("listing", "shop"):
            seen_keys.update(p.sku for p in first.products if p.sku is not None)

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
                    else page_url(session.driver.current_url, 2)))
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
                           (nxt[0] if nxt else
                            page_url(session.driver.current_url, page_num + 1)))
                    time.sleep(args.delay)
    finally:
        if session is not None:
            session.close()

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

    # A shop run's own facts. Read from page 1's markup, because that is the
    # page that carries the seller's `Organization` data, and put in the
    # sidecar rather than repeated down a column — see run_meta's `extra`.
    extra = None
    if args.mode == "shop":
        first = next((o for o in outcomes if o.ok and o.shop_facts), None)
        if first is not None:
            extra = first.shop_facts
            if extra:
                logger.info("Shop: %s%s, rating %s from %s review(s).",
                            extra.get("shop_name") or "?",
                            " (%s)" % extra["shop_location"]
                            if extra.get("shop_location") else "",
                            extra.get("shop_rating"),
                            extra.get("shop_review_count"))

    return finish_run(all_rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=args.pages, pages_completed=len(ok_pages),
                      pages_failed=failed_pages, mode=args.mode,
                      source=site_host(final_url),
                      start_url=args.url, final_url=final_url,
                      extra=extra)


def parse_args():
    p = argparse.ArgumentParser(
        description="Etsy scraper (Selenium edition). Cannot authenticate a "
                    "proxy or a remote CDP endpoint — see the module "
                    "docstring; playwright_scraper.py is the primary engine.")
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
                   help="Proxy URL. NOTE: Selenium cannot authenticate a "
                        "proxy; credentials are stripped and a warning says "
                        "so. Use the Playwright or pyppeteer engine for an "
                        "authenticated exit.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=2)
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a fingerprint from 2captcha's Fingerprint API "
                        "and apply it over CDP. Needs --twocaptcha-key. "
                        "Ignored with --cdp-endpoint.")
    # ONE OS-family tag, not a list — and this default is what makes
    # --fingerprint work at all. It shipped as "Windows,Chrome,Desktop",
    # which the fingerprint API rejects with HTTP 400 ("Request parameters
    # are invalid"), so --fingerprint failed on every invocation.
    #
    # fingerprint_client.py's own --tags help has said so all along; the
    # engines' default contradicted it. Measured against the live API on
    # 2026-09-10: `Windows` succeeds, and `Windows,Chrome,Desktop`,
    # `Chrome` and `Desktop` each 400.
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400, and no combination is accepted. Use "
                        "--fp-country to narrow further. (default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): only pay to solve a "
                        "reCAPTCHA if the content is not already readable. "
                        "always: solve whenever one is detected, AND opt in "
                        "to the DataDome solve — which needs `always` because "
                        "it was measured 0 for 2 (both purchases returned "
                        "cookies Etsy rejected), and needs --proxy because "
                        "the cookie is bound to the exit that solved it. "
                        "Neither setting touches a t=bv refusal: it carries "
                        "no challenge, so nothing is attempted or billed.")
    p.add_argument("--min-score", type=float, default=0.7)
    p.add_argument("--cdp-endpoint", default=None,
                   help="Attach to a running browser at host:port. Must NOT "
                        "carry credentials — chromedriver's debuggerAddress "
                        "cannot send them, so a credentialed endpoint is "
                        "refused with exit 2 rather than silently failing.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact HTML the parser is given, on success "
                        "as well as failure.")
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
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key.")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "remote browser supplies its own.")
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
