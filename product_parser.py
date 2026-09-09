"""
product_parser.py
-----------------
Extracts rows from Etsy search results, category listings, shop fronts and
listing (product) pages. This module is where essentially all of the site
knowledge in this repo lives; the engines carry about a dozen constants and
nothing else.

Where the data comes from — and why the family's usual order is INVERTED
---------------------------------------------------------------------------
The rest of this family reads structured data first and falls back to the
DOM. On Etsy that would silently lose most of a search page, because Etsy's
JSON-LD `ItemList` is PARTIAL and how partial depends on the page kind.
Measured on live captures taken 2026-09-09 over the Scraping Browser API:

    /de/search?q=handmade+mug      64 tiles,  8 ItemList entries
    /de/c/.../mugs                 65 tiles, 61 ItemList entries
    /de/shop/StonehousePotteryOH   40 tiles, 36 ItemList entries

Eight of sixty-four. So the PRIMARY anchor here is Etsy's own data attribute
`data-listing-id` — the `data-asin` case from the family notes — and JSON-LD
is used to ENRICH and to cross-check. The `/listing/<id>/<slug>` URL pattern
is the fallback below that.

The two views join reliably: every listing id found in the JSON-LD was also
present among the DOM tiles (61/61, 8/8, 36/36 on the three captures), so an
enriched row is never a guess about which product it belongs to.

`[data-listing-id]` matches MORE nodes than there are products — 129 div
cards for 64 distinct ids on the live search page, because each tile is
emitted twice for the desktop and mobile layouts, and the attribute also sits
on `<video>` and on the favourite button. Count DISTINCT ids.

WARNING — sponsored listings are mixed into organic results
-----------------------------------------------------------
16 of 64 tiles on the live German search page are ads, and Etsy labels them
in the page's own language: "Anzeige" / "Anzeige des Etsy-Shops" on a German
page, "Ad from shop" on an English one. A text marker would catch one locale
and silently let the other through, which is how paid placements enter the
data as organic results.

The signal used instead is STRUCTURAL: the tile's own link carries `ls=a` on
an ad and `ls=s` on an organic result. Verified against the text marker
across four captures with ZERO disagreements — 128/128 on the live German
search page, 64/64 on the live category page, 64/64 on a second category
capture, 24/24 on an English one. Exactly one tile per page carries no `ls`
at all, and that row reports `is_ad=None` rather than a guess.

Note the raw markup writes those separators as `&amp;`, so a grep for
`&ls=a` over the HTML text finds nothing while the parsed attribute has it.
The same trap hides Etsy's pagination links from a naive `&page=` grep.

A listing can appear BOTH as an ad and organically on one page, so `is_ad` is
a property of the ROW and a dedupe on `sku` collapses the pair.

WARNING — two rating markups are live at the same time
------------------------------------------------------
This is the reason this parser was written against live captures rather than
archived ones. On 2026-09-09, in the same hour:

    search page    <clg-static-review-stars rating="5.0"
                                            review-count-text="(17)">
                   a custom element carrying both numbers as ATTRIBUTES.
                   62 of 63 tiles. No `star-rating-` class anywhere.

    category page  <div class="sprite-img black-stars star-rating-5"
                        aria-label="5 von 5 Sternen">  + "(7)" in the text
                   56 of 65 tiles. No custom element anywhere.

    shop page      neither. 0 of 40 tiles carry a rating at all.

Both forms are read. The `star-rating-N` CLASS is used for the older form
rather than its aria-label, because the label is written in the page's
language ("5 von 5 Sternen") while the class is not. A Wayback snapshot from
three weeks earlier used the sprite form on the search page too, so this
markup is moving — if ratings go null on one page kind, look for a third
form before assuming the parser broke.

The prices are one node, and it holds two of them
-------------------------------------------------
A discounted tile's price container reads, as text:

    "Sale-Preis 35,87 € 35,87 € 59,79 € Ursprünglicher Preis 59,79 € (40% Rabatt)"

Both figures and a percentage badge live inside `div.n-listing-card__price`,
so reading "the first price" out of that node's text is a coin flip. Every
read here is scoped: the current price comes from the node with the
strikethrough and promotion subtrees REMOVED, and the original price comes
from the strikethrough span only.

`div.n-listing-card__price` is the one price container present on all three
page kinds. `p.lc-price` is NOT — it is absent on every shop-page tile (0 of
39), which is why the inner `span.currency-value` / `span.currency-symbol`
pair is what this parser actually reads.

"ab 34,00 €" / "from $34.00" is a MINIMUM, not a price
------------------------------------------------------
12 of 63 live search tiles and 15 of 64 category tiles print a from-price,
because the listing has variations. Its structured counterpart is an
`AggregateOffer` with `lowPrice`/`highPrice` and NO `price` key at all — so
`offers.get("price")` returns None on a product with a perfectly good price
range. Both are recorded honestly: `price` holds the low end and
`price_is_from` says so, with `price_max` carrying the high end where the
structured data gives one.

What was deliberately NOT ported from the sibling repos
-------------------------------------------------------
* The dash-cents form ("349,– €"). Zero occurrences across every Etsy
  capture: Etsy generates its own price markup rather than letting sellers
  write it, so the German retail convention never appears.
* The second struck-through price (the EU Omnibus 30-day low). Etsy tiles
  carry exactly ONE kind of strikethrough — the was-price — so there is no
  `lowest_price_30d` column here and nothing to confuse it with.
* The tile-price overlay that reconciles a structured price against a
  rendered one. It survives in spirit as `price_source`, but the direction
  is reversed: the DOM is primary and JSON-LD confirms it.

Percentage stripping IS kept, and is load-bearing rather than inherited:
"(40% Rabatt)" sits inside the price node on every discounted tile.
"""

import copy
import json
import logging
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import (urlparse, urlunparse, parse_qsl, urlencode, unquote,
                          urljoin)

from bs4 import BeautifulSoup

from output_writer import Product, SOURCE_DEFAULT

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The site, and its locales
# ---------------------------------------------------------------------------
# Etsy is ONE host. Unlike the ten country hostnames in the sibling
# mediamarkt-scraper in this family, every Etsy market lives on
# www.etsy.com and the LOCALE is a
# path prefix (/de/, /uk/, /ca-fr/). So there is no host table to derive and
# no per-host currency — the currency follows the locale segment instead.
HOSTS: Dict[str, str] = {"etsy.com": ""}

# Not a guessed list: exactly the `<link rel="alternate" hreflang=...>` set
# Etsy publishes on its own pages, read off a live capture on 2026-09-09 (29
# entries), with the three that carry no path segment (`en`, `en-US`,
# `x-default`, all pointing at /market/) collapsed into the default
# storefront under the "" key.
#
# The currency is a FALLBACK ONLY. Every structured price on this site names
# its own currency in `offers.priceCurrency` and that always wins; this table
# is consulted when a DOM-only read produced a bare symbol that cannot name
# itself. "$" is the whole problem — it is USD, CAD, AUD, NZD, HKD, SGD and
# MXN across seven of these storefronts.
#
# Etsy also lets a visitor pick a shopping currency independently of the
# locale, so this is a fallback in the honest sense: a best guess for an
# unlabelled symbol, never an assertion. Only /de/ (EUR) has been confirmed
# against a live page; the rest follow the region and are marked as
# unverified in the README.
LOCALE_CURRENCY: Dict[str, str] = {
    "":        "USD",   # www.etsy.com with no locale segment — the US default
    "uk":      "GBP",
    "de":      "EUR",
    "at":      "EUR",
    "ch":      "CHF",
    "fr":      "EUR",
    "nl":      "EUR",
    "be":      "EUR",
    "ie":      "EUR",
    "it":      "EUR",
    "es":      "EUR",
    "pt":      "EUR",
    "pl":      "PLN",
    "se":      "SEK",
    "ca":      "CAD",
    "ca-fr":   "CAD",
    "au":      "AUD",
    "nz":      "NZD",
    "jp":      "JPY",
    "mx":      "MXN",
    "fi-en":   "EUR",
    "dk-en":   "DKK",
    "no-en":   "NOK",
    "hk-en":   "HKD",
    "il-en":   "ILS",
    "in-en":   "INR",
    "sg-en":   "SGD",
}

# Etsy hosts that are NOT this scraper's target, with the reason. A caller
# pointing at one deserves to be told what is actually wrong rather than
# "not an Etsy site", which is false and sends them looking for a typo.
UNSUPPORTED: Dict[str, str] = {
    "etsy.me": (
        "is Etsy's URL shortener, not a page. Resolve the short link first "
        "and pass the /listing/, /search, /c/ or /shop/ URL it redirects to"
    ),
    "help.etsy.com": (
        "is Etsy's help centre — documentation, not the marketplace. There "
        "are no listings on it"
    ),
    "community.etsy.com": (
        "is the seller forum. No listings, and no product markup to read"
    ),
    "partners.etsy.com": (
        "is the affiliate portal, not the marketplace"
    ),
    "etsystatic.com": (
        "is Etsy's image CDN. Product images are served from it; pages are "
        "not"
    ),
}


def site_host(url: str) -> str:
    """The bare hostname of `url` with a leading "www." removed.

    Returns "" for anything unparseable, so a caller can tell "not one of
    ours" from "etsy.com" without a try/except at every call site.
    """
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def unsupported_reason(url: str) -> Optional[str]:
    """Why this host is refused, when the answer is more than "not ours"."""
    return UNSUPPORTED.get(site_host(url))


def is_supported_host(url: str) -> bool:
    return site_host(url) in HOSTS


