#!/usr/bin/env python3
"""
Etsy Scraper — Pyppeteer (Puppeteer) Edition
===============================================
Etsy product scraper built with Pyppeteer — the Python port of Puppeteer.
Supports all Etsy categories, JSON/CSV export, proxy rotation (2prx.com),
CAPTCHA solving (2captcha.com), and browser fingerprint randomization.

Repository : https://github.com/2parser/etsy-parser
License    : MIT
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
try:
    import pyppeteer
    from pyppeteer import launch
    from pyppeteer.page import Page
except ImportError:
    sys.exit(
        "Pyppeteer is required.  Install it:\n"
        "  pip install pyppeteer"
    )

try:
    from twocaptcha import TwoCaptcha
    HAS_2CAPTCHA = True
except ImportError:
    HAS_2CAPTCHA = False

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("etsy-ppt")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_URL = "https://www.etsy.com"
SEARCH_URL = f"{BASE_URL}/search"
LISTING_URL_RE = re.compile(r"/listing/(\d+)/")

FINGERPRINTS = [
    {
        "width": 1920, "height": 1080,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    },
    {
        "width": 1440, "height": 900,
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    },
    {
        "width": 1536, "height": 864,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        },
    {
        "width": 2560, "height": 1440,
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    },
]

STEALTH_JS = """
() => {
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
    window.chrome = { runtime: {} };
    const origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (params) =>
        params.name === 'notifications'
            ? Promise.resolve({state: Notification.permission})
            : origQuery(params);
}
"""

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class EtsyProduct:
    listing_id: str = ""
    title: str = ""
    url: str = ""
    price: str = ""
    currency: str = "USD"
    original_price: str = ""
    discount: str = ""
    shop_name: str = ""
    shop_url: str = ""
    rating: str = ""
    reviews_count: str = ""
    image_url: str = ""
    images: list[str] = field(default_factory=list)
    description: str = ""
    tags: list[str] = field(default_factory=list)
    materials: list[str] = field(default_factory=list)
    category: str = ""
    is_bestseller: bool = False
    is_star_seller: bool = False
    free_shipping: bool = False
    ships_from: str = ""
    estimated_delivery: str = ""
    quantity_available: str = ""
    favorites_count: str = ""
    scraped_at: str = ""

    def uid(self) -> str:
        return self.listing_id or hashlib.md5(self.url.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Captcha solver
# ---------------------------------------------------------------------------
class CaptchaSolver:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("TWOCAPTCHA_API_KEY", "")
        self.solver = TwoCaptcha(self.api_key) if HAS_2CAPTCHA and self.api_key else None

    async def solve_recaptcha(self, sitekey: str, page_url: str) -> str | None:
        if not self.solver:
            log.warning("2captcha not configured — skipping CAPTCHA")
            return None
        log.info("Solving reCAPTCHA via 2captcha.com …")
        try:
            result = await asyncio.to_thread(
                self.solver.recaptcha, sitekey=sitekey, url=page_url
            )
            return result.get("code")
        except Exception as exc:
            log.error("CAPTCHA solve failed: %s", exc)
            return None

    async def solve_hcaptcha(self, sitekey: str, page_url: str) -> str | None:
        if not self.solver:
            return None
        try:
            result = await asyncio.to_thread(
                self.solver.hcaptcha, sitekey=sitekey, url=page_url
            )
            return result.get("code")
        except Exception as exc:
            log.error("CAPTCHA solve failed: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Core scraper
# ---------------------------------------------------------------------------
class EtsyScraperPyppeteer:
    """Pyppeteer-based Etsy scraper with stealth, proxy & CAPTCHA support."""

    def __init__(
        self,
        *,
        headless: bool = True,
        proxy: str | None = None,
        captcha_key: str | None = None,
        fingerprint: bool = True,
        max_pages: int = 5,
        delay: tuple[float, float] = (1.5, 4.0),
        output_dir: str = "output",
    ):
        self.headless = headless
        self.proxy_url = proxy or os.getenv("PROXY_URL") or os.getenv("TWO_PRX_URL")
        self.captcha = CaptchaSolver(captcha_key)
        self.use_fingerprint = fingerprint
        self.max_pages = max_pages
        self.delay = delay
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.products: list[EtsyProduct] = []

    # ---- browser lifecycle ------------------------------------------------
    async def _launch(self):
        fp = random.choice(FINGERPRINTS) if self.use_fingerprint else FINGERPRINTS[0]

        args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            f"--window-size={fp['width']},{fp['height']}",
        ]
        if self.proxy_url:
            args.append(f"--proxy-server={self.proxy_url}")
            log.info("Using proxy: %s", self.proxy_url)

        browser = await launch(
            headless=self.headless,
            args=args,
            ignoreHTTPSErrors=True,
        )
        page = await browser.newPage()
        await page.setViewport({"width": fp["width"], "height": fp["height"]})
        await page.setUserAgent(fp["user_agent"])

        if self.use_fingerprint:
            await page.evaluateOnNewDocument(STEALTH_JS)
            log.info("Fingerprint applied: %s", fp["user_agent"][:60])

        return browser, page

    async def _wait(self):
        await asyncio.sleep(random.uniform(*self.delay))

    # ---- helpers ----------------------------------------------------------
    async def _text(self, page: Page, selector: str) -> str:
        try:
            el = await page.querySelector(selector)
            if el:
                return (await page.evaluate("el => el.innerText", el)).strip()
        except Exception:
            pass
        return ""

    async def _attr(self, page: Page, selector: str, attr: str) -> str:
        try:
            el = await page.querySelector(selector)
            if el:
                return (await page.evaluate(f"el => el.getAttribute('{attr}')", el)) or ""
        except Exception:
            pass
        return ""

    # ---- CAPTCHA handling -------------------------------------------------
    async def _handle_captcha(self, page: Page) -> bool:
        sitekey = await self._attr(page, "[data-sitekey]", "data-sitekey")
        if sitekey:
            token = await self.captcha.solve_recaptcha(sitekey, page.url)
            if token:
                await page.evaluate(
                    f'document.getElementById("g-recaptcha-response").innerHTML="{token}";'
                    'document.querySelector("form").submit();'
                )
                await page.waitForNavigation({"waitUntil": "domcontentloaded"})
                return True

        sitekey = await self._attr(page, ".h-captcha, [data-hcaptcha-sitekey]", "data-sitekey")
        if sitekey:
            token = await self.captcha.solve_hcaptcha(sitekey, page.url)
            if token:
                await page.evaluate(
                    f'document.querySelector(\'[name="h-captcha-response"]\').value = "{token}";'
                    'document.querySelector("form").submit();'
                )
                await page.waitForNavigation({"waitUntil": "domcontentloaded"})
                return True

        return False

    # ---- search page parsing ---------------------------------------------
    async def _parse_search_page(self, page: Page) -> list[EtsyProduct]:
        items: list[EtsyProduct] = []

        cards = await page.querySelectorAll(
            "[data-search-results] .wt-grid__item-xs-6, "
            ".search-listings-group .wt-grid__item-xs-6, "
            ".listing-card"
        )
        if not cards:
            cards = await page.querySelectorAll("a[href*='/listing/']")

        for card in cards:
            try:
                p = EtsyProduct()
                p.scraped_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

                # URL
                href = await page.evaluate(
                    """el => {
                        const a = el.tagName === 'A' ? el : el.querySelector('a[href*="/listing/"]');
                        return a ? a.href : '';
                    }""",
                    card,
                )
                if href:
                    p.url = href.split("?")[0]
                    m = LISTING_URL_RE.search(href)
                    if m:
                        p.listing_id = m.group(1)

                # Title
                p.title = await page.evaluate(
                    """el => {
                        const t = el.querySelector('h3, h2, [data-listing-card-title]');
                        return t ? t.innerText.trim() : '';
                    }""",
                    card,
                )

                # Price
                p.price = await page.evaluate(
                    """el => {
                        const pr = el.querySelector('.currency-value, .lc-price .wt-text-title-01');
                        return pr ? pr.innerText.trim() : '';
                    }""",
                    card,
                )

                # Shop name
                p.shop_name = await page.evaluate(
                    """el => {
                        const s = el.querySelector('.wt-text-gray .wt-text-link-no-underline, p.wt-text-caption');
                        return s ? s.innerText.trim() : '';
                    }""",
                    card,
                )

                # Image
                p.image_url = await page.evaluate(
                    "el => { const i = el.querySelector('img'); return i ? i.src : ''; }",
                    card,
                )

                # Badges
                text = await page.evaluate("el => el.innerText.toLowerCase()", card)
                p.is_bestseller = "bestseller" in text
                p.is_star_seller = "star seller" in text
                p.free_shipping = "free shipping" in text

                if p.listing_id or p.url:
                    items.append(p)
            except Exception as exc:
                log.debug("Card parse error: %s", exc)

        return items

    # ---- detail page enrichment ------------------------------------------
    async def _enrich_product(self, page: Page, product: EtsyProduct):
        try:
            await page.goto(product.url, {"waitUntil": "domcontentloaded", "timeout": 30000})
            await self._wait()
            await self._handle_captcha(page)

            product.description = (
                await self._text(
                    page,
                    "#wt-content-toggle-product-details-read-more, "
                    "[data-id='description-text']",
                )
            )[:2000]

            product.images = await page.evaluate(
                """() => {
                    return Array.from(document.querySelectorAll(
                        'ul.carousel-pane-list img, [data-carousel] img'
                    )).map(i => i.src).filter(s => s && !s.includes('75x75'));
                }"""
            )

            shop_href = await self._attr(page, "a[href*='/shop/']", "href")
            if shop_href:
                product.shop_url = shop_href if shop_href.startswith("http") else urljoin(BASE_URL, shop_href)

            product.reviews_count = await self._text(page, "a[href*='#reviews'] span")

            product.tags = await page.evaluate(
                """() => {
                    return Array.from(document.querySelectorAll(
                        '#wt-content-toggle-tags-read-more a'
                    )).slice(0, 15).map(a => a.innerText.trim()).filter(Boolean);
                }"""
            )

            log.info("  ✓ Enriched: %s", product.title[:50])
        except Exception as exc:
            log.warning("  ✗ Enrich failed for %s: %s", product.listing_id, exc)

    # ---- public API -------------------------------------------------------
    async def search(
        self,
        query: str = "",
        category: str = "",
        min_price: float | None = None,
        max_price: float | None = None,
        enrich: bool = False,
    ) -> list[EtsyProduct]:
        browser, page = await self._launch()
        try:
            for page_num in range(1, self.max_pages + 1):
                params: dict[str, Any] = {}
                if query:
                    params["q"] = query
                if min_price is not None:
                    params["min"] = str(min_price)
                if max_price is not None:
                    params["max"] = str(max_price)
                params["ref"] = "pagination"
                params["page"] = str(page_num)

                url = (
                    f"{BASE_URL}/c/{category}?{urlencode(params)}"
                    if category
                    else f"{SEARCH_URL}?{urlencode(params)}"
                )

                log.info("Page %d/%d → %s", page_num, self.max_pages, url)
                await page.goto(url, {"waitUntil": "domcontentloaded", "timeout": 30000})
                await self._wait()
                await self._handle_captcha(page)

                for _ in range(3):
                    await page.evaluate("window.scrollBy(0, window.innerHeight)")
                    await asyncio.sleep(0.5)

                items = await self._parse_search_page(page)
                if not items:
                    log.info("No more results — stopping")
                    break

                self.products.extend(items)
                log.info("  → Collected %d items (total: %d)", len(items), len(self.products))
                await self._wait()

            if enrich:
                log.info("Enriching %d products …", len(self.products))
                for prod in self.products:
                    if prod.url:
                        await self._enrich_product(page, prod)
        finally:
            await browser.close()

        return self.products

    async def scrape_listing(self, url: str) -> EtsyProduct:
        browser, page = await self._launch()
        try:
            p = EtsyProduct(url=url)
            m = LISTING_URL_RE.search(url)
            if m:
                p.listing_id = m.group(1)
            p.scraped_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            await self._enrich_product(page, p)
            p.title = await self._text(page, "h1")
            p.price = await self._text(page, "[data-buy-box-listing-price] .currency-value")
            self.products.append(p)
        finally:
            await browser.close()
        return p

    # ---- export -----------------------------------------------------------
    def to_json(self, filename: str = "etsy_results.json") -> Path:
        path = self.output_dir / filename
        data = [asdict(p) for p in self.products]
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("Saved JSON → %s  (%d items)", path, len(data))
        return path

    def to_csv(self, filename: str = "etsy_results.csv") -> Path:
        path = self.output_dir / filename
        if not self.products:
            return path
        flat = []
        for p in self.products:
            d = asdict(p)
            d["images"] = "; ".join(d.get("images", []))
            d["tags"] = "; ".join(d.get("tags", []))
            d["materials"] = "; ".join(d.get("materials", []))
            flat.append(d)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=flat[0].keys())
            writer.writeheader()
            writer.writerows(flat)
        log.info("Saved CSV → %s  (%d items)", path, len(flat))
        return path

    def deduplicate(self):
        seen: set[str] = set()
        unique = []
        for p in self.products:
            uid = p.uid()
            if uid not in seen:
                seen.add(uid)
                unique.append(p)
        self.products = unique


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="etsy_pyppeteer",
        description="Etsy Scraper (Pyppeteer / Puppeteer) — by 2parser",
    )
    p.add_argument("query", nargs="?", default="", help="Search query")
    p.add_argument("-c", "--category", default="")
    p.add_argument("-p", "--pages", type=int, default=3)
    p.add_argument("--min-price", type=float, default=None)
    p.add_argument("--max-price", type=float, default=None)
    p.add_argument("--enrich", action="store_true")
    p.add_argument("--url", default="")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--output", default="output")
    p.add_argument("--proxy", default=None)
    p.add_argument("--captcha-key", default=None)
    p.add_argument("--no-fingerprint", action="store_true")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--delay", type=float, nargs=2, default=[1.5, 4.0], metavar=("MIN", "MAX"))
    return p


async def main():
    args = build_parser().parse_args()

    scraper = EtsyScraperPyppeteer(
        headless=not args.headed,
        proxy=args.proxy,
        captcha_key=args.captcha_key,
        fingerprint=not args.no_fingerprint,
        max_pages=args.pages,
        delay=tuple(args.delay),
        output_dir=args.output,
    )

    if args.url:
        await scraper.scrape_listing(args.url)
    else:
        await scraper.search(
            query=args.query,
            category=args.category,
            min_price=args.min_price,
            max_price=args.max_price,
            enrich=args.enrich,
        )

    scraper.deduplicate()

    if args.format in ("json", "both"):
        scraper.to_json()
    if args.format in ("csv", "both"):
        scraper.to_csv()

    log.info("Done — %d products scraped", len(scraper.products))


if __name__ == "__main__":
    asyncio.run(main())
