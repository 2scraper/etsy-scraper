# 🛒 Etsy Scraper by 2parser

**Open-source Etsy product scraper** with three engine options, CAPTCHA bypass, proxy support, and anti-detection features.

![Python](https://img.shields.io/badge/python-3.10+-blue?logo=python&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)
![Playwright](https://img.shields.io/badge/engine-Playwright-45ba4b?logo=playwright)
![Selenium](https://img.shields.io/badge/engine-Selenium-43B02A?logo=selenium)
![Puppeteer](https://img.shields.io/badge/engine-Pyppeteer-40B5A4)

---

## What It Does

Scrapes product data from **any Etsy category** — search results and individual listings — and exports clean structured data in **JSON** or **CSV**.

### Data Points Collected

| Field | Search | Detail |
|---|:-:|:-:|
| Listing ID, Title, URL | ✅ | ✅ |
| Price / Original Price / Discount | ✅ | ✅ |
| Shop Name & URL | ✅ | ✅ |
| Rating & Reviews Count | ✅ | ✅ |
| All Product Images | — | ✅ |
| Full Description | — | ✅ |
| Tags & Materials | — | ✅ |
| Bestseller / Star Seller Badge | ✅ | ✅ |
| Free Shipping | ✅ | ✅ |
| Ships From / Delivery Estimate | — | ✅ |
| Favorites Count | — | ✅ |

---

## Three Engines — One Interface

All three scrapers share the **same CLI interface and data model**, so you can switch engines without changing your pipeline.

| Engine | Script | Async | Best For |
|---|---|:-:|---|
| **Playwright** ⭐ | `etsy_playwright.py` | ✅ | Recommended default — fast, reliable, modern |
| **Selenium** | `etsy_selenium.py` | — | Legacy environments, existing Selenium infra |
| **Pyppeteer** | `etsy_pyppeteer.py` | ✅ | Puppeteer fans who prefer Python |

---

## Quick Start

### 1. Clone

```bash
git clone https://github.com/2parser/etsy-parser.git
cd etsy-parser
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt

# For Playwright — install browser binaries:
python -m playwright install chromium
```

### 3. Run

```bash
# Search for products (Playwright — recommended)
python scrapers/etsy_playwright.py "handmade jewelry" --pages 5 --format both

# Browse a category
python scrapers/etsy_playwright.py -c jewelry --pages 3

# Scrape a single listing with full details
python scrapers/etsy_playwright.py --url "https://www.etsy.com/listing/123456789/..."

# Get full details for every product (slower but complete)
python scrapers/etsy_playwright.py "vintage posters" --enrich --pages 2

# Same commands work for Selenium:
python scrapers/etsy_selenium.py "leather bags" --pages 3

# ...and Pyppeteer:
python scrapers/etsy_pyppeteer.py "ceramic mugs" --pages 3
```

---

## Anti-Detection Features

### 🎭 Browser Fingerprints

Every run randomizes viewport, user-agent, platform, and timezone. WebDriver flags are masked with stealth scripts injected before any page loads.

```bash
# Disable fingerprint randomization (use a fixed profile)
python scrapers/etsy_playwright.py "rings" --no-fingerprint
```

### 🔑 CAPTCHA Solving via 2captcha.com

Integrates with [2captcha.com](https://2captcha.com/?from=2parser) — supports reCAPTCHA v2/v3 and hCaptcha.

```bash
# Pass API key directly
python scrapers/etsy_playwright.py "earrings" --captcha-key YOUR_2CAPTCHA_KEY

# Or set environment variable (recommended)
export TWOCAPTCHA_API_KEY=your_key_here
python scrapers/etsy_playwright.py "earrings"
```

### 🌐 Proxy Support via 2prx.com

Route all traffic through residential or datacenter proxies from [2prx.com](https://2prx.com/?from=2parser).

```bash
# Pass proxy URL directly
python scrapers/etsy_playwright.py "candles" --proxy http://user:pass@gate.2prx.com:8080

# Or set environment variable
export PROXY_URL=http://user:pass@gate.2prx.com:8080
python scrapers/etsy_playwright.py "candles"

# Alternative env variable name
export TWO_PRX_URL=socks5://user:pass@gate.2prx.com:1080
```

### 🕵️ Anti-Detect Browser

For maximum stealth on large-scale scraping, we offer a dedicated **Anti-Detect Browser** that provides hardware-level fingerprint spoofing, isolated browser profiles, and a cookie manager. [Contact us](https://2captcha.com/anti-detect-browser) for details and pricing.

---

## CLI Reference

```
usage: etsy_playwright.py [-h] [-c CATEGORY] [-p PAGES] [--min-price MIN]
                          [--max-price MAX] [--enrich] [--url URL]
                          [--format {json,csv,both}] [--output DIR]
                          [--proxy URL] [--captcha-key KEY]
                          [--no-fingerprint] [--headed] [--delay MIN MAX]
                          [query]

positional arguments:
  query                  Search keywords (e.g. "handmade jewelry")

options:
  -c, --category SLUG    Etsy category slug (jewelry, clothing, home-and-living…)
  -p, --pages N          Max result pages to scrape (default: 3)
  --min-price FLOAT      Filter: minimum price
  --max-price FLOAT      Filter: maximum price
  --enrich               Visit each listing for full details
  --url URL              Scrape a single listing URL
  --format {json,csv,both}  Output format (default: both)
  --output DIR           Output directory (default: output/)
  --proxy URL            Proxy URL (or set PROXY_URL env)
  --captcha-key KEY      2captcha.com API key (or set TWOCAPTCHA_API_KEY env)
  --no-fingerprint       Disable fingerprint randomization
  --headed               Show browser window (non-headless)
  --delay MIN MAX        Random delay range in seconds (default: 1.5 4.0)
```

---

## Etsy Categories

Pass any category slug with `-c`. Some popular ones:

| Category | Slug |
|---|---|
| Jewelry & Accessories | `jewelry` |
| Clothing & Shoes | `clothing` |
| Home & Living | `home-and-living` |
| Wedding & Party | `weddings` |
| Toys & Entertainment | `toys-and-games` |
| Art & Collectibles | `art-and-collectibles` |
| Craft Supplies & Tools | `craft-supplies-and-tools` |
| Vintage | `vintage` |
| Gifts | `gifts` |
| Electronics & Accessories | `electronics-and-accessories` |
| Books, Movies & Music | `books-movies-and-music` |
| Bags & Purses | `bags-and-purses` |
| Bath & Beauty | `bath-and-beauty` |
| Pet Supplies | `pet-supplies` |

Or leave empty and use a search query to scrape across all categories.

---

## Output Examples

### JSON

```json
[
  {
    "listing_id": "1234567890",
    "title": "Handmade Silver Ring — Minimalist Band",
    "url": "https://www.etsy.com/listing/1234567890/handmade-silver-ring",
    "price": "42.00",
    "currency": "USD",
    "shop_name": "SilverCraftStudio",
    "rating": "4.9",
    "reviews_count": "2,847",
    "is_bestseller": true,
    "free_shipping": true,
    "tags": ["silver ring", "minimalist", "handmade"],
    "scraped_at": "2025-03-15T14:22:00Z"
  }
]
```

### CSV

```
listing_id,title,url,price,currency,shop_name,rating,...
1234567890,Handmade Silver Ring,https://www.etsy.com/listing/...,42.00,USD,SilverCraftStudio,4.9,...
```

---

## Use as a Python Library

```python
import asyncio
from scrapers.etsy_playwright import EtsyScraper

async def main():
    scraper = EtsyScraper(
        proxy="http://user:pass@gate.2prx.com:8080",
        captcha_key="your_2captcha_key",
        max_pages=10,
    )

    products = await scraper.search(
        query="vintage dress",
        min_price=20,
        max_price=100,
        enrich=True,
    )

    scraper.to_json("vintage_dresses.json")
    scraper.to_csv("vintage_dresses.csv")

    for p in products[:3]:
        print(f"{p.title} — ${p.price} ({p.shop_name})")

asyncio.run(main())
```

---

## Project Structure

```
etsy-parser/
├── scrapers/
│   ├── etsy_playwright.py    # ⭐ Recommended
│   ├── etsy_selenium.py
│   └── etsy_pyppeteer.py
├── output/                   # Scraped data lands here
├── requirements.txt
├── LICENSE
└── README.md
```

---

## Links

- **GitHub**: [github.com/2parser/etsy-parser](https://github.com/2parser/etsy-parser)
- **CAPTCHA Solving**: [2captcha.com](https://2captcha.com/?from=2parser)
- **Proxies**: [2prx.com](https://2prx.com/?from=2parser)
- **Anti-Detect Browser**: [2captcha.com/anti-detect-browser](https://2captcha.com/anti-detect-browser)
- **Landing Page**: [2captcha.com/etsy-scraper](https://2captcha.com/etsy-scraper)

---

## License

MIT — free for personal and commercial use. See [LICENSE](LICENSE).

---

## Disclaimer

This tool is provided for educational and research purposes. Users are responsible for complying with Etsy's Terms of Service and applicable laws. The authors are not liable for any misuse.