def locale_of(url: str) -> str:
    """The locale segment of `url`, or "" for the default US storefront.

    Etsy's locale is the FIRST path segment, and only when it is one of the
    ones the site itself advertises. Guessing from shape would be wrong in
    both directions: `/c/` and `/de/` are the same shape, and `/ca-fr/`
    would not match a naive two-letter test.
    """
    try:
        parts = [p for p in urlparse(url or "").path.split("/") if p]
    except ValueError:
        return ""
    if parts and parts[0] and parts[0] in LOCALE_CURRENCY:
        return parts[0]
    return ""


def host_currency(url: str) -> Optional[str]:
    """The currency this storefront most likely quotes. A FALLBACK only.

    Named `host_currency` rather than `locale_currency` because that is the
    name every engine in this family calls, and keeping it means the engines
    stay identical across repos even though the concept moved from the host
    to the path.
    """
    return LOCALE_CURRENCY.get(locale_of(url))


def path_without_locale(url: str) -> str:
    """`url`'s path with the locale segment removed, always leading with "/".

    So `/de/c/home-and-living/...` and `/c/home-and-living/...` reduce to the
    same thing, and every routing decision below is written once instead of
    once per locale.
    """
    try:
        path = urlparse(url or "").path
    except ValueError:
        return "/"
    loc = locale_of(url)
    if loc:
        path = path[len(loc) + 1:] or "/"
    return path if path.startswith("/") else "/" + path


SELECTORS = {
    # The fallback anchor, and the engines' "has the grid painted?" probe.
    # Anchored on the URL pattern rather than a class, per the family rule —
    # and it matters here because Etsy's own class names are a mix of design
    # tokens and build hashes ("b1d071b88e382...").
    "item_link": 'a[href*="/listing/"]',
    # The tile. Etsy's own data attribute: semantic, present on every page
    # kind, and stable across the two rating markups and both locales
    # captured. `div[...]` and not `[...]` on purpose — the bare attribute
    # selector also matches <video>, the favourite button and the anchor.
    "product_card": "div[data-listing-id]",
    "title": "h3.v2-listing-card__title",
    # The one price container present on search, category AND shop tiles.
    # `p.lc-price` is absent from every shop tile — see the module docstring.
    "price_block": "div.n-listing-card__price",
    "price_value": "span.currency-value",
    "price_symbol": "span.currency-symbol",
    # The was-price. Etsy renders exactly one kind of strikethrough, unlike
    # the sibling repo where telling two apart is the most dangerous thing
    # on the site.
    "strike": "span.wt-text-strikethrough",
    # The promotion line, which restates the original price and the discount
    # percentage as text. Removed before the current price is read.
    "promotion": "p[class*='search-collage-promotion-price']",
    # Ratings, both live forms. See the module docstring.
    "rating_new": "clg-static-review-stars[rating]",
    "rating_old": "[class*='star-rating-']",
    # The shop name, present on search tiles only (63/63 there, 0/64 on
    # category, 0/39 on shop pages). Category and shop rows get the shop
    # name from JSON-LD instead, which covers 61/65 and 36/40.
    "shop_name": "span.clickable-shop-name",
    # Detail page.
    "detail_price": "[data-buy-box-region='price']",
    "detail_title": "h1",
}


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
# The symbol table covers the currencies the locales above quote. A bare "$"
# is the interesting one: it means seven different things across Etsy's
# storefronts, so it is resolved from the locale and left as None when the
# locale is unknown rather than defaulted to USD.
_CURRENCY_SYMBOLS = {
    "€": "EUR", "£": "GBP", "$": "USD", "¥": "JPY", "₪": "ILS", "₹": "INR",
    "zł": "PLN", "kr": "SEK",
}

# Symbols whose meaning depends on which storefront rendered them. Listed
# separately so a match can be DECLINED rather than guessed when the locale
# is unknown. "kr" is here for the same reason as "$": Sweden, Denmark and
# Norway all print it.
_HOST_RESOLVED_SYMBOLS = {"$", "kr"}

# Matched longest-first so a prefix is not swallowed by the bare symbol —
# "CA$" must not read as "$".
_PREFIXED_SYMBOLS = {
    "CA$": "CAD", "A$": "AUD", "NZ$": "NZD", "HK$": "HKD", "SG$": "SGD",
    "MX$": "MXN", "US$": "USD", "CHF": "CHF",
}

# An explicit allowlist, not a bare [A-Z]{3}: the latter matches any three
# capitals next to a number, and Etsy titles are full of them — a size chart
# ("XXL 100"), a paper format, a thread count. Every entry is a real ISO 4217
# code quoted by one of the storefronts above.
_CURRENCY_CODES = frozenset("""
    USD EUR GBP CAD AUD NZD CHF SEK DKK NOK PLN JPY ILS INR HKD SGD MXN
""".split())

# Space characters used as a THOUSANDS separator. A rendered page uses a
# no-break variant so the number does not wrap: plain space, NBSP (U+00A0),
# narrow NBSP (U+202F) and thin space (U+2009) all appear. The French and
# Polish storefronts group with a space, so missing these does not merely
# mis-group a number there — it fails to match the price at all, and
# "1 234 €" comes back as 234.
#
# WRITTEN AS ESCAPES ON PURPOSE. An earlier version of this file held the
# characters themselves and they were silently normalised to four plain
# spaces somewhere between an editor and a `sed`, which left the constant
# looking correct and covering only one of the four. The smoke test now pins
# all four codepoints; escapes make the value survive the tooling as well.
_GROUP_SPACES = "\u0020\u00a0\u202f\u2009"

# Amount, in any of the three grouping conventions:
#   1,234.56 / 1.234,56 / 1 234,56 / 125 / 125.00
# The space-grouped form deliberately requires FULL groups of exactly three
# digits, so a stray "5 200" out of two unrelated numbers cannot merge.
_AMOUNT = (r"\d{1,3}(?:[" + _GROUP_SPACES + r"]\d{3})+(?:[.,]\d{1,2})?"
           r"|[\d.,]+(?:[.,]\d{1,2})?")
_PREFIXED_RE = "|".join(re.escape(s) for s in
                        sorted(_PREFIXED_SYMBOLS, key=len, reverse=True))
_BARE_RE = "|".join(re.escape(s) for s in sorted(
    set(_CURRENCY_SYMBOLS) | _HOST_RESOLVED_SYMBOLS, key=len, reverse=True))
_SPACE = "[" + _GROUP_SPACES + "]?"
_PRICE_RE = re.compile(
    r"(?:(" + _PREFIXED_RE + r"|" + _BARE_RE + r")" + _SPACE + r"(" + _AMOUNT + r")"
    r"|(" + _AMOUNT + r")" + _SPACE + r"(" + _PREFIXED_RE + r"|" + _BARE_RE + r")"
    r"|\b([A-Z]{3})" + _SPACE + r"(" + _AMOUNT + r")"
    r"|\b(" + _AMOUNT + r")" + _SPACE + r"([A-Z]{3})\b)"
)

# A discount badge is not a price, and removing it BEFORE matching is the
# only way to be sure of that. Rejecting the match afterwards is not enough:
# a rejected match has still consumed its text, and where the currency is a
# PREFIX a percent-before-number badge reads as the badge's number wearing
# the next price's symbol.
#
# On Etsy this is not a hypothetical inherited from a sibling repo — every
# discounted tile prints "(40% Rabatt)" / "(40% off)" INSIDE the same
# container the current price is read from.
#
# Both word orders are handled because locales disagree about which side the
# sign goes on: German writes "-16%", Hebrew and Turkish write "-%10".
_PERCENTAGE_RE = re.compile(
    r"[-+−]?\s*(?:%\s*\d[\d.,]*|\d[\d.,]*\s*%)")

# The listing id, recovered from the product URL. Etsy's URL shape is
# `/listing/<id>/<slug>`, and the slug is full of other numbers — sizes,
# volumes, counts ("...-set-aus-2-10-handbemalten-bechern-300-ml") — so this
# anchors on the `/listing/` segment and takes the id that FOLLOWS it, not
# the last number in the path. Verified against the detail page's own `sku`
# field, which agrees (519688604 on both).
_SKU_IN_URL_RE = re.compile(r"/listing/(\d+)(?:[/?#]|$)")

# "ab 34,00 €" / "from $34.00" — a variation listing's from-price. The word
# is localised, so the match is anchored on the START of the price node's
# text and covers the languages of the storefronts above. A from-price whose
# word is not in this set still parses its NUMBER correctly; it merely fails
# to set `price_is_from`, which is why the set errs on the side of more
# words rather than fewer.
_FROM_PREFIX_RE = re.compile(
    r"^\s*(?:ab|from|à partir de|a partir de|desde|vanaf|od|från|fra|"
    r"alkaen|da|price:?\s*from)\b",
    re.IGNORECASE)


def _normalize_amount(raw: str) -> Optional[float]:
    """Parse a price amount written in either decimal convention.

    When BOTH separators appear, 'whichever comes last is the decimal point'
    disambiguates on its own. When only one appears, that is ambiguous
    between a thousands grouping and a decimal point — and no currency this
    parser recognises has a 3-digit subunit. So a single separator followed
    by exactly 3 digits is a thousands grouping; anything else is a decimal.
    """
    if raw is None:
        return None
    for space in _GROUP_SPACES:
        raw = raw.replace(space, "")

    last_dot, last_comma = raw.rfind("."), raw.rfind(",")
    if last_dot != -1 and last_comma != -1:
        norm = (raw.replace(",", "") if last_dot > last_comma
                else raw.replace(".", "").replace(",", "."))
    else:
        sep_pos = max(last_dot, last_comma)
        trailing = raw[sep_pos + 1:] if sep_pos != -1 else ""
        if len(trailing) == 3 and trailing.isdigit():
            norm = raw.replace(".", "").replace(",", "")
        else:
            norm = raw.replace(",", ".")
    try:
        return float(norm)
    except ValueError:
        return None


