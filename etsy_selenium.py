#!/usr/bin/env python3
"""
Etsy Scraper — Selenium Edition
=================================
Etsy product scraper built with Selenium WebDriver.
Supports all Etsy categories, JSON/CSV export, proxy rotation (2prx.com),
CAPTCHA solving (2captcha.com), and browser fingerprint randomization.

Repository : https://github.com/2scraper/etsy-scraper
License    : MIT
"""

from __future__ import annotations

import argparse
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
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.common.exceptions import (
        NoSuchElementException,
        TimeoutException,
        WebDriverException,
    )
except ImportError:
    sys.exit(
        "Selenium is required.  Install it:\n"
        "  pip install selenium webdriver-manager"
    )

try:
    from webdriver_manager.chrome import ChromeDriverManager
    HAS_WDM = True
except ImportError:
    HAS_WDM = False

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
log = logging.getLogger("etsy-sel")

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
        "width": 1680, "height": 1050,
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    },
]

# ---------------------------------------------------------------------------
# Data model (same as Playwright version for consistency)
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

    def solve_recaptcha(self, sitekey: str, page_url: str) -> str | None:
        if not self.solver:
            log.warning("2captcha not configured — skipping CAPTCHA")
            return None
        log.info("Solving reCAPTCHA via 2captcha.com …")
        try:
            result = self.solver.recaptcha(sitekey=sitekey, url=page_url)
            log.info("CAPTCHA solved ✓")
            return result.get("code")
        except Exception as exc:
            log.error("CAPTCHA solve failed: %s", exc)
            return None

    def solve_hcaptcha(self, sitekey: str, page_url: str) -> str | None:
        if not self.solver:
            return None
        log.info("Solving hCaptcha via 2captcha.com …")
        try:
            result = self.solver.hcaptcha(sitekey=sitekey, url=page_url)
            log.info("CAPTCHA solved ✓")
            return result.get("code")
        except Exception as exc:
            log.error("CAPTCHA solve failed: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Stealth JS (injected via execute_script)
# ---------------------------------------------------------------------------
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
window.chrome = { runtime: {} };
"""


# ---------------------------------------------------------------------------
# Core scraper
# ---------------------------------------------------------------------------
class EtsyScraperSelenium:
    """Selenium-based Etsy scraper with stealth, proxy & CAPTCHA support."""

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
        self.driver: webdriver.Chrome | None = None

    # ---- browser lifecycle ------------------------------------------------
    def _build_driver(self) -> webdriver.Chrome:
        fp = random.choice(FINGERPRINTS) if self.use_fingerprint else FINGERPRINTS[0]

        opts = ChromeOptions()
        if self.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument(f"--window-size={fp['width']},{fp['height']}")
        opts.add_argument(f"user-agent={fp['user_agent']}")

        # Exclude automation switches
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)

        if self.proxy_url:
            opts.add_argument(f"--proxy-server={self.proxy_url}")
            log.info("Using proxy: %s", self.proxy_url)

        if HAS_WDM:
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=opts)
        else:
            driver = webdriver.Chrome(options=opts)

        # Stealth
        if self.use_fingerprint:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": STEALTH_JS},
            )
            log.info("Fingerprint applied: %s", fp["user_agent"][:60])

        return driver

    def _wait(self):
        time.sleep(random.uniform(*self.delay))

    # ---- CAPTCHA handling -------------------------------------------------
    def _handle_captcha(self):
        driver = self.driver
        # reCAPTCHA
        try:
            el = driver.find_element(By.CSS_SELECTOR, "[data-sitekey]")
            sitekey = el.get_attribute("data-sitekey")
            token = self.captcha.solve_recaptcha(sitekey, driver.current_url)
            if token:
                driver.execute_script(
                    f'document.getElementById("g-recaptcha-response").innerHTML="{token}";'
                    'document.querySelector("form").submit();'
                )
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                return True
        except NoSuchElementException:
            pass

        # hCaptcha
        try:
            el = driver.find_element(By.CSS_SELECTOR, "[data-hcaptcha-sitekey], .h-captcha")
            sitekey = el.get_attribute("data-sitekey") or el.get_attribute("data-hcaptcha-sitekey")
            token = self.captcha.solve_hcaptcha(sitekey, driver.current_url)
            if token:
                driver.execute_script(
                    f'document.querySelector(\'[name="h-captcha-response"]\').value = "{token}";'
                    'document.querySelector("form").submit();'
                )
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                return True
        except NoSuchElementException:
            pass

        return False

    # ---- search page parsing ---------------------------------------------
    def _safe_text(self, parent, css: str) -> str:
        try:
            return parent.find_element(By.CSS_SELECTOR, css).text.strip()
        except NoSuchElementException:
            return ""

    def _safe_attr(self, parent, css: str, attr: str) -> str:
        try:
            return parent.find_element(By.CSS_SELECTOR, css).get_attribute(attr) or ""
        except NoSuchElementException:
            return ""

    def _parse_search_page(self) -> list[EtsyProduct]:
        driver = self.driver
        items: list[EtsyProduct] = []

        cards = driver.find_elements(
            By.CSS_SELECTOR,
            "[data-search-results] .wt-grid__item-xs-6, "
            ".search-listings-group .wt-grid__item-xs-6, "
            ".listing-card",
        )
        if not cards:
            cards = driver.find_elements(By.CSS_SELECTOR, "a[href*='/listing/']")

        for card in cards:
            try:
                p = EtsyProduct()
                p.scraped_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

                # URL
                try:
                    link = card if card.tag_name == "a" else card.find_element(
                        By.CSS_SELECTOR, "a[href*='/listing/']"
                    )
                    href = link.get_attribute("href") or ""
                    p.url = urljoin(BASE_URL, href.split("?")[0])
                    m = LISTING_URL_RE.search(href)
                    if m:
                        p.listing_id = m.group(1)
                except NoSuchElementException:
                    continue

                p.title = self._safe_text(card, "h3, h2, [data-listing-card-title]")
                p.price = self._safe_text(card, ".currency-value, .lc-price .wt-text-title-01")
                p.original_price = self._safe_text(card, ".wt-text-strikethrough")
                p.shop_name = self._safe_text(card, ".wt-text-gray .wt-text-link-no-underline, p.wt-text-caption")
                p.image_url = self._safe_attr(card, "img", "src")

                text_block = card.text.lower()
                p.is_bestseller = "bestseller" in text_block
                p.is_star_seller = "star seller" in text_block
                p.free_shipping = "free shipping" in text_block

                if p.listing_id or p.url:
                    items.append(p)
            except Exception as exc:
                log.debug("Card parse error: %s", exc)

        return items

    # ---- detail page enrichment ------------------------------------------
    def _enrich_product(self, product: EtsyProduct):
        driver = self.driver
        try:
            driver.get(product.url)
            self._wait()
            self._handle_captcha()

            product.description = self._safe_text(
                driver,
                "#wt-content-toggle-product-details-read-more, "
                "[data-id='description-text'], "
                ".wt-content-toggle--truncated",
            )[:2000]

            imgs = driver.find_elements(By.CSS_SELECTOR, "ul.carousel-pane-list img, [data-carousel] img")
            product.images = [
                img.get_attribute("src") or ""
                for img in imgs
                if "75x75" not in (img.get_attribute("src") or "")
            ]

            shop_href = self._safe_attr(driver, "a[href*='/shop/']", "href")
            if shop_href:
                product.shop_url = urljoin(BASE_URL, shop_href) if not shop_href.startswith("http") else shop_href

            product.reviews_count = self._safe_text(driver, "a[href*='#reviews'] span")

            tag_els = driver.find_elements(By.CSS_SELECTOR, "#wt-content-toggle-tags-read-more a")
            product.tags = [t.text.strip() for t in tag_els[:15] if t.text.strip()]

            log.info("  ✓ Enriched: %s", product.title[:50])
        except Exception as exc:
            log.warning("  ✗ Enrich failed for %s: %s", product.listing_id, exc)

    # ---- public API -------------------------------------------------------
    def search(
        self,
        query: str = "",
        category: str = "",
        min_price: float | None = None,
        max_price: float | None = None,
        enrich: bool = False,
    ) -> list[EtsyProduct]:
        self.driver = self._build_driver()
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
                self.driver.get(url)
                self._wait()
                self._handle_captcha()

                # Scroll
                for _ in range(3):
                    self.driver.execute_script("window.scrollBy(0, window.innerHeight)")
                    time.sleep(0.5)

                items = self._parse_search_page()
                if not items:
                    log.info("No more results — stopping")
                    break

                self.products.extend(items)
                log.info("  → Collected %d items (total: %d)", len(items), len(self.products))
                self._wait()

            if enrich:
                log.info("Enriching %d products …", len(self.products))
                for prod in self.products:
                    if prod.url:
                        self._enrich_product(prod)

        finally:
            self.driver.quit()

        return self.products

    def scrape_listing(self, url: str) -> EtsyProduct:
        self.driver = self._build_driver()
        try:
            p = EtsyProduct(url=url)
            m = LISTING_URL_RE.search(url)
            if m:
                p.listing_id = m.group(1)
            p.scraped_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._enrich_product(p)
            p.title = self._safe_text(self.driver, "h1")
            p.price = self._safe_text(self.driver, "[data-buy-box-listing-price] .currency-value")
            self.products.append(p)
        finally:
            self.driver.quit()
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
def build_scraper() -> argparse.Argumentscraper:
    p = argparse.Argumentscraper(
        prog="etsy_selenium",
        description="Etsy Scraper (Selenium) — by 2scraper",
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


def main():
    args = build_scraper().parse_args()

    scraper = EtsyScraperSelenium(
        headless=not args.headed,
        proxy=args.proxy,
        captcha_key=args.captcha_key,
        fingerprint=not args.no_fingerprint,
        max_pages=args.pages,
        delay=tuple(args.delay),
        output_dir=args.output,
    )

    if args.url:
        scraper.scrape_listing(args.url)
    else:
        scraper.search(
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
    main()
