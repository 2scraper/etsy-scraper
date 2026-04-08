#!/usr/bin/env python3
"""
Etsy Scraper — Playwright Edition
===================================
High-performance Etsy product scraper built with Playwright.
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
from urllib.parse import quote_plus, urlencode, urljoin

# ---------------------------------------------------------------------------
# Optional imports — graceful degradation
# ---------------------------------------------------------------------------
try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
except ImportError:
    sys.exit(
        "Playwright is required.  Install it:\n"
        "  pip install playwright && python -m playwright install chromium"
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
log = logging.getLogger("etsy-pw")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_URL = "https://www.etsy.com"
SEARCH_URL = f"{BASE_URL}/search"
LISTING_URL_RE = re.compile(r"/listing/(\d+)/")

DEFAULT_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Fingerprint presets — viewport + UA combos that look real
FINGERPRINTS = [
    {
        "viewport": {"width": 1920, "height": 1080},
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "platform": "Win32",
    },
    {
        "viewport": {"width": 1440, "height": 900},
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "platform": "MacIntel",
    },
    {
        "viewport": {"width": 1536, "height": 864},
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        "platform": "Win32",
    },
    {
        "viewport": {"width": 1680, "height": 1050},
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "platform": "MacIntel",
    },
    {
        "viewport": {"width": 2560, "height": 1440},
        "user_agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "platform": "Linux x86_64",
    },
]

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class EtsyProduct:
    """Single Etsy listing."""
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
# Captcha solver (2captcha.com)
# ---------------------------------------------------------------------------
class CaptchaSolver:
    """Thin wrapper around 2captcha SDK."""

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
            log.info("CAPTCHA solved ✓")
            return result.get("code")
        except Exception as exc:
            log.error("CAPTCHA solve failed: %s", exc)
            return None

    async def solve_hcaptcha(self, sitekey: str, page_url: str) -> str | None:
        if not self.solver:
            log.warning("2captcha not configured — skipping CAPTCHA")
            return None
        log.info("Solving hCaptcha via 2captcha.com …")
        try:
            result = await asyncio.to_thread(
                self.solver.hcaptcha, sitekey=sitekey, url=page_url
            )
            log.info("CAPTCHA solved ✓")
            return result.get("code")
        except Exception as exc:
            log.error("CAPTCHA solve failed: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Proxy helpers (2prx.com)
# ---------------------------------------------------------------------------
def build_proxy_config(proxy_url: str | None = None) -> dict | None:
    """
    Accept proxy string in format:
      protocol://user:pass@host:port
    or read from env  PROXY_URL / TWO_PRX_URL
    """
    url = proxy_url or os.getenv("PROXY_URL") or os.getenv("TWO_PRX_URL")
    if not url:
        return None
    # Playwright proxy dict
    return {"server": url}


# ---------------------------------------------------------------------------
# Fingerprint injection
# ---------------------------------------------------------------------------
def pick_fingerprint() -> dict:
    return random.choice(FINGERPRINTS)


STEALTH_JS = """
() => {
    // Mask webdriver flag
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

    // Spoof plugins length
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5],
    });

    // Spoof languages
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
    });

    // Override chrome runtime
    window.chrome = { runtime: {} };

    // Spoof permissions
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({state: Notification.permission})
            : originalQuery(parameters);
}
"""


# ---------------------------------------------------------------------------
# Core scraper
# ---------------------------------------------------------------------------
class EtsyScraper:
    """Playwright-based Etsy scraper with stealth, proxy & CAPTCHA support."""

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
        self.proxy_cfg = build_proxy_config(proxy)
        self.captcha = CaptchaSolver(captcha_key)
        self.use_fingerprint = fingerprint
        self.max_pages = max_pages
        self.delay = delay
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.products: list[EtsyProduct] = []

    # ---- browser lifecycle ------------------------------------------------
    async def _launch(self) -> tuple[Browser, BrowserContext]:
        pw = await async_playwright().start()
        fp = pick_fingerprint() if self.use_fingerprint else FINGERPRINTS[0]

        launch_args: dict[str, Any] = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        }
        if self.proxy_cfg:
            launch_args["proxy"] = self.proxy_cfg
            log.info("Using proxy: %s", self.proxy_cfg["server"])

        browser = await pw.chromium.launch(**launch_args)
        context = await browser.new_context(
            viewport=fp["viewport"],
            user_agent=fp["user_agent"],
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers=DEFAULT_HEADERS,
        )
        # Inject stealth script on every new page
        if self.use_fingerprint:
            await context.add_init_script(STEALTH_JS)
            log.info("Fingerprint applied: %s", fp["user_agent"][:60])

        return browser, context

    # ---- human-like delay -------------------------------------------------
    async def _wait(self):
        t = random.uniform(*self.delay)
        log.debug("Sleeping %.1fs", t)
        await asyncio.sleep(t)

    # ---- CAPTCHA detection & solving --------------------------------------
    async def _handle_captcha(self, page: Page) -> bool:
        """Detect and solve CAPTCHA if present. Returns True if solved."""
        # reCAPTCHA
        rc = await page.query_selector("[data-sitekey]")
        if rc:
            sitekey = await rc.get_attribute("data-sitekey")
            token = await self.captcha.solve_recaptcha(sitekey, page.url)
            if token:
                await page.evaluate(
                    f'document.getElementById("g-recaptcha-response").innerHTML="{token}";'
                )
                await page.evaluate("document.querySelector('form').submit();")
                await page.wait_for_load_state("networkidle")
                return True

        # hCaptcha
        hc = await page.query_selector("[data-hcaptcha-sitekey], .h-captcha")
        if hc:
            sitekey = await hc.get_attribute("data-sitekey") or await hc.get_attribute(
                "data-hcaptcha-sitekey"
            )
            token = await self.captcha.solve_hcaptcha(sitekey, page.url)
            if token:
                await page.evaluate(
                    f"""
                    document.querySelector('[name="h-captcha-response"]').value = "{token}";
                    document.querySelector('form').submit();
                    """
                )
                await page.wait_for_load_state("networkidle")
                return True

        return False

    # ---- search result parsing -------------------------------------------
    async def _parse_search_page(self, page: Page) -> list[EtsyProduct]:
        """Extract product cards from a search/category result page."""
        items: list[EtsyProduct] = []

        cards = await page.query_selector_all(
            "[data-search-results] .wt-grid__item-xs-6, "
            ".search-listings-group .wt-grid__item-xs-6, "
            "li.wt-list-unstyled [data-logger-id='search_listing_card'],"
            ".listing-card"
        )
        if not cards:
            # Fallback: try broader selector
            cards = await page.query_selector_all("a[href*='/listing/']")

        for card in cards:
            try:
                p = EtsyProduct()
                p.scraped_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

                # URL & listing ID
                link = card if card.evaluate("el => el.tagName") == "A" else await card.query_selector("a[href*='/listing/']")
                if link:
                    href = await link.get_attribute("href") or ""
                    p.url = urljoin(BASE_URL, href.split("?")[0])
                    m = LISTING_URL_RE.search(href)
                    if m:
                        p.listing_id = m.group(1)

                # Title
                title_el = await card.query_selector("h3, h2, [data-listing-card-title]")
                if title_el:
                    p.title = (await title_el.inner_text()).strip()

                # Price
                price_el = await card.query_selector(
                    ".currency-value, .lc-price .wt-text-title-01, span.currency-value"
                )
                if price_el:
                    p.price = (await price_el.inner_text()).strip()

                # Original price (strike-through)
                orig_el = await card.query_selector(
                    ".lc-price .wt-text-strikethrough, .search-collage-original-price"
                )
                if orig_el:
                    p.original_price = (await orig_el.inner_text()).strip()

                # Shop name
                shop_el = await card.query_selector(
                    ".wt-text-gray .wt-text-link-no-underline, .v2-listing-card__shop, "
                    "p.wt-text-caption"
                )
                if shop_el:
                    p.shop_name = (await shop_el.inner_text()).strip()

                # Image
                img_el = await card.query_selector("img")
                if img_el:
                    p.image_url = await img_el.get_attribute("src") or ""

                # Rating
                rating_el = await card.query_selector(
                    "[data-star-rating], .wt-icon--star-filled"
                )
                if rating_el:
                    p.rating = await rating_el.get_attribute("data-star-rating") or ""

                # Badges
                text_block = await card.inner_text()
                p.is_bestseller = "bestseller" in text_block.lower()
                p.is_star_seller = "star seller" in text_block.lower()
                p.free_shipping = "free shipping" in text_block.lower()

                if p.listing_id or p.url:
                    items.append(p)
            except Exception as exc:
                log.debug("Card parse error: %s", exc)
                continue

        return items

    # ---- detail page enrichment ------------------------------------------
    async def _enrich_product(self, page: Page, product: EtsyProduct):
        """Visit a listing page to collect full details."""
        try:
            await page.goto(product.url, wait_until="domcontentloaded", timeout=30_000)
            await self._wait()
            await self._handle_captcha(page)

            # Description
            desc_el = await page.query_selector(
                "#wt-content-toggle-product-details-read-more, "
                "[data-id='description-text'], "
                ".wt-content-toggle--truncated"
            )
            if desc_el:
                product.description = (await desc_el.inner_text()).strip()[:2000]

            # All images
            imgs = await page.query_selector_all(
                "ul.carousel-pane-list img, [data-carousel] img"
            )
            product.images = []
            for img in imgs:
                src = await img.get_attribute("src") or await img.get_attribute("data-src") or ""
                if src and "75x75" not in src:
                    product.images.append(src)

            # Shop URL
            shop_link = await page.query_selector("a[href*='/shop/']")
            if shop_link:
                product.shop_url = await shop_link.get_attribute("href") or ""
                if product.shop_url and not product.shop_url.startswith("http"):
                    product.shop_url = urljoin(BASE_URL, product.shop_url)

            # Reviews count
            reviews_el = await page.query_selector(
                "a[href*='#reviews'] span, [data-reviews-count]"
            )
            if reviews_el:
                product.reviews_count = (await reviews_el.inner_text()).strip()

            # Tags
            tag_els = await page.query_selector_all(
                "#wt-content-toggle-tags-read-more a, a[href*='/search?q=']"
            )
            product.tags = []
            for t in tag_els[:15]:
                txt = (await t.inner_text()).strip()
                if txt and len(txt) < 60:
                    product.tags.append(txt)

            # Favorites
            fav_el = await page.query_selector(
                "[data-favorite-count], .wt-text-caption:has-text('favorites')"
            )
            if fav_el:
                product.favorites_count = re.sub(
                    r"[^\d]", "", await fav_el.inner_text()
                )

            # Ships from
            ships_el = await page.query_selector(
                "[data-ships-from], .wt-text-caption:has-text('Ships from')"
            )
            if ships_el:
                product.ships_from = (await ships_el.inner_text()).replace("Ships from", "").strip()

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
        """
        Scrape Etsy search results.

        Args:
            query:      Search keywords (e.g. "handmade jewelry")
            category:   Category slug (e.g. "jewelry", "clothing", "home-and-living")
            min_price:  Filter — minimum price
            max_price:  Filter — maximum price
            enrich:     Visit each listing for full details

        Returns:
            List of EtsyProduct dataclasses
        """
        browser, ctx = await self._launch()
        try:
            page = await ctx.new_page()

            for page_num in range(1, self.max_pages + 1):
                # Build URL
                params: dict[str, Any] = {}
                if query:
                    params["q"] = query
                if min_price is not None:
                    params["min"] = str(min_price)
                if max_price is not None:
                    params["max"] = str(max_price)
                params["ref"] = "pagination"
                params["page"] = str(page_num)

                if category:
                    url = f"{BASE_URL}/c/{category}?{urlencode(params)}"
                else:
                    url = f"{SEARCH_URL}?{urlencode(params)}"

                log.info("Page %d/%d → %s", page_num, self.max_pages, url)
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await self._wait()

                # Handle CAPTCHA
                await self._handle_captcha(page)

                # Scroll to trigger lazy loading
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

            # Enrich each product with full details
            if enrich and self.products:
                log.info("Enriching %d products …", len(self.products))
                for prod in self.products:
                    if prod.url:
                        await self._enrich_product(page, prod)

        finally:
            await browser.close()

        return self.products

    async def scrape_listing(self, url: str) -> EtsyProduct:
        """Scrape a single Etsy listing by URL."""
        browser, ctx = await self._launch()
        try:
            page = await ctx.new_page()
            p = EtsyProduct(url=url)
            m = LISTING_URL_RE.search(url)
            if m:
                p.listing_id = m.group(1)
            p.scraped_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            await self._enrich_product(page, p)
            # Also grab title/price from detail page
            title_el = await page.query_selector("h1")
            if title_el:
                p.title = (await title_el.inner_text()).strip()
            price_el = await page.query_selector("[data-buy-box-listing-price] .currency-value, .wt-text-title-03 .currency-value")
            if price_el:
                p.price = (await price_el.inner_text()).strip()
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
            log.warning("No data to export")
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

    # ---- deduplication ----------------------------------------------------
    def deduplicate(self):
        seen: set[str] = set()
        unique: list[EtsyProduct] = []
        for p in self.products:
            uid = p.uid()
            if uid not in seen:
                seen.add(uid)
                unique.append(p)
        removed = len(self.products) - len(unique)
        self.products = unique
        if removed:
            log.info("Removed %d duplicates", removed)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="etsy_playwright",
        description="Etsy Scraper (Playwright) — by 2parser",
    )
    p.add_argument("query", nargs="?", default="", help="Search query")
    p.add_argument("-c", "--category", default="", help="Etsy category slug")
    p.add_argument("-p", "--pages", type=int, default=3, help="Max pages to scrape")
    p.add_argument("--min-price", type=float, default=None)
    p.add_argument("--max-price", type=float, default=None)
    p.add_argument("--enrich", action="store_true", help="Visit each listing for full details")
    p.add_argument("--url", default="", help="Scrape a single listing URL")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--output", default="output", help="Output directory")
    p.add_argument("--proxy", default=None, help="Proxy URL (or set PROXY_URL / TWO_PRX_URL env)")
    p.add_argument("--captcha-key", default=None, help="2captcha.com API key (or set TWOCAPTCHA_API_KEY env)")
    p.add_argument("--no-fingerprint", action="store_true", help="Disable fingerprint randomization")
    p.add_argument("--headed", action="store_true", help="Show browser window")
    p.add_argument("--delay", type=float, nargs=2, default=[1.5, 4.0], metavar=("MIN", "MAX"))
    return p


async def main():
    args = build_parser().parse_args()

    scraper = EtsyScraper(
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