def _prices_in(text: str, host_cur: Optional[str] = None
               ) -> Tuple[List[float], Optional[str]]:
    """Return ([amounts], currency_code_or_None) for all prices in `text`.

    `host_cur` resolves a symbol that cannot name itself using the storefront
    whose page rendered it. That is still a guess, but a much better one: if
    the page printed a local symbol at all, the visitor is being served that
    market's own currency. When the locale is unknown, such a symbol yields a
    price with currency None rather than a plausible wrong code.
    """
    amounts, currency = [], None
    prepared = _PERCENTAGE_RE.sub(" ", text or "")
    for m in _PRICE_RE.finditer(prepared):
        sym = m.group(1) or m.group(4)
        code = m.group(5) or m.group(8)
        if code and code not in _CURRENCY_CODES:
            # Three capitals next to a number that are not a real currency —
            # a size, a spec, a model name. Not a price.
            continue
        raw = m.group(2) or m.group(3) or m.group(6) or m.group(7)
        if currency is None:
            if code:
                currency = code
            elif sym in _PREFIXED_SYMBOLS:
                currency = _PREFIXED_SYMBOLS[sym]
            elif sym in _HOST_RESOLVED_SYMBOLS:
                currency = host_cur
            else:
                currency = _CURRENCY_SYMBOLS.get(sym)
        amount = _normalize_amount(raw)
        if amount is not None:
            amounts.append(amount)
    return amounts, currency


def _first_price(node, host_cur: Optional[str] = None
                 ) -> Tuple[Optional[float], Optional[str]]:
    """(amount, currency) from a price node's text, or (None, None)."""
    if node is None:
        return None, None
    amounts, currency = _prices_in(node.get_text(" ", strip=True), host_cur)
    return (amounts[0] if amounts else None), currency


def _price_from_parts(node, host_cur: Optional[str] = None
                      ) -> Tuple[Optional[float], Optional[str]]:
    """Read a price out of Etsy's own `currency-value`/`currency-symbol` pair.

    Preferred over `_first_price` wherever the pair exists, because it does
    not depend on the surrounding text at all: no percentage badge, no
    "Sale-Preis" label and no second price can reach it. Falls back to the
    text pattern when the pair is absent (older markup, and the detail page's
    buy box).
    """
    if node is None:
        return None, None
    value = node.select_one(SELECTORS["price_value"])
    if value is None:
        return _first_price(node, host_cur)
    amount = _normalize_amount(value.get_text(strip=True))
    symbol = node.select_one(SELECTORS["price_symbol"])
    currency = None
    if symbol is not None:
        sym = symbol.get_text(strip=True)
        if sym in _PREFIXED_SYMBOLS:
            currency = _PREFIXED_SYMBOLS[sym]
        elif sym in _HOST_RESOLVED_SYMBOLS:
            currency = host_cur
        else:
            currency = _CURRENCY_SYMBOLS.get(sym)
    return amount, currency


# ---------------------------------------------------------------------------
# Page state: what came back, and what to do about it
# ---------------------------------------------------------------------------
# Etsy answers a request in four ways and three of them are not content.
#
#   blocked    HTTP 403 behind a DataDome page whose own JS object says the
#              address or the browser is banned outright (`t=bv`). There is
#              nothing on it to solve. -> rotate, or retry the same Scraping
#              Browser profile, which is what actually cleared it (below).
#   captcha    the same 403 shell, but the challenge iframe has moved to
#              `t=fe` — a real, solvable slider. This is the paid path.
#   empty      HTTP 200, a real page, no listings on it — an exhausted
#              listing, or a hub category. -> EXIT_NO_PRODUCTS, not
#              EXIT_BLOCKED.
#   content    tiles, or a detail page.
#
# THE `t` PARAMETER IS THE WHOLE DECISION, and it is why a captcha state and
# a blocked state are told apart here rather than collapsed. 2Captcha's own
# DataDome documentation: "The value of t must be equal to fe. If t=bv, it
# means that your ip is banned by the captcha and you need to change the ip
# address." A solve bought for a `t=bv` page returns a cookie DataDome will
# not accept — so reporting that page as `captcha` would spend the user's
# money to learn nothing.
#
# Measured 2026-09-09, all against live pages:
#   * plain `curl` from a hosting ASN                -> rt=c, t=bv
#   * Playwright Chromium via a residential US exit  -> rt=c, t=bv
#   * real Chrome via the SAME exit                  -> rt=i, then t=fe (~6s)
#   * Scraping Browser API, fresh profile            -> HTTP 200, no DataDome
#
# The middle two matter: the same address is banned outright for an automated
# browser and merely challenged for a real one, so `t=bv` is not purely a
# property of the exit IP.
_DATADOME_HOST = "captcha-delivery.com"
_DD_T_RE = re.compile(r"['\"]?\bt['\"]?\s*[:=]\s*['\"](\w+)['\"]")

# Etsy's own asset host. Every page Etsy actually serves is built out of it —
# hundreds of references on each live capture — and the DataDome shell
# references it zero times. A structural signal, unlike any text marker, and
# it is what catches a block page in a locale whose wording has never been
# captured.
_CDN_MARKER = "etsystatic.com"

# Kept broad on purpose: which vendor appears depends on the exit country and
# on what the address has been doing. DataDome is the one actually observed
# on Etsy; the rest cost nothing to keep and are the family's standing policy.
BOT_CHALLENGE_MARKERS = {
    "datadome": ("captcha-delivery.com", "geo.captcha-delivery.com"),
    "recaptcha": ("www.google.com/recaptcha", "grecaptcha", "g-recaptcha"),
    "hcaptcha": ("hcaptcha.com", "h-captcha"),
    "turnstile": ("challenges.cloudflare.com", "cf-turnstile"),
    "akamai": ("_abck", "ak_bmsc", "AkamaiGHost"),
}

# Presence of tiles, checked as raw text rather than by parsing:
# `detect_page_state` runs on every fetch, including ones that turn out to be
# a 2.4 MB page, and building a soup just to answer "is this content?" is
# wasted work.
_CARD_MARKER = "data-listing-id"


# Anything a browser EXTENSION injected is not the site talking, and has to
# come out before the markers above are looked for.
#
# This guard is LOAD-BEARING in this repo, unlike in farfetch-scraper where
# the same code would be dead. The 2Captcha Scraping Browser API — the access
# path that actually works on Etsy — injects SIXTEEN script tags into every
# page it loads, among them hunters for recaptcha and turnstile:
#
#     <script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/
#                  content/captcha/turnstile/hunter.js"
#             data-ts-input="cf-turnstile-response"></script>
#
# Both vendors are in the marker set above, so without this strip every
# successful 1.3 MB page fetched over `--cdp-endpoint` would be reported as a
# challenge and the run would exit 3 while holding the full catalogue.
_EXTENSION_TAG_RE = re.compile(
    r"<script[^>]*\b(?:chrome|moz)-extension://[^>]*>(?:.*?</script>)?",
    re.IGNORECASE | re.DOTALL)


def _strip_extension_tags(html: str) -> str:
    return _EXTENSION_TAG_RE.sub("", html or "")


def detect_bot_challenge(html: str, url: Optional[str] = None) -> Optional[str]:
    """Name the challenge vendor the SITE put on the page, or None.

    Broad by design about vendors, narrow about provenance: a caller decides
    what to DO about a challenge, but a marker injected by the local browser
    is not one to begin with. See _EXTENSION_TAG_RE.
    """
    if not html:
        return None
    cleaned = _strip_extension_tags(html)
    for vendor, markers in BOT_CHALLENGE_MARKERS.items():
        if any(m in cleaned for m in markers):
            return vendor
    return None


# The response TYPE DataDome states in its own JS object, independently of
# `t`. `rt:'c'` is a decided answer ("captcha"), `rt:'i'` an INTERSTITIAL —
# a device check that has not finished making up its mind.
_DD_RT_RE = re.compile(r"['\"]?\brt['\"]?\s*[:=]\s*['\"](\w+)['\"]")


# THE TOP-LEVEL `dd` OBJECT NEVER UPDATES, AND THE CHALLENGE MOVES WITHOUT IT.
#
# Measured 2026-09-09 over 120 seconds on one live page:
#
#     +3.1s   dd says rt='i'   no challenge iframe yet
#     +5.1s   dd says rt='i'   iframe at geo.captcha-delivery.com/interstitial/
#     +7.1s   dd says rt='i'   iframe at geo.captcha-delivery.com/captcha/?t=fe
#     ...
#     +120s   dd STILL says rt='i'
#
# The device check hands off to a solvable slider by NAVIGATING ITS IFRAME,
# and neither the `dd` object nor the iframe's `src` ATTRIBUTE in the
# top-level document changes when it does. So a detector that reads only the
# HTML sees `rt='i'` forever and can never find the `t=fe` it is waiting for.
#
# That is not a theoretical gap: six live attempts through this repo's own
# code reported "interstitial, do not pay" on four pages that had a solvable
# challenge sitting in their iframe, and the paid path was unreachable.
#
# So the LIVE FRAME URLS are the primary evidence where a caller can supply
# them, and the HTML is the fallback for callers that cannot — a `--dump-html`
# file read back has no frames at all.
_DATADOME_FRAME_T_RE = re.compile(r"[?&]t=(\w+)")


def datadome_frame_verdict(frame_urls) -> Optional[str]:
    """The `t` of the DataDome challenge frame, or None if there is none.

    Prefers a solvable frame when several are present: a page can hold the
    interstitial and the slider at once during the hand-off, and the
    solvable one is the answer that matters.
    """
    verdicts = []
    for url in frame_urls or ():
        if not url or _DATADOME_HOST not in url:
            continue
        m = _DATADOME_FRAME_T_RE.search(url)
        verdicts.append(m.group(1) if m else
                        ("i" if "/interstitial/" in url else ""))
    if not verdicts:
        return None
    for preferred in ("fe", "bv"):
        if preferred in verdicts:
            return preferred
    return verdicts[0]


def datadome_verdict(html: str, frame_urls=None) -> Optional[str]:
    """"bv", "fe", "i" (interstitial), "" (DataDome, unstated) or None.

    Separate from `detect_page_state` because the engines need the raw answer
    too: a `t=fe` page is worth handing to the solver, a `t=bv` page is not,
    and an "i" page has not decided yet — that one wants WAITING, which is
    the engines' business. The policy that maps these onto run behaviour
    lives in `page_flow.STATE_POLICY`.

    The interstitial is not a theoretical state. Every live block seen on
    Etsy through the Scraping Browser arrived as one:

        var dd={'rt':'i','cid':…,'b':1301560,…}
        <iframe src="https://geo.captcha-delivery.com/interstitial/?…">

    with NO `t` at all, and its iframe moved to
    `/captcha/?…&t=fe` about six seconds later. Reading the markup
    immediately after `domcontentloaded` — 1.6 s in on the run that found
    this — sees only the interstitial, so a parser that decides there
    reports "blocked" on a page that was about to become solvable, or about
    to clear itself. See `page_flow.settle_datadome`.
    """
    # Frames first, and they WIN. See the note above: the top-level object
    # keeps saying rt='i' while the iframe has already moved to t=fe, so
    # trusting the HTML here is what made the paid path unreachable.
    from_frames = datadome_frame_verdict(frame_urls)
    if from_frames:
        return from_frames

    if not html or _DATADOME_HOST not in _strip_extension_tags(html):
        # No frames said anything and the markup has no DataDome in it. If
        # frames were supplied and held nothing, that is a real "not a
        # DataDome page" rather than a missing observation.
        return None
    m = _DD_T_RE.search(html)
    if m:
        return m.group(1)
    rt = _DD_RT_RE.search(html)
    if rt and rt.group(1) == "i":
        return "i"
    if "/interstitial/" in html:
        return "i"
    return ""


def is_datadome_interstitial(html: str, frame_urls=None) -> bool:
    """Whether the page is a DataDome check that has not resolved yet.

    `frame_urls` matters here more than anywhere: without it, a page whose
    iframe has already reached the solvable slider still reads as an
    unresolved interstitial, and a caller waiting for it to settle waits
    forever.
    """
    return datadome_verdict(html, frame_urls) == "i"


def datadome_captcha_url(html: str, frame_urls=None) -> Optional[str]:
    """The challenge iframe's src, for `DataDomeSliderTask`'s `captchaUrl`.

    2Captcha wants the iframe URL as the page carries it — it holds the
    `cid`, `hash` and `e` values the solve is scoped to. Returned
    HTML-unescaped, because the served markup writes its separators as
    `&amp;` and the API needs the real URL.
    """
    # A live frame URL beats the markup for the same reason the verdict does:
    # the iframe navigates and its `src` attribute does not follow. Prefer a
    # solvable frame, then any DataDome frame, then the attribute.
    solvable = [u for u in (frame_urls or ())
                if u and _DATADOME_HOST in u and "t=fe" in u]
    if solvable:
        return solvable[0]
    any_frame = [u for u in (frame_urls or ())
                 if u and _DATADOME_HOST in u and "/captcha/" in u]
    if any_frame:
        return any_frame[0]

    if not html:
        return None
    m = re.search(r'<iframe[^>]+src="([^"]*captcha-delivery\.com[^"]*)"',
                  html, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).replace("&amp;", "&")


def detect_page_state(html: str, status: Optional[int] = None,
                      url: Optional[str] = None, frame_urls=None) -> str:
    """One of "blocked", "captcha", "empty", "content".

    `status` is optional because not every engine path can see it — a page
    fetched over CDP reports one, a page read back out of a `--dump-html`
    file does not.
    """
    if not html:
        return "blocked" if status and status >= 400 else "empty"

    verdict = datadome_verdict(html, frame_urls)
    if verdict is not None:
        # A DataDome shell is never content: it is ~1.5 KB with no tiles on
        # it. Which state it is depends on `t`.
        #
        # An unresolved interstitial ("i") maps to "blocked" here, which is
        # the right answer for a caller that has only this one snapshot to go
        # on — but it is the WRONG place to stop. An engine should wait it
        # out first (`page_flow.settle_datadome`) and classify what it turns
        # into; several of them turn into a served page, and others into a
        # `t=fe` challenge worth solving.
        return "captcha" if verdict == "fe" else "blocked"

    if status == 403:
        return "blocked"

    if _CARD_MARKER in html or "/listing/" in html:
        # Content wins over a challenge marker that is merely PRESENT: a
        # rendered grid with a captcha script somewhere on it is a page we
        # can read, and reporting it as a challenge would spend a solve on a
        # page that needs none.
        return "content"

    if detect_bot_challenge(html, url):
        return "captcha"

    if _CDN_MARKER not in html:
        # Not content, not a challenge, and not built out of Etsy's own
        # assets — so it is not a page Etsy served us. This is the signal
        # that survives a locale whose block wording has never been seen.
        return "blocked"

    return "empty"


# ---------------------------------------------------------------------------
# URLs: which kind of page, which page number, which category
# ---------------------------------------------------------------------------
def listing_kind(url: str) -> str:
    """"search", "category", "shop", "market", "listing" or "other".

    Etsy has FOUR page kinds that carry a grid of listings, and they differ
    in ways that matter: `/search` and `/market/<term>` are query-driven,
    `/c/<taxonomy>` is a category tree, `/shop/<name>` is one seller's
    catalogue. All four render the same tile, which is why one parser reads
    them all — but only `/search` publishes its own page count, so the
    engines still have to tell them apart.
    """
    path = path_without_locale(url).rstrip("/")
    if re.match(r"^/listing/\d+", path):
        return "listing"
    if path.startswith("/search"):
        return "search"
    if path.startswith("/c/"):
        return "category"
    if path.startswith("/shop/"):
        return "shop"
    if path.startswith("/market/"):
        return "market"
    return "other"


# Etsy paginates every listing kind with `?page=N`. Verified on the served
# markup of all four kinds: the site's own next-link is
# `...?ref=pagination&page=2`, which `page_url()` reproduces exactly once
# `ref` is stripped as the tracking parameter it is.
PAGE_PARAM = "page"


def page_url(url: str, page_num: int) -> str:
    """`url` addressed at page `page_num`, preserving its other parameters.

    Replaces an existing `page` rather than appending a second one, which is
    what makes a plan of page URLs safe to build from a URL the user pasted
    out of the address bar mid-listing.
    """
    parts = urlparse(url)
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
              if k != PAGE_PARAM]
    if page_num > 1:
        params.append((PAGE_PARAM, str(page_num)))
    return urlunparse(parts._replace(query=urlencode(params)))


def page_number_from_url(url: str) -> Optional[int]:
    """The `page` parameter of `url` as an int, or None."""
    try:
        for k, v in parse_qsl(urlparse(url or "").query):
            if k == PAGE_PARAM and v.isdigit():
                return int(v)
    except ValueError:
        return None
    return None


# Path segments that are NOT a category name, so `category_from_url` reports
# None instead of inventing one out of Etsy's routing.
_NOT_A_CATEGORY = {"search", "listing", "shop", "market", "c", "featured",
                   "deals", "gift-guides", "your", "cart", "favorites"}


def category_from_url(url: str) -> Optional[str]:
    """A human-readable category for the row, or None.

    Three sources, because Etsy's four listing kinds carry the idea in three
    different places:

      * `/c/home-and-living/kitchen-and-dining/.../mugs` -> "mugs", the leaf
        of the taxonomy path.
      * `/search?q=handmade+mug` -> "handmade mug", the query. Not a
        category in Etsy's own taxonomy, and recorded anyway because it is
        what the rows have in common and a consumer diffing two runs needs
        to know which listing produced them.
      * `/market/handmade_mug` -> "handmade mug".

    A shop front returns None: a seller's catalogue spans categories, so
    naming one would be false.
    """
    kind = listing_kind(url)
    if kind == "search":
        try:
            for k, v in parse_qsl(urlparse(url).query):
                if k == "q" and v.strip():
                    return unquote(v).replace("+", " ").strip()
        except ValueError:
            return None
        return None
    if kind == "market":
        seg = path_without_locale(url).rstrip("/").split("/")[-1]
        return unquote(seg).replace("_", " ").replace("-", " ").strip() or None
    if kind == "category":
        segs = [s for s in path_without_locale(url).strip("/").split("/") if s]
        segs = [s for s in segs if s not in _NOT_A_CATEGORY]
        if segs:
            return unquote(segs[-1]).replace("-", " ").strip() or None
    return None


def shop_from_url(url: str) -> Optional[str]:
    """The shop name out of a `/shop/<name>` URL, or None."""
    path = path_without_locale(url).strip("/")
    m = re.match(r"^shop/([^/?#]+)", path)
    return unquote(m.group(1)) if m else None


def sku_from_url(url: Optional[str]) -> Optional[str]:
    """Etsy's listing id, out of a `/listing/<id>/<slug>` URL."""
    if not url:
        return None
    m = _SKU_IN_URL_RE.search(url)
    return m.group(1) if m else None


# Parameters Etsy hangs on its own links that do not select content. Kept
# here rather than in page_flow.py because `_absolute_url` needs them too,
# and one list beats two that can disagree.
#
# `ls` is in here even though `_tile_is_ad` READS it: the ad flag is captured
# into its own column before the URL is cleaned, and leaving a
# per-impression parameter in the stored URL would make two runs of the same
# listing look like different rows to anything comparing URLs.
TRACKING_PARAMS = {
    "click_key", "click_sum", "ga_order", "ga_search_type", "ga_view_type",
    "ga_search_query", "ref", "sr_prefetch", "pf_from", "frs", "cns", "sts",
    "local_signal_search", "content_source", "ls", "organic_search_click",
    "bes", "sr_gallery_page", "utm_source", "utm_medium", "utm_campaign",
    "utm_term", "utm_content", "gclid", "dd_referrer",
}


def _absolute_url(base_url: str, href: str) -> str:
    """`href` resolved against `base_url`, with tracking parameters dropped.

    Etsy hangs a dozen click-attribution parameters on every tile link
    (`click_key`, `click_sum`, `ga_order`, `sr_prefetch`, `ls`, …). They are
    per-impression, so leaving them in makes two runs of the same listing
    look like different rows to anything comparing URLs, and they are the
    single biggest contributor to output size.
    """
    absolute = urljoin(base_url or "https://www.etsy.com/", href or "")
    parts = urlparse(absolute)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in TRACKING_PARAMS]
    return urlunparse(parts._replace(query=urlencode(kept), fragment=""))


# ---------------------------------------------------------------------------
# Small readers
# ---------------------------------------------------------------------------
# Zero-width characters Etsy sprinkles into titles for layout. Left in, they
# break both number parsing and any downstream string match.
_INVISIBLE_RE = re.compile("[​‌‍﻿⁠]")


def _clean_text(value):
    """Collapsed whitespace with zero-width characters removed, or None."""
    if value is None:
        return None
    text = value if isinstance(value, str) else value.get_text(" ", strip=True)
    text = _INVISIBLE_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


# Values Etsy writes where a field has no value. A literal "n/a" reached the
# `gtin` column on two of the captured listings, and a placeholder in a data
# set is worse than a null: it is a string that compares equal across
# unrelated products and survives every "is it populated?" check.
_PLACEHOLDER_VALUES = {"n/a", "na", "none", "null", "-", "--", "unknown",
                       "not applicable", "keine angabe"}


def _clean_value(value) -> Optional[str]:
    """`_clean_text`, with Etsy's own placeholders reduced to None."""
    text = _clean_text(value)
    if text is None or text.strip().lower() in _PLACEHOLDER_VALUES:
        return None
    return text


def _to_float(text) -> Optional[float]:
    if text is None:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", str(text))
    return _normalize_amount(m.group(0)) if m else None


def _int_from(text) -> Optional[int]:
    """The first integer in `text`, thousands separators included.

    Reads the number BEFORE any word, deliberately: a sibling repo shipped a
    `review_count` built by stripping every digit out of
    "4.4 out of 5 stars, 279,961 ratings" and put 445279961 on every row of
    every run while its coverage check reported 100%.
    """
    if text is None:
        return None
    m = re.search(r"\d[\d.,\u0020\u00a0\u202f\u2009]*", str(text))
    if not m:
        return None
    digits = re.sub(r"[^\d]", "", m.group(0))
    return int(digits) if digits else None


# The older rating markup states the value in its CLASS, not its label:
# `star-rating-5`, `star-rating-4-5`, `star-rating-3-5`. The class is
# locale-independent; the aria-label beside it is written in the page's own
# language ("5 von 5 Sternen"), so the class is what this reads.
_RATING_CLASS_RE = re.compile(r"star-rating-(\d)(?:-(\d))?\b")


def _shop_rating_from(tile) -> Tuple[Optional[float], Optional[int]]:
    """(shop_rating, shop_review_count) from a tile, whichever markup it uses.

    THE STARS ON A TILE ARE THE SELLER'S, NOT THE LISTING'S, and that is
    measured rather than assumed: on every page captured, every shop with
    more than one listing on it printed the SAME rating and the same count on
    all of them — 12 shops across a search page and a category page, no
    exceptions. Meanwhile two listings of one shop report 825 and 375 reviews
    on their own detail pages, so the per-listing figure is a different
    number that only a detail page states.

    Reading these into `rating`/`review_count` would therefore make those
    columns mean the shop on a listing run and the listing on a product run,
    with nothing in the data to say which. They get their own columns.

    Both live markup forms are read; see the module docstring for why there
    are two. Returns (None, None) on a tile with no stars, which is a fact —
    a new shop has no reviews — and not a parser failure.
    """
    new = tile.select_one(SELECTORS["rating_new"])
    if new is not None:
        return _to_float(new.get("rating")), _int_from(new.get("review-count-text"))

    old = tile.select_one(SELECTORS["rating_old"])
    if old is not None:
        classes = " ".join(old.get("class") or [])
        m = _RATING_CLASS_RE.search(classes)
        rating = None
        if m:
            rating = float(m.group(1)) + (0.5 if m.group(2) else 0.0)
        return rating, _count_beside_stars(old)

    return None, None


# How far to climb from the stars looking for their "(7)". Three levels is
# enough on the captured markup — the count sits two above — and stopping
# there is what keeps the walk out of `v2-listing-card__info`, which holds
# the title.
_RATING_COUNT_CLIMB = 3

# The count node holds essentially nothing but the count. Anything longer is
# a container that has swallowed the title, and a title like
# "Set aus 2-10 handbemalten Bechern (300 ml)" would then report 300 reviews
# — a plausible number that is entirely wrong, which is this codebase's worst
# failure mode.
_RATING_COUNT_MAX_NOISE = 30
_PAREN_COUNT_RE = re.compile(r"\((\d[\d.,\u00a0\u202f\u2009 ]*)\)")


def _count_beside_stars(stars) -> Optional[int]:
    """The review count printed beside the older sprite-star markup.

    The number is NOT inside the stars node, and NOT in a node whose class
    names it — on the captured category page it sits two levels up, in a
    `span.wt-display-flex-xs` whose classes are pure layout. So this climbs,
    but only as far as a node that still contains nothing except the count.
    """
    node = stars
    for _ in range(_RATING_COUNT_CLIMB):
        node = node.parent
        if node is None:
            return None
        text = node.get_text(" ", strip=True)
        m = _PAREN_COUNT_RE.search(text)
        if not m:
            continue
        if len(_PAREN_COUNT_RE.sub("", text).strip()) > _RATING_COUNT_MAX_NOISE:
            # This ancestor holds the title as well, so its parenthesised
            # number may be part of it. Stop rather than guess.
            return None
        return _int_from(m.group(1))
    return None


def _discount_from(price: Optional[float], original_price: Optional[float]
                   ) -> Optional[float]:
    """The discount as a percentage, or None when the two are not a discount.

    Returns None rather than 0 or a negative number when the "original" is at
    or below the price: that combination means the two figures are not what
    they were taken for, and a negative discount in a data set reads as a
    price rise rather than as a parsing problem.
    """
    if price is None or original_price is None:
        return None
    if original_price <= price:
        return None
    return round((original_price - price) / original_price * 100, 2)


# The site's own statement of how many pages a listing has. Etsy embeds it as
# JSON in the search page's own markup — `"initial_total_pages":20` — which
# is a stronger terminating signal than any selector: a missing next-link is
# a property of markup, an exhausted listing is a property of the catalogue.
# Present on `/search` only; absent on category and shop fronts.
_TOTAL_PAGES_RE = re.compile(r'"initial_total_pages"\s*:\s*(\d+)')
_TOTAL_RESULTS_RE = re.compile(r'"numberOfItems"\s*:\s*(\d+)')


def total_pages(html: str) -> Optional[int]:
    """How many pages this listing has, per the site's own embedded config."""
    if not html:
        return None
    m = _TOTAL_PAGES_RE.search(html)
    return int(m.group(1)) if m else None


def total_results(html: str, shown: Optional[int] = None) -> Optional[int]:
    """How many listings the whole result set holds, per the site.

    Read from the JSON-LD `numberOfItems` rather than the rendered heading,
    which is localised and rounded ("mehr als 100.000").
    """
    if not html:
        return None
    m = _TOTAL_RESULTS_RE.search(html)
    if not m:
        return None
    value = int(m.group(1))
    if shown is not None and value < shown:
        # The set cannot be smaller than what was rendered out of it. Trust
        # the count we can see over the one we were told.
        return None
    return value


# ---------------------------------------------------------------------------
# JSON-LD
# ---------------------------------------------------------------------------
def _ld_blocks(soup) -> List[dict]:
    """Every parseable `application/ld+json` object on the page, flattened.

    A block may hold a single object, a list of them, or an `@graph`. All
    three are legal, so they are flattened here rather than at each call
    site — products living under `@graph` instead of `itemListElement` is the
    shape that returns "zero products, silently".
    """
    blocks: List[dict] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            logger.debug("unparseable JSON-LD block skipped (%d bytes)", len(raw))
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                blocks.extend(g for g in graph if isinstance(g, dict))
            else:
                blocks.append(item)
    return blocks


def _ld_image(value) -> Optional[str]:
    """The first image URL out of any of the shapes schema.org allows.

    Etsy uses the one shape the family's other repos do NOT: a list of
    `ImageObject`, with the URL under **`contentURL`** — capital URL. A
    reader that knows only `url` and `contentUrl` returns None on every Etsy
    row while the images are plainly there, so all four spellings are tried.
    """
    if not value:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        for key in ("contentURL", "contentUrl", "url", "thumbnail"):
            got = value.get(key)
            if isinstance(got, str) and got:
                return got
        return None
    if isinstance(value, list):
        for item in value:
            got = _ld_image(item)
            if got:
                return got
    return None


def _ld_images(value) -> List[str]:
    """Every image URL in `value`, in order, de-duplicated."""
    out: List[str] = []
    items = value if isinstance(value, list) else [value]
    for item in items:
        got = _ld_image(item)
        if got and got not in out:
            out.append(got)
    return out


def _ld_offer(node: dict) -> dict:
    """The offer dict out of a product node, whatever shape it is in.

    Four legal shapes, and three of them break a naive `.get("offers", {})`:

      * `"offers": null` — an EXPLICIT null. A `.get()` default only applies
        to a MISSING key, so this raises AttributeError downstream.
      * a LIST of offers, possibly holding non-dicts.
      * an `AggregateOffer`, which has `lowPrice`/`highPrice` and NO `price`.
      * the plain `Offer`.

    The AggregateOffer is not an edge case here: it is what every variation
    listing publishes, and it is the structured face of the "ab 34,00 €"
    from-price on the tiles.
    """
    offers = node.get("offers")
    if isinstance(offers, list):
        offers = next((o for o in offers if isinstance(o, dict)), None)
    return offers if isinstance(offers, dict) else {}


def _ld_list_price(offer: dict) -> Optional[float]:
    """The offer's LIST price, from a nested `priceSpecification`.

    A fifth offer shape, and the one that matters most for agreeing with the
    page. Five listings on the live category capture publish BOTH:

        "offers": {"price": "11.92", "priceCurrency": "EUR",
                   "priceSpecification": {"@type": "UnitPriceSpecification",
                                          "priceType": ".../ListPrice",
                                          "price": "14.90"}}

    and the tile prints **14,90 €** — the ListPrice — with no sale label and
    no strikethrough on it at all. The ratios are exact (11.92/14.90 = 0.80,
    11.04/12.99 = 0.85), so `offers.price` is a discounted figure Etsy
    publishes without showing it on the tile.

    But it is NOT simply "the displayed price", and reading it as one was
    measured and rejected: on a tile with a visible sale the roles reverse.
    Listing 4540857923 renders

        "Sale-Preis 35,87 € ... 59,79 € Ursprünglicher Preis 59,79 € (40% Rabatt)"

    and publishes `offers.price` 35.87 with ListPrice 59.79 — so there the
    ListPrice is the STRUCK-THROUGH price and `offers.price` is what the
    customer pays. Treating ListPrice as the displayed price cut the
    confirmed-price share from 56/64 to 40/64 on the category capture and
    raised 24 spurious disagreements.

    So: `offers.price` is the current price and is what `price` is confirmed
    against; the ListPrice is the was-price and is used to cross-check
    `original_price` — and only on a tile that HAS a strike, because on the
    five listings above there is no discount on the page at all and reading
    59.79-style ListPrice into `original_price` there would invent one.
    """
    spec = offer.get("priceSpecification")
    if isinstance(spec, list):
        spec = next((x for x in spec if isinstance(x, dict)), None)
    if not isinstance(spec, dict):
        return None
    price_type = spec.get("priceType") or ""
    if isinstance(price_type, str) and "listprice" not in price_type.lower():
        return None
    return _to_float(spec.get("price"))


def _ld_offer_prices(offer: dict
                     ) -> Tuple[Optional[float], Optional[float], bool]:
    """(price, price_max, is_from) out of an offer node.

    An `AggregateOffer`'s `lowPrice` becomes the row's price and `highPrice`
    its `price_max`, with `is_from` set — because "from 145,15 €" is what the
    page itself says, and a single number would claim more than Etsy does.

    A nested ListPrice is deliberately NOT consulted here — it is the
    was-price, not this one. See `_ld_list_price`.
    """
    if not offer:
        return None, None, False
    price = _to_float(offer.get("price"))
    if price is not None:
        return price, None, False
    low = _to_float(offer.get("lowPrice"))
    high = _to_float(offer.get("highPrice"))
    if low is not None:
        return low, high, True
    return None, None, False


def _ld_rating(node: dict) -> Tuple[Optional[float], Optional[int]]:
    """(rating, review_count) from `aggregateRating`, tolerating a null."""
    agg = node.get("aggregateRating")
    if not isinstance(agg, dict):
        return None, None
    return _to_float(agg.get("ratingValue")), _int_from(agg.get("reviewCount"))


def _ld_in_stock(offer: dict) -> Optional[bool]:
    """True/False from `availability`, or None when it says nothing.

    None and not False for a missing value: "we were not told" and "out of
    stock" are different facts, and only one of them is Etsy's.
    """
    availability = offer.get("availability")
    if not isinstance(availability, str) or not availability:
        return None
    tail = availability.rstrip("/").rsplit("/", 1)[-1].lower()
    if tail in ("instock", "limitedavailability", "onlineonly", "preorder",
                "presale", "backorder"):
        return True
    if tail in ("outofstock", "soldout", "discontinued"):
        return False
    return None


def _ld_shipping_node(offer: dict) -> dict:
    """The offer's `shippingDetails`, tolerating a list or a null."""
    details = offer.get("shippingDetails")
    if isinstance(details, list):
        details = next((d for d in details if isinstance(d, dict)), None)
    return details if isinstance(details, dict) else {}


def _ld_free_shipping(offer: dict) -> Optional[bool]:
    """Whether shipping is free, from the offer's own shipping RATE.

    A FACT rather than a badge: the detail page publishes
    `offers.shippingDetails.shippingRate.value == "0"`, which is
    locale-independent. Listing tiles carry only a localised badge
    ("Kostenloser Versand" / "FREE shipping") and their JSON-LD has no
    shipping node at all, so this is null on listing rows — see the note in
    `Product`.
    """
    rate = _ld_shipping_node(offer).get("shippingRate")
    if isinstance(rate, list):
        rate = next((r for r in rate if isinstance(r, dict)), None)
    if not isinstance(rate, dict):
        return None
    value = _to_float(rate.get("value"))
    return None if value is None else value == 0


def _ld_ships_from(offer: dict) -> Optional[str]:
    """The ISO country the item ships from, per `shippingOrigin`."""
    origin = _ld_shipping_node(offer).get("shippingOrigin")
    if isinstance(origin, list):
        origin = next((o for o in origin if isinstance(o, dict)), None)
    if not isinstance(origin, dict):
        return None
    country = origin.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name") or country.get("identifier")
    return _clean_text(country) if isinstance(country, str) else None


def _ld_brand(node: dict) -> Optional[str]:
    """The shop name out of `brand`, which Etsy fills with the SELLER.

    On a marketplace of makers the shop IS the brand, and Etsy's own
    structured data says so — `brand.name` is "StonehousePotteryOH" on that
    seller's listings. So the family's `brand` column carries the shop name
    here rather than being null on every row, and `shop_id` beside it carries
    the numeric id that a name cannot be joined on.
    """
    brand = node.get("brand")
    if isinstance(brand, dict):
        return _clean_text(brand.get("name"))
    if isinstance(brand, str):
        return _clean_text(brand)
    return None


def _products_in_ld(blocks) -> List[dict]:
    """Every product node in `blocks`, from either page shape.

    Two shapes, both live on this site:

      * a listing page's `ItemList`, whose `itemListElement` holds
        `ListItem`s wrapping the product in `item`.
      * a detail page's bare `Product`.
    """
    products: List[dict] = []
    for block in blocks:
        btype = block.get("@type")
        types = btype if isinstance(btype, list) else [btype]
        if "Product" in types:
            products.append(block)
            continue
        if "ItemList" in types:
            for entry in block.get("itemListElement") or []:
                if not isinstance(entry, dict):
                    continue
                item = entry.get("item")
                if isinstance(item, dict):
                    products.append(item)
                elif entry.get("@type") == "Product":
                    products.append(entry)
    return products


def _ld_by_sku(blocks) -> Dict[str, dict]:
    """Product nodes indexed by listing id, for joining onto DOM tiles.

    The id comes from the node's own `sku` where it has one and from its URL
    otherwise — a listing page's `ItemList` entries carry no `sku` field at
    all, only a `url`, so without the URL fallback the join would find
    nothing on exactly the page kind that needs it most.
    """
    index: Dict[str, dict] = {}
    for node in _products_in_ld(blocks):
        sku = _clean_text(node.get("sku")) or sku_from_url(node.get("url"))
        if sku:
            index.setdefault(sku, node)
    return index


# ---------------------------------------------------------------------------
# Tiles
# ---------------------------------------------------------------------------
# How far to walk up from a product link when looking for its tile. Capped so
# a malformed document cannot walk to <body> and read the whole page as one
# product.
_MAX_TILE_WIDEN = 8


def _distinct_skus(node) -> int:
    """How many DISTINCT listings `node` covers.

    Counting distinct ids and not links is what lets the widening walk pass a
    tile that links to its own product several times (image, title,
    favourite button) and still stop at the edge of the next tile. Stopping
    one level too late is the worse failure: every tile would then report its
    neighbour's price, and a junk link matching the URL shape by coincidence
    would steal a real product's data.
    """
    skus = set()
    for attr_node in node.select("[data-listing-id]"):
        got = attr_node.get("data-listing-id")
        if got:
            skus.add(got)
    for link in node.select('a[href*="/listing/"]'):
        got = sku_from_url(link.get("href"))
        if got:
            skus.add(got)
    return len(skus)


def _tiles(soup) -> List:
    """One tile per distinct listing id, in document order.

    Etsy emits each tile TWICE — once for the desktop layout and once for
    mobile — so the live search page carries 129 `div[data-listing-id]`
    nodes for 64 products. Both copies are complete, so the first is taken
    and the duplicate dropped; taking both would double every row and make
    `position` meaningless.
    """
    seen, out = set(), []
    for card in soup.select(SELECTORS["product_card"]):
        sku = card.get("data-listing-id")
        if not sku or sku in seen:
            continue
        seen.add(sku)
        out.append(card)
    return out


def _tile_is_ad(tile) -> Optional[bool]:
    """True for a sponsored tile, False for organic, None when unstated.

    Reads the `ls` parameter off the tile's own link — `ls=a` for an ad,
    `ls=s` for a search result — because Etsy's visible label is written in
    the page's language and a text marker would pass ads through on every
    locale but the one it was written for. See the module docstring for the
    280/280 verification.

    None rather than False when the parameter is absent: exactly one tile per
    captured page has no `ls`, and calling those organic would be a guess
    presented as a fact.
    """
    for link in tile.select("a[href]"):
        m = re.search(r"[?&]ls=([a-z]+)", link.get("href") or "")
        if m:
            return m.group(1) == "a"
    return None


def _current_price_node(tile):
    """The price container with everything that is not the price removed.

    A COPY is taken and the strikethrough and promotion subtrees are deleted
    from it, because on a discounted tile both prices and the discount badge
    live in one node:

        "Sale-Preis 35,87 € 35,87 € 59,79 € Ursprünglicher Preis 59,79 € (40% Rabatt)"

    Reading the first number out of that is a coin flip between the sale
    price and the original. Deleting from a copy leaves the caller's soup
    untouched, which matters because the same tile is read again for the
    original price.
    """
    node = tile.select_one(SELECTORS["price_block"])
    if node is None:
        return None
    node = copy.copy(node)
    for junk in node.select(SELECTORS["strike"]) + node.select(SELECTORS["promotion"]):
        junk.decompose()
    return node


def _tile_prices(tile, host_cur: Optional[str]
                 ) -> Tuple[Optional[float], Optional[float], Optional[str], bool]:
    """(price, original_price, currency, price_is_from) from one tile."""
    block = tile.select_one(SELECTORS["price_block"])
    price, currency = _price_from_parts(_current_price_node(tile), host_cur)

    original = None
    if block is not None:
        strike = block.select_one(SELECTORS["strike"])
        if strike is not None:
            original, strike_cur = _price_from_parts(strike, host_cur)
            currency = currency or strike_cur

    is_from = False
    if block is not None:
        is_from = bool(_FROM_PREFIX_RE.match(block.get_text(" ", strip=True)))

    return price, original, currency, is_from


def _tile_shop(tile) -> Tuple[Optional[str], Optional[str]]:
    """(shop_name, shop_id) from one tile.

    `data-shop-id` is on every tile of every page kind (100% on all three
    live captures). The NAME is on search tiles only, so a category or shop
    row gets it from JSON-LD instead — between them the coverage is 63/63 on
    search and 61/65 on category.
    """
    name = _clean_text(tile.select_one(SELECTORS["shop_name"]))
    return name, _clean_text(tile.get("data-shop-id"))


def _tile_url(tile, base_url: str) -> Optional[str]:
    """The listing URL a tile points at, tracking parameters removed."""
    for link in tile.select('a[href*="/listing/"]'):
        href = link.get("href")
        if href and sku_from_url(href):
            return _absolute_url(base_url, href)
    return None


def _tile_image(tile) -> Optional[str]:
    """The tile's product image.

    Prefers the widest source Etsy offers in `srcset` over the `src`
    thumbnail, because the thumbnail is 340px wide and useless for anything
    but a thumbnail.
    """
    for img in tile.select("img"):
        srcset = img.get("srcset") or ""
        if srcset:
            candidates = []
            for part in srcset.split(","):
                bits = part.strip().split()
                if not bits:
                    continue
                width = 0
                if len(bits) > 1 and bits[1].endswith("w"):
                    try:
                        width = int(bits[1][:-1])
                    except ValueError:
                        width = 0
                candidates.append((width, bits[0]))
            if candidates:
                return max(candidates)[1]
        src = img.get("src")
        if src and "etsystatic" in src:
            return src
    return None


# ---------------------------------------------------------------------------
# Parsing: listing pages (search, category, market, shop front)
# ---------------------------------------------------------------------------
def parse_products(html: str, url: str, page: Optional[int] = None,
                   category: Optional[str] = None) -> List[Product]:
    """Rows for every listing on one listing page.

    `category` overrides the label derived from the URL. A search URL has no
    category in Etsy's own taxonomy — the query is recorded instead — so the
    override is how a caller labels a search run with something meaningful to
    them.

    DOM-first, JSON-LD-enriched — the inversion of the family's usual order,
    for the reason in the module docstring. The fallback path (URL-pattern
    links with no tile around them) runs only if the tile path yields nothing
    at all.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    host_cur = host_currency(url)
    ld_index = _ld_by_sku(_ld_blocks(soup))
    source = site_host(url) or SOURCE_DEFAULT
    category = category or category_from_url(url)
    shop_of_page = shop_from_url(url)
    page_num = page if page is not None else (page_number_from_url(url) or 1)

    # A shop front's tiles carry no stars at all (0 of 40 on the capture),
    # but the page states the seller's rating in its own `Organization`
    # data — so on that page kind every row can still carry it, and does.
    page_shop = shop_metadata(html, url) if shop_of_page else None
    page_shop_rating = (page_shop or {}).get("shop_rating")
    page_shop_reviews = (page_shop or {}).get("shop_review_count")

    rows: List[Product] = []
    confirmed = 0
    disagreed: List[Tuple[str, float, float]] = []
    disagreed_original: List[Tuple[str, float, float]] = []
    for position, tile in enumerate(_tiles(soup), start=1):
        sku = _clean_text(tile.get("data-listing-id"))
        if not sku:
            continue
        node = ld_index.get(sku, {})

        price, original, currency, is_from = _tile_prices(tile, host_cur)
        offer = _ld_offer(node)
        ld_price, ld_max, ld_is_from = _ld_offer_prices(offer)
        ld_currency = _clean_text(offer.get("priceCurrency"))

        price_source = "dom" if price is not None else None
        if ld_price is not None:
            if price is None:
                price, price_source = ld_price, "jsonld"
                is_from = is_from or ld_is_from
            elif abs(ld_price - price) < 0.005:
                price_source = "jsonld+dom"
                confirmed += 1
            else:
                # The two views disagree about the price of the same listing
                # id. Keep the row on the DOM figure — that is what the page
                # showed the visitor — and record the sku so the run can say
                # which rows they were. Overwriting a correct row is worse
                # than leaving one unconfirmed.
                #
                # Reported as a BATCH below rather than one line per row:
                # this happens on a handful of listings on every healthy
                # page (5 of 64 on the category capture, where Etsy
                # publishes a price 15-20% below the one it prints and shows
                # no sale on the tile), and 64 warnings a page would train
                # the reader to ignore the log.
                disagreed.append((sku, ld_price, price))
                price_source = "dom"

        # The was-price, cross-checked against the structured ListPrice where
        # the tile carries a strike at all. Not used as a SOURCE for
        # `original_price`: a listing can publish a ListPrice while showing
        # no discount, and reading it in would invent one.
        if original is not None:
            ld_list = _ld_list_price(offer)
            if ld_list is not None and abs(ld_list - original) >= 0.005:
                disagreed_original.append((sku, ld_list, original))

        shop_rating, shop_review_count = _shop_rating_from(tile)
        # The LISTING's own rating, if the page happens to publish one. It
        # does not: 0 of 105 listing-page JSON-LD nodes carry an
        # `aggregateRating` across the three captured page kinds. The read is
        # kept rather than hardcoded to None so that a future Etsy that does
        # publish it starts populating the column without a code change.
        rating, review_count = _ld_rating(node)

        shop_name, shop_id = _tile_shop(tile)

        rows.append(Product(
            source=source,
            url=_tile_url(tile, url) or _absolute_url(url, "/listing/%s/" % sku),
            sku=sku,
            title=(_clean_text(tile.select_one(SELECTORS["title"]))
                   or _clean_text(node.get("name"))),
            brand=shop_name or _ld_brand(node) or shop_of_page,
            price=price,
            # Structured data naming its own currency is a fact and wins over
            # a symbol read off the page.
            currency=ld_currency or currency,
            original_price=original,
            discount_pct=_discount_from(price, original),
            rating=rating,
            review_count=review_count,
            in_stock=_ld_in_stock(offer),
            image_url=_tile_image(tile) or _ld_image(node.get("image")),
            category=category,
            price_source=price_source,
            page=page_num,
            position=position,
            is_ad=_tile_is_ad(tile),
            shop_id=shop_id,
            shop_rating=shop_rating if shop_rating is not None else page_shop_rating,
            shop_review_count=(shop_review_count if shop_review_count is not None
                               else page_shop_reviews),
            price_is_from=(is_from or ld_is_from) or None,
            price_max=ld_max,
        ))

    if not rows:
        rows = _parse_products_fallback(soup, url, page_num, host_cur,
                                        category)

    if rows:
        # Logged, not warned: Etsy's own JSON-LD covers 8 of 64 tiles on a
        # search page BY DESIGN, so a low share is expected there and a
        # warning would cry wolf on every healthy run. The canary thresholds
        # it per page kind instead.
        logger.info("%d/%d rows had their price confirmed by structured data "
                    "(%.0f%%)", confirmed, len(rows),
                    confirmed / len(rows) * 100)
        _report_disagreements(disagreed, disagreed_original, len(rows))
    return rows


# Above this share of rows, a price disagreement stops being Etsy's ordinary
# behaviour and starts being a reason to look at the parser. Set from
# measurement: 5 of 64 rows (8%) on the live category capture and 0 of 63 on
# the search capture, so a fifth of the page disagreeing is well outside
# what this site does.
_DISAGREEMENT_WARN_SHARE = 0.2


def _report_disagreements(price_rows, original_rows, total: int) -> None:
    """Say which rows the two views disagreed on, once per page.

    Loud enough to be actionable — every sku is named — without one line per
    row. A handful per page is normal on this site and is reported at INFO;
    a large share means the parser is reading the wrong node and is reported
    at WARNING.
    """
    for label, items in (("price", price_rows), ("original_price", original_rows)):
        if not items:
            continue
        share = len(items) / total if total else 0
        detail = ", ".join("%s (structured %s vs rendered %s)" % row
                           for row in items[:5])
        if len(items) > 5:
            detail += " and %d more" % (len(items) - 5)
        message = ("%d/%d rows: structured %s disagrees with the rendered "
                   "one; kept the rendered figure. %s")
        args = (len(items), total, label, detail)
        if share >= _DISAGREEMENT_WARN_SHARE:
            logger.warning(message, *args)
        else:
            logger.info(message, *args)


def _parse_products_fallback(soup, url: str, page_num: int,
                             host_cur: Optional[str],
                             category: Optional[str] = None) -> List[Product]:
    """Rows built from `/listing/` links alone, when no tile matched.

    The weaker path, and it exists because a data attribute is a build
    artefact while the URL shape is a contract with search engines. It walks
    up from each link to the outermost ancestor still covering exactly ONE
    listing, so a price read from it belongs to that listing and not to its
    neighbour.
    """
    source = site_host(url) or SOURCE_DEFAULT
    category = category or category_from_url(url)
    rows: List[Product] = []
    seen = set()
    for position, link in enumerate(soup.select(SELECTORS["item_link"]), start=1):
        sku = sku_from_url(link.get("href"))
        if not sku or sku in seen:
            continue
        seen.add(sku)

        scope = link
        for _ in range(_MAX_TILE_WIDEN):
            parent = scope.parent
            if parent is None or parent.name in ("body", "html", "[document]"):
                break
            if _distinct_skus(parent) > 1:
                break
            scope = parent

        price, original, currency, is_from = _tile_prices(scope, host_cur)
        if price is None:
            price, currency = _first_price(scope, host_cur)
        rows.append(Product(
            source=source,
            url=_absolute_url(url, link.get("href")),
            sku=sku,
            title=(_clean_text(scope.select_one(SELECTORS["title"]))
                   or _clean_text(link)),
            price=price,
            currency=currency,
            original_price=original,
            discount_pct=_discount_from(price, original),
            image_url=_tile_image(scope),
            category=category,
            price_source="dom" if price is not None else None,
            page=page_num,
            position=position,
            price_is_from=is_from or None,
        ))
    if rows:
        logger.info("tile path found nothing; %d rows came from the "
                    "URL-pattern fallback", len(rows))
    return rows


# ---------------------------------------------------------------------------
# Parsing: one listing (detail) page
# ---------------------------------------------------------------------------
def parse_product_page(html: str, url: str,
                       category: Optional[str] = None) -> List[Product]:
    """A single row for one `/listing/<id>` page, with the detail-only fields.

    Returns a LIST so the engines can treat every mode the same way, and so a
    page that turns out to carry no product yields `[]` rather than a row of
    nulls.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    host_cur = host_currency(url)
    url_sku = sku_from_url(url)

    candidates = []
    for node in _products_in_ld(_ld_blocks(soup)):
        node_sku = _clean_text(node.get("sku")) or sku_from_url(node.get("url"))
        if node_sku:
            candidates.append((node_sku, node))

    # A detail page also carries "similar items" and "more from this shop"
    # carousels, and their entries are products too. Pick the node whose id
    # matches the URL; only fall back to the first when the URL carries no id
    # at all, and never guess when it does.
    node = {}
    if url_sku is not None:
        node = next((n for s, n in candidates if s == url_sku), {})
    elif candidates:
        node = candidates[0][1]
    if not node:
        if not _ld_blocks(soup):
            # A live listing page always carries Product JSON-LD — verified on
            # every detail capture. Zero blocks on a /listing/ URL is Etsy's
            # "this item is unavailable" page, which it serves with HTTP 200
            # and 170 KB of site chrome, so neither the status nor the size
            # gives it away. Saying so is the difference between a user
            # checking their setup and a user checking the listing.
            logger.warning(
                "listing %s is not available — Etsy served its "
                "\"item unavailable\" page (HTTP 200, no product data). The "
                "listing was probably sold out or removed by the seller.",
                url_sku or "?")
        else:
            logger.warning("no Product structured data for listing %s on %s",
                           url_sku or "?", url)
        return []

    offer = _ld_offer(node)
    price, price_max, is_from = _ld_offer_prices(offer)
    currency = _clean_text(offer.get("priceCurrency"))
    price_source = "jsonld" if price is not None else None

    # The buy box, as a cross-check and as the fallback. Etsy prints
    # "ab 145,15 €" there for a variation listing, which is the same claim
    # the AggregateOffer makes.
    box = soup.select_one(SELECTORS["detail_price"])
    dom_price, dom_cur = _first_price(box, host_cur)
    if price is None:
        price, currency, price_source = dom_price, currency or dom_cur, (
            "dom" if dom_price is not None else None)
        if box is not None:
            is_from = bool(_FROM_PREFIX_RE.match(box.get_text(" ", strip=True)))
    elif dom_price is not None and abs(dom_price - price) < 0.005:
        price_source = "jsonld+dom"

    rating, review_count = _ld_rating(node)

    return [Product(
        source=site_host(url) or SOURCE_DEFAULT,
        url=_clean_text(node.get("url")) or _absolute_url(url, ""),
        sku=_clean_text(node.get("sku")) or url_sku,
        title=(_clean_text(node.get("name"))
               or _clean_text(soup.select_one(SELECTORS["detail_title"]))),
        brand=_ld_brand(node),
        price=price,
        currency=currency,
        # A detail page carries no was-price of its own in the captures taken
        # (0 strikethrough nodes on the live one), so `original_price` stays
        # null in this mode rather than being invented out of the carousel of
        # similar products — which DOES have strike prices, belonging to
        # other listings.
        original_price=None,
        discount_pct=None,
        rating=rating,
        # The LISTING's review count, which is not the shop's: this listing
        # reports 827 while its shop's own page reports 16679.
        review_count=review_count,
        in_stock=_ld_in_stock(offer),
        image_url=_ld_image(node.get("image")),
        # The site's own breadcrumb string, e.g.
        # "Haus & Wohnen < Küche & Essen < Trink- & Barzubehör < Gläser < Becher".
        category=category or _clean_text(node.get("category")),
        price_source=price_source,
        # No page/position: a detail page is not a position in a listing, and
        # a 1 here would be indistinguishable from a real first row.
        page=None,
        position=None,
        is_ad=None,
        shop_id=None,
        price_is_from=is_from or None,
        price_max=price_max,
        free_shipping=_ld_free_shipping(offer),
        ships_from=_ld_ships_from(offer),
        material=_clean_value(node.get("material")),
        gtin=_clean_value(node.get("gtin")),
        description=_clean_text(node.get("description")),
        images=_ld_images(node.get("image")) or None,
    )]


# ---------------------------------------------------------------------------
# Parsing: a shop front's own metadata
# ---------------------------------------------------------------------------
_SHOP_LD_TYPES = ("Organization", "LocalBusiness", "Store", "OnlineStore")


def shop_metadata(html: str, url: str) -> Optional[dict]:
    """The shop's own facts, for the run sidecar rather than for a row.

    A shop front's ROWS are its listings — same tiles, same schema, read by
    `parse_products` — so `--mode shop` produces `Product` rows like the
    other modes and stays comparable and diffable. But the page also states
    things about the SELLER that no product row can hold, and a run covers
    exactly one shop, so the sidecar is where they belong.

    Note the shop's `reviewCount` (16679 on the captured shop) is NOT the
    listing-level count its own products report (827 on one of them). Two
    different facts that would look interchangeable in one column.
    """
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for block in _ld_blocks(soup):
        btype = block.get("@type")
        types = btype if isinstance(btype, list) else [btype]
        if not any(t in _SHOP_LD_TYPES for t in types):
            continue
        rating, review_count = _ld_rating(block)
        return {
            "shop_name": _clean_text(block.get("name")) or shop_from_url(url),
            "shop_url": _clean_text(block.get("url")) or url,
            "shop_location": _clean_text(block.get("location")),
            "shop_slogan": _clean_text(block.get("slogan")),
            "shop_rating": rating,
            "shop_review_count": review_count,
        }
    name = shop_from_url(url)
    return {"shop_name": name, "shop_url": url} if name else None
