#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke_test.py
--------------
Zero-network, zero-browser sanity check for etsy-scraper.

Run this FIRST, before touching a real browser or etsy.com, to confirm the
parsing, the output contract, the page-state policy and the shared engine
decisions still hold:

    python3 smoke_test.py        # exits non-zero if anything failed

One file of plain functions with inline fixtures — no pytest, no conftest, no
fixtures directory. `tests/test_smoke.py` wraps this as a single pytest test
so `pytest` works as an entry point without a second copy of the checks.

It must pass with NO engine library installed at all: every
`import playwright_scraper` / `puppeteer_scraper` / `selenium_scraper` is
guarded and the skip is recorded. CI's `engine-smoke` job installs each engine
in its own virtualenv and fails if the corresponding group reports a skip —
"skipped, engine absent" reads identically to a real import error, so the two
have to be told apart somewhere.

WHAT THE FIXTURES ARE
---------------------
Real captures, taken 2026-09-09 over the 2Captcha Scraping Browser API from a
German residential exit, trimmed and then VERIFIED to parse identically to the
untrimmed original — every pinned field, every row. Tiles are whole: each one
is here because it pins a specific behaviour (an ad, a discount, a from-price,
the older rating markup), and its role is named in a comment above it.

The detail fixture's `review[]` is SCRUBBED. A real listing dump carries a
customer's display name and their words; the checks need the STRUCTURE of a
review, not the person, so those two fields are obvious placeholders and
everything Etsy generates around them is untouched. `test_no_capture_leaks`
guards that with PATTERNS rather than the old literals, so the next capture is
caught too.
"""

import io
import ast
import csv
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import builtins
from contextlib import redirect_stdout
from dataclasses import asdict as dataclasses_asdict, fields

import captcha_solver
from captcha_solver import (CaptchaChallenge, detect_recaptcha_v3,
                            reconcile_detections, _v2_task_for, _redact)
from diff_runs import diff_products
import env_config
import page_flow
from output_writer import (Product, save, finish_run, write_csv,
                           dedupe_by_key, dedupe_by_sku, run_meta,
                           ROW_CLASS_BY_MODE, UNIQUE_BY_SKU_MODES,
                           EXIT_BLOCKED, EXIT_NO_PRODUCTS, EXIT_PARTIAL,
                           EXIT_API_ERROR, COMPLETE_STOP_REASONS,
                           LIST_CSV_SEPARATOR)
import product_parser
from product_parser import (parse_products, parse_product_page, shop_metadata,
                            page_url, category_from_url, listing_kind,
                            site_host, is_supported_host, host_currency,
                            locale_of, path_without_locale, shop_from_url,
                            LOCALE_CURRENCY, HOSTS, detect_page_state,
                            detect_bot_challenge, datadome_verdict,
                            datadome_captcha_url, is_datadome_interstitial,
                            page_number_from_url, total_results, total_pages,
                            sku_from_url, unsupported_reason, SELECTORS)
from proxy_pool import (ProxyPool, mask, to_playwright, split_credentials,
                        parse_proxy_line)

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

_failures = []


def check(label, condition):
    """Print and record one check. Returns the condition, so callers can
    accumulate with `ok &= check(...)`."""
    if condition:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s" % label)
        _failures.append(label)
    return bool(condition)


def group(title):
    print("\n== %s" % title)


def _raises(fn):
    """True if `fn()` raises. Used where refusing is the correct behaviour."""
    try:
        fn()
    except Exception:
        return True
    return False


def page(*fragments):
    """Wrap fragments in a minimal document, as the engines hand it over.

    The asset-host reference is not decoration: `detect_page_state` treats a
    page that references none of Etsy's own assets as a block page (see its
    docstring), so a bare `<html><body>` wrapper would classify every
    hand-built fixture below as "blocked".
    """
    return ('<html lang="de"><body>'
            '<img src="https://i.etsystatic.com/x/il_fullxfull.jpg"/>'
            '%s</body></html>' % "".join(fragments))


# THE SECOND STOREFRONT. /search?q=handmade+mug from a US residential exit,
# 2026-09-10 — English, USD, and the same three roles as the German fixture
# so the two can be compared field by field:
#   4296141840  a SPONSORED tile, labelled "Ad from shop" in English
#   1702915637  organic, discounted, confirmed by structured data
#   4512181427  a 75%-off sale ($130 against $520), which is a real Etsy shape
#
# This is what the German captures could not test: a bare "$" resolving to
# USD through the locale table, and the ad signal agreeing with an ENGLISH
# label. Both are pinned below.
US_SEARCH_FIXTURE = r"""<html lang="de"><head>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "ItemList", "numberOfItems": 199067, "itemListElement": [{"@type": "ListItem", "position": 1, "item": {"@type": "Product", "image": "https://i.etsystatic.com/19096197/r/il/00b10c/5918841167/il_fullxfull.5918841167_7pog.jpg", "name": "Solstice Mug: Handmade Pottery Mug with Sun Design, Ceramic Coffee Cup in a Variety of Colors", "url": "https://www.etsy.com/listing/1702915637/solstice-mug-handmade-pottery-mug-with", "brand": {"@type": "Brand", "name": "ShorProducts"}, "offers": {"@type": "Offer", "price": "34.87", "priceCurrency": "USD", "availability": "https://schema.org/InStock", "priceSpecification": {"@type": "UnitPriceSpecification", "priceType": "https://schema.org/ListPrice", "price": "38.75", "priceCurrency": "USD"}}}}]}</script>
</head><body>
<img src="https://i.etsystatic.com/x/il_fullxfull.jpg"/>
<div class="js-merch-stash-check-listing v2-listing-card wt-mr-xs-0 search-listing-card--desktop wt-height-full wt-display-flex-xs wt-flex-direction-column-xs wt-justify-content-space-between listing-card-experimental-style appears-ready" data-listing-card-v2="" data-listing-id="4296141840" data-page-type="search" data-palette-listing-id="4296141840" data-shop-id="7825028">
<div class="listing-link wt-display-inline-block bdfe01948ca541107" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="4296141840" data-logging-key="PLACEHOLDER" data-palette-listing-image="PLACEHOLDER" data-shop-id="7825028">
<a aria-label="Coffee Mug - KJ Pottery" class="v2-listing-card__img wt-position-relative listing-card-rounded-corners wt-display-block" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="4296141840" data-listing-link="" data-logging-key="PLACEHOLDER" data-seller-preview-nav-guard="true" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/listing/4296141840/coffee-mug-kj-pottery?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=a&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;frs=1&amp;etp=1&amp;sts=1" target="etsy.4296141840">
<div class="placeholder listing-card-rounded-corners">
<div class="placeholder vertically-centered-placeholder listing-card-rounded-corners">
<img alt="Coffee Mug - KJ Pottery" class="wt-width-full wt-display-block listing-card-rounded-corners sc_gallery-1-1 wt-image--cover wt-image" data-clg-id="WtImage" data-listing-card-listing-image="" data-preload-lp-src="https://i.etsystatic.com/7825028/r/il/553467/6863380541/il_794xN.6863380541_5poq.jpg" data-preload-lp-srcset="https://i.etsystatic.com/7825028/r/il/553467/6863380541/il_794xN.6863380541_5poq.jpg 1x, https://i.etsystatic.com/7825028/r/il/553467/6863380541/il_1588xN.6863380541_5poq.jpg 2x" src="https://i.etsystatic.com/7825028/r/il/553467/6863380541/il_255x319.6863380541_5poq.jpg"/>
</div>
<div class="wt-position-absolute seller-preview-button-on-image" tabindex="0">
<clg-button aria-describedby="ad-listing-title-4296141840" background-type="dark" data-close-label="Close seller preview" data-listing-id="4296141840" data-seller-preview-button="" data-seller-preview-data='{"seller_first_name":"Kelsey Jo","owner_of_label":"Owner of KJPottery","seller_avatar_url":"https:\/\/i.etsystatic.com\/iusa\/cf5903\/42985865\/iusa_90x90.42985865_btjh.jpg?version=0","avatar_color":"green","avatar_shape":"3","avatar_initial":"K","tenure_label":"13 years on Etsy","seller_sales_label":"20.7k sales","custom_orders_label":"Open to custom orders","ships_from_label":"Ships from Spokane, Washington","rec_image_urls":[]}' data-seller-preview-listener-attached="true" data-shop-id="7825028" data-shop-url="https://www.etsy.com/shop/KJPottery" hydrated="" size="small" title="Preview seller" type="button" variant="primary">
<clg-icon name="eye" size="smaller"></clg-icon>
<span class="wt-text-title-small">Preview seller</span>
</clg-button>
</div>
</div>
</a>
<div class="v2-listing-card__info wt-mt-xs-1 wt-pt-xs-0">
<div class="wt-display-flex-xs wt-align-items-center search-half-unit-my swatch-container">
<a alt="Black" class="wt-circle wt-overflow-hidden wt-z-index-1 wt-mr-xs-1 swatch-link smaller" data-color-swatch="" data-sr-prefetch="PLACEHOLDER" data-variation-id="5528463188" href=PLACEHOLDER"https://www.etsy.com/listing/4296141840/coffee-mug-kj-pottery?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=a&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;frs=1&amp;etp=1&amp;sts=1&amp;variation0=5528463188" target="etsy.4296141840.5528463188">
<div alt="Black" class="wt-circle wt-overflow-hidden wt-z-index-1 listing-card-swatch add-transparent-outline smaller hover-effect">
</div>
</a>
<a alt="Tan" class="wt-circle wt-overflow-hidden wt-z-index-1 wt-mr-xs-1 swatch-link smaller" data-color-swatch="" data-sr-prefetch="PLACEHOLDER" data-variation-id="5367487358" href=PLACEHOLDER"https://www.etsy.com/listing/4296141840/coffee-mug-kj-pottery?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=a&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;frs=1&amp;etp=1&amp;sts=1&amp;variation0=5367487358" target="etsy.4296141840.5367487358">
<div alt="Tan" class="wt-circle wt-overflow-hidden wt-z-index-1 listing-card-swatch add-transparent-outline smaller hover-effect">
</div>
</a>
<a alt="Dark green" class="wt-circle wt-overflow-hidden wt-z-index-1 wt-mr-xs-1 swatch-link smaller" data-color-swatch="" data-sr-prefetch="PLACEHOLDER" data-variation-id="5347386175" href=PLACEHOLDER"https://www.etsy.com/listing/4296141840/coffee-mug-kj-pottery?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=a&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;frs=1&amp;etp=1&amp;sts=1&amp;variation0=5347386175" target="etsy.4296141840.5347386175">
<div alt="Dark green" class="wt-circle wt-overflow-hidden wt-z-index-1 listing-card-swatch add-transparent-outline smaller hover-effect">
</div>
</a>
<a alt="Yellow" class="wt-circle wt-overflow-hidden wt-z-index-1 wt-mr-xs-1 swatch-link smaller" data-color-swatch="" data-sr-prefetch="PLACEHOLDER" data-variation-id="5347386177" href=PLACEHOLDER"https://www.etsy.com/listing/4296141840/coffee-mug-kj-pottery?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=a&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;frs=1&amp;etp=1&amp;sts=1&amp;variation0=5347386177" target="etsy.4296141840.5347386177">
<div alt="Yellow" class="wt-circle wt-overflow-hidden wt-z-index-1 listing-card-swatch add-transparent-outline smaller hover-effect">
</div>
</a>
<a alt="Gray" class="wt-circle wt-overflow-hidden wt-z-index-1 wt-mr-xs-1 swatch-link smaller" data-color-swatch="" data-sr-prefetch="PLACEHOLDER" data-variation-id="5347386179" href=PLACEHOLDER"https://www.etsy.com/listing/4296141840/coffee-mug-kj-pottery?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=a&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;frs=1&amp;etp=1&amp;sts=1&amp;variation0=5347386179" target="etsy.4296141840.5347386179">
<div alt="Gray" class="wt-circle wt-overflow-hidden wt-z-index-1 listing-card-swatch add-transparent-outline smaller hover-effect">
</div>
</a>
<a alt="Blue" class="wt-circle wt-overflow-hidden wt-z-index-1 wt-mr-xs-1 swatch-link smaller" data-color-swatch="" data-sr-prefetch="PLACEHOLDER" data-variation-id="5347386183" href=PLACEHOLDER"https://www.etsy.com/listing/4296141840/coffee-mug-kj-pottery?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=a&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;frs=1&amp;etp=1&amp;sts=1&amp;variation0=5347386183" target="etsy.4296141840.5347386183">
<div alt="Blue" class="wt-circle wt-overflow-hidden wt-z-index-1 listing-card-swatch add-transparent-outline smaller hover-effect">
</div>
</a>
<span class="wt-text-grey wt-text-body-smaller swatch-counter">
</span>
</div>
<a aria-label="" class="wt-z-index-1" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="" data-listing-link="" data-logging-key="PLACEHOLDER" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/listing/4296141840/coffee-mug-kj-pottery?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=a&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;frs=1&amp;etp=1&amp;sts=1" target="etsy.4296141840">
<h3 class="wt-text-caption v2-listing-card__title wt-text-truncate search-half-unit-mb" id="ad-listing-title-4296141840" title="Coffee Mug - KJ Pottery">
                    Coffee Mug - KJ Pottery
                </h3>
<div class="streamline-spacing-shop-rating">
<div class="shop-name-with-rating wt-display-flex-xs flex-direction-row-xs wt-align-items-center">
<span class="wt-display-flex-xs wt-flex-nowrap wt-align-items-center larger_review_stars">
<clg-static-review-stars class="him-review-stars wt-pal-grid-mr-xs-050" rating="4.9" review-count-text="(3.5k)" size="smaller" variant="one-star"></clg-static-review-stars>
</span>
<div class="wt-flex-basis-lg-full wt-flex-basis-xl-auto wt star-badge-wrap-spacing wt-width-full min-width-0 wt-mt-xs-0">
<p class="wt-text-caption wt-text-truncate wt-text-gray use_body_small_text wt-text-body-smaller streamline-seller-shop-name__line-height" data-seller-name-container="">
<span aria-hidden="true">
                        Ad<strong>・</strong>By <span class="wt-text-link clickable-shop-name" data-seller-name-link="" data-shop-url="https://www.etsy.com/shop/KJPottery?plkey=EuXTLBw8d-WMJE9ktmeWoyCa5N2e%3ALT28e267a675f3769f01b2988c777312312484420a">KJPottery<clg-icon class="wt-flex-shrink-xs-0 wt-vertical-align-text-top wt-sem-text-star-seller wt-ml-xs-05" data-star-seller-badge="true" name="starseller" size="smaller"></clg-icon></span>
</span>
<span class="wt-screen-reader-only">Ad from shop KJPottery</span>
</p>
</div>
</div>
</div>
<div class="search-half-unit-mt"></div>
<div class="n-listing-card__price wt-display-block wt-text-title-01 lc-price dense wt-display-inline-block">
<p class="wt-text-title-01 lc-price dense wt-display-inline-block">
<span class="currency-symbol">$</span><span class="currency-value">55.00</span>
</p>
</div>
<div class="streamline-spacing-pricing-info">
<div class="wt-signal-group wt-signal-group--horizontal" data-clg-id="WtSignalGroup">
<span class="wt-signal wt-signal--generic-subtle" data-clg-id="WtSignal">
  
  Etsy’s Pick
</span>
<span aria-hidden="true" class="lc-signal-separator">·</span><span class="wt-signal wt-signal--generic-subtle" data-clg-id="WtSignal">
  
  Free shipping
</span>
</div>
</div>
</a>
</div>
</div>
<div class="search-half-unit-mt wt-display-flex-xs wt-flex-wrap wt-align-items-center row-gap-2">
<span class="wt-mr-xs-2"><form action="/cart/listing.php" class="wt-display-inline-block" data-logging-key="PLACEHOLDER" method="post">
<input name="listing_id" type="hidden" value="4296141840"/>
<input name="listing_title" type="hidden" value="Coffee Mug - KJ Pottery"/>
<input name="listing_url" type="hidden" value="https://www.etsy.com/listing/4296141840/coffee-mug-kj-pottery"/>
<input name="quantity" type="hidden" value="1"/>
<input name="ref" type="hidden" value="search_lc_cart_pl"/>
<input name="show_listing_disclaimer" type="hidden" value="true"/>
<input name="show_cart_edit_panel" type="hidden" value="true"/>
<input name="is_pl" type="hidden" value="true"/>
<input name="listing_source" type="hidden" value="ads"/>
<input name="logging_key" type="hidden" value="EuXTLBw8d-WMJE9ktmeWoyCa5N2e:LT28e267a675f3769f01b2988c777312312484420a"/>
<input name="listing_image_url" type="hidden" value="https://i.etsystatic.com/7825028/r/il/553467/6863380541/il_372x296.6863380541_5poq.jpg"/>
<input name="query" type="hidden" value="handmade mug"/>
<input name="organic_listings_count" type="hidden" value="199067"/>
<input name="formatted_original_price" type="hidden" value="$55.00"/>
<input name="formatted_discounted_price" type="hidden" value=""/>
<input name="percent_discount" type="hidden" value=""/>
<input name="placement" type="hidden" value="wsg"/>
<input name="use_listing_title_for_mlt" type="hidden" value="false"/>
<input class="wt-display-none" name="_nnc" type="hidden" value="3:1789025451:QwAOMHv8ZPupKuHP3RrRdSBMK2ye:e720f9399acce047ee60fc013ff5ced38ede4d4e6067a7b844209b3065d89c14"/>
<clg-button aria-describedby="ad-listing-title-4296141840" background-type="dynamic" class="" data-listing-card-add-to-cart="" hydrated="" size="small" type="submit" variant="secondary" with-submit="">
<clg-icon name="add" size="smaller"></clg-icon>
<span>Add to cart</span>
</clg-button>
</form></span>
<span></span>
<span class="wt-vertical-align-middle"><clg-text-link aria-describedby="ad-listing-title-4296141840" class="refine-by-listing-link wt-text-title-small--tight" data-more-like-this-button="" href=PLACEHOLDER"https://www.etsy.com/r/similar/4296141840/?ref=PLACEHOLDER" icon="end" rel="nofollow" target="_blank" title="More like this">
        More like this
        <clg-icon class="refine-by-listing-link-icon refine-by-listing-link-icon--end" name="rightarrow" size="smaller" slot="icon"></clg-icon>
</clg-text-link></span>
</div>
<div class="v2-listing-card__actions wt-z-index-1 wt-position-absolute" data-favorite-button-wrapper="">

</div>
</div>
<div class="js-merch-stash-check-listing v2-listing-card wt-mr-xs-0 search-listing-card--desktop wt-height-full wt-display-flex-xs wt-flex-direction-column-xs wt-justify-content-space-between listing-card-experimental-style appears-ready" data-listing-card-v2="" data-listing-id="4512181427" data-page-type="search" data-palette-listing-id="4512181427" data-shop-id="41859003">
<div class="listing-link wt-display-inline-block bdfe01948ca541107" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="4512181427" data-logging-key="PLACEHOLDER" data-palette-listing-image="PLACEHOLDER" data-shop-id="41859003">
<a aria-label="Set of 6 Natural Multi Green Onyx Whiskey Cups, Handmade Stone Whisky Glasses, Luxury Bourbon Tumbler Set, Gift for Him" class="v2-listing-card__img wt-position-relative listing-card-rounded-corners wt-display-block" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="4512181427" data-listing-link="" data-logging-key="PLACEHOLDER" data-seller-preview-nav-guard="true" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/listing/4512181427/set-6-pcs-handmade-onyx-coffee-cup-with?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=a&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;pro=1&amp;sts=1" target="etsy.4512181427">
<div class="placeholder listing-card-rounded-corners" tabindex="-1">
<div class="placeholder vertically-centered-placeholder listing-card-rounded-corners">
<img alt="Set of 6 Natural Multi Green Onyx Whiskey Cups, Handmade Stone Whisky Glasses, Luxury Bourbon Tumbler Set, Gift for Him" class="wt-width-full wt-display-block listing-card-rounded-corners sc_gallery-1-2 wt-image--cover wt-image" data-clg-id="WtImage" data-listing-card-listing-image="" data-preload-lp-src="https://i.etsystatic.com/41859003/r/il/207144/8493711664/il_794xN.8493711664_f3xh.jpg" data-preload-lp-srcset="https://i.etsystatic.com/41859003/r/il/207144/8493711664/il_794xN.8493711664_f3xh.jpg 1x, https://i.etsystatic.com/41859003/r/il/207144/8493711664/il_1588xN.8493711664_f3xh.jpg 2x" src="https://i.etsystatic.com/41859003/r/il/207144/8493711664/il_255x319.8493711664_f3xh.jpg"/>
</div>
<div aria-hidden="true" class="listing-card-video-spinner wt-align-items-center wt-display-none wt-position-absolute wt-position-top wt-position-bottom wt-position-left wt-position-right">
<div class="wt-spinner wt-spinner--01">
<span class="etsy-icon"></span>
        Loading
    </div>
</div>
<div class="listing-card-video-container wt-animated wt-animated--disappear-01 wt-position-absolute wt-position-top wt-position-bottom wt-position-left wt-position-right listing-card-rounded-corners">

</div>
<div aria-hidden="false" class="listing-card-video-signal wt-position-absolute wt-circle wt-overflow-hidden wt-sem-bg-elevation-0">
<clg-icon class="wt-horizontal-center wt-vertical-center" name="play" size="smaller"></clg-icon>
</div>
<div class="wt-position-absolute seller-preview-button-on-image" tabindex="0">
<clg-button aria-describedby="ad-listing-title-4512181427" background-type="dark" data-close-label="Close seller preview" data-listing-id="4512181427" data-seller-preview-button="" data-seller-preview-data='{"seller_first_name":"KSMINERALS","owner_of_label":"Owner of KSMineralsExportBG","seller_avatar_url":"https:\/\/i.etsystatic.com\/iusa\/925ec2\/112353487\/iusa_90x90.112353487_8m2d.jpg?version=0","avatar_color":"purple","avatar_shape":"7","avatar_initial":"K","tenure_label":"3 years on Etsy","seller_sales_label":"4.8k sales","custom_orders_label":"Open to custom orders","ships_from_label":"Ships from Madan, Bulgaria","rec_image_urls":[]}' data-seller-preview-listener-attached="true" data-shop-id="41859003" data-shop-url="https://www.etsy.com/shop/KSMineralsExportBG" hydrated="" size="small" title="Preview seller" type="button" variant="primary">
<clg-icon name="eye" size="smaller"></clg-icon>
<span class="wt-text-title-small">Preview seller</span>
</clg-button>
</div>
</div>
</a>
<div class="v2-listing-card__info wt-pt-xs-0">
<a aria-label="" class="wt-z-index-1" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="" data-listing-link="" data-logging-key="PLACEHOLDER" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/listing/4512181427/set-6-pcs-handmade-onyx-coffee-cup-with?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=a&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;pro=1&amp;sts=1" target="etsy.4512181427">
<h3 class="wt-text-caption v2-listing-card__title wt-text-truncate search-half-unit-mt wt-mt-xs-1 search-half-unit-mb" id="ad-listing-title-4512181427" title="Set of 6 Natural Multi Green Onyx Whiskey Cups, Handmade Stone Whisky Glasses, Luxury Bourbon Tumbler Set, Gift for Him">
                    Set of 6 Natural Multi Green Onyx Whiskey Cups, Handmade Stone Whisky Glasses, Luxury Bourbon Tumbler Set, Gift for Him
                </h3>
<div class="streamline-spacing-shop-rating">
<div class="shop-name-with-rating wt-display-flex-xs flex-direction-row-xs wt-align-items-center">
<span class="wt-display-flex-xs wt-flex-nowrap wt-align-items-center larger_review_stars">
<clg-static-review-stars class="him-review-stars wt-pal-grid-mr-xs-050" rating="4.9" review-count-text="(1.4k)" size="smaller" variant="one-star"></clg-static-review-stars>
</span>
<div class="wt-flex-basis-lg-full wt-flex-basis-xl-auto wt star-badge-wrap-spacing wt-width-full min-width-0 wt-mt-xs-0">
<p class="wt-text-caption wt-text-truncate wt-text-gray use_body_small_text wt-text-body-smaller streamline-seller-shop-name__line-height" data-seller-name-container="">
<span aria-hidden="true">
                        Ad<strong>・</strong>By <span class="wt-text-link clickable-shop-name" data-seller-name-link="" data-shop-url="https://www.etsy.com/shop/KSMineralsExportBG?plkey=EuXTLBw8d-WMJE9ktmeWoyCa5N2e%3ALT297a0548d2a26c08bee25d2c8ac1f62e855bd11d">KSMineralsExportBG<clg-icon class="wt-flex-shrink-xs-0 wt-vertical-align-text-top wt-sem-text-star-seller wt-ml-xs-05" data-star-seller-badge="true" name="starseller" size="smaller"></clg-icon></span>
</span>
<span class="wt-screen-reader-only">Ad from shop KSMineralsExportBG</span>
</p>
</div>
</div>
</div>
<div class="search-half-unit-mt"></div>
<div class="n-listing-card__price wt-display-block wt-text-title-01 lc-price dense wt-display-inline-block">
<p class="wt-text-slime wt-text-title-01 lc-price dense wt-display-inline-block">
<span class="wt-screen-reader-only">
                            Sale Price $130.00
                        </span>
<span aria-hidden="true">
<span class="currency-symbol">$</span><span class="currency-value">130.00</span>
</span>
</p><p class="wt-text-caption search-collage-promotion-price search-collage-original-price wt-text-slime wt-text-truncate wt-no-wrap">
<span aria-hidden="true" class="wt-text-strikethrough wt-text-black"><span class="currency-symbol">$</span><span class="currency-value">520.00</span></span>
<span class="wt-screen-reader-only">
                                    Original Price $520.00
                                </span>
<span class="wt-text-black">
<span class="wt-text-grey">
                                    (75% off)
                                    </span>
</span>
</p>
<p></p>
</div>
<div class="streamline-spacing-pricing-info streamline-spacing-reduce-margin">
<div class="wt-signal-group wt-signal-group--horizontal" data-clg-id="WtSignalGroup">
</div>
</div>
</a>
</div>
</div>
<div class="search-half-unit-mt wt-display-flex-xs wt-flex-wrap wt-align-items-center row-gap-2">
<span class="wt-mr-xs-2"><form action="/cart/listing.php" class="wt-display-inline-block" data-logging-key="PLACEHOLDER" method="post">
<input name="listing_id" type="hidden" value="4512181427"/>
<input name="listing_title" type="hidden" value="Set of 6 Natural Multi Green Onyx Whiskey Cups, Handmade Stone Whisky Glasses, Luxury Bourbon Tumbler Set, Gift for Him"/>
<input name="listing_url" type="hidden" value="https://www.etsy.com/listing/4512181427/set-6-pcs-handmade-onyx-coffee-cup-with"/>
<input name="quantity" type="hidden" value="1"/>
<input name="ref" type="hidden" value="search_lc_cart_pl"/>
<input name="show_listing_disclaimer" type="hidden" value="true"/>
<input name="show_cart_edit_panel" type="hidden" value="true"/>
<input name="is_pl" type="hidden" value="true"/>
<input name="listing_source" type="hidden" value="ads"/>
<input name="logging_key" type="hidden" value="EuXTLBw8d-WMJE9ktmeWoyCa5N2e:LT297a0548d2a26c08bee25d2c8ac1f62e855bd11d"/>
<input name="listing_image_url" type="hidden" value="https://i.etsystatic.com/41859003/r/il/207144/8493711664/il_372x296.8493711664_f3xh.jpg"/>
<input name="query" type="hidden" value="handmade mug"/>
<input name="organic_listings_count" type="hidden" value="199067"/>
<input name="formatted_original_price" type="hidden" value="$520.00"/>
<input name="formatted_discounted_price" type="hidden" value="$130.00"/>
<input name="percent_discount" type="hidden" value="75"/>
<input name="placement" type="hidden" value="wsg"/>
<input name="use_listing_title_for_mlt" type="hidden" value="false"/>
<input class="wt-display-none" name="_nnc" type="hidden" value="3:1789025451:z69JWCFs9v2I0GTR7xUCV7TYAKWH:f9a2cf3b649ff96e68db4136667370231e0f1afce6e3afa544a600f76fc51aaa"/>
<clg-button aria-describedby="ad-listing-title-4512181427" background-type="dynamic" class="" data-listing-card-add-to-cart="" hydrated="" size="small" type="submit" variant="secondary" with-submit="">
<clg-icon name="add" size="smaller"></clg-icon>
<span>Add to cart</span>
</clg-button>
</form></span>
<span></span>
<span class="wt-vertical-align-middle"><clg-text-link aria-describedby="ad-listing-title-4512181427" class="refine-by-listing-link wt-text-title-small--tight" data-more-like-this-button="" href=PLACEHOLDER"https://www.etsy.com/r/similar/4512181427/?ref=PLACEHOLDER" icon="end" rel="nofollow" target="_blank" title="More like this">
        More like this
        <clg-icon class="refine-by-listing-link-icon refine-by-listing-link-icon--end" name="rightarrow" size="smaller" slot="icon"></clg-icon>
</clg-text-link></span>
</div>
<div class="v2-listing-card__actions wt-z-index-1 wt-position-absolute" data-favorite-button-wrapper="">

</div>
</div>
<div class="js-merch-stash-check-listing v2-listing-card wt-mr-xs-0 search-listing-card--desktop wt-height-full wt-display-flex-xs wt-flex-direction-column-xs wt-justify-content-space-between listing-card-experimental-style appears-ready" data-listing-card-v2="" data-listing-id="1702915637" data-page-type="search" data-palette-listing-id="1702915637" data-shop-id="19096197">
<div class="listing-link wt-display-inline-block b571c464f0986ce1f" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="1702915637" data-logging-key="PLACEHOLDER" data-palette-listing-image="PLACEHOLDER">
<a aria-label="Solstice Mug: Handmade Pottery Mug with Sun Design, Ceramic Coffee Cup in a Variety of Colors" class="v2-listing-card__img wt-position-relative listing-card-rounded-corners wt-display-block" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="1702915637" data-listing-link="" data-logging-key="PLACEHOLDER" data-seller-preview-nav-guard="true" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/listing/1702915637/solstice-mug-handmade-pottery-mug-with?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;pro=1&amp;etp=1&amp;content_source=PLACEHOLDER" target="etsy.1702915637">
<div class="placeholder listing-card-rounded-corners" tabindex="-1">
<div class="placeholder vertically-centered-placeholder listing-card-rounded-corners">
<img alt="Solstice Mug: Handmade Pottery Mug with Sun Design, Ceramic Coffee Cup in a Variety of Colors" class="wt-width-full wt-display-block listing-card-rounded-corners sr_gallery-1-1 wt-image--cover wt-image" data-clg-id="WtImage" data-listing-card-listing-image="" data-preload-lp-src="https://i.etsystatic.com/19096197/r/il/00b10c/5918841167/il_794xN.5918841167_7pog.jpg" data-preload-lp-srcset="https://i.etsystatic.com/19096197/r/il/00b10c/5918841167/il_794xN.5918841167_7pog.jpg 1x, https://i.etsystatic.com/19096197/r/il/00b10c/5918841167/il_1588xN.5918841167_7pog.jpg 2x" src="https://i.etsystatic.com/19096197/r/il/00b10c/5918841167/il_255x319.5918841167_7pog.jpg"/>
</div>
<div aria-hidden="true" class="listing-card-video-spinner wt-align-items-center wt-display-none wt-position-absolute wt-position-top wt-position-bottom wt-position-left wt-position-right">
<div class="wt-spinner wt-spinner--01">
<span class="etsy-icon"></span>
        Loading
    </div>
</div>
<div class="listing-card-video-container wt-animated wt-animated--disappear-01 wt-position-absolute wt-position-top wt-position-bottom wt-position-left wt-position-right listing-card-rounded-corners">

</div>
<div aria-hidden="false" class="listing-card-video-signal wt-position-absolute wt-circle wt-overflow-hidden wt-sem-bg-elevation-0">
<clg-icon class="wt-horizontal-center wt-vertical-center" name="play" size="smaller"></clg-icon>
</div>
<div class="wt-position-absolute seller-preview-button-on-image" tabindex="0">
<clg-button aria-describedby="listing-title-1702915637" background-type="dark" data-close-label="Close seller preview" data-listing-id="1702915637" data-seller-preview-button="" data-seller-preview-data='{"seller_first_name":"SH\u014cR Products","owner_of_label":"Owner of ShorProducts","seller_avatar_url":"https:\/\/i.etsystatic.com\/iusa\/d9bce6\/85519605\/iusa_90x90.85519605_ea3k.jpg?version=0","avatar_color":"purple","avatar_shape":"5","avatar_initial":"S","tenure_label":"5 years on Etsy","seller_sales_label":"4.6k sales","custom_orders_label":"Open to custom orders","ships_from_label":"Ships from Grand Rapids, Minnesota","rec_image_urls":[]}' data-seller-preview-listener-attached="true" data-shop-id="19096197" data-shop-url="https://www.etsy.com/shop/ShorProducts" hydrated="" size="small" title="Preview seller" type="button" variant="primary">
<clg-icon name="eye" size="smaller"></clg-icon>
<span class="wt-text-title-small">Preview seller</span>
</clg-button>
</div>
</div>
</a>
<div class="v2-listing-card__info wt-mt-xs-1 wt-pt-xs-0">
<div class="wt-display-flex-xs wt-align-items-center search-half-unit-my swatch-container">
<a alt="Dark blue" class="wt-circle wt-overflow-hidden wt-z-index-1 wt-mr-xs-1 swatch-link smaller" data-color-swatch="" data-sr-prefetch="PLACEHOLDER" data-variation-id="4981481404" href=PLACEHOLDER"https://www.etsy.com/listing/1702915637/solstice-mug-handmade-pottery-mug-with?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;pro=1&amp;etp=1&amp;content_source=PLACEHOLDER&amp;variation0=4981481404" target="etsy.1702915637.4981481404">
<div alt="Dark blue" class="wt-circle wt-overflow-hidden wt-z-index-1 listing-card-swatch add-transparent-outline smaller hover-effect">
</div>
</a>
<a alt="Green" class="wt-circle wt-overflow-hidden wt-z-index-1 wt-mr-xs-1 swatch-link smaller" data-color-swatch="" data-sr-prefetch="PLACEHOLDER" data-variation-id="4803006659" href=PLACEHOLDER"https://www.etsy.com/listing/1702915637/solstice-mug-handmade-pottery-mug-with?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;pro=1&amp;etp=1&amp;content_source=PLACEHOLDER&amp;variation0=4803006659" target="etsy.1702915637.4803006659">
<div alt="Green" class="wt-circle wt-overflow-hidden wt-z-index-1 listing-card-swatch add-transparent-outline smaller hover-effect">
</div>
</a>
<a alt="Blue" class="wt-circle wt-overflow-hidden wt-z-index-1 wt-mr-xs-1 swatch-link smaller" data-color-swatch="" data-sr-prefetch="PLACEHOLDER" data-variation-id="4807181288" href=PLACEHOLDER"https://www.etsy.com/listing/1702915637/solstice-mug-handmade-pottery-mug-with?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;pro=1&amp;etp=1&amp;content_source=PLACEHOLDER&amp;variation0=4807181288" target="etsy.1702915637.4807181288">
<div alt="Blue" class="wt-circle wt-overflow-hidden wt-z-index-1 listing-card-swatch add-transparent-outline smaller hover-effect">
</div>
</a>
<a alt="Dark orange" class="wt-circle wt-overflow-hidden wt-z-index-1 wt-mr-xs-1 swatch-link smaller" data-color-swatch="" data-sr-prefetch="PLACEHOLDER" data-variation-id="4401266233" href=PLACEHOLDER"https://www.etsy.com/listing/1702915637/solstice-mug-handmade-pottery-mug-with?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;pro=1&amp;etp=1&amp;content_source=PLACEHOLDER&amp;variation0=4401266233" target="etsy.1702915637.4401266233">
<div alt="Dark orange" class="wt-circle wt-overflow-hidden wt-z-index-1 listing-card-swatch add-transparent-outline smaller hover-effect">
</div>
</a>
<a alt="Olive green" class="wt-circle wt-overflow-hidden wt-z-index-1 wt-mr-xs-1 swatch-link smaller" data-color-swatch="" data-sr-prefetch="PLACEHOLDER" data-variation-id="4807172242" href=PLACEHOLDER"https://www.etsy.com/listing/1702915637/solstice-mug-handmade-pottery-mug-with?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;pro=1&amp;etp=1&amp;content_source=PLACEHOLDER&amp;variation0=4807172242" target="etsy.1702915637.4807172242">
<div alt="Olive green" class="wt-circle wt-overflow-hidden wt-z-index-1 listing-card-swatch add-transparent-outline smaller hover-effect">
</div>
</a>
<span class="wt-text-grey wt-text-body-smaller swatch-counter">
</span>
</div>
<a aria-label="" class="wt-z-index-1" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="" data-listing-link="" data-logging-key="PLACEHOLDER" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/listing/1702915637/solstice-mug-handmade-pottery-mug-with?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;pro=1&amp;etp=1&amp;content_source=PLACEHOLDER" target="etsy.1702915637">
<h3 class="wt-text-caption v2-listing-card__title wt-text-truncate search-half-unit-mb" id="listing-title-1702915637" title="Solstice Mug: Handmade Pottery Mug with Sun Design, Ceramic Coffee Cup in a Variety of Colors">
                    Solstice Mug: Handmade Pottery Mug with Sun Design, Ceramic Coffee Cup in a Variety of Colors
                </h3>
<div class="streamline-spacing-shop-rating">
<div class="shop-name-with-rating wt-display-flex-xs flex-direction-row-xs wt-align-items-center">
<span class="wt-display-flex-xs wt-flex-nowrap wt-align-items-center larger_review_stars">
<clg-static-review-stars class="him-review-stars wt-pal-grid-mr-xs-050" rating="5.0" review-count-text="(1.1k)" size="smaller" variant="one-star"></clg-static-review-stars>
</span>
<div class="wt-flex-basis-lg-full wt-flex-basis-xl-auto wt star-badge-wrap-spacing wt-width-full min-width-0 wt-mt-xs-0">
<p class="wt-text-caption wt-text-truncate wt-text-gray use_body_small_text wt-text-body-smaller streamline-seller-shop-name__line-height" data-seller-name-container="">
<span aria-hidden="true">
                        By <span class="wt-text-link clickable-shop-name" data-seller-name-link="" data-shop-url="https://www.etsy.com/shop/ShorProducts">ShorProducts</span>
</span>
<span class="wt-screen-reader-only">From shop ShorProducts</span>
</p>
</div>
</div>
</div>
<div class="search-half-unit-mt"></div>
<div class="n-listing-card__price wt-display-block wt-text-title-01 lc-price dense wt-display-inline-block">
<p class="wt-text-slime wt-text-title-01 lc-price dense wt-display-inline-block">
<span class="wt-screen-reader-only">
                            Sale Price $34.87
                        </span>
<span aria-hidden="true">
<span class="currency-symbol">$</span><span class="currency-value">34.87</span>
</span>
</p><p class="wt-text-caption search-collage-promotion-price search-collage-original-price wt-text-slime wt-text-truncate wt-no-wrap">
<span aria-hidden="true" class="wt-text-strikethrough wt-text-black"><span class="currency-symbol">$</span><span class="currency-value">38.75</span></span>
<span class="wt-screen-reader-only">
                                    Original Price $38.75
                                </span>
<span class="wt-text-black">
<span class="wt-text-grey">
                                    (10% off)
                                    </span>
</span>
</p>
<p></p>
</div>
<div class="streamline-spacing-pricing-info streamline-spacing-reduce-margin">
<div class="wt-signal-group wt-signal-group--horizontal" data-clg-id="WtSignalGroup">
<span class="wt-signal wt-signal--generic-subtle" data-clg-id="WtSignal">
  
  Etsy’s Pick
</span>
</div>
</div>
</a>
</div>
</div>
<div class="search-half-unit-mt wt-display-flex-xs wt-flex-wrap wt-align-items-center row-gap-2">
<span class="wt-mr-xs-2"><form action="/cart/listing.php" class="wt-display-inline-block" data-logging-key="PLACEHOLDER" method="post">
<input name="listing_id" type="hidden" value="1702915637"/>
<input name="listing_title" type="hidden" value="Solstice Mug: Handmade Pottery Mug with Sun Design, Ceramic Coffee Cup in a Variety of Colors"/>
<input name="listing_url" type="hidden" value="https://www.etsy.com/listing/1702915637/solstice-mug-handmade-pottery-mug-with"/>
<input name="quantity" type="hidden" value="1"/>
<input name="ref" type="hidden" value="search_lc_cart_og"/>
<input name="show_listing_disclaimer" type="hidden" value="true"/>
<input name="show_cart_edit_panel" type="hidden" value="true"/>
<input name="is_pl" type="hidden" value="false"/>
<input name="listing_source" type="hidden" value="search"/>
<input name="logging_key" type="hidden" value="3d93e4f0-fd4c-4ce1-bd4d-190c14545b56:LT3d2374055c667a7677b246bf7a6c10a92ade8f2f"/>
<input name="listing_image_url" type="hidden" value="https://i.etsystatic.com/19096197/r/il/00b10c/5918841167/il_372x296.5918841167_7pog.jpg"/>
<input name="query" type="hidden" value="handmade mug"/>
<input name="organic_listings_count" type="hidden" value="199067"/>
<input name="formatted_original_price" type="hidden" value="$38.75"/>
<input name="formatted_discounted_price" type="hidden" value="$34.87"/>
<input name="percent_discount" type="hidden" value="10"/>
<input name="placement" type="hidden" value="wsg"/>
<input name="use_listing_title_for_mlt" type="hidden" value="false"/>
<input class="wt-display-none" name="_nnc" type="hidden" value="3:1789025451:61JK-CkysC44MKS-ql9zF9GeOvIg:f00a97a628a07e83a47d2b8a182d321b17dd71017a6ee019d505476df9225210"/>
<clg-button aria-describedby="listing-title-1702915637" background-type="dynamic" class="" data-listing-card-add-to-cart="" hydrated="" size="small" type="submit" variant="secondary" with-submit="">
<clg-icon name="add" size="smaller"></clg-icon>
<span>Add to cart</span>
</clg-button>
</form></span>
<span></span>
<span class="wt-vertical-align-middle"><clg-text-link aria-describedby="listing-title-1702915637" class="refine-by-listing-link wt-text-title-small--tight" data-more-like-this-button="" href=PLACEHOLDER"https://www.etsy.com/r/similar/1702915637/?ref=PLACEHOLDER" icon="end" rel="nofollow" target="_blank" title="More like this">
        More like this
        <clg-icon class="refine-by-listing-link-icon refine-by-listing-link-icon--end" name="rightarrow" size="smaller" slot="icon"></clg-icon>
</clg-text-link></span>
</div>
<div class="v2-listing-card__actions wt-z-index-1 wt-position-absolute" data-favorite-button-wrapper="">

</div>
</div>
</body></html>"""
# The same listing as LISTING_FIXTURE, from the US storefront. Its review
# list is scrubbed the same way. Kept because it pins the fields that are
# TRANSLATED rather than stable (material "Ceramic" vs "Keramik", the
# breadcrumb category) and one that the US page simply does not publish
# (`free_shipping`: its shippingDetails carries a shippingOrigin and NO
# shippingRate, so None is the honest answer rather than a lost read).
US_LISTING_FIXTURE = r"""<html lang="de"><head>
<script type="application/ld+json">{"@type": "Product", "@context": "https://schema.org", "url": "https://www.etsy.com/listing/519688604/handthrown-pottery-mug", "name": "Handthrown Pottery Mug", "sku": "519688604", "description": "14-16 ounces or 10-12 ounces\nbulbous shaped coffee tea mug.  Simple, traditional, elegant.  Blue and Earth green.  Thumb rest.  This is the mug shape and sized most frequently used in our house, from the littlest cousin to the coffee loving French grandmother!  Food safe.  Microwave and dishwasher safe. Each mug varies slightly in size but are approximately 4 inches tall, the base is just over 2 inches and the top opening is just over 3 1/2 inches.", "image": [{"@type": "ImageObject", "@context": "https://schema.org", "author": "StonehousePotteryOH", "contentURL": "https://i.etsystatic.com/8740835/r/il/2537fd/7287100059/il_fullxfull.7287100059_hj1w.jpg", "thumbnail": "https://i.etsystatic.com/8740835/r/il/2537fd/7287100059/il_340x270.7287100059_hj1w.jpg"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "StonehousePotteryOH", "contentURL": "https://i.etsystatic.com/8740835/r/il/d8dd86/7287100067/il_fullxfull.7287100067_i29o.jpg", "thumbnail": "https://i.etsystatic.com/8740835/r/il/d8dd86/7287100067/il_340x270.7287100067_i29o.jpg"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "StonehousePotteryOH", "contentURL": "https://i.etsystatic.com/8740835/r/il/221f3d/1409835265/il_fullxfull.1409835265_erg5.jpg", "thumbnail": "https://i.etsystatic.com/8740835/r/il/221f3d/1409835265/il_340x270.1409835265_erg5.jpg", "description": "Blue and green Handthrown Pottery Mug"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "StonehousePotteryOH", "contentURL": "https://i.etsystatic.com/8740835/r/il/ac3727/2716760440/il_fullxfull.2716760440_dtuf.jpg", "thumbnail": "https://i.etsystatic.com/8740835/r/il/ac3727/2716760440/il_340x270.2716760440_dtuf.jpg", "description": "Blue and green Handthrown Pottery Mug"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "StonehousePotteryOH", "contentURL": "https://i.etsystatic.com/8740835/r/il/2b4ce4/2716760606/il_fullxfull.2716760606_r4r6.jpg", "thumbnail": "https://i.etsystatic.com/8740835/r/il/2b4ce4/2716760606/il_340x270.2716760606_r4r6.jpg", "description": "Blue and green Handthrown Pottery Mug"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "StonehousePotteryOH", "contentURL": "https://i.etsystatic.com/8740835/r/il/12bca6/7287100065/il_fullxfull.7287100065_704l.jpg", "thumbnail": "https://i.etsystatic.com/8740835/r/il/12bca6/7287100065/il_340x270.7287100065_704l.jpg"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "StonehousePotteryOH", "contentURL": "https://i.etsystatic.com/8740835/r/il/019498/7239137826/il_fullxfull.7239137826_gwof.jpg", "thumbnail": "https://i.etsystatic.com/8740835/r/il/019498/7239137826/il_340x270.7239137826_gwof.jpg"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "StonehousePotteryOH", "contentURL": "https://i.etsystatic.com/8740835/r/il/5b0308/7287100063/il_fullxfull.7287100063_jl3a.jpg", "thumbnail": "https://i.etsystatic.com/8740835/r/il/5b0308/7287100063/il_340x270.7287100063_jl3a.jpg"}], "category": "Home & Living < Kitchen & Dining < Drink & Barware < Drinkware < Mugs", "brand": {"@type": "Brand", "@context": "https://schema.org", "name": "StonehousePotteryOH"}, "logo": "https://i.etsystatic.com/8740835/r/isla/f10a52/38851706/isla_500x500.38851706_2x3sl6r2.jpg", "aggregateRating": {"@type": "AggregateRating", "ratingValue": "5.0", "reviewCount": 828}, "offers": {"@type": "Offer", "eligibleQuantity": 15, "price": "31.50", "priceCurrency": "USD", "availability": "https://schema.org/InStock", "shippingDetails": {"@type": "OfferShippingDetails", "shippingOrigin": {"@type": "DefinedRegion", "addressCountry": "US", "addressRegion": "OH"}}}, "review": [{"@type": "Review", "reviewRating": {"@type": "Rating", "ratingValue": 5, "bestRating": 5}, "datePublished": "2026-09-09", "reviewBody": "REDACTED REVIEW TEXT — a real customer wrote here; the structure is kept, the words are not.", "author": {"@type": "Person", "name": "REDACTED REVIEWER"}}, {"@type": "Review", "reviewRating": {"@type": "Rating", "ratingValue": 5, "bestRating": 5}, "datePublished": "2026-09-09", "reviewBody": "REDACTED REVIEW TEXT — a real customer wrote here; the structure is kept, the words are not.", "author": {"@type": "Person", "name": "REDACTED REVIEWER"}}], "material": "Ceramic"}</script>
</head><body>
<img src="https://i.etsystatic.com/x/il_fullxfull.jpg"/>
<div class="wt-display-flex-xs wt-align-items-center wt-flex-wrap appears-ready" data-buy-box-region="price" data-selector="price-only">
<p class="wt-text-title-larger wt-mr-xs-1">
<span class="wt-screen-reader-only">Price:</span>$31.50
    </p>
<div aria-live="assertive" class="wt-spinner wt-spinner--01 wt-display-none" data-buy-box-price-spinner="" data-clg-id="WtSpinner">
<span class="wt-icon"></span>
        Loading
    </div>
</div>
</body></html>"""

# URLs the fixtures were captured from. Kept as constants because several
# checks need the SAME url a fixture came from — `category_from_url` and
# `host_currency` both read it, and a mismatched pair would pin the wrong
# expectation.
SEARCH_URL = "https://www.etsy.com/de/search?q=handmade+mug"
CATEGORY_URL = ("https://www.etsy.com/de/c/home-and-living/kitchen-and-dining"
                "/drink-and-barware/drinkware/mugs")
SHOP_URL = "https://www.etsy.com/de/shop/StonehousePotteryOH"
LISTING_URL = "https://www.etsy.com/de/listing/519688604/handthrown-pottery-mug"
LISTING_VARIATIONS_URL = ("https://www.etsy.com/de/listing/1000079942/"
                          "handgemachter-irisierender-pailletten")
# The US storefront carries NO locale prefix — that is what "the default
# storefront" means on this site, and it is why `locale_of` returns "".
US_SEARCH_URL = "https://www.etsy.com/search?q=handmade+mug"
US_LISTING_URL = "https://www.etsy.com/listing/519688604/handthrown-pottery-mug"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
# Every one of these is a REAL capture, taken 2026-09-09 over the 2Captcha
# Scraping Browser API from a German residential exit — so the prices are in
# EUR and the labels are German, which is what the locale checks below pin.
#
# Trimming: <script> (except application/ld+json), <style>, <svg>,
# <noscript>, <button> and the favourite-button custom element are removed,
# and the srcset/sizes/style attribute soup with them. Tiles are kept WHOLE
# and the JSON-LD is trimmed to the entries for the tiles that remain, so the
# join between the two views is exactly the one Etsy serves.
#
# Faithfulness was VERIFIED, not assumed: every trimmed fixture was parsed
# beside its untrimmed original and every pinned field of every row agreed
# (see the notes in this repo's development log). Re-verify before replacing
# any of them.
#
# The DataDome shells have their `cid`, `hash`, `e` and `cookie` values
# replaced with the literal PLACEHOLDER. Those are per-request session
# material: expired, granting nothing, and still not something to commit —
# a 32-hex-ish blob in a public repo reads as a credential to every scanner
# that looks, including this repo's own. The SHAPE is what the checks need.

# /de/search?q=handmade+mug, page 1. Five tiles, each pinning one thing:
#   1881219122  a SPONSORED tile (ls=a), and a from-price
#   1765159813  organic, with the NEW rating markup (clg-static-review-stars)
#   4361727707  a DISCOUNTED tile: sale + strike + '(20% Rabatt)' in one node
#   4461212869  a from-price ('ab ...') confirmed by structured data
#   4421533207  a plain tile whose price the JSON-LD confirms
SEARCH_FIXTURE = r"""<html lang="de"><head>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "ItemList", "numberOfItems": 71314, "itemListElement": [{"@type": "ListItem", "position": 1, "item": {"@type": "Product", "image": "https://i.etsystatic.com/52168346/r/il/e6077a/8084684510/il_fullxfull.8084684510_ct1x.jpg", "name": "Ladybug Ceramic Mug, Handmade Pottery Coffee Cup, Nature Lover Gift, Stoneware Tea Mug", "url": "https://www.etsy.com/de/listing/1765159813/ladybug-mug-coffee-mug-ceramic-mug-tea", "brand": {"@type": "Brand", "name": "DaNaKeramikShop", "@id": "https://www.etsy.com/de/shop/DaNaKeramikShop#shop"}, "offers": {"@type": "Offer", "price": "54.90", "priceCurrency": "EUR", "availability": "https://schema.org/InStock", "@id": "https://www.etsy.com/de/listing/1765159813/ladybug-mug-coffee-mug-ceramic-mug-tea#offer"}, "@id": "https://www.etsy.com/de/listing/1765159813/ladybug-mug-coffee-mug-ceramic-mug-tea#product"}}, {"@type": "ListItem", "position": 2, "item": {"@type": "Product", "image": "https://i.etsystatic.com/59850037/r/il/4a6bc9/8267596980/il_fullxfull.8267596980_n837.jpg", "name": "Handgemachte Tasse Becher Keramik - 380 ml  - Frühstückstasse- Cappuccino Becher Unikat - Croissanttasse - Kaffeebecher - Keramiktasse", "url": "https://www.etsy.com/de/listing/4421533207/handgemachte-tasse-becher-keramik-400-ml", "brand": {"@type": "Brand", "name": "MuddyRosie", "@id": "https://www.etsy.com/de/shop/MuddyRosie#shop"}, "offers": {"@type": "Offer", "price": "28.00", "priceCurrency": "EUR", "availability": "https://schema.org/InStock", "@id": "https://www.etsy.com/de/listing/4421533207/handgemachte-tasse-becher-keramik-400-ml#offer"}, "@id": "https://www.etsy.com/de/listing/4421533207/handgemachte-tasse-becher-keramik-400-ml#product"}}, {"@type": "ListItem", "position": 3, "item": {"@type": "Product", "image": "https://i.etsystatic.com/59850037/r/il/6a1dac/8217663462/il_fullxfull.8217663462_5r5i.jpg", "name": "Handgemachte Tasse  Keramik - 380 ml  - Frühstückstasse- Cappuccino Becher Unikat - Croissanttasse - Kaffeebecher - Keramiktasse", "url": "https://www.etsy.com/de/listing/4461212869/handgemachte-tasse-keramik-400-ml", "brand": {"@type": "Brand", "name": "MuddyRosie", "@id": "https://www.etsy.com/de/shop/MuddyRosie#shop"}, "offers": {"@type": "Offer", "price": "28.00", "priceCurrency": "EUR", "availability": "https://schema.org/InStock", "@id": "https://www.etsy.com/de/listing/4461212869/handgemachte-tasse-keramik-400-ml#offer"}, "@id": "https://www.etsy.com/de/listing/4461212869/handgemachte-tasse-keramik-400-ml#product"}}, {"@type": "ListItem", "position": 4, "item": {"@type": "Product", "image": "https://i.etsystatic.com/17705718/r/il/73f165/7533232038/il_fullxfull.7533232038_inwr.jpg", "name": "Handgemachter Pilzbecher • Wald-Inspirierte Tasse • Cottagecore Keramik • Größe S oder M • Künstlerische Geschenkidee • Natur Steinzeug", "url": "https://www.etsy.com/de/listing/1881219122/handgemachter-pilzbecher-wald", "brand": {"@type": "Brand", "name": "KuzuArte", "@id": "https://www.etsy.com/de/shop/KuzuArte#shop"}, "offers": {"@type": "Offer", "price": "48.75", "priceCurrency": "EUR", "availability": "https://schema.org/InStock", "@id": "https://www.etsy.com/de/listing/1881219122/handgemachter-pilzbecher-wald#offer", "priceSpecification": {"@type": "UnitPriceSpecification", "priceType": "https://schema.org/ListPrice", "price": "65.00", "priceCurrency": "EUR"}}, "@id": "https://www.etsy.com/de/listing/1881219122/handgemachter-pilzbecher-wald#product"}}, {"@type": "ListItem", "position": 5, "item": {"@type": "Product", "image": "https://i.etsystatic.com/41860268/r/il/a2e894/7820778929/il_fullxfull.7820778929_af7v.jpg", "name": "Handmade ceramic mug with gold accent heart pottery coffee cup gift for her christmas present valentines day gift idea unique handcrafted", "url": "https://www.etsy.com/de/listing/4361727707/handmade-ceramic-mug-with-gold-accent", "brand": {"@type": "Brand", "name": "TheKaloo", "@id": "https://www.etsy.com/de/shop/TheKaloo#shop"}, "offers": {"@type": "Offer", "price": "25.60", "priceCurrency": "EUR", "availability": "https://schema.org/InStock", "@id": "https://www.etsy.com/de/listing/4361727707/handmade-ceramic-mug-with-gold-accent#offer", "priceSpecification": {"@type": "UnitPriceSpecification", "priceType": "https://schema.org/ListPrice", "price": "32.00", "priceCurrency": "EUR"}}, "@id": "https://www.etsy.com/de/listing/4361727707/handmade-ceramic-mug-with-gold-accent#product"}}]}</script>
</head><body>
<img src="https://i.etsystatic.com/x/il_fullxfull.jpg"/>
<div class="js-merch-stash-check-listing v2-listing-card wt-mr-xs-0 search-listing-card--desktop wt-height-full wt-display-flex-xs wt-flex-direction-column-xs wt-justify-content-space-between listing-card-experimental-style appears-ready" data-listing-card-v2="" data-listing-id="1765159813" data-page-type="search" data-palette-listing-id="1765159813" data-shop-id="52168346">
<div class="listing-link wt-display-inline-block b54090be1450263c2" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="1765159813" data-logging-key="PLACEHOLDER" data-palette-listing-image="PLACEHOLDER">
<a aria-label="Ladybug Ceramic Mug, Handmade Pottery Coffee Cup, Nature Lover Gift, Stoneware Tea Mug" class="v2-listing-card__img wt-position-relative listing-card-rounded-corners" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="1765159813" data-listing-link="" data-logging-key="PLACEHOLDER" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/de/listing/1765159813/ladybug-mug-coffee-mug-ceramic-mug-tea?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;frs=1&amp;sts=1&amp;local_signal_search=1&amp;content_source=PLACEHOLDER" target="etsy.1765159813">
<div class="placeholder listing-card-rounded-corners">
<div class="placeholder vertically-centered-placeholder listing-card-rounded-corners">
<img alt="Ladybug Ceramic Mug, Handmade Pottery Coffee Cup, Nature Lover Gift, Stoneware Tea Mug" class="wt-width-full wt-display-block listing-card-rounded-corners sr_gallery-1-1 wt-image--cover wt-image" data-clg-id="WtImage" data-listing-card-listing-image="" data-preload-lp-src="https://i.etsystatic.com/52168346/r/il/e6077a/8084684510/il_794xN.8084684510_ct1x.jpg" data-preload-lp-srcset="https://i.etsystatic.com/52168346/r/il/e6077a/8084684510/il_794xN.8084684510_ct1x.jpg 1x, https://i.etsystatic.com/52168346/r/il/e6077a/8084684510/il_1588xN.8084684510_ct1x.jpg 2x" src="https://i.etsystatic.com/52168346/r/il/e6077a/8084684510/il_255x319.8084684510_ct1x.jpg"/>
</div>
</div>
</a>
<div class="v2-listing-card__info wt-pt-xs-0">
<a aria-label="" class="wt-z-index-1" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="" data-listing-link="" data-logging-key="PLACEHOLDER" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/de/listing/1765159813/ladybug-mug-coffee-mug-ceramic-mug-tea?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;frs=1&amp;sts=1&amp;local_signal_search=1&amp;content_source=PLACEHOLDER" target="etsy.1765159813">
<h3 class="wt-text-caption v2-listing-card__title wt-text-truncate search-half-unit-mt wt-mt-xs-1 search-half-unit-mb" id="listing-title-1765159813" title="Ladybug Ceramic Mug, Handmade Pottery Coffee Cup, Nature Lover Gift, Stoneware Tea Mug">
                    Ladybug Ceramic Mug, Handmade Pottery Coffee Cup, Nature Lover Gift, Stoneware Tea Mug
                </h3>
<div class="streamline-spacing-shop-rating">
<div class="shop-name-with-rating wt-display-flex-xs flex-direction-row-xs wt-align-items-center">
<span class="wt-display-flex-xs wt-flex-nowrap wt-align-items-center larger_review_stars">
<clg-static-review-stars class="him-review-stars wt-pal-grid-mr-xs-050" rating="5.0" review-count-text="(495)" size="smaller" variant="one-star"></clg-static-review-stars>
</span>
<div class="wt-flex-basis-lg-full wt-flex-basis-xl-auto wt star-badge-wrap-spacing wt-width-full min-width-0 wt-mt-xs-0">
<p class="wt-text-caption wt-text-truncate wt-text-gray wt-text-body-smaller streamline-seller-shop-name__line-height" data-seller-name-container="">
<span aria-hidden="true">
</span></p><div class="listing-card-tooltip-container" data-selector="popover-container">
<div class="wt-popover wt-text-caption wt-text-grey wt-align-items-center wt-vertical-align-middle wt-display-flex-xs wt-flex-nowrap" data-selector="popover-replacement" data-wt-popover="">

<div class="listing-card-tooltip wt-text-center-xs" id="listing-card-tooltip-shop-name-1765159813" role="tooltip">
<p class="wt-text-caption wt-no-wrap wt-mb-xs-0 wt-display-block wt-overflow-hidden tooltip-content-row">
<span class="wt-display-inline wt-no-wrap">Hergestellt von <strong class="wt-display-inline wt-no-wrap">DaNaKeramikShop</strong></span>
</p>
<p class="wt-text-caption wt-no-wrap wt-mb-xs-0 wt-display-block wt-overflow-hidden tooltip-content-row">
<span class="wt-display-block wt-no-wrap">Shop im Besitz von <strong>Mira</strong></span>
</p>
<p class="wt-text-caption wt-no-wrap wt-mb-xs-0 wt-display-block wt-overflow-hidden tooltip-content-row">
<span class="wt-display-block wt-no-wrap">2 Jahre auf Etsy</span>
</p>
<span class="wt-popover__arrow"></span>
</div>
</div>
</div>
<span class="wt-screen-reader-only">Aus dem Shop DaNaKeramikShop</span>
<p></p>
</div>
</div>
</div>
<div class="search-half-unit-mt"></div>
<div class="n-listing-card__price wt-display-block wt-text-title-01 lc-price dense wt-display-inline-block">
<p class="wt-text-title-01 lc-price dense wt-display-inline-block">
<span class="currency-value">54,90</span> <span class="currency-symbol">€</span>
</p>
</div>
<div class="streamline-spacing-pricing-info">
<div class="promotion-badge-line wt-display-flex-xs search-collage-original-price">
<p class="wt-text-truncate streamline-reduce-line-height">
<span class="wt-text-black wt-text-body-smaller search-collage-original-price">
    Kostenloser Versand
</span>
</p>
</div>
</div>
<span class="wt-display-flex-xs wt-align-items-center wt-mb-xs-1">
<clg-icon class="wt-sem-text-action" name="location" size="smaller"></clg-icon>
<p class="wt-text-body-smaller lc-signal-bold wt-text-grey wt-display-inline-block">
        Versand aus DE
    </p>
</span>
</a>
</div>
</div>
<div class="search-half-unit-mt wt-display-flex-xs wt-flex-wrap wt-align-items-center row-gap-2">
<span class="wt-mr-xs-2"><form action="/de/cart/listing.php" class="wt-display-inline-block" data-logging-key="PLACEHOLDER" method="post">
<input name="listing_id" type="hidden" value="1765159813"/>
<input name="listing_title" type="hidden" value="Ladybug Ceramic Mug, Handmade Pottery Coffee Cup, Nature Lover Gift, Stoneware Tea Mug"/>
<input name="listing_url" type="hidden" value="https://www.etsy.com/de/listing/1765159813/ladybug-mug-coffee-mug-ceramic-mug-tea"/>
<input name="quantity" type="hidden" value="1"/>
<input name="ref" type="hidden" value="search_lc_cart_og"/>
<input name="show_listing_disclaimer" type="hidden" value="true"/>
<input name="show_cart_edit_panel" type="hidden" value="true"/>
<input name="is_pl" type="hidden" value="false"/>
<input name="listing_source" type="hidden" value="search"/>
<input name="logging_key" type="hidden" value="a55d4358-9d21-4dd3-9ae9-cf5fc18d4e9f:LT43ad870261019a474ef0b61f10c75dd367d53168"/>
<input class="wt-display-none" name="_nnc" type="hidden" value="3:1788988829:Cy5difvE-AGnVFa56uHH75g8urAW:23e9c8f4bd222dafc0e4177093e9fd902c281bce7b44b59ffe550cb1e29cfc48"/>
<clg-button aria-describedby="listing-title-1765159813" background-type="dynamic" class="" data-listing-card-add-to-cart="" hydrated="" size="small" type="submit" variant="secondary" with-submit="">
<clg-icon name="add" size="smaller"></clg-icon>
<span>Ab in den Warenkorb</span>
</clg-button>
</form></span>
<span></span>
<span class="wt-vertical-align-middle"><clg-text-link aria-describedby="listing-title-1765159813" class="refine-by-listing-link wt-text-title-small--tight" data-more-like-this-button="" href=PLACEHOLDER"https://www.etsy.com/de/r/similar/1765159813/?ref=PLACEHOLDER" icon="end" rel="nofollow" target="_blank" title="Ähnliche Artikel">
        Ähnliche Artikel
        <clg-icon class="refine-by-listing-link-icon refine-by-listing-link-icon--end" name="rightarrow" size="smaller" slot="icon"></clg-icon>
</clg-text-link></span>
</div>
<div class="v2-listing-card__actions wt-z-index-1 wt-position-absolute" data-favorite-button-wrapper="">

</div>
</div>
<div class="js-merch-stash-check-listing v2-listing-card wt-mr-xs-0 search-listing-card--desktop wt-height-full wt-display-flex-xs wt-flex-direction-column-xs wt-justify-content-space-between listing-card-experimental-style appears-ready" data-listing-card-v2="" data-listing-id="1881219122" data-page-type="search" data-palette-listing-id="1881219122" data-shop-id="17705718">
<div class="listing-link wt-display-inline-block b99a13bca9da6c75a" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="1881219122" data-logging-key="PLACEHOLDER" data-palette-listing-image="PLACEHOLDER" data-shop-id="17705718">
<a aria-label="Handgemachter Pilzbecher • Wald-Inspirierte Tasse • Cottagecore Keramik • Größe S oder M • Künstlerische Geschenkidee • Natur Steinzeug" class="v2-listing-card__img wt-position-relative listing-card-rounded-corners" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="1881219122" data-listing-link="" data-logging-key="PLACEHOLDER" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/de/listing/1881219122/handgemachter-pilzbecher-wald?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=a&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;frs=1&amp;local_signal_search=1" target="etsy.1881219122">
<div class="placeholder listing-card-rounded-corners" tabindex="-1">
<div class="placeholder vertically-centered-placeholder listing-card-rounded-corners">
<img alt="Handgemachter Pilzbecher • Wald-Inspirierte Tasse • Cottagecore Keramik • Größe S oder M • Künstlerische Geschenkidee • Natur Steinzeug" class="wt-width-full wt-display-block listing-card-rounded-corners sc_gallery-1-1 wt-image--cover wt-image" data-clg-id="WtImage" data-listing-card-listing-image="" data-preload-lp-src="https://i.etsystatic.com/17705718/r/il/73f165/7533232038/il_794xN.7533232038_inwr.jpg" data-preload-lp-srcset="https://i.etsystatic.com/17705718/r/il/73f165/7533232038/il_794xN.7533232038_inwr.jpg 1x, https://i.etsystatic.com/17705718/r/il/73f165/7533232038/il_1588xN.7533232038_inwr.jpg 2x" src="https://i.etsystatic.com/17705718/r/il/73f165/7533232038/il_255x319.7533232038_inwr.jpg"/>
</div>
<div aria-hidden="true" class="listing-card-video-spinner wt-align-items-center wt-display-none wt-position-absolute wt-position-top wt-position-bottom wt-position-left wt-position-right">
<div class="wt-spinner wt-spinner--01">
<span class="etsy-icon"></span>
        Loading
    </div>
</div>
<div class="listing-card-video-container wt-animated wt-animated--disappear-01 wt-position-absolute wt-position-top wt-position-bottom wt-position-left wt-position-right listing-card-rounded-corners">

</div>
<div aria-hidden="false" class="listing-card-video-signal wt-position-absolute wt-circle wt-overflow-hidden wt-sem-bg-elevation-0">
<clg-icon class="wt-horizontal-center wt-vertical-center" name="play" size="smaller"></clg-icon>
</div>
</div>
</a>
<div class="v2-listing-card__info wt-mt-xs-1 wt-pt-xs-0">
<div class="wt-display-flex-xs wt-align-items-center search-half-unit-my swatch-container">
<a alt="Dunkelblau" class="wt-circle wt-overflow-hidden wt-z-index-1 wt-mr-xs-1 swatch-link smaller" data-color-swatch="" data-sr-prefetch="PLACEHOLDER" data-variation-id="5237375692" href=PLACEHOLDER"https://www.etsy.com/de/listing/1881219122/handgemachter-pilzbecher-wald?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=a&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;frs=1&amp;local_signal_search=1&amp;variation0=5237375692" target="etsy.1881219122.5237375692">
<div alt="Dunkelblau" class="wt-circle wt-overflow-hidden wt-z-index-1 listing-card-swatch add-transparent-outline smaller hover-effect">
</div>
</a>
<a alt="Dunkelgrün" class="wt-circle wt-overflow-hidden wt-z-index-1 wt-mr-xs-1 swatch-link smaller" data-color-swatch="" data-sr-prefetch="PLACEHOLDER" data-variation-id="6021291086" href=PLACEHOLDER"https://www.etsy.com/de/listing/1881219122/handgemachter-pilzbecher-wald?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=a&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;frs=1&amp;local_signal_search=1&amp;variation0=6021291086" target="etsy.1881219122.6021291086">
<div alt="Dunkelgrün" class="wt-circle wt-overflow-hidden wt-z-index-1 listing-card-swatch add-transparent-outline smaller hover-effect">
</div>
</a>
<a alt="Gebrochen weiß" class="wt-circle wt-overflow-hidden wt-z-index-1 wt-mr-xs-1 swatch-link smaller" data-color-swatch="" data-sr-prefetch="PLACEHOLDER" data-variation-id="5237375684" href=PLACEHOLDER"https://www.etsy.com/de/listing/1881219122/handgemachter-pilzbecher-wald?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=a&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;frs=1&amp;local_signal_search=1&amp;variation0=5237375684" target="etsy.1881219122.5237375684">
<div alt="Gebrochen weiß" class="wt-circle wt-overflow-hidden wt-z-index-1 listing-card-swatch add-transparent-outline smaller hover-effect">
</div>
</a>
<span class="wt-text-grey wt-text-body-smaller swatch-counter">
</span>
</div>
<a aria-label="" class="wt-z-index-1" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="" data-listing-link="" data-logging-key="PLACEHOLDER" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/de/listing/1881219122/handgemachter-pilzbecher-wald?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=a&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;frs=1&amp;local_signal_search=1" target="etsy.1881219122">
<h3 class="wt-text-caption v2-listing-card__title wt-text-truncate search-half-unit-mb" id="ad-listing-title-1881219122" title="Handgemachter Pilzbecher • Wald-Inspirierte Tasse • Cottagecore Keramik • Größe S oder M • Künstlerische Geschenkidee • Natur Steinzeug">
                    Handgemachter Pilzbecher • Wald-Inspirierte Tasse • Cottagecore Keramik • Größe S oder M • Künstlerische Geschenkidee • Natur Steinzeug
                </h3>
<div class="streamline-spacing-shop-rating">
<div class="shop-name-with-rating wt-display-flex-xs flex-direction-row-xs wt-align-items-center">
<span class="wt-display-flex-xs wt-flex-nowrap wt-align-items-center larger_review_stars">
<clg-static-review-stars class="him-review-stars wt-pal-grid-mr-xs-050" rating="4.9" review-count-text="(1,4 Tsd.)" size="smaller" variant="one-star"></clg-static-review-stars>
</span>
<div class="wt-flex-basis-lg-full wt-flex-basis-xl-auto wt star-badge-wrap-spacing wt-width-full min-width-0 wt-mt-xs-0">
<p class="wt-text-caption wt-text-truncate wt-text-gray wt-text-body-smaller streamline-seller-shop-name__line-height" data-seller-name-container="">
<span aria-hidden="true">
</span></p><div class="listing-card-tooltip-container" data-selector="popover-container">
<div class="wt-popover wt-text-caption wt-text-grey wt-align-items-center wt-vertical-align-middle wt-display-flex-xs wt-flex-nowrap" data-selector="popover-replacement" data-wt-popover="">

<div class="listing-card-tooltip wt-text-center-xs" id="listing-card-tooltip-shop-name-1881219122" role="tooltip">
<p class="wt-text-caption wt-no-wrap wt-mb-xs-0 wt-display-block wt-overflow-hidden tooltip-content-row">
<span class="wt-display-inline wt-no-wrap">Von <strong class="wt-display-inline wt-no-wrap">KuzuArte</strong></span>
</p>
<p class="wt-text-caption wt-no-wrap wt-mb-xs-0 wt-display-block wt-overflow-hidden tooltip-content-row">
<span class="wt-display-block wt-no-wrap">Shop im Besitz von <strong>Sophie</strong></span>
</p>
<p class="wt-text-caption wt-no-wrap wt-mb-xs-0 wt-display-block wt-overflow-hidden tooltip-content-row">
<span class="wt-display-block wt-no-wrap">8 Jahre auf Etsy</span>
</p>
<span class="wt-popover__arrow"></span>
</div>
</div>
</div>
<span class="wt-screen-reader-only">Anzeige des Shops KuzuArte</span>
<p></p>
</div>
</div>
</div>
<div class="search-half-unit-mt"></div>
<div class="n-listing-card__price wt-display-block wt-text-title-01 lc-price dense wt-display-inline-block">
<p class="wt-text-title-01 lc-price dense wt-display-inline-block">
                    ab <span class="currency-value">65,00</span> <span class="currency-symbol">€</span>
</p>
</div>
<div class="streamline-spacing-pricing-info">
<p class="wt-text-truncate streamline-reduce-line-height">
<span class="wt-text-black wt-text-body-smaller search-collage-original-price">
    Kostenloser Versand
</span>
</p>
</div>
<span class="wt-display-flex-xs wt-align-items-center wt-mb-xs-1">
<clg-icon class="wt-sem-text-action local-signal-icon-reduce-left-margin" name="location" size="smaller"></clg-icon>
<p class="wt-text-body-smaller lc-signal-bold wt-text-grey wt-display-inline-block">
        Versand aus DE
    </p>
</span>
</a>
</div>
</div>
<div class="search-half-unit-mt wt-display-flex-xs wt-flex-wrap wt-align-items-center row-gap-2">
<span class="wt-mr-xs-2"><form action="/de/cart/listing.php" class="wt-display-inline-block" data-logging-key="PLACEHOLDER" method="post">
<input name="listing_id" type="hidden" value="1881219122"/>
<input name="listing_title" type="hidden" value="Handgemachter Pilzbecher • Wald-Inspirierte Tasse • Cottagecore Keramik • Größe S oder M • Künstlerische Geschenkidee • Natur Steinzeug"/>
<input name="listing_url" type="hidden" value="https://www.etsy.com/de/listing/1881219122/handgemachter-pilzbecher-wald"/>
<input name="quantity" type="hidden" value="1"/>
<input name="ref" type="hidden" value="search_lc_cart_pl"/>
<input name="show_listing_disclaimer" type="hidden" value="true"/>
<input name="show_cart_edit_panel" type="hidden" value="true"/>
<input name="is_pl" type="hidden" value="true"/>
<input name="listing_source" type="hidden" value="ads"/>
<input name="logging_key" type="hidden" value="Eu0YL4BwxX9-YEy_ngjgGRsS9-e0:LT1903ea987155c5347a5b3121fc45bd5cb9cb7d89"/>
<input class="wt-display-none" name="_nnc" type="hidden" value="3:1788988829:pkqzzggzf8XdtnNffoYLncesfQ6v:34be94347d8b38d637cc039db647c403db3247a7fbb3835485135e19772f407c"/>
<clg-button aria-describedby="ad-listing-title-1881219122" background-type="dynamic" class="" data-listing-card-add-to-cart="" hydrated="" size="small" type="submit" variant="secondary" with-submit="">
<clg-icon name="add" size="smaller"></clg-icon>
<span>Ab in den Warenkorb</span>
</clg-button>
</form></span>
<span></span>
<span class="wt-vertical-align-middle"><clg-text-link aria-describedby="ad-listing-title-1881219122" class="refine-by-listing-link wt-text-title-small--tight" data-more-like-this-button="" href=PLACEHOLDER"https://www.etsy.com/de/r/similar/1881219122/?ref=PLACEHOLDER" icon="end" rel="nofollow" target="_blank" title="Ähnliche Artikel">
        Ähnliche Artikel
        <clg-icon class="refine-by-listing-link-icon refine-by-listing-link-icon--end" name="rightarrow" size="smaller" slot="icon"></clg-icon>
</clg-text-link></span>
</div>
<div class="v2-listing-card__actions wt-z-index-1 wt-position-absolute" data-favorite-button-wrapper="">

</div>
</div>
<div class="js-merch-stash-check-listing v2-listing-card wt-mr-xs-0 search-listing-card--desktop wt-height-full wt-display-flex-xs wt-flex-direction-column-xs wt-justify-content-space-between listing-card-experimental-style appears-ready" data-listing-card-v2="" data-listing-id="4421533207" data-page-type="search" data-palette-listing-id="4421533207" data-shop-id="59850037">
<div class="listing-link wt-display-inline-block b54090be1450263c2" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="4421533207" data-logging-key="PLACEHOLDER" data-palette-listing-image="PLACEHOLDER">
<a aria-label="Handgemachte Tasse Becher Keramik - 380 ml  - Frühstückstasse- Cappuccino Becher Unikat - Croissanttasse - Kaffeebecher - Keramiktasse" class="v2-listing-card__img wt-position-relative listing-card-rounded-corners" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="4421533207" data-listing-link="" data-logging-key="PLACEHOLDER" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/de/listing/4421533207/handgemachte-tasse-becher-keramik-400-ml?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;bes=PLACEHOLDER&amp;cns=1&amp;sts=1&amp;local_signal_search=1&amp;content_source=PLACEHOLDER" target="etsy.4421533207">
<div class="placeholder listing-card-rounded-corners" tabindex="-1">
<div class="placeholder vertically-centered-placeholder listing-card-rounded-corners">
<img alt="Handgemachte Tasse Becher Keramik - 380 ml  - Frühstückstasse- Cappuccino Becher Unikat - Croissanttasse - Kaffeebecher - Keramiktasse" class="wt-width-full wt-display-block listing-card-rounded-corners sr_gallery-1-2 wt-image--cover wt-image" data-clg-id="WtImage" data-listing-card-listing-image="" data-preload-lp-src="https://i.etsystatic.com/59850037/r/il/4a6bc9/8267596980/il_794xN.8267596980_n837.jpg" data-preload-lp-srcset="https://i.etsystatic.com/59850037/r/il/4a6bc9/8267596980/il_794xN.8267596980_n837.jpg 1x, https://i.etsystatic.com/59850037/r/il/4a6bc9/8267596980/il_1588xN.8267596980_n837.jpg 2x" src="https://i.etsystatic.com/59850037/r/il/4a6bc9/8267596980/il_255x319.8267596980_n837.jpg"/>
</div>
<div aria-hidden="true" class="listing-card-video-spinner wt-align-items-center wt-display-none wt-position-absolute wt-position-top wt-position-bottom wt-position-left wt-position-right">
<div class="wt-spinner wt-spinner--01">
<span class="etsy-icon"></span>
        Loading
    </div>
</div>
<div class="listing-card-video-container wt-animated wt-animated--disappear-01 wt-position-absolute wt-position-top wt-position-bottom wt-position-left wt-position-right listing-card-rounded-corners">

</div>
<div aria-hidden="false" class="listing-card-video-signal wt-position-absolute wt-circle wt-overflow-hidden wt-sem-bg-elevation-0">
<clg-icon class="wt-horizontal-center wt-vertical-center" name="play" size="smaller"></clg-icon>
</div>
<span class="wt-position-absolute ranked-badges-position">
<clg-signal color="neutral" size="large">            

    Bestseller
</clg-signal>
</span>
</div>
</a>
<div class="v2-listing-card__info wt-pt-xs-0">
<a aria-label="" class="wt-z-index-1" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="" data-listing-link="" data-logging-key="PLACEHOLDER" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/de/listing/4421533207/handgemachte-tasse-becher-keramik-400-ml?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;bes=PLACEHOLDER&amp;cns=1&amp;sts=1&amp;local_signal_search=1&amp;content_source=PLACEHOLDER" target="etsy.4421533207">
<h3 class="wt-text-caption v2-listing-card__title wt-text-truncate search-half-unit-mt wt-mt-xs-1 search-half-unit-mb" id="listing-title-4421533207" title="Handgemachte Tasse Becher Keramik - 380 ml  - Frühstückstasse- Cappuccino Becher Unikat - Croissanttasse - Kaffeebecher - Keramiktasse">
                    Handgemachte Tasse Becher Keramik - 380 ml  - Frühstückstasse- Cappuccino Becher Unikat - Croissanttasse - Kaffeebecher - Keramiktasse
                </h3>
<div class="streamline-spacing-shop-rating">
<div class="shop-name-with-rating wt-display-flex-xs flex-direction-row-xs wt-align-items-center">
<span class="wt-display-flex-xs wt-flex-nowrap wt-align-items-center larger_review_stars">
<clg-static-review-stars class="him-review-stars wt-pal-grid-mr-xs-050" rating="5.0" review-count-text="(224)" size="smaller" variant="one-star"></clg-static-review-stars>
</span>
<div class="wt-flex-basis-lg-full wt-flex-basis-xl-auto wt star-badge-wrap-spacing wt-width-full min-width-0 wt-mt-xs-0">
<p class="wt-text-caption wt-text-truncate wt-text-gray wt-text-body-smaller streamline-seller-shop-name__line-height" data-seller-name-container="">
<span aria-hidden="true">
</span></p><div class="listing-card-tooltip-container" data-selector="popover-container">
<div class="wt-popover wt-text-caption wt-text-grey wt-align-items-center wt-vertical-align-middle wt-display-flex-xs wt-flex-nowrap" data-selector="popover-replacement" data-wt-popover="">

<div class="listing-card-tooltip wt-text-center-xs" id="listing-card-tooltip-shop-name-4421533207" role="tooltip">
<p class="wt-text-caption wt-no-wrap wt-mb-xs-0 wt-display-block wt-overflow-hidden tooltip-content-row">
<span class="wt-display-inline wt-no-wrap">Hergestellt von <strong class="wt-display-inline wt-no-wrap">MuddyRosie</strong></span>
</p>
<p class="wt-text-caption wt-no-wrap wt-mb-xs-0 wt-display-block wt-overflow-hidden tooltip-content-row">
<span class="wt-display-block wt-no-wrap">Shop im Besitz von <strong>Rosie</strong></span>
</p>
<p class="wt-text-caption wt-no-wrap wt-mb-xs-0 wt-display-block wt-overflow-hidden tooltip-content-row">
<span class="wt-display-block wt-no-wrap">9 Monate auf Etsy</span>
</p>
<span class="wt-popover__arrow"></span>
</div>
</div>
</div>
<span class="wt-screen-reader-only">Aus dem Shop MuddyRosie</span>
<p></p>
</div>
</div>
</div>
<div class="search-half-unit-mt"></div>
<div class="n-listing-card__price wt-display-block wt-text-title-01 lc-price dense wt-display-inline-block">
<p class="wt-text-title-01 lc-price dense wt-display-inline-block">
<span class="currency-value">28,00</span> <span class="currency-symbol">€</span>
</p>
</div>
<div class="streamline-spacing-pricing-info streamline-spacing-reduce-margin">
<div class="wt-text-brick use-smallest-text wt-text-title-smallest">
    Begrenzte Verfügbarkeit
</div>
</div>
<span class="wt-display-flex-xs wt-align-items-center wt-mb-xs-1">
<clg-icon class="wt-sem-text-action" name="location" size="smaller"></clg-icon>
<p class="wt-text-body-smaller lc-signal-bold wt-text-grey wt-display-inline-block">
        Versand aus DE
    </p>
</span>
</a>
</div>
</div>
<div class="search-half-unit-mt wt-display-flex-xs wt-flex-wrap wt-align-items-center row-gap-2">
<span class="wt-mr-xs-2"><form action="/de/cart/listing.php" class="wt-display-inline-block" data-logging-key="PLACEHOLDER" method="post">
<input name="listing_id" type="hidden" value="4421533207"/>
<input name="listing_title" type="hidden" value="Handgemachte Tasse Becher Keramik - 380 ml  - Frühstückstasse- Cappuccino Becher Unikat - Croissanttasse - Kaffeebecher - Keramiktasse"/>
<input name="listing_url" type="hidden" value="https://www.etsy.com/de/listing/4421533207/handgemachte-tasse-becher-keramik-400-ml"/>
<input name="quantity" type="hidden" value="1"/>
<input name="ref" type="hidden" value="search_lc_cart_og"/>
<input name="show_listing_disclaimer" type="hidden" value="true"/>
<input name="show_cart_edit_panel" type="hidden" value="true"/>
<input name="is_pl" type="hidden" value="false"/>
<input name="listing_source" type="hidden" value="search"/>
<input name="logging_key" type="hidden" value="a55d4358-9d21-4dd3-9ae9-cf5fc18d4e9f:LT9fc528447098c0f4a65ea08b57c31331b5e464e1"/>
<input class="wt-display-none" name="_nnc" type="hidden" value="3:1788988829:TCk0MXuPf6xYOBlxFpt27T_2jqUR:d673d84f0be5f87dda20d067b4ba208e356e1075a9ebcf3d747c04f5ad09cde1"/>
<clg-button aria-describedby="listing-title-4421533207" background-type="dynamic" class="" data-listing-card-add-to-cart="" hydrated="" size="small" type="submit" variant="secondary" with-submit="">
<clg-icon name="add" size="smaller"></clg-icon>
<span>Ab in den Warenkorb</span>
</clg-button>
</form></span>
<span></span>
<span class="wt-vertical-align-middle"><clg-text-link aria-describedby="listing-title-4421533207" class="refine-by-listing-link wt-text-title-small--tight" data-more-like-this-button="" href=PLACEHOLDER"https://www.etsy.com/de/r/similar/4421533207/?ref=PLACEHOLDER" icon="end" rel="nofollow" target="_blank" title="Ähnliche Artikel">
        Ähnliche Artikel
        <clg-icon class="refine-by-listing-link-icon refine-by-listing-link-icon--end" name="rightarrow" size="smaller" slot="icon"></clg-icon>
</clg-text-link></span>
</div>
<div class="v2-listing-card__actions wt-z-index-1 wt-position-absolute" data-favorite-button-wrapper="">

</div>
</div>
<div class="js-merch-stash-check-listing v2-listing-card wt-mr-xs-0 search-listing-card--desktop wt-height-full wt-display-flex-xs wt-flex-direction-column-xs wt-justify-content-space-between listing-card-experimental-style appears-ready" data-listing-card-v2="" data-listing-id="4461212869" data-page-type="search" data-palette-listing-id="4461212869" data-shop-id="59850037">
<div class="listing-link wt-display-inline-block b54090be1450263c2" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="4461212869" data-logging-key="PLACEHOLDER" data-palette-listing-image="PLACEHOLDER">
<a aria-label="Handgemachte Tasse  Keramik - 380 ml  - Frühstückstasse- Cappuccino Becher Unikat - Croissanttasse - Kaffeebecher - Keramiktasse" class="v2-listing-card__img wt-position-relative listing-card-rounded-corners" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="4461212869" data-listing-link="" data-logging-key="PLACEHOLDER" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/de/listing/4461212869/handgemachte-tasse-keramik-400-ml?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;bes=PLACEHOLDER&amp;sts=1&amp;local_signal_search=1&amp;content_source=PLACEHOLDER" target="etsy.4461212869">
<div class="placeholder listing-card-rounded-corners">
<div class="placeholder vertically-centered-placeholder listing-card-rounded-corners">
<img alt="Handgemachte Tasse  Keramik - 380 ml  - Frühstückstasse- Cappuccino Becher Unikat - Croissanttasse - Kaffeebecher - Keramiktasse" class="wt-width-full wt-display-block listing-card-rounded-corners sr_gallery-1-3 wt-image--cover wt-image" data-clg-id="WtImage" data-listing-card-listing-image="" data-preload-lp-src="https://i.etsystatic.com/59850037/r/il/6a1dac/8217663462/il_794xN.8217663462_5r5i.jpg" data-preload-lp-srcset="https://i.etsystatic.com/59850037/r/il/6a1dac/8217663462/il_794xN.8217663462_5r5i.jpg 1x, https://i.etsystatic.com/59850037/r/il/6a1dac/8217663462/il_1588xN.8217663462_5r5i.jpg 2x" src="https://i.etsystatic.com/59850037/r/il/6a1dac/8217663462/il_255x319.8217663462_5r5i.jpg"/>
</div>
<span class="wt-position-absolute ranked-badges-position">
<clg-signal color="neutral" size="large">            

    Bestseller
</clg-signal>
</span>
</div>
</a>
<div class="v2-listing-card__info wt-mt-xs-1 wt-pt-xs-0">
<div class="wt-display-flex-xs wt-align-items-center search-half-unit-my swatch-container">
<a alt="Rosa" class="wt-circle wt-overflow-hidden wt-z-index-1 wt-mr-xs-1 swatch-link smaller" data-color-swatch="" data-sr-prefetch="PLACEHOLDER" data-variation-id="6335311048" href=PLACEHOLDER"https://www.etsy.com/de/listing/4461212869/handgemachte-tasse-keramik-400-ml?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;bes=PLACEHOLDER&amp;sts=1&amp;local_signal_search=1&amp;content_source=PLACEHOLDER" target="etsy.4461212869.6335311048">
<div alt="Rosa" class="wt-circle wt-overflow-hidden wt-z-index-1 listing-card-swatch add-transparent-outline smaller hover-effect">
</div>
</a>
<a alt="Weiß" class="wt-circle wt-overflow-hidden wt-z-index-1 wt-mr-xs-1 swatch-link smaller" data-color-swatch="" data-sr-prefetch="PLACEHOLDER" data-variation-id="6335311046" href=PLACEHOLDER"https://www.etsy.com/de/listing/4461212869/handgemachte-tasse-keramik-400-ml?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;bes=PLACEHOLDER&amp;sts=1&amp;local_signal_search=1&amp;content_source=PLACEHOLDER" target="etsy.4461212869.6335311046">
<div alt="Weiß" class="wt-circle wt-overflow-hidden wt-z-index-1 listing-card-swatch add-transparent-outline smaller hover-effect">
</div>
</a>
<span class="wt-text-grey wt-text-body-smaller swatch-counter">
</span>
</div>
<a aria-label="" class="wt-z-index-1" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="" data-listing-link="" data-logging-key="PLACEHOLDER" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/de/listing/4461212869/handgemachte-tasse-keramik-400-ml?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;bes=PLACEHOLDER&amp;sts=1&amp;local_signal_search=1&amp;content_source=PLACEHOLDER" target="etsy.4461212869">
<h3 class="wt-text-caption v2-listing-card__title wt-text-truncate search-half-unit-mb" id="listing-title-4461212869" title="Handgemachte Tasse  Keramik - 380 ml  - Frühstückstasse- Cappuccino Becher Unikat - Croissanttasse - Kaffeebecher - Keramiktasse">
                    Handgemachte Tasse  Keramik - 380 ml  - Frühstückstasse- Cappuccino Becher Unikat - Croissanttasse - Kaffeebecher - Keramiktasse
                </h3>
<div class="streamline-spacing-shop-rating">
<div class="shop-name-with-rating wt-display-flex-xs flex-direction-row-xs wt-align-items-center">
<span class="wt-display-flex-xs wt-flex-nowrap wt-align-items-center larger_review_stars">
<clg-static-review-stars class="him-review-stars wt-pal-grid-mr-xs-050" rating="5.0" review-count-text="(224)" size="smaller" variant="one-star"></clg-static-review-stars>
</span>
<div class="wt-flex-basis-lg-full wt-flex-basis-xl-auto wt star-badge-wrap-spacing wt-width-full min-width-0 wt-mt-xs-0">
<p class="wt-text-caption wt-text-truncate wt-text-gray wt-text-body-smaller streamline-seller-shop-name__line-height" data-seller-name-container="">
<span aria-hidden="true">
</span></p><div class="listing-card-tooltip-container" data-selector="popover-container">
<div class="wt-popover wt-text-caption wt-text-grey wt-align-items-center wt-vertical-align-middle wt-display-flex-xs wt-flex-nowrap" data-selector="popover-replacement" data-wt-popover="">

<div class="listing-card-tooltip wt-text-center-xs" id="listing-card-tooltip-shop-name-4461212869" role="tooltip">
<p class="wt-text-caption wt-no-wrap wt-mb-xs-0 wt-display-block wt-overflow-hidden tooltip-content-row">
<span class="wt-display-inline wt-no-wrap">Hergestellt von <strong class="wt-display-inline wt-no-wrap">MuddyRosie</strong></span>
</p>
<p class="wt-text-caption wt-no-wrap wt-mb-xs-0 wt-display-block wt-overflow-hidden tooltip-content-row">
<span class="wt-display-block wt-no-wrap">Shop im Besitz von <strong>Rosie</strong></span>
</p>
<p class="wt-text-caption wt-no-wrap wt-mb-xs-0 wt-display-block wt-overflow-hidden tooltip-content-row">
<span class="wt-display-block wt-no-wrap">9 Monate auf Etsy</span>
</p>
<span class="wt-popover__arrow"></span>
</div>
</div>
</div>
<span class="wt-screen-reader-only">Aus dem Shop MuddyRosie</span>
<p></p>
</div>
</div>
</div>
<div class="search-half-unit-mt"></div>
<div class="n-listing-card__price wt-display-block wt-text-title-01 lc-price dense wt-display-inline-block">
<p class="wt-text-title-01 lc-price dense wt-display-inline-block">
                    ab <span class="currency-value">28,00</span> <span class="currency-symbol">€</span>
</p>
</div>
<div class="streamline-spacing-pricing-info streamline-spacing-reduce-margin">
</div>
<span class="wt-display-flex-xs wt-align-items-center wt-mb-xs-1">
<clg-icon class="wt-sem-text-action" name="location" size="smaller"></clg-icon>
<p class="wt-text-body-smaller lc-signal-bold wt-text-grey wt-display-inline-block">
        Versand aus DE
    </p>
</span>
</a>
</div>
</div>
<div class="search-half-unit-mt wt-display-flex-xs wt-flex-wrap wt-align-items-center row-gap-2">
<span class="wt-mr-xs-2"><form action="/de/cart/listing.php" class="wt-display-inline-block" data-logging-key="PLACEHOLDER" method="post">
<input name="listing_id" type="hidden" value="4461212869"/>
<input name="listing_title" type="hidden" value="Handgemachte Tasse  Keramik - 380 ml  - Frühstückstasse- Cappuccino Becher Unikat - Croissanttasse - Kaffeebecher - Keramiktasse"/>
<input name="listing_url" type="hidden" value="https://www.etsy.com/de/listing/4461212869/handgemachte-tasse-keramik-400-ml"/>
<input name="quantity" type="hidden" value="1"/>
<input name="ref" type="hidden" value="search_lc_cart_og"/>
<input name="show_listing_disclaimer" type="hidden" value="true"/>
<input name="show_cart_edit_panel" type="hidden" value="true"/>
<input name="is_pl" type="hidden" value="false"/>
<input name="listing_source" type="hidden" value="search"/>
<input name="logging_key" type="hidden" value="a55d4358-9d21-4dd3-9ae9-cf5fc18d4e9f:LTf3daadeb01befba115197c289bbd5f2a0824ad3e"/>
<input class="wt-display-none" name="_nnc" type="hidden" value="3:1788988829:nZi1Lnty_aI2OvxZ7s9Qs2p_6uRh:e859beea04cd893b38f3331f8366c09d56b3923a18769d03034dc4e8e8a6a6ce"/>
<clg-button aria-describedby="listing-title-4461212869" background-type="dynamic" class="" data-listing-card-add-to-cart="" hydrated="" size="small" type="submit" variant="secondary" with-submit="">
<clg-icon name="add" size="smaller"></clg-icon>
<span>Ab in den Warenkorb</span>
</clg-button>
</form></span>
<span></span>
<span class="wt-vertical-align-middle"><clg-text-link aria-describedby="listing-title-4461212869" class="refine-by-listing-link wt-text-title-small--tight" data-more-like-this-button="" href=PLACEHOLDER"https://www.etsy.com/de/r/similar/4461212869/?ref=PLACEHOLDER" icon="end" rel="nofollow" target="_blank" title="Ähnliche Artikel">
        Ähnliche Artikel
        <clg-icon class="refine-by-listing-link-icon refine-by-listing-link-icon--end" name="rightarrow" size="smaller" slot="icon"></clg-icon>
</clg-text-link></span>
</div>
<div class="v2-listing-card__actions wt-z-index-1 wt-position-absolute" data-favorite-button-wrapper="">

</div>
</div>
<div class="js-merch-stash-check-listing v2-listing-card wt-mr-xs-0 search-listing-card--desktop wt-height-full wt-display-flex-xs wt-flex-direction-column-xs wt-justify-content-space-between listing-card-experimental-style appears-ready" data-listing-card-v2="" data-listing-id="4361727707" data-page-type="search" data-palette-listing-id="4361727707" data-shop-id="41860268">
<div class="listing-link wt-display-inline-block b54090be1450263c2" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="4361727707" data-logging-key="PLACEHOLDER" data-palette-listing-image="PLACEHOLDER">
<a aria-label="Handmade ceramic mug with gold accent heart pottery coffee cup gift for her christmas present valentines day gift idea unique handcrafted" class="v2-listing-card__img wt-position-relative listing-card-rounded-corners" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="4361727707" data-listing-link="" data-logging-key="PLACEHOLDER" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/de/listing/4361727707/handmade-ceramic-mug-with-gold-accent?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;pro=1&amp;local_signal_search=1&amp;content_source=PLACEHOLDER" target="etsy.4361727707">
<div class="placeholder listing-card-rounded-corners">
<div class="placeholder vertically-centered-placeholder listing-card-rounded-corners">
<img alt="Handmade ceramic mug with gold accent heart pottery coffee cup gift for her christmas present valentines day gift idea unique handcrafted" class="wt-width-full wt-display-block listing-card-rounded-corners sr_gallery-1-5 wt-image--cover wt-image" data-clg-id="WtImage" data-listing-card-listing-image="" data-preload-lp-src="https://i.etsystatic.com/41860268/r/il/a2e894/7820778929/il_794xN.7820778929_af7v.jpg" data-preload-lp-srcset="https://i.etsystatic.com/41860268/r/il/a2e894/7820778929/il_794xN.7820778929_af7v.jpg 1x, https://i.etsystatic.com/41860268/r/il/a2e894/7820778929/il_1588xN.7820778929_af7v.jpg 2x" src="https://i.etsystatic.com/41860268/r/il/a2e894/7820778929/il_255x319.7820778929_af7v.jpg"/>
</div>
</div>
</a>
<div class="v2-listing-card__info wt-mt-xs-1 wt-pt-xs-0">
<div class="wt-display-flex-xs wt-align-items-center search-half-unit-my swatch-container">
<a alt="Grün" class="wt-circle wt-overflow-hidden wt-z-index-1 wt-mr-xs-1 swatch-link smaller" data-color-swatch="" data-sr-prefetch="PLACEHOLDER" data-variation-id="5771977252" href=PLACEHOLDER"https://www.etsy.com/de/listing/4361727707/handmade-ceramic-mug-with-gold-accent?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;pro=1&amp;local_signal_search=1&amp;content_source=PLACEHOLDER&amp;variation0=5771977252" target="etsy.4361727707.5771977252">
<div alt="Grün" class="wt-circle wt-overflow-hidden wt-z-index-1 listing-card-swatch add-transparent-outline smaller hover-effect">
</div>
</a>
<a alt="Rosa" class="wt-circle wt-overflow-hidden wt-z-index-1 wt-mr-xs-1 swatch-link smaller" data-color-swatch="" data-sr-prefetch="PLACEHOLDER" data-variation-id="5866524533" href=PLACEHOLDER"https://www.etsy.com/de/listing/4361727707/handmade-ceramic-mug-with-gold-accent?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;pro=1&amp;local_signal_search=1&amp;content_source=PLACEHOLDER&amp;variation0=5866524533" target="etsy.4361727707.5866524533">
<div alt="Rosa" class="wt-circle wt-overflow-hidden wt-z-index-1 listing-card-swatch add-transparent-outline smaller hover-effect">
</div>
</a>
<a alt="Schwarz" class="wt-circle wt-overflow-hidden wt-z-index-1 wt-mr-xs-1 swatch-link smaller" data-color-swatch="" data-sr-prefetch="PLACEHOLDER" data-variation-id="5763236447" href=PLACEHOLDER"https://www.etsy.com/de/listing/4361727707/handmade-ceramic-mug-with-gold-accent?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;pro=1&amp;local_signal_search=1&amp;content_source=PLACEHOLDER&amp;variation0=5763236447" target="etsy.4361727707.5763236447">
<div alt="Schwarz" class="wt-circle wt-overflow-hidden wt-z-index-1 listing-card-swatch add-transparent-outline smaller hover-effect">
</div>
</a>
<a alt="Rot" class="wt-circle wt-overflow-hidden wt-z-index-1 wt-mr-xs-1 swatch-link smaller" data-color-swatch="" data-sr-prefetch="PLACEHOLDER" data-variation-id="5771977246" href=PLACEHOLDER"https://www.etsy.com/de/listing/4361727707/handmade-ceramic-mug-with-gold-accent?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;pro=1&amp;local_signal_search=1&amp;content_source=PLACEHOLDER&amp;variation0=5771977246" target="etsy.4361727707.5771977246">
<div alt="Rot" class="wt-circle wt-overflow-hidden wt-z-index-1 listing-card-swatch add-transparent-outline smaller hover-effect">
</div>
</a>
<span class="wt-text-grey wt-text-body-smaller swatch-counter">
</span>
</div>
<a aria-label="" class="wt-z-index-1" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="" data-listing-link="" data-logging-key="PLACEHOLDER" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/de/listing/4361727707/handmade-ceramic-mug-with-gold-accent?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;pro=1&amp;local_signal_search=1&amp;content_source=PLACEHOLDER" target="etsy.4361727707">
<h3 class="wt-text-caption v2-listing-card__title wt-text-truncate search-half-unit-mb" id="listing-title-4361727707" title="Handmade ceramic mug with gold accent heart pottery coffee cup gift for her christmas present valentines day gift idea unique handcrafted">
                    Handmade ceramic mug with gold accent heart pottery coffee cup gift for her christmas present valentines day gift idea unique handcrafted
                </h3>
<div class="streamline-spacing-shop-rating">
<div class="shop-name-with-rating wt-display-flex-xs flex-direction-row-xs wt-align-items-center">
<span class="wt-display-flex-xs wt-flex-nowrap wt-align-items-center larger_review_stars">
<clg-static-review-stars class="him-review-stars wt-pal-grid-mr-xs-050" rating="4.9" review-count-text="(93)" size="smaller" variant="one-star"></clg-static-review-stars>
</span>
<div class="wt-flex-basis-lg-full wt-flex-basis-xl-auto wt star-badge-wrap-spacing wt-width-full min-width-0 wt-mt-xs-0">
<p class="wt-text-caption wt-text-truncate wt-text-gray wt-text-body-smaller streamline-seller-shop-name__line-height" data-seller-name-container="">
<span aria-hidden="true">
</span></p><div class="listing-card-tooltip-container" data-selector="popover-container">
<div class="wt-popover wt-text-caption wt-text-grey wt-align-items-center wt-vertical-align-middle wt-display-flex-xs wt-flex-nowrap" data-selector="popover-replacement" data-wt-popover="">

<div class="listing-card-tooltip wt-text-center-xs" id="listing-card-tooltip-shop-name-4361727707" role="tooltip">
<p class="wt-text-caption wt-no-wrap wt-mb-xs-0 wt-display-block wt-overflow-hidden tooltip-content-row">
<span class="wt-display-inline wt-no-wrap">Hergestellt von <strong class="wt-display-inline wt-no-wrap">TheKaloo</strong></span>
</p>
<p class="wt-text-caption wt-no-wrap wt-mb-xs-0 wt-display-block wt-overflow-hidden tooltip-content-row">
<span class="wt-display-block wt-no-wrap">Shop im Besitz von <strong>Nasta</strong></span>
</p>
<p class="wt-text-caption wt-no-wrap wt-mb-xs-0 wt-display-block wt-overflow-hidden tooltip-content-row">
<span class="wt-display-block wt-no-wrap">3 Jahre auf Etsy</span>
</p>
<span class="wt-popover__arrow"></span>
</div>
</div>
</div>
<span class="wt-screen-reader-only">Aus dem Shop TheKaloo</span>
<p></p>
</div>
</div>
</div>
<div class="search-half-unit-mt"></div>
<div class="n-listing-card__price wt-display-block wt-text-title-01 lc-price dense wt-display-inline-block">
<p class="wt-text-slime wt-text-title-01 lc-price dense wt-display-inline-block">
<span class="wt-screen-reader-only">
                            Sale-Preis ab 25,60 €
                        </span>
<span aria-hidden="true">
                            ab <span class="currency-value">25,60</span> <span class="currency-symbol">€</span>
</span>
</p><p class="wt-text-caption search-collage-promotion-price search-collage-original-price wt-text-slime wt-text-truncate wt-no-wrap">
<span aria-hidden="true" class="wt-text-strikethrough wt-text-black">ab <span class="currency-value">32,00</span> <span class="currency-symbol">€</span></span>
<span class="wt-screen-reader-only">
                                    Ursprünglicher Preis ab 32,00 €
                                </span>
<span class="wt-text-black">
<span class="wt-text-grey">
                                    (20% Rabatt)
                                    </span>
</span>
</p>
<p></p>
</div>
<div class="streamline-spacing-pricing-info streamline-spacing-reduce-margin">
</div>
<span class="wt-display-flex-xs wt-align-items-center wt-mb-xs-1">
<clg-icon class="wt-sem-text-action" name="location" size="smaller"></clg-icon>
<p class="wt-text-body-smaller lc-signal-bold wt-text-grey wt-display-inline-block">
        Versand aus DE
    </p>
</span>
</a>
</div>
</div>
<div class="search-half-unit-mt wt-display-flex-xs wt-flex-wrap wt-align-items-center row-gap-2">
<span class="wt-mr-xs-2"><form action="/de/cart/listing.php" class="wt-display-inline-block" data-logging-key="PLACEHOLDER" method="post">
<input name="listing_id" type="hidden" value="4361727707"/>
<input name="listing_title" type="hidden" value="Handmade ceramic mug with gold accent heart pottery coffee cup gift for her christmas present valentines day gift idea unique handcrafted"/>
<input name="listing_url" type="hidden" value="https://www.etsy.com/de/listing/4361727707/handmade-ceramic-mug-with-gold-accent"/>
<input name="quantity" type="hidden" value="1"/>
<input name="ref" type="hidden" value="search_lc_cart_og"/>
<input name="show_listing_disclaimer" type="hidden" value="true"/>
<input name="show_cart_edit_panel" type="hidden" value="true"/>
<input name="is_pl" type="hidden" value="false"/>
<input name="listing_source" type="hidden" value="search"/>
<input name="logging_key" type="hidden" value="a55d4358-9d21-4dd3-9ae9-cf5fc18d4e9f:LTd8a777fdacfd8eb72c1501b8e75e658686f39a6a"/>
<input class="wt-display-none" name="_nnc" type="hidden" value="3:1788988829:UMftdvx7j3li9wOPhuSfzvoZdGqR:4f25ac201b60076a280588b495e63fba2338f5e5c44293979014cadbb958d49a"/>
<clg-button aria-describedby="listing-title-4361727707" background-type="dynamic" class="" data-listing-card-add-to-cart="" hydrated="" size="small" type="submit" variant="secondary" with-submit="">
<clg-icon name="add" size="smaller"></clg-icon>
<span>Ab in den Warenkorb</span>
</clg-button>
</form></span>
<span></span>
<span class="wt-vertical-align-middle"><clg-text-link aria-describedby="listing-title-4361727707" class="refine-by-listing-link wt-text-title-small--tight" data-more-like-this-button="" href=PLACEHOLDER"https://www.etsy.com/de/r/similar/4361727707/?ref=PLACEHOLDER" icon="end" rel="nofollow" target="_blank" title="Ähnliche Artikel">
        Ähnliche Artikel
        <clg-icon class="refine-by-listing-link-icon refine-by-listing-link-icon--end" name="rightarrow" size="smaller" slot="icon"></clg-icon>
</clg-text-link></span>
</div>
<div class="v2-listing-card__actions wt-z-index-1 wt-position-absolute" data-favorite-button-wrapper="">

</div>
</div>
</body></html>"""

# /de/c/home-and-living/.../mugs. Five tiles:
#   4541371530  the OLDER rating markup (sprite classes + '(7)')
#   4339556119  discounted, 14.39 against 17.99
#   4448501401  discounted, and a HALF star (star-rating-4-5)
#   4569410287  the ListPrice case: Etsy publishes 11.04 and prints 12.99
#   4568642767  the same, and carries no rating at all
CATEGORY_FIXTURE = r"""<html lang="de"><head>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "ItemList", "numberOfItems": 1100158, "itemListElement": [{"@type": "ListItem", "position": 4, "item": {"@type": "Product", "image": "https://i.etsystatic.com/67224282/r/il/f4cd67/8493537084/il_fullxfull.8493537084_j4xr.jpg", "name": "Pilot Mug, Time To Fly Pilots, Gifts, Coffe Cup, Aircraft Mug, Kaffee Tasse, Gift for him, Flugzeug Tasse, 11 oz (ca. 325 ml)", "url": "https://www.etsy.com/de/listing/4569410287/pilots-mug-time-to-fly-pilots-coffe-cup", "brand": {"@type": "Brand", "name": "PolatsStudio", "@id": "https://www.etsy.com/de/shop/PolatsStudio#shop"}, "offers": {"@type": "Offer", "price": "11.04", "priceCurrency": "EUR", "availability": "https://schema.org/InStock", "@id": "https://www.etsy.com/de/listing/4569410287/pilots-mug-time-to-fly-pilots-coffe-cup#offer", "priceSpecification": {"@type": "UnitPriceSpecification", "priceType": "https://schema.org/ListPrice", "price": "12.99", "priceCurrency": "EUR"}}, "@id": "https://www.etsy.com/de/listing/4569410287/pilots-mug-time-to-fly-pilots-coffe-cup#product"}}, {"@type": "ListItem", "position": 5, "item": {"@type": "Product", "image": "https://i.etsystatic.com/67224282/r/il/7cee09/8489910994/il_fullxfull.8489910994_cau2.jpg", "name": "Butterfly Mug,  Coffee Mug, Butterlfy Gifts, Schmetterling Tasse, Kaffee Tasse, Geschenk für Sie, Gifts for her, 11 oz (ca. 325 ml)", "url": "https://www.etsy.com/de/listing/4568642767/butterfly-coffee-mug-butterlfy-gifts", "brand": {"@type": "Brand", "name": "PolatsStudio", "@id": "https://www.etsy.com/de/shop/PolatsStudio#shop"}, "offers": {"@type": "Offer", "price": "11.04", "priceCurrency": "EUR", "availability": "https://schema.org/InStock", "@id": "https://www.etsy.com/de/listing/4568642767/butterfly-coffee-mug-butterlfy-gifts#offer", "priceSpecification": {"@type": "UnitPriceSpecification", "priceType": "https://schema.org/ListPrice", "price": "12.99", "priceCurrency": "EUR"}}, "@id": "https://www.etsy.com/de/listing/4568642767/butterfly-coffee-mug-butterlfy-gifts#product"}}, {"@type": "ListItem", "position": 47, "item": {"@type": "Product", "image": "https://i.etsystatic.com/66007375/r/il/8588de/8326518731/il_fullxfull.8326518731_13o3.jpg", "name": "Set aus 2–10 handbemalten, runden Keramikbechern, farbenfrohes Keramiktassen-Set", "url": "https://www.etsy.com/de/listing/4541371530/handbemalter-keramikbecher-runde", "brand": {"@type": "Brand", "name": "fammembers", "@id": "https://www.etsy.com/de/shop/fammembers#shop"}, "offers": {"@type": "Offer", "price": "42.77", "priceCurrency": "EUR", "availability": "https://schema.org/InStock", "@id": "https://www.etsy.com/de/listing/4541371530/handbemalter-keramikbecher-runde#offer"}, "@id": "https://www.etsy.com/de/listing/4541371530/handbemalter-keramikbecher-runde#product"}}, {"@type": "ListItem", "position": 48, "item": {"@type": "Product", "image": "https://i.etsystatic.com/49219730/r/il/e867ce/8341209007/il_fullxfull.8341209007_3psw.jpg", "name": "Whimsical Botanical Cat Cup | Cat Lover Mug | Floral Cat Coffee Mug | Gift for Cat Mom | Birthday Gift for Cat Lover | Cute Cat Design Mug", "url": "https://www.etsy.com/de/listing/4339556119/adorable-cat-mug-vibrant-floral-design", "brand": {"@type": "Brand", "name": "CozyBreezeBusiness", "@id": "https://www.etsy.com/de/shop/CozyBreezeBusiness#shop"}, "offers": {"@type": "Offer", "price": "14.39", "priceCurrency": "EUR", "availability": "https://schema.org/InStock", "@id": "https://www.etsy.com/de/listing/4339556119/adorable-cat-mug-vibrant-floral-design#offer", "priceSpecification": {"@type": "UnitPriceSpecification", "priceType": "https://schema.org/ListPrice", "price": "17.99", "priceCurrency": "EUR"}}, "@id": "https://www.etsy.com/de/listing/4339556119/adorable-cat-mug-vibrant-floral-design#product"}}, {"@type": "ListItem", "position": 49, "item": {"@type": "Product", "image": "https://i.etsystatic.com/63629787/r/il/443478/7843843799/il_fullxfull.7843843799_te2q.jpg", "name": "Vintage Steinzeug Kaffeetasse, handgemachte Japandi Keramik Tasse, rustikale Keramik Teetasse, Wabi-Sabi Boho Wohnkultur", "url": "https://www.etsy.com/de/listing/4448501401/vintage-steinzeug-kaffeetasse", "brand": {"@type": "Brand", "name": "OceanAndLake", "@id": "https://www.etsy.com/de/shop/OceanAndLake#shop"}, "offers": {"@type": "Offer", "price": "15.44", "priceCurrency": "EUR", "availability": "https://schema.org/InStock", "@id": "https://www.etsy.com/de/listing/4448501401/vintage-steinzeug-kaffeetasse#offer", "priceSpecification": {"@type": "UnitPriceSpecification", "priceType": "https://schema.org/ListPrice", "price": "18.82", "priceCurrency": "EUR"}}, "@id": "https://www.etsy.com/de/listing/4448501401/vintage-steinzeug-kaffeetasse#product"}}]}</script>
</head><body>
<img src="https://i.etsystatic.com/x/il_fullxfull.jpg"/>
<div class="js-merch-stash-check-listing v2-listing-card wt-mr-xs-0 search-listing-card--desktop listing-card-experimental-style appears-ready" data-listing-card-v2="" data-listing-id="4541371530" data-page-type="category" data-palette-listing-id="4541371530" data-shop-id="66007375">
<a class="listing-link wt-display-inline-block b1d071b88e3822bc7" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="4541371530" data-listing-link="" data-logging-key="PLACEHOLDER" data-palette-listing-image="PLACEHOLDER" data-shop-id="66007375" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/de/listing/4541371530/handbemalter-keramikbecher-runde?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=a&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER" target="etsy.4541371530" title="Set aus 2–10 handbemalten, runden Keramikbechern, farbenfrohes Keramiktassen-Set">
<div class="v2-listing-card__img wt-position-relative">
<div class="placeholder placeholder-landscape">
<div class="placeholder vertically-centered-placeholder placeholder-content placeholder-landscape">
<div class="height-placeholder">
<img alt="Könnte beinhalten: Ein leuchtend gelber Keramikbecher mit handgemaltem Blumenmuster in Blau und Pink, hellblauen Blattmotiven und kleinen schwarzen Punkten. Der Becher hat ein weißes Inneres, einen schwarzen Rand und einen geschwungenen Henkel mit weißen Punkten." class="wt-width-full wt-height-full wt-display-block wt-position-absolute sc_gallery-1-1" data-listing-card-listing-image="" src="https://i.etsystatic.com/66007375/r/il/8588de/8326518731/il_340x270.8326518731_13o3.jpg"/>
</div>
</div>
</div>
</div>
<div class="v2-listing-card__info">
<h3 class="wt-text-caption v2-listing-card__title wt-text-truncate" id="ad-listing-title-4541371530">
                    Set aus 2–10 handbemalten, runden Keramikbechern, farbenfrohes Keramiktassen-Set
                </h3>
<span class="wt-display-flex-xs wt-flex-wrap wt-align-items-center larger_review_stars wt-nudge-t-1">
<span class="wt-display-inline-block wt-nudge-b-1 set-review-stars-line-height-to-zero" data-stars-svg-container="">
<input name="initial-rating" type="hidden" value="5"/>
<input name="rating" type="hidden" value="5"/>
<div aria-label="5 von 5 Sternen" class="sprite-img black-stars star-rating-5 stars-larger" role="img"></div>
</span>
<span class="wt-text-caption wt-text-gray wt-display-inline-block wt-pr-xs-1 wt-nudge-l-3">
                    (7)
                </span>
</span>
<div class="n-listing-card__price wt-display-flex-xs wt-align-items-center wt-width-full wt-flex-wrap wt-width-full wt-text-title-01 lc-price">
<p class="wt-text-title-01 lc-price">
                    ab <span class="currency-value">42,77</span> <span class="currency-symbol">€</span>
</p>
</div>
<p class="wt-text-caption wt-text-truncate wt-text-gray wt-mb-xs-1" data-seller-name-container="">
<span aria-hidden="true">
                Anzeige des Etsy-Shops
            </span>
<span class="wt-screen-reader-only">Anzeige des Etsy-Shops</span>
</p>
</div>
</a>
</div>
<div class="js-merch-stash-check-listing v2-listing-card wt-mr-xs-0 search-listing-card--desktop listing-card-experimental-style appears-ready" data-listing-card-v2="" data-listing-id="4339556119" data-page-type="category" data-palette-listing-id="4339556119" data-shop-id="49219730">
<a class="listing-link wt-display-inline-block b1d071b88e3822bc7" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="4339556119" data-listing-link="" data-logging-key="PLACEHOLDER" data-palette-listing-image="PLACEHOLDER" data-shop-id="49219730" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/de/listing/4339556119/adorable-cat-mug-vibrant-floral-design?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=a&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;pro=1&amp;frs=1&amp;sts=1" target="etsy.4339556119" title="Whimsical Botanical Cat Cup | Cat Lover Mug | Floral Cat Coffee Mug | Gift for Cat Mom | Birthday Gift for Cat Lover | Cute Cat Design Mug">
<div class="v2-listing-card__img wt-position-relative">
<div class="placeholder placeholder-landscape">
<div class="placeholder vertically-centered-placeholder placeholder-content placeholder-landscape">
<div class="height-placeholder">
<img alt="Whimsical Botanical Cat Cup | Cat Lover Mug | Floral Cat Coffee Mug | Gift for Cat Mom | Birthday Gift for Cat Lover | Cute Cat Design Mug" class="wt-width-full wt-height-full wt-display-block wt-position-absolute sc_gallery-1-2" data-listing-card-listing-image="" src="https://i.etsystatic.com/49219730/c/636/636/213/294/il/e867ce/8341209007/il_340x270.8341209007_3psw.jpg"/>
</div>
</div>
</div>
</div>
<div class="v2-listing-card__info">
<h3 class="wt-text-caption v2-listing-card__title wt-text-truncate" id="ad-listing-title-4339556119">
                    Whimsical Botanical Cat Cup | Cat Lover Mug | Floral Cat Coffee Mug | Gift for Cat Mom | Birthday Gift for Cat Lover | Cute Cat Design Mug
                </h3>
<span class="wt-display-flex-xs wt-flex-wrap wt-align-items-center larger_review_stars wt-nudge-t-1">
<span class="wt-display-inline-block wt-nudge-b-1 set-review-stars-line-height-to-zero" data-stars-svg-container="">
<input name="initial-rating" type="hidden" value="4.92"/>
<input name="rating" type="hidden" value="4.92"/>
<div aria-label="5 von 5 Sternen" class="sprite-img black-stars star-rating-5 stars-larger" role="img"></div>
</span>
<span class="wt-text-caption wt-text-gray wt-display-inline-block wt-pr-xs-1 wt-nudge-l-3">
                    (10)
                </span>
<span class="wt-flex-basis-lg-full wt-flex-basis-xl-auto star-badge-wrap-spacing">
<div class="wt-display-inline-flex-xs wt-flex-nowrap wt-align-items-center">
<span class="wt-icon wt-icon--core wt-fill-star-seller-dark wt-icon--smaller-xs"></span>
<p class="wt-text-caption-title wt-nudge-l-2 star-seller-badge-lavender-text-light">Verkäufer-Star</p>
</div>
</span>
</span>
<div class="n-listing-card__price wt-display-flex-xs wt-align-items-center wt-width-full wt-flex-wrap wt-width-full wt-text-title-01 lc-price">
<p class="wt-text-title-01 lc-price">
<span class="wt-screen-reader-only">
                            Sale-Preis ab 14,39 €
                        </span>
<span aria-hidden="true">
                            ab <span class="currency-value">14,39</span> <span class="currency-symbol">€</span>
</span>
</p><p class="wt-text-caption search-collage-promotion-price wt-text-slime wt-text-truncate wt-no-wrap">
<span aria-hidden="true" class="wt-text-strikethrough wt-text-grey">ab <span class="currency-value">17,99</span> <span class="currency-symbol">€</span></span>
<span class="wt-screen-reader-only">
                                    Ursprünglicher Preis ab 17,99 €
                                </span>
<span class="wt-text-grey">
                                    
                                    (20% Rabatt)
                                    
                                </span>
</p>
<p></p>
</div>
<p class="wt-text-caption wt-text-truncate wt-text-gray wt-mb-xs-1" data-seller-name-container="">
<span aria-hidden="true">
                Anzeige des Etsy-Shops
            </span>
<span class="wt-screen-reader-only">Anzeige des Etsy-Shops</span>
</p>
<div class="promotion-badge-line wt-display-flex-xs">
<p class="wt-text-truncate wt-text-caption-title">
<span class="wt-badge wt-badge--small wt-badge--statusValue">
    KOSTENLOSER Versand
</span>
</p>
</div>
</div>
</a>
</div>
<div class="js-merch-stash-check-listing v2-listing-card wt-mr-xs-0 search-listing-card--desktop listing-card-experimental-style appears-ready" data-listing-card-v2="" data-listing-id="4448501401" data-page-type="category" data-palette-listing-id="4448501401" data-shop-id="63629787">
<a class="listing-link wt-display-inline-block b1d071b88e3822bc7" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="4448501401" data-listing-link="" data-logging-key="PLACEHOLDER" data-palette-listing-image="PLACEHOLDER" data-shop-id="63629787" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/de/listing/4448501401/vintage-steinzeug-kaffeetasse?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=a&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;pro=1" target="etsy.4448501401" title="Vintage Steinzeug Kaffeetasse, handgemachte Japandi Keramik Tasse, rustikale Keramik Teetasse, Wabi-Sabi Boho Wohnkultur">
<div class="v2-listing-card__img wt-position-relative">
<div class="placeholder placeholder-landscape">
<div class="placeholder vertically-centered-placeholder placeholder-content placeholder-landscape">
<div class="height-placeholder">
<img alt="Vintage Steinzeug Kaffeetasse, handgemachte Japandi Keramik Tasse, rustikale Keramik Teetasse, Wabi-Sabi Boho Wohnkultur" class="wt-width-full wt-height-full wt-display-block wt-position-absolute sc_gallery-1-3" data-listing-card-listing-image="" src="https://i.etsystatic.com/63629787/r/il/443478/7843843799/il_340x270.7843843799_te2q.jpg"/>
</div>
</div>
</div>
</div>
<div class="v2-listing-card__info">
<h3 class="wt-text-caption v2-listing-card__title wt-text-truncate" id="ad-listing-title-4448501401">
                    Vintage Steinzeug Kaffeetasse, handgemachte Japandi Keramik Tasse, rustikale Keramik Teetasse, Wabi-Sabi Boho Wohnkultur
                </h3>
<span class="wt-display-flex-xs wt-flex-wrap wt-align-items-center larger_review_stars wt-nudge-t-1">
<span class="wt-display-inline-block wt-nudge-b-1 set-review-stars-line-height-to-zero" data-stars-svg-container="">
<input name="initial-rating" type="hidden" value="4.69"/>
<input name="rating" type="hidden" value="4.69"/>
<div aria-label="4.5 von 5 Sternen" class="sprite-img black-stars star-rating-4-5 stars-larger" role="img"></div>
</span>
<span class="wt-text-caption wt-text-gray wt-display-inline-block wt-pr-xs-1 wt-nudge-l-3">
                    (81)
                </span>
</span>
<div class="n-listing-card__price wt-display-flex-xs wt-align-items-center wt-width-full wt-flex-wrap wt-width-full wt-text-title-01 lc-price">
<p class="wt-text-title-01 lc-price">
<span class="wt-screen-reader-only">
                            Sale-Preis ab 15,44 €
                        </span>
<span aria-hidden="true">
                            ab <span class="currency-value">15,44</span> <span class="currency-symbol">€</span>
</span>
</p><p class="wt-text-caption search-collage-promotion-price wt-text-slime wt-text-truncate wt-no-wrap">
<span aria-hidden="true" class="wt-text-strikethrough wt-text-grey">ab <span class="currency-value">18,82</span> <span class="currency-symbol">€</span></span>
<span class="wt-screen-reader-only">
                                    Ursprünglicher Preis ab 18,82 €
                                </span>
<span class="wt-text-grey">
                                    
                                    (18% Rabatt)
                                    
                                </span>
</p>
<p></p>
</div>
<p class="wt-text-caption wt-text-truncate wt-text-gray wt-mb-xs-1" data-seller-name-container="">
<span aria-hidden="true">
                Anzeige des Etsy-Shops
            </span>
<span class="wt-screen-reader-only">Anzeige des Etsy-Shops</span>
</p>
</div>
</a>
</div>
<div class="js-merch-stash-check-listing v2-listing-card wt-mr-xs-0 search-listing-card--desktop listing-card-experimental-style appears-ready" data-listing-card-v2="" data-listing-id="4569410287" data-page-type="category" data-palette-listing-id="4569410287" data-shop-id="67224282">
<a class="listing-link wt-display-inline-block b92068c368dfcb9af" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="4569410287" data-listing-link="" data-logging-key="PLACEHOLDER" data-palette-listing-image="PLACEHOLDER" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/de/listing/4569410287/pilots-mug-time-to-fly-pilots-coffe-cup?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;content_source=PLACEHOLDER" target="etsy.4569410287" title="Pilot Mug, Time To Fly Pilots, Gifts, Coffe Cup, Aircraft Mug, Kaffee Tasse, Gift for him, Flugzeug Tasse, 11 oz (ca. 325 ml)">
<div class="v2-listing-card__img wt-position-relative">
<div class="placeholder placeholder-landscape">
<div class="placeholder vertically-centered-placeholder placeholder-content placeholder-landscape">
<div class="height-placeholder">
<img alt="Pilot Mug, Time To Fly Pilots, Gifts, Coffe Cup, Aircraft Mug, Kaffee Tasse, Gift for him, Flugzeug Tasse, 11 oz (ca. 325 ml)" class="wt-width-full wt-height-full wt-display-block wt-position-absolute sr_gallery-1-4" data-listing-card-listing-image="" src="https://i.etsystatic.com/67224282/r/il/f4cd67/8493537084/il_340x270.8493537084_j4xr.jpg"/>
</div>
</div>
</div>
</div>
<div class="v2-listing-card__info">
<h3 class="wt-text-caption v2-listing-card__title wt-text-truncate" id="listing-title-4569410287">
                    Pilot Mug, Time To Fly Pilots, Gifts, Coffe Cup, Aircraft Mug, Kaffee Tasse, Gift for him, Flugzeug Tasse, 11 oz (ca. 325 ml)
                </h3>
<div class="n-listing-card__price wt-display-flex-xs wt-align-items-center wt-width-full wt-flex-wrap wt-width-full wt-text-title-01 lc-price">
<p class="wt-text-title-01 lc-price">
                    ab <span class="currency-value">12,99</span> <span class="currency-symbol">€</span>
</p>
</div>
<p class="wt-text-caption wt-text-truncate wt-text-gray wt-mb-xs-1" data-seller-name-container="">
<span aria-hidden="true">
                PolatsStudio
            </span>
<span class="wt-screen-reader-only">Aus dem Shop PolatsStudio</span>
</p>
</div>
</a>
</div>
<div class="js-merch-stash-check-listing v2-listing-card wt-mr-xs-0 search-listing-card--desktop listing-card-experimental-style appears-ready" data-listing-card-v2="" data-listing-id="4568642767" data-page-type="category" data-palette-listing-id="4568642767" data-shop-id="67224282">
<a class="listing-link wt-display-inline-block b92068c368dfcb9af" data-display-loc="PLACEHOLDER" data-index="PLACEHOLDER" data-listing-id="4568642767" data-listing-link="" data-logging-key="PLACEHOLDER" data-palette-listing-image="PLACEHOLDER" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/de/listing/4568642767/butterfly-coffee-mug-butterlfy-gifts?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ls=s&amp;ga_order=most_relevant&amp;ga_search_type=all&amp;ga_view_type=gallery&amp;ga_search_query=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;content_source=PLACEHOLDER" target="etsy.4568642767" title="Butterfly Mug,  Coffee Mug, Butterlfy Gifts, Schmetterling Tasse, Kaffee Tasse, Geschenk für Sie, Gifts for her, 11 oz (ca. 325 ml)">
<div class="v2-listing-card__img wt-position-relative">
<div class="placeholder placeholder-landscape">
<div class="placeholder vertically-centered-placeholder placeholder-content placeholder-landscape">
<div class="height-placeholder">
<img alt="Könnte beinhalten: Ein weißer Keramikbecher mit rosa Innenseite und Henkel. Auf der Vorderseite steht das Wort Butterfly in einer dunkelrosa Schreibschrift über einem vertikalen Streifenmuster und einem Schmetterlingsmotiv. Darunter steht der Text: butterflies unfold their wings so bright and gather strength with hidden might they dry them gently in the golden light before they take their first good..." class="wt-width-full wt-height-full wt-display-block wt-position-absolute sr_gallery-1-5" data-listing-card-listing-image="" src="https://i.etsystatic.com/67224282/c/3000/3000/0/0/il/7cee09/8489910994/il_340x270.8489910994_cau2.jpg"/>
</div>
</div>
</div>
</div>
<div class="v2-listing-card__info">
<h3 class="wt-text-caption v2-listing-card__title wt-text-truncate" id="listing-title-4568642767">
                    Butterfly Mug,  Coffee Mug, Butterlfy Gifts, Schmetterling Tasse, Kaffee Tasse, Geschenk für Sie, Gifts for her, 11 oz (ca. 325 ml)
                </h3>
<div class="n-listing-card__price wt-display-flex-xs wt-align-items-center wt-width-full wt-flex-wrap wt-width-full wt-text-title-01 lc-price">
<p class="wt-text-title-01 lc-price">
                    ab <span class="currency-value">12,99</span> <span class="currency-symbol">€</span>
</p>
</div>
<p class="wt-text-caption wt-text-truncate wt-text-gray wt-mb-xs-1" data-seller-name-container="">
<span aria-hidden="true">
                PolatsStudio
            </span>
<span class="wt-screen-reader-only">Aus dem Shop PolatsStudio</span>
</p>
</div>
</a>
</div>
</body></html>"""

# /de/shop/StonehousePotteryOH. Three tiles plus the seller's own
# `Organization` block, which is where a shop front's rating lives —
# its TILES carry none (0 of 40 on the full capture).
SHOP_FIXTURE = r"""<html lang="de"><head>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "Organization", "name": "StonehousePotteryOH", "description": "Entdecke Stonehouse Pottery von Emily Moorefield Mariola von StonehousePotteryOH, ansässig in Ohio, Vereinigte Staaten.", "url": "https://www.etsy.com/de/shop/StonehousePotteryOH", "logo": "https://i.etsystatic.com/8740835/r/isla/f10a52/38851706/isla_500x500.38851706_2x3sl6r2.jpg", "@id": "https://www.etsy.com/de/shop/StonehousePotteryOH#shop", "location": "Ohio, Vereinigte Staaten", "image": "https://www.etsy.com/images/grey.gif", "slogan": "Stonehouse Pottery by Emily Moorefield Mariola", "aggregateRating": {"@type": "AggregateRating", "ratingValue": "5", "worstRating": 1, "bestRating": 5, "reviewCount": 16679, "@id": "https://www.etsy.com/de/shop/StonehousePotteryOH#rating"}}</script>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "ItemList", "numberOfItems": 110, "itemListElement": [{"@type": "ListItem", "position": 35, "item": {"@type": "Product", "image": "https://i.etsystatic.com/8740835/r/il/0afd89/7408707651/il_fullxfull.7408707651_pbkt.jpg", "name": "The Irene Pottery Mug snowflake winter coffee mug", "url": "https://www.etsy.com/de/listing/1644949004/the-irene-pottery-mug-snowflake-winter", "brand": {"@type": "Brand", "name": "StonehousePotteryOH", "@id": "https://www.etsy.com/de/shop/StonehousePotteryOH#shop"}, "offers": {"@type": "Offer", "price": "36.91", "priceCurrency": "EUR", "availability": "https://schema.org/InStock", "@id": "https://www.etsy.com/de/listing/1644949004/the-irene-pottery-mug-snowflake-winter#offer"}, "@id": "https://www.etsy.com/de/listing/1644949004/the-irene-pottery-mug-snowflake-winter#product"}}]}</script>
</head><body>
<img src="https://i.etsystatic.com/x/il_fullxfull.jpg"/>
<div class="js-merch-stash-check-listing v2-listing-card wt-position-relative wt-grid__item-xs-6 wt-flex-shrink-xs-1 wt-grid__item-xl-3 wt-grid__item-lg-4 wt-grid__item-md-4 listing-card-experimental-style" data-listing-card-v2="" data-listing-id="647501162" data-page-type="shop_collage_cards_experiment" data-palette-listing-id="647501162" data-shop-id="8740835">
<a class="listing-link wt-display-inline-block wt-transparent-card" data-listing-id="647501162" data-listing-link="" data-palette-listing-image="PLACEHOLDER" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/de/listing/647501162/winter-tree-ceramic-coffee-mug?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER&amp;bes=PLACEHOLDER" target="etsy.647501162" title="Winter Tree Ceramic Coffee Mug">
<div class="v2-listing-card__img wt-position-relative listing-card-image-no-shadow">
<div class="placeholder placeholder-square wt-mb-xs-1" tabindex="-1">
<div class="placeholder vertically-centered-placeholder placeholder-content placeholder-square">
<img alt="Winter Tree Ceramic Coffee Mug" class="wt-width-full wt-display-block wt-height-full wt-position-absolute shop_home_feat_1 wt-image--cover wt-image" data-clg-id="WtImage" data-listing-card-listing-image="" src="https://i.etsystatic.com/8740835/r/il/f7ac87/1743128247/il_340x270.1743128247_4hfb.jpg"/>
</div>
<div aria-hidden="true" class="listing-card-video-spinner wt-align-items-center wt-display-none wt-position-absolute wt-position-top wt-position-bottom wt-position-left wt-position-right">
<div class="wt-spinner wt-spinner--01">
<span class="etsy-icon"></span>
        Loading
    </div>
</div>
<div class="listing-card-video-container wt-display-none wt-position-absolute wt-position-top wt-position-bottom wt-position-left wt-position-right">

</div>
<div aria-hidden="false" class="listing-card-video-signal wt-position-absolute wt-circle wt-overflow-hidden wt-sem-bg-elevation-0">
<clg-icon class="wt-horizontal-center wt-vertical-center" name="play" size="smaller"></clg-icon>
</div>
</div>
</div>
<div class="v2-listing-card__info">
<h3 class="wt-text-caption v2-listing-card__title wt-text-truncate" id="listing-title-647501162">
                    Winter Tree Ceramic Coffee Mug
                </h3>
<div class="n-listing-card__price wt-display-flex-xs wt-align-items-center wt-width-full wt-flex-wrap wt-width-full wt-pr-xs-1 wt-text-title-01">
<p class="wt-pr-xs-1 wt-text-title-01">
                    ab <span class="currency-value">35,84</span> <span class="currency-symbol">€</span>
</p>
</div>
</div>
</a>
<div class="v2-listing-card__actions wt-z-index-1 wt-position-absolute" data-favorite-button-wrapper="">

</div>
</div>
<div class="js-merch-stash-check-listing v2-listing-card wt-position-relative wt-grid__item-xs-6 wt-flex-shrink-xs-1 wt-grid__item-xl-3 wt-grid__item-lg-4 wt-grid__item-md-4 listing-card-experimental-style" data-listing-card-v2="" data-listing-id="787633992" data-page-type="shop_collage_cards_experiment" data-palette-listing-id="787633992" data-shop-id="8740835">
<a class="listing-link wt-display-inline-block wt-transparent-card" data-listing-id="787633992" data-listing-link="" data-palette-listing-image="PLACEHOLDER" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/de/listing/787633992/pottery-berry-bowl-strainer-one-quart?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER" target="etsy.787633992" title="Pottery Berry Bowl Strainer: One Quart, Ocean Blue Glaze">
<div class="v2-listing-card__img wt-position-relative listing-card-image-no-shadow">
<div class="placeholder placeholder-square wt-mb-xs-1" tabindex="-1">
<div class="placeholder vertically-centered-placeholder placeholder-content placeholder-square">
<img alt="Pottery Berry Bowl Strainer: One Quart, Ocean Blue Glaze" class="wt-width-full wt-display-block wt-height-full wt-position-absolute shop_home_feat_2 wt-image--cover wt-image" data-clg-id="WtImage" data-listing-card-listing-image="" src="https://i.etsystatic.com/8740835/r/il/d5ab58/6693910262/il_340x270.6693910262_qavc.jpg"/>
</div>
<div aria-hidden="true" class="listing-card-video-spinner wt-align-items-center wt-display-none wt-position-absolute wt-position-top wt-position-bottom wt-position-left wt-position-right">
<div class="wt-spinner wt-spinner--01">
<span class="etsy-icon"></span>
        Loading
    </div>
</div>
<div class="listing-card-video-container wt-display-none wt-position-absolute wt-position-top wt-position-bottom wt-position-left wt-position-right">

</div>
<div aria-hidden="false" class="listing-card-video-signal wt-position-absolute wt-circle wt-overflow-hidden wt-sem-bg-elevation-0">
<clg-icon class="wt-horizontal-center wt-vertical-center" name="play" size="smaller"></clg-icon>
</div>
</div>
</div>
<div class="v2-listing-card__info">
<h3 class="wt-text-caption v2-listing-card__title wt-text-truncate" id="listing-title-787633992">
                    Pottery Berry Bowl Strainer: One Quart, Ocean Blue Glaze
                </h3>
<div class="n-listing-card__price wt-display-flex-xs wt-align-items-center wt-width-full wt-flex-wrap wt-width-full wt-pr-xs-1 wt-text-title-01">
<p class="wt-pr-xs-1 wt-text-title-01">
<span class="currency-value">37,16</span> <span class="currency-symbol">€</span>
</p>
</div>
</div>
</a>
<div class="v2-listing-card__actions wt-z-index-1 wt-position-absolute" data-favorite-button-wrapper="">

</div>
</div>
<div class="js-merch-stash-check-listing v2-listing-card wt-position-relative wt-grid__item-xs-6 wt-flex-shrink-xs-1 wt-grid__item-xl-3 wt-grid__item-lg-4 wt-grid__item-md-4 listing-card-experimental-style" data-listing-card-v2="" data-listing-id="1644949004" data-page-type="shop_collage_cards_experiment" data-palette-listing-id="1644949004" data-shop-id="8740835">
<a class="listing-link wt-display-inline-block wt-transparent-card" data-listing-id="1644949004" data-listing-link="" data-palette-listing-image="PLACEHOLDER" data-sr-prefetch="PLACEHOLDER" href=PLACEHOLDER"https://www.etsy.com/de/listing/1644949004/the-irene-pottery-mug-snowflake-winter?click_key=PLACEHOLDER&amp;click_sum=PLACEHOLDER&amp;ref=PLACEHOLDER&amp;sr_prefetch=1&amp;pf_from=PLACEHOLDER" target="etsy.1644949004" title="The Irene Pottery Mug snowflake winter coffee mug">
<div class="v2-listing-card__img wt-position-relative listing-card-image-no-shadow">
<div class="placeholder placeholder-square wt-mb-xs-1" tabindex="-1">
<div class="placeholder vertically-centered-placeholder placeholder-content placeholder-square">
<img alt="The Irene Pottery Mug snowflake winter coffee mug" class="wt-width-full wt-display-block wt-height-full wt-position-absolute shop_home_feat_3 wt-image--cover wt-image" data-clg-id="WtImage" data-listing-card-listing-image="" src="https://i.etsystatic.com/8740835/r/il/0afd89/7408707651/il_340x270.7408707651_pbkt.jpg"/>
</div>
<div aria-hidden="true" class="listing-card-video-spinner wt-align-items-center wt-display-none wt-position-absolute wt-position-top wt-position-bottom wt-position-left wt-position-right">
<div class="wt-spinner wt-spinner--01">
<span class="etsy-icon"></span>
        Loading
    </div>
</div>
<div class="listing-card-video-container wt-display-none wt-position-absolute wt-position-top wt-position-bottom wt-position-left wt-position-right">

</div>
<div aria-hidden="false" class="listing-card-video-signal wt-position-absolute wt-circle wt-overflow-hidden wt-sem-bg-elevation-0">
<clg-icon class="wt-horizontal-center wt-vertical-center" name="play" size="smaller"></clg-icon>
</div>
</div>
</div>
<div class="v2-listing-card__info">
<h3 class="wt-text-caption v2-listing-card__title wt-text-truncate" id="listing-title-1644949004">
                    The Irene Pottery Mug snowflake winter coffee mug
                </h3>
<div class="n-listing-card__price wt-display-flex-xs wt-align-items-center wt-width-full wt-flex-wrap wt-width-full wt-pr-xs-1 wt-text-title-01">
<p class="wt-pr-xs-1 wt-text-title-01">
<span class="currency-value">36,91</span> <span class="currency-symbol">€</span>
</p>
</div>
</div>
</a>
<div class="v2-listing-card__actions wt-z-index-1 wt-position-absolute" data-favorite-button-wrapper="">

</div>
</div>
</body></html>"""

# /de/listing/519688604. The Product JSON-LD plus the buy box, which is
# the DOM half of the price cross-check. Its `review[]` is SCRUBBED: the
# author name and the review body are placeholders, everything Etsy
# generates around them is verbatim.
LISTING_FIXTURE = """<html lang="de"><head>
<script type="application/ld+json">{"@type": "Product", "@id": "https://www.etsy.com/de/listing/519688604/handthrown-pottery-mug#product", "@context": "https://schema.org", "url": "https://www.etsy.com/de/listing/519688604/handthrown-pottery-mug", "name": "Handthrown Pottery Mug", "sku": "519688604", "description": "14-16 ounces or 10-12 ounces\\nbulbous shaped coffee tea mug.  Simple, traditional, elegant.  Blue and Earth green.  Thumb rest.  This is the mug shape and sized most frequently used in our house, from the littlest cousin to the coffee loving French grandmother!  Food safe.  Microwave and dishwasher safe. Each mug varies slightly in size but are approximately 4 inches tall, the base is just over 2 inches and the top opening is just over 3 1/2 inches.", "image": [{"@type": "ImageObject", "@context": "https://schema.org", "author": "StonehousePotteryOH", "contentURL": "https://i.etsystatic.com/8740835/r/il/2537fd/7287100059/il_fullxfull.7287100059_hj1w.jpg", "thumbnail": "https://i.etsystatic.com/8740835/r/il/2537fd/7287100059/il_340x270.7287100059_hj1w.jpg"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "StonehousePotteryOH", "contentURL": "https://i.etsystatic.com/8740835/r/il/d8dd86/7287100067/il_fullxfull.7287100067_i29o.jpg", "thumbnail": "https://i.etsystatic.com/8740835/r/il/d8dd86/7287100067/il_340x270.7287100067_i29o.jpg"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "StonehousePotteryOH", "contentURL": "https://i.etsystatic.com/8740835/r/il/221f3d/1409835265/il_fullxfull.1409835265_erg5.jpg", "thumbnail": "https://i.etsystatic.com/8740835/r/il/221f3d/1409835265/il_340x270.1409835265_erg5.jpg", "description": "Blue and green Handthrown Pottery Mug"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "StonehousePotteryOH", "contentURL": "https://i.etsystatic.com/8740835/r/il/ac3727/2716760440/il_fullxfull.2716760440_dtuf.jpg", "thumbnail": "https://i.etsystatic.com/8740835/r/il/ac3727/2716760440/il_340x270.2716760440_dtuf.jpg", "description": "Blue and green Handthrown Pottery Mug"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "StonehousePotteryOH", "contentURL": "https://i.etsystatic.com/8740835/r/il/2b4ce4/2716760606/il_fullxfull.2716760606_r4r6.jpg", "thumbnail": "https://i.etsystatic.com/8740835/r/il/2b4ce4/2716760606/il_340x270.2716760606_r4r6.jpg", "description": "Blue and green Handthrown Pottery Mug"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "StonehousePotteryOH", "contentURL": "https://i.etsystatic.com/8740835/r/il/12bca6/7287100065/il_fullxfull.7287100065_704l.jpg", "thumbnail": "https://i.etsystatic.com/8740835/r/il/12bca6/7287100065/il_340x270.7287100065_704l.jpg"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "StonehousePotteryOH", "contentURL": "https://i.etsystatic.com/8740835/r/il/019498/7239137826/il_fullxfull.7239137826_gwof.jpg", "thumbnail": "https://i.etsystatic.com/8740835/r/il/019498/7239137826/il_340x270.7239137826_gwof.jpg"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "StonehousePotteryOH", "contentURL": "https://i.etsystatic.com/8740835/r/il/5b0308/7287100063/il_fullxfull.7287100063_jl3a.jpg", "thumbnail": "https://i.etsystatic.com/8740835/r/il/5b0308/7287100063/il_340x270.7287100063_jl3a.jpg"}], "category": "Haus & Wohnen < Küche & Essen < Trink- & Barzubehör < Gläser < Becher", "brand": {"@type": "Brand", "@context": "https://schema.org", "name": "StonehousePotteryOH", "@id": "https://www.etsy.com/de/shop/StonehousePotteryOH#shop"}, "logo": "https://i.etsystatic.com/8740835/r/isla/f10a52/38851706/isla_500x500.38851706_2x3sl6r2.jpg", "aggregateRating": {"@type": "AggregateRating", "ratingValue": "5.0", "reviewCount": 827}, "offers": {"@type": "Offer", "eligibleQuantity": 15, "price": "33.70", "@id": "https://www.etsy.com/de/listing/519688604/handthrown-pottery-mug#offer", "priceCurrency": "EUR", "availability": "https://schema.org/InStock", "shippingDetails": {"@type": "OfferShippingDetails", "shippingRate": {"@type": "MonetaryAmount", "value": "0", "currency": "EUR"}, "shippingOrigin": {"@type": "DefinedRegion", "addressCountry": "US"}}}, "review": [{"@type": "Review", "reviewRating": {"@type": "Rating", "ratingValue": 5, "bestRating": 5}, "datePublished": "2026-09-09", "reviewBody": "REDACTED REVIEW TEXT — a real customer wrote here; the structure is kept, the words are not.", "author": {"@type": "Person", "name": "REDACTED REVIEWER"}}, {"@type": "Review", "reviewRating": {"@type": "Rating", "ratingValue": 5, "bestRating": 5}, "datePublished": "2026-08-29", "reviewBody": "REDACTED REVIEW TEXT — a real customer wrote here; the structure is kept, the words are not.", "author": {"@type": "Person", "name": "REDACTED REVIEWER"}}], "material": "Keramik"}</script>
</head><body>
<img src="https://i.etsystatic.com/x/il_fullxfull.jpg"/>
<div class="wt-display-flex-xs wt-align-items-center wt-flex-wrap appears-ready" data-buy-box-region="price" data-selector="price-only">
<p class="wt-text-title-larger wt-mr-xs-1">
<span class="wt-screen-reader-only">Preis:</span>33,70 €
    </p>
<div aria-live="assertive" class="wt-spinner wt-spinner--01 wt-display-none" data-buy-box-price-spinner="" data-clg-id="WtSpinner">
<span class="wt-icon"></span>
        Wird geladen...
    </div>
</div>
</body></html>"""

# A listing WITH VARIATIONS, whose offer is an AggregateOffer carrying
# lowPrice/highPrice and NO `price` key — the shape that returns None
# from a naive .get("price") on a product with a perfectly good price.
LISTING_VARIATIONS_FIXTURE = """<html lang="de"><head>
<script type="application/ld+json">{"@type": "Product", "@context": "https://schema.org", "url": "https://www.etsy.com/de/listing/1000079942/handgemachter-irisierender-pailletten", "name": "Handgemachter Irisierender Pailletten Fransen Rock: Weisses Tassel Festival Outfit", "sku": "1000079942", "gtin": "n/a", "description": "Handgemachter irisierender Paillettenrock mit irisierenden Pailletten, Gummibündchen in der Taille und 2 lagiger weißer Quaste.\\n\\nDies ist voll verstellbar und Krawatten mit Bändern\\n\\nPerfekt für jede Party, Strand oder Festivalevent!\\n\\nIch verpacke die Schulterklappen gut und versende eine Box in Karton, damit es keinen Schaden nimmt\\n\\nAlle Schulterklappen und Epauletten habe ich selbst entworfen und angefertigt\\n\\nEs ist ein handgefertigter Artikel und verwendet Profil, um es herzustellen. Kein Kleber verwendet\\n\\nIch würde mich freuen, Feedback und Fotos von Ihnen zu erhalten, die Ihr Kostüm tragen\\n\\nMein Shop ist für Sonderbestellungen verfügbar und bietet spezielle Rabatte für Großhandelsbestellungen an.\\n\\nBei Sammelbestellungen benötige ich eine gewisse Bearbeitungszeit\\n\\n\\n    Alle Fotos sind echt\\nhttps://www.etsy.com/de/listing/721160409/goldfarbene-pailletten-pink-silber-and-pink-gold-beige\\n\\nGRÖßE:\\nQuaste und Pailletten Länge: 30 cm\\nBund: 3 cm\\n\\nVielen Dank für Ihr Besuch\\n\\nfür lila Option:\\n\\nhttps://www.etsy.com/listing/863466544/purple-sequin-fringed-skirtmix-sequin?ref=shop_home_active_94", "image": [{"@type": "ImageObject", "@context": "https://schema.org", "author": "ByDENIZdsgn", "contentURL": "https://i.etsystatic.com/19632075/r/il/e76c0c/3125104991/il_fullxfull.3125104991_smt4.jpg", "description": "K&ouml;nnte beinhalten: Ein wei&szlig;er Rock mit schimmernden Pailletten und Fransen. Der Rock hat einen silbernen Bund.", "thumbnail": "https://i.etsystatic.com/19632075/r/il/e76c0c/3125104991/il_340x270.3125104991_smt4.jpg"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "ByDENIZdsgn", "contentURL": "https://i.etsystatic.com/19632075/r/il/839c96/3062936586/il_fullxfull.3062936586_3yrz.jpg", "description": "K&ouml;nnte beinhalten: Ein wei&szlig;er Fransenrock mit schimmernden Pailletten in verschiedenen Formen und Gr&ouml;&szlig;en. Der Rock hat einen silbernen Gummizug.", "thumbnail": "https://i.etsystatic.com/19632075/r/il/839c96/3062936586/il_340x270.3062936586_3yrz.jpg"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "ByDENIZdsgn", "contentURL": "https://i.etsystatic.com/19632075/r/il/d8be8f/3062936720/il_fullxfull.3062936720_2ptc.jpg", "description": "K&ouml;nnte beinhalten: Ein wei&szlig;er Rock mit schimmernden Pailletten und Fransen. Der Rock hat einen silbernen G&uuml;rtel aus Band und wird seitlich gebunden.", "thumbnail": "https://i.etsystatic.com/19632075/r/il/d8be8f/3062936720/il_340x270.3062936720_2ptc.jpg"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "ByDENIZdsgn", "contentURL": "https://i.etsystatic.com/19632075/r/il/5e19df/3062936614/il_fullxfull.3062936614_pa70.jpg", "description": "K&ouml;nnte beinhalten: Ein silberner Paillettenrock mit wei&szlig;en Fransen und einer silbernen Schleife zum Binden. Die Pailletten sind schillernd und haben verschiedene Farben, darunter Rosa, Blau und Gr&uuml;n.", "thumbnail": "https://i.etsystatic.com/19632075/r/il/5e19df/3062936614/il_340x270.3062936614_pa70.jpg"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "ByDENIZdsgn", "contentURL": "https://i.etsystatic.com/19632075/r/il/a35da9/3110769469/il_fullxfull.3110769469_2un5.jpg", "description": "K&ouml;nnte beinhalten: Ein schwarzer Rock mit goldfarbenem Bund und lila und rosa Pailletten. Der Rock hat einen Fransenbesatz aus schwarzen F&auml;den.", "thumbnail": "https://i.etsystatic.com/19632075/r/il/a35da9/3110769469/il_340x270.3110769469_2un5.jpg"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "ByDENIZdsgn", "contentURL": "https://i.etsystatic.com/19632075/r/il/0d07c9/3062936738/il_fullxfull.3062936738_cy7i.jpg", "description": "K&ouml;nnte beinhalten: Ein wei&szlig;er Rock mit schimmernden Pailletten und Fransen. Der Rock ist aus einem weichen, flie&szlig;enden Stoff gefertigt und hat eine bequeme Passform.", "thumbnail": "https://i.etsystatic.com/19632075/r/il/0d07c9/3062936738/il_340x270.3062936738_cy7i.jpg"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "ByDENIZdsgn", "contentURL": "https://i.etsystatic.com/19632075/r/il/421281/3062936724/il_fullxfull.3062936724_hp6q.jpg", "description": "K&ouml;nnte beinhalten: Ein wei&szlig;er Rock mit schimmernden Pailletten und Fransen. Die Pailletten haben verschiedene Formen, darunter Kreise und Ovale.", "thumbnail": "https://i.etsystatic.com/19632075/r/il/421281/3062936724/il_340x270.3062936724_hp6q.jpg"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "ByDENIZdsgn", "contentURL": "https://i.etsystatic.com/19632075/r/il/315180/3110673513/il_fullxfull.3110673513_j5au.jpg", "description": "K&ouml;nnte beinhalten: Ein wei&szlig;er Rock mit schimmernden Pailletten und Fransen. Der Rock hat einen silbernen Gummizug.", "thumbnail": "https://i.etsystatic.com/19632075/r/il/315180/3110673513/il_340x270.3110673513_j5au.jpg"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "ByDENIZdsgn", "contentURL": "https://i.etsystatic.com/19632075/r/il/1a9b37/3110673769/il_fullxfull.3110673769_29rh.jpg", "description": "K&ouml;nnte beinhalten: Ein wei&szlig;er Rock mit schimmernden Pailletten und einem Fransen-Saum. Der Rock ist aus einem weichen, flie&szlig;enden Stoff gefertigt und hat eine bequeme Passform.", "thumbnail": "https://i.etsystatic.com/19632075/r/il/1a9b37/3110673769/il_340x270.3110673769_29rh.jpg"}, {"@type": "ImageObject", "@context": "https://schema.org", "author": "ByDENIZdsgn", "contentURL": "https://i.etsystatic.com/19632075/r/il/947e2c/3064703330/il_fullxfull.3064703330_5doc.jpg", "description": "K&ouml;nnte beinhalten: Ein wei&szlig;er Fransenrock mit schimmernden Pailletten in verschiedenen Formen und Gr&ouml;&szlig;en. Der Rock hat einen silbernen G&uuml;rtel aus Band und bindet sich hinten.", "thumbnail": "https://i.etsystatic.com/19632075/r/il/947e2c/3064703330/il_340x270.3064703330_5doc.jpg"}], "category": "Kleidung < Kleidung für Frauen < Röcke", "brand": {"@type": "Brand", "@context": "https://schema.org", "name": "ByDENIZdsgn"}, "logo": "https://i.etsystatic.com/19632075/r/isla/33e0f2/55691442/isla_500x500.55691442_mbzcerat.jpg", "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.9", "reviewCount": 388}, "offers": {"@type": "AggregateOffer", "offerCount": 2, "lowPrice": "145.15", "highPrice": "162.90", "priceCurrency": "EUR", "availability": "https://schema.org/InStock", "shippingDetails": {"@type": "OfferShippingDetails", "shippingOrigin": {"@type": "DefinedRegion", "addressCountry": "TR"}}}, "material": "Paillette/Quaste/Elastischen Lurexband"}</script>
</head><body>
<img src="https://i.etsystatic.com/x/il_fullxfull.jpg"/>
<div class="wt-display-flex-xs wt-align-items-center wt-flex-wrap" data-buy-box-region="price" data-selector="price-only">
<p class="wt-text-title-larger wt-mr-xs-1">
<span class="wt-screen-reader-only">Preis:</span>ab 145,15 €
    </p>
<div aria-live="assertive" class="wt-spinner wt-spinner--01 wt-display-none" data-buy-box-price-spinner="" data-clg-id="WtSpinner">
<span class="wt-icon"></span>
        Wird geladen...
    </div>
</div>
</body></html>"""

# A DataDome refusal with t=bv: the address or the browser is banned and
# the challenge is NOT solvable. Paying for this buys a cookie Etsy
# rejects, which is why `blocked` and `captcha` are separate states.
DD_BANNED_FIXTURE = r"""<html lang="de"><head><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/captchafox/interceptor.js"></script><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/mt_captcha/interceptor.js"></script><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/turnstile/interceptor.js"></script><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/turnstile/hunter.js" data-ts-input="cf-turnstile-response"></script><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/amazon_waf/interceptor.js"></script><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/yandex/interceptor.js"></script><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/lemin/interceptor.js"></script><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/arkoselabs/hunter.js"></script><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/arkoselabs/interceptor.js"></script><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/recaptcha/interceptor.js"></script><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/recaptcha/hunter.js"></script><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/keycaptcha/hunter.js"></script><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/geetest_v4/interceptor.js"></script><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/geetest/interceptor.js"></script><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/communication_helpers.js"></script><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/core_helpers.js"></script><title>etsy.com</title><style>#cmsg{animation: A 1.5s;}@keyframes A{0%{opacity:0;}99%{opacity:0;}100%{opacity:1;}}</style><meta name="viewport" content="width=device-width, initial-scale=PLACEHOLDER"><captcha-widgets></captcha-widgets></head><body style="margin:0"><script data-cfasync="false">var dd={'rt':'c','cid':'PLACEHOLDER','hsh':'PLACEHOLDER','t':'bv','rr':'','qp':'q%3Dhandmade%2Bmug','s':45977,'e':'PLACEHOLDER','host':'geo.captcha-delivery.com','cookie':'PLACEHOLDER'}</script><script data-cfasync="false" src="https://ct.captcha-delivery.com/c.js"></script><iframe src="https://geo.captcha-delivery.com/captcha/?initialCid=PLACEHOLDER&amp;hash=PLACEHOLDER&amp;cid=PLACEHOLDER&amp;t=bv&amp;referer=https%3A%2F%2Fwww.etsy.com%2Fsearch%3Fq%3Dhandmade%2Bmug%26dd_referrer%3D&amp;s=45977&amp;e=PLACEHOLDER&amp;dm=cd" sandbox="allow-scripts allow-same-origin allow-forms" allow="accelerometer; gyroscope; magnetometer" title="DataDome CAPTCHA" width="100%" height="100%" style="height:100vh;" frameborder="0" border="0" scrolling="yes"></iframe></body></html>"""

# rt=i: a device check IN PROGRESS, with no `t` at all and its iframe
# still on /interstitial/. Measured resolving into either content or a
# t=fe challenge about six seconds later — which is why the engines wait
# it out (page_flow.settle_datadome) instead of deciding here.
DD_INTERSTITIAL_FIXTURE = r"""<html lang="en"><head><title>etsy.com</title><style>#cmsg{animation: A 1.5s;}@keyframes A{0%{opacity:0;}99%{opacity:0;}100%{opacity:1;}}</style><meta name="viewport" content="width=device-width, initial-scale=PLACEHOLDER"></head><body style="margin:0"><script data-cfasync="false">var dd={'rt':'i','cid':'PLACEHOLDER','hsh':'PLACEHOLDER','b':1301560,'s':45977,'e':'PLACEHOLDER','rr':'','qp':'q%3Dhandmade%2Bmug','host':'geo.captcha-delivery.com','cookie':'PLACEHOLDER'}</script><script data-cfasync="false" src="https://ct.captcha-delivery.com/i.js"></script><iframe src="https://geo.captcha-delivery.com/interstitial/?initialCid=PLACEHOLDER&amp;hash=PLACEHOLDER&amp;cid=PLACEHOLDER&amp;referer=https%3A%2F%2Fwww.etsy.com%2Fsearch%3Fq%3Dhandmade%2Bmug&amp;s=45977&amp;e=PLACEHOLDER&amp;b=1301560&amp;dm=cd" sandbox="allow-scripts allow-same-origin allow-forms" allow="accelerometer; gyroscope; magnetometer" title="DataDome Device Check" width="100%" height="100%" style="height:100vh;" frameborder="0" border="0" scrolling="yes"></iframe></body></html>"""

# t=fe: a real slider, and the ONE state worth buying a solve for. Built
# from the interstitial above with the transition applied that was
# observed live — the iframe moving from /interstitial/ to /captcha/ and
# `t` appearing as fe.
DD_SOLVABLE_FIXTURE = r"""<html lang="en"><head><title>etsy.com</title><style>#cmsg{animation: A 1.5s;}@keyframes A{0%{opacity:0;}99%{opacity:0;}100%{opacity:1;}}</style><meta name="viewport" content="width=device-width, initial-scale=PLACEHOLDER"></head><body style="margin:0"><script data-cfasync="false">var dd={'rt':'c','t':'fe','cid':'PLACEHOLDER','hsh':'PLACEHOLDER','b':1301560,'s':45977,'e':'PLACEHOLDER','rr':'','qp':'q%3Dhandmade%2Bmug','host':'geo.captcha-delivery.com','cookie':'PLACEHOLDER'}</script><script data-cfasync="false" src="https://ct.captcha-delivery.com/i.js"></script><iframe src="https://geo.captcha-delivery.com/captcha/?initialCid=PLACEHOLDER&amp;hash=PLACEHOLDER&amp;cid=PLACEHOLDER&amp;referer=https%3A%2F%2Fwww.etsy.com%2Fsearch%3Fq%3Dhandmade%2Bmug&amp;s=45977&amp;e=PLACEHOLDER&amp;b=1301560&amp;t=fe&amp;dm=cd" sandbox="allow-scripts allow-same-origin allow-forms" allow="accelerometer; gyroscope; magnetometer" title="DataDome Device Check" width="100%" height="100%" style="height:100vh;" frameborder="0" border="0" scrolling="yes"></iframe></body></html>"""

# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
def test_price_parsing():
    group("price and currency parsing")
    prices_in = product_parser._prices_in
    normalize = product_parser._normalize_amount
    ok = True

    # The three grouping conventions, all of which Etsy's storefronts use.
    ok &= check("1,234.56 -> 1234.56", normalize("1,234.56") == 1234.56)
    ok &= check("1.234,56 -> 1234.56", normalize("1.234,56") == 1234.56)
    ok &= check("1 234,56 -> 1234.56 (plain space)", normalize("1 234,56") == 1234.56)
    ok &= check("1 234,56 -> 1234.56 (NBSP)", normalize("1 234,56") == 1234.56)
    ok &= check("1 234,56 -> 1234.56 (narrow NBSP)",
                normalize("1 234,56") == 1234.56)
    ok &= check("1 234,56 -> 1234.56 (thin space)",
                normalize("1 234,56") == 1234.56)

    # A single separator with exactly three trailing digits is a THOUSANDS
    # grouping: no currency here has a 3-digit subunit.
    ok &= check("$1,234 is 1234 not 1.234", normalize("1,234") == 1234)
    ok &= check("14,90 is 14.9 (two trailing digits)", normalize("14,90") == 14.9)

    # Currency, in descending trustworthiness.
    ok &= check("suffix euro reads EUR",
                prices_in("106,15 €") == ([106.15], "EUR"))
    ok &= check("prefix pound reads GBP",
                prices_in("£15.85") == ([15.85], "GBP"))
    ok &= check("bare $ with no locale is None, not USD",
                prices_in("$31.50")[1] is None)
    ok &= check("bare $ on the US storefront reads USD",
                prices_in("$31.50", host_cur="USD") == ([31.5], "USD"))
    ok &= check("CA$ is CAD, not USD swallowing the prefix",
                prices_in("CA$31.50") == ([31.5], "CAD"))
    ok &= check("A$ is AUD", prices_in("A$31.50") == ([31.5], "AUD"))
    ok &= check("written ISO code wins", prices_in("EUR 100") == ([100.0], "EUR"))
    ok &= check("three capitals that are NOT a currency are not a price",
                prices_in("XXL 100") == ([], None))

    # A discount badge is not a price, and it lives INSIDE Etsy's price node.
    ok &= check("'(40% Rabatt)' contributes no price",
                prices_in("(40% Rabatt)") == ([], None))
    ok &= check("percentage stripped before matching, price survives",
                prices_in("35,87 € (40% Rabatt)") == ([35.87], "EUR"))
    ok &= check("percent-before-number badge does not steal the symbol",
                prices_in("-%10,34 €25.999")[0] == [25999.0])

    # The dash-cents form is a MediaMarkt convention and does NOT appear on
    # Etsy (0 occurrences across every capture). Pinned as a known
    # limitation rather than half-guarded: if Etsy ever prints "349,- €",
    # this check turns red and the decision becomes deliberate.
    ok &= check("dash cents are NOT parsed (measured absent on Etsy)",
                prices_in("349,– €")[0] != [349.0])
    return ok


# ---------------------------------------------------------------------------
# The second storefront — what one locale can never tell you
# ---------------------------------------------------------------------------
def test_second_locale():
    group("the US storefront: USD, English labels, and what is translated")
    ok = True
    us = {r.sku: r for r in parse_products(US_SEARCH_FIXTURE, US_SEARCH_URL)}
    de = {r.sku: r for r in parse_products(SEARCH_FIXTURE, SEARCH_URL)}

    # A BARE "$" RESOLVED THROUGH THE LOCALE TABLE. This is the check the
    # German captures could not make, and the one most likely to be wrong: on
    # Etsy "$" means seven different currencies across the storefronts, so it
    # cannot be read as USD by itself — the URL has to say.
    ok &= check("the default storefront has no locale prefix",
                locale_of(US_SEARCH_URL) == "")
    ok &= check("and it resolves to USD", host_currency(US_SEARCH_URL) == "USD")
    ok &= check("every US row reads USD from a bare $",
                us and all(r.currency == "USD" for r in us.values()))
    ok &= check("every German row reads EUR",
                de and all(r.currency == "EUR" for r in de.values()))
    ok &= check("a bare $ with NO locale is None, not a defaulted USD",
                product_parser._prices_in("$31.50")[1] is None)

    # US price formatting is the OTHER decimal convention: $34.87 with a dot,
    # against 34,87 € with a comma. Both live, both pinned.
    ok &= check("US prices parse with a dot decimal",
                us["1702915637"].price == 34.87)
    ok &= check("a US sale keeps both figures the right way round",
                (us["4512181427"].price, us["4512181427"].original_price)
                == (130.0, 520.0))
    ok &= check("and its discount is computed, not read",
                us["4512181427"].discount_pct == 75.0)

    # THE AD SIGNAL AGAINST AN ENGLISH LABEL. It was verified 280/280 against
    # German text; this is the same structural parameter checked against
    # "Ad from shop", which is what makes it a locale-independent signal
    # rather than one that happens to work on one language.
    ok &= check("the English-labelled ad tile is flagged",
                us["4296141840"].is_ad is True)
    ok &= check("an English organic tile is not",
                us["1702915637"].is_ad is False)
    ok &= check("the US fixture really does say 'Ad from shop'",
                "Ad from shop" in US_SEARCH_FIXTURE)
    ok &= check("and the German one really does say 'Anzeige'",
                "Anzeige" in SEARCH_FIXTURE)

    # The rating markup is the NEW form on both storefronts' search pages, so
    # the two forms are a per-PAGE-KIND difference and not a per-locale one.
    ok &= check("US search tiles use the custom-element rating markup",
                "clg-static-review-stars" in US_SEARCH_FIXTURE)
    ok &= check("US ratings are read", all(r.shop_rating is not None
                                           for r in us.values()))

    # WHICH FIELDS ARE STABLE ACROSS STOREFRONTS AND WHICH ARE NOT. Getting
    # this wrong is what makes a cross-locale diff look like a catalogue
    # change, and it is why diff_runs now refuses one.
    a = parse_product_page(US_LISTING_FIXTURE, US_LISTING_URL)[0]
    b = parse_product_page(LISTING_FIXTURE, LISTING_URL)[0]
    for f in ("sku", "title", "brand", "rating", "in_stock", "ships_from"):
        ok &= check("%s is identical across storefronts" % f,
                    getattr(a, f) == getattr(b, f))
    ok &= check("price differs (Etsy converts at a live rate)",
                a.price != b.price)
    ok &= check("currency differs", (a.currency, b.currency) == ("USD", "EUR"))
    ok &= check("material is TRANSLATED, so it is not an identifier",
                (a.material, b.material) == ("Ceramic", "Keramik"))
    ok &= check("the breadcrumb category is translated too",
                a.category != b.category and "Home & Living" in a.category)

    # A NULL THAT IS THE PAGE'S SILENCE, NOT A LOST READ. The US listing's
    # shippingDetails carries a shippingOrigin and NO shippingRate, so there
    # is nothing to read; the German one publishes a rate of 0. Same listing,
    # same parser, different storefront.
    ok &= check("free_shipping is None where the US page does not say",
                a.free_shipping is None)
    ok &= check("and True where the German page publishes a zero rate",
                b.free_shipping is True)
    ok &= check("the US fixture really has no shippingRate",
                "shippingRate" not in US_LISTING_FIXTURE)
    return ok


# ---------------------------------------------------------------------------
# Sponsored listings — the locale trap
# ---------------------------------------------------------------------------
def test_ads():
    group("sponsored listings: structural signal, not a localised label")
    ok = True
    rows = {r.sku: r for r in parse_products(SEARCH_FIXTURE, SEARCH_URL)}

    ok &= check("the ad tile is flagged", rows["1881219122"].is_ad is True)
    ok &= check("an organic tile is not", rows["1765159813"].is_ad is False)

    # The signal is the link's own `ls` parameter, so it survives a locale
    # whose label has never been captured. Both directions are checked,
    # because a detector that says True for everything would pass one of them.
    de = page('<div data-listing-id="1" data-shop-id="9">'
              '<a href="/de/listing/1/x?ls=a&amp;ref=x">Anzeige des Etsy-Shops</a>'
              '<h3 class="v2-listing-card__title">T</h3>'
              '<div class="n-listing-card__price">'
              '<span class="currency-symbol">€</span>'
              '<span class="currency-value">10,00</span></div></div>')
    en = de.replace("ls=a", "ls=s").replace("Anzeige des Etsy-Shops", "Ad from shop")
    ok &= check("ls=a is an ad whatever the label says",
                parse_products(de, SEARCH_URL)[0].is_ad is True)
    ok &= check("ls=s is organic even when the text says 'Ad'",
                parse_products(en, SEARCH_URL)[0].is_ad is False)

    no_ls = de.replace("?ls=a&amp;ref=x", "")
    ok &= check("a tile with no ls says None rather than guessing False",
                parse_products(no_ls, SEARCH_URL)[0].is_ad is None)

    # The per-impression parameters must not reach the stored URL, or two
    # runs of the same listing look like different rows.
    url = parse_products(de, SEARCH_URL)[0].url
    ok &= check("tracking parameters are stripped from the row's url",
                "ls=" not in url and "ref=" not in url and "/listing/1/" in url)
    return ok


# ---------------------------------------------------------------------------
# The two rating markups, and WHOSE rating it is
# ---------------------------------------------------------------------------
def test_ratings():
    group("ratings: two live markups, and the shop-vs-listing distinction")
    ok = True
    search = {r.sku: r for r in parse_products(SEARCH_FIXTURE, SEARCH_URL)}
    category = {r.sku: r for r in parse_products(CATEGORY_FIXTURE, CATEGORY_URL)}

    # Form 1: the custom element on a search page, numbers in ATTRIBUTES.
    ok &= check("search tile rating read from clg-static-review-stars",
                search["1765159813"].shop_rating == 5.0)
    ok &= check("search tile review count read from review-count-text",
                search["1765159813"].shop_review_count == 495)

    # Form 2: the sprite classes on a category page, in the same hour.
    ok &= check("category tile rating read from the star-rating- CLASS",
                category["4541371530"].shop_rating == 5.0)
    ok &= check("category tile review count read from the '(n)' beside it",
                category["4541371530"].shop_review_count == 7)
    ok &= check("a half star reads as .5",
                category["4448501401"].shop_rating == 4.5)

    # The class is used and not the aria-label, because the label is written
    # in the page's own language.
    localised = page(
        '<div data-listing-id="2" data-shop-id="9">'
        '<a href="/de/listing/2/x?ls=s">x</a>'
        '<h3 class="v2-listing-card__title">T</h3>'
        '<span><div class="sprite-img star-rating-4-5" '
        'aria-label="4,5 sur 5 étoiles"></div>(12)</span>'
        '<div class="n-listing-card__price">'
        '<span class="currency-symbol">€</span>'
        '<span class="currency-value">9,00</span></div></div>')
    row = parse_products(localised, SEARCH_URL)[0]
    ok &= check("a French aria-label still yields 4.5 from the class",
                row.shop_rating == 4.5 and row.shop_review_count == 12)

    # THE DISTINCTION. A tile's stars are the SHOP's; only a detail page
    # states the listing's own. Reading one into the other's column would
    # make `rating` mean different things in different modes.
    ok &= check("a listing row's own `rating` is null (Etsy publishes none)",
                all(r.rating is None for r in search.values()))
    ok &= check("a listing row's own `review_count` is null too",
                all(r.review_count is None for r in search.values()))
    detail = parse_product_page(LISTING_FIXTURE, LISTING_URL)[0]
    ok &= check("a detail row's `rating` IS populated", detail.rating == 5.0)
    ok &= check("a detail row's `review_count` is the LISTING's (827)",
                detail.review_count == 827)
    ok &= check("the shop's own count (16679) is a DIFFERENT number",
                shop_metadata(SHOP_FIXTURE, SHOP_URL)["shop_review_count"] == 16679)

    # A title with a parenthesised number must not be read as a review count.
    trap = page(
        '<div data-listing-id="3" data-shop-id="9">'
        '<a href="/de/listing/3/x?ls=s">x</a>'
        '<div class="v2-listing-card__info">'
        '<h3 class="v2-listing-card__title">Keramikbecher (300 ml)</h3>'
        '<span><div class="sprite-img star-rating-5"></div></span>'
        '<div class="n-listing-card__price">'
        '<span class="currency-symbol">€</span>'
        '<span class="currency-value">9,00</span></div></div></div>')
    row = parse_products(trap, SEARCH_URL)[0]
    ok &= check("'(300 ml)' in the title is not read as 300 reviews",
                row.shop_review_count is None)
    return ok


# ---------------------------------------------------------------------------
# Prices on a tile: two figures in one node
# ---------------------------------------------------------------------------
def test_tile_prices():
    group("tile prices: sale vs original in one node, and the from-price")
    ok = True
    search = {r.sku: r for r in parse_products(SEARCH_FIXTURE, SEARCH_URL)}
    category = {r.sku: r for r in parse_products(CATEGORY_FIXTURE, CATEGORY_URL)}

    # A discounted tile: the price node's text holds BOTH figures plus a
    # percentage badge. Values pinned from the real capture.
    disc = search["4361727707"]
    ok &= check("discounted tile: price is the SALE price", disc.price == 25.6)
    ok &= check("discounted tile: original_price is the strike", disc.original_price == 32.0)
    ok &= check("discounted tile: discount computed, not read from the badge",
                disc.discount_pct == 20.0)

    cat_disc = category["4339556119"]
    ok &= check("category discounted tile: 14.39 / 17.99",
                (cat_disc.price, cat_disc.original_price) == (14.39, 17.99))

    # THE INVARIANT. `original_price` at or below `price` means the two
    # figures are not what they were taken for; the parser must report None
    # rather than a zero or negative discount.
    for label, rows in (("search", search), ("category", category)):
        bad = [r.sku for r in rows.values()
               if r.original_price is not None and r.original_price <= (r.price or 0)]
        ok &= check("%s: no row has original_price at or below price" % label, not bad)
    ok &= check("a was-price below the price yields discount None, not negative",
                product_parser._discount_from(100.0, 80.0) is None)

    # The from-price. Both faces of it: the tile's "ab", and the
    # AggregateOffer that has no `price` key at all.
    ok &= check("'ab 34,00 €' sets price_is_from",
                search["4461212869"].price_is_from is True)
    ok &= check("a plain price does not", search["1765159813"].price_is_from is None)
    var = parse_product_page(LISTING_VARIATIONS_FIXTURE, LISTING_VARIATIONS_URL)[0]
    ok &= check("AggregateOffer: price is lowPrice", var.price == 145.15)
    ok &= check("AggregateOffer: price_max is highPrice", var.price_max == 162.9)
    ok &= check("AggregateOffer: price_is_from is set", var.price_is_from is True)
    ok &= check("AggregateOffer: currency still read", var.currency == "EUR")

    # An instalment or shipping line inside the price container must not be
    # read as the price. Etsy prints the promotion line there.
    scoped = page(
        '<div data-listing-id="4" data-shop-id="9">'
        '<a href="/de/listing/4/x?ls=s">x</a>'
        '<h3 class="v2-listing-card__title">T</h3>'
        '<div class="n-listing-card__price">'
        '<p class="lc-price"><span class="currency-symbol">€</span>'
        '<span class="currency-value">35,87</span></p>'
        '<p class="search-collage-promotion-price">'
        '<span class="wt-text-strikethrough">'
        '<span class="currency-symbol">€</span>'
        '<span class="currency-value">59,79</span></span>'
        ' Ursprünglicher Preis 59,79 € (40% Rabatt)</p></div></div>')
    row = parse_products(scoped, SEARCH_URL)[0]
    ok &= check("current price read with the promotion subtree removed",
                row.price == 35.87)
    ok &= check("original price read from the strikethrough only",
                row.original_price == 59.79)
    return ok


# ---------------------------------------------------------------------------
# JSON-LD shapes that are legal and break a naive parser
# ---------------------------------------------------------------------------
def test_jsonld_shapes():
    group("legal JSON-LD shapes a naive parser gets wrong")
    ok = True

    def detail(product_node, url=LISTING_URL):
        doc = ('<html><head><script type="application/ld+json">%s</script>'
               '</head><body><img src="https://i.etsystatic.com/x.jpg"/>'
               '</body></html>' % json.dumps(product_node))
        return parse_product_page(doc, url)

    base = {"@context": "https://schema.org", "@type": "Product",
            "url": LISTING_URL, "sku": "519688604", "name": "T"}

    # `"offers": null` — an EXPLICIT null. A .get() default only applies to a
    # MISSING key, so this is the shape that raises AttributeError.
    rows = detail(dict(base, offers=None))
    ok &= check("offers: null does not raise", len(rows) == 1)
    ok &= check("offers: null leaves price None", rows[0].price is None)

    # A LIST of offers, possibly holding non-dicts.
    rows = detail(dict(base, offers=["junk", {"@type": "Offer", "price": "9.99",
                                              "priceCurrency": "EUR"}]))
    ok &= check("offers as a list with junk in it still finds the offer",
                rows and rows[0].price == 9.99)

    # `aggregateRating: null`.
    rows = detail(dict(base, aggregateRating=None,
                       offers={"@type": "Offer", "price": "1.00"}))
    ok &= check("aggregateRating: null does not raise", len(rows) == 1)

    # `image` as ImageObject list with contentURL — capital URL, which the
    # family's other repos do not read.
    rows = detail(dict(base, offers={"@type": "Offer", "price": "1.00"},
                       image=[{"@type": "ImageObject",
                               "contentURL": "https://i.etsystatic.com/a.jpg",
                               "thumbnail": "https://i.etsystatic.com/t.jpg"}]))
    ok &= check("image_url read from contentURL (capital URL)",
                rows[0].image_url == "https://i.etsystatic.com/a.jpg")
    for shape, expected in (
            ("https://i.etsystatic.com/s.jpg", "https://i.etsystatic.com/s.jpg"),
            ({"@type": "ImageObject", "url": "https://i.etsystatic.com/u.jpg"},
             "https://i.etsystatic.com/u.jpg"),
            ({"@type": "ImageObject", "contentUrl": "https://i.etsystatic.com/c.jpg"},
             "https://i.etsystatic.com/c.jpg")):
        rows = detail(dict(base, offers={"@type": "Offer", "price": "1.00"},
                           image=shape))
        ok &= check("image shape %s reads" % type(shape).__name__ + " " + expected[-9:],
                    rows[0].image_url == expected)

    # Products under `@graph` rather than `itemListElement` — the shape that
    # returns "zero products, silently".
    graph = ('<html><head><script type="application/ld+json">%s</script></head>'
             '<body><img src="https://i.etsystatic.com/x.jpg"/></body></html>'
             % json.dumps({"@context": "https://schema.org", "@graph": [
                 {"@type": "Product", "url": LISTING_URL, "sku": "519688604",
                  "name": "In a graph",
                  "offers": {"@type": "Offer", "price": "7.00",
                             "priceCurrency": "EUR"}}]}))
    rows = parse_product_page(graph, LISTING_URL)
    ok &= check("a Product under @graph is found", rows and rows[0].price == 7.0)

    # A detail page's carousels are products too. The node whose id matches
    # the URL must win, or a run reports a neighbour's price.
    two = ('<html><head>'
           '<script type="application/ld+json">%s</script>'
           '<script type="application/ld+json">%s</script>'
           '</head><body><img src="https://i.etsystatic.com/x.jpg"/></body></html>'
           % (json.dumps({"@context": "https://schema.org", "@type": "Product",
                          "url": "https://www.etsy.com/de/listing/999/other",
                          "sku": "999", "name": "A neighbour from the carousel",
                          "offers": {"@type": "Offer", "price": "1.00"}}),
              json.dumps({"@context": "https://schema.org", "@type": "Product",
                          "url": LISTING_URL, "sku": "519688604",
                          "name": "The listing itself",
                          "offers": {"@type": "Offer", "price": "2.00"}})))
    rows = parse_product_page(two, LISTING_URL)
    ok &= check("the node matching the URL wins over a carousel neighbour",
                rows and rows[0].sku == "519688604" and rows[0].price == 2.0)

    # The ListPrice shape. `offers.price` is the current price and the nested
    # ListPrice is the was-price — reading it the other way round cut the
    # confirmed share from 56/64 to 40/64 on the live category capture.
    node = {"@type": "Offer", "price": "35.87", "priceCurrency": "EUR",
            "priceSpecification": {"@type": "UnitPriceSpecification",
                                   "priceType": "https://schema.org/ListPrice",
                                   "price": "59.79"}}
    ok &= check("_ld_offer_prices returns the CURRENT price, not the ListPrice",
                product_parser._ld_offer_prices(node)[0] == 35.87)
    ok &= check("_ld_list_price returns the was-price",
                product_parser._ld_list_price(node) == 59.79)
    ok &= check("a priceSpecification that is not a ListPrice is ignored",
                product_parser._ld_list_price(
                    {"priceSpecification": {"priceType": "https://schema.org/SalePrice",
                                            "price": "1.00"}}) is None)

    # A listing page's ItemList entries carry no `sku`, only a `url`, so the
    # join onto DOM tiles has to fall back to the URL.
    blocks = [{"@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "item": {
            "@type": "Product", "url": "https://www.etsy.com/de/listing/4242/x",
            "name": "No sku field", "offers": {"@type": "Offer", "price": "3.00"}}}]}]
    ok &= check("ItemList entries index by the id in their url",
                "4242" in product_parser._ld_by_sku(blocks))

    # A placeholder is not a value.
    rows = detail(dict(base, offers={"@type": "Offer", "price": "1.00"},
                       gtin="n/a", material="  "))
    ok &= check("gtin 'n/a' is None, not the string 'n/a'", rows[0].gtin is None)
    ok &= check("blank material is None", rows[0].material is None)
    return ok


# ---------------------------------------------------------------------------
# Listing pages, pinned to values from real captures
# ---------------------------------------------------------------------------
def test_listing_values():
    group("listing rows, pinned to values from real captures")
    ok = True

    search = parse_products(SEARCH_FIXTURE, SEARCH_URL, page=1)
    ok &= check("search fixture yields 5 rows (one per distinct id)",
                len(search) == 5)
    ok &= check("no duplicate skus, though Etsy emits each tile twice",
                len({r.sku for r in search}) == len(search))

    by_sku = {r.sku: r for r in search}
    row = by_sku["1765159813"]
    ok &= check("title pinned", row.title.startswith("Ladybug Ceramic Mug"))
    ok &= check("brand is the SHOP name", row.brand == "DaNaKeramikShop")
    ok &= check("price pinned", row.price == 54.9)
    ok &= check("currency pinned", row.currency == "EUR")
    ok &= check("shop_id pinned", row.shop_id == "52168346")
    ok &= check("url is absolute and carries the listing id",
                row.url.startswith("https://www.etsy.com/") and "1765159813" in row.url)
    ok &= check("image_url points at Etsy's CDN",
                row.image_url and "etsystatic.com" in row.image_url)
    ok &= check("page recorded", row.page == 1)
    ok &= check("position recorded and 1-based", row.position >= 1)
    ok &= check("category on a search URL is the query",
                row.category == "handmade mug")
    ok &= check("source is the host", row.source == "etsy.com")

    # Coverage, as MEASURED on these fixtures, so a null is not read as a bug.
    for label, rows in (("search", search),
                        ("category", parse_products(CATEGORY_FIXTURE, CATEGORY_URL)),
                        ("shop", parse_products(SHOP_FIXTURE, SHOP_URL))):
        n = len(rows)
        ok &= check("%s: every row has a sku" % label,
                    all(r.sku for r in rows))
        ok &= check("%s: every row has a title" % label,
                    all(r.title for r in rows))
        ok &= check("%s: every row has a price" % label,
                    all(r.price is not None for r in rows) and n > 0)
        ok &= check("%s: every row has a currency" % label,
                    all(r.currency for r in rows))
        ok &= check("%s: every row has an image" % label,
                    all(r.image_url for r in rows))

    # A shop front's rows carry the seller in `brand` even though its tiles
    # print no shop name at all.
    shop_rows = parse_products(SHOP_FIXTURE, SHOP_URL)
    ok &= check("shop rows carry the seller in brand",
                all(r.brand == "StonehousePotteryOH" for r in shop_rows))
    ok &= check("shop rows have no category (a catalogue spans them)",
                all(r.category is None for r in shop_rows))
    ok &= check("shop rows carry the shop's rating from its Organization data",
                all(r.shop_rating == 5.0 for r in shop_rows))
    return ok


# ---------------------------------------------------------------------------
# Detail pages
# ---------------------------------------------------------------------------
def test_product_detail():
    group("detail rows, pinned to values from a real capture")
    ok = True
    rows = parse_product_page(LISTING_FIXTURE, LISTING_URL)
    ok &= check("one row from one listing page", len(rows) == 1)
    row = rows[0]
    ok &= check("sku pinned", row.sku == "519688604")
    ok &= check("title pinned", row.title == "Handthrown Pottery Mug")
    ok &= check("brand is the shop", row.brand == "StonehousePotteryOH")
    ok &= check("price pinned", row.price == 33.7)
    ok &= check("currency pinned", row.currency == "EUR")
    ok &= check("in_stock True from availability", row.in_stock is True)
    ok &= check("rating pinned", row.rating == 5.0)
    ok &= check("review_count pinned (the LISTING's)", row.review_count == 827)
    ok &= check("category is the breadcrumb string",
                row.category and " < " in row.category)
    ok &= check("free_shipping True from shippingRate 0",
                row.free_shipping is True)
    ok &= check("ships_from pinned", row.ships_from == "US")
    ok &= check("material pinned", row.material == "Keramik")
    ok &= check("images is the full list, not one",
                row.images and len(row.images) == 8)
    ok &= check("image_url is the first of them",
                row.image_url == row.images[0])
    ok &= check("description populated", row.description and len(row.description) > 100)
    ok &= check("price confirmed against the buy box",
                row.price_source == "jsonld+dom")

    # Listing-only columns must be null here, or a consumer cannot tell a
    # detail row from a listing row.
    for f in ("page", "position", "is_ad", "shop_id", "original_price",
              "discount_pct", "shop_rating", "shop_review_count"):
        ok &= check("detail row leaves %s null" % f, getattr(row, f) is None)

    # A delisted listing: HTTP 200, full site chrome, zero product data.
    gone = ('<html lang="de"><head><title>Dieser Artikel ist nicht verfügbar'
            '</title></head><body><img src="https://i.etsystatic.com/x.jpg"/>'
            '<div>Dieser Artikel ist nicht verfügbar</div></body></html>')
    ok &= check("a delisted listing yields no rows rather than a row of nulls",
                parse_product_page(gone, LISTING_URL) == [])
    return ok


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------
def test_urls():
    group("URLs: locales, pagination, categories, skus, hosts")
    ok = True

    # Etsy is one host and the locale is a PATH PREFIX.
    ok &= check("etsy.com is supported", is_supported_host("https://www.etsy.com/x"))
    ok &= check("www. is stripped from the host", site_host("https://www.etsy.com/x") == "etsy.com")
    ok &= check("another marketplace is refused",
                not is_supported_host("https://www.amazon.de/x"))
    ok &= check("etsy.me is refused WITH a reason",
                unsupported_reason("https://etsy.me/abc"))
    ok &= check("the reason says what it is, not 'not Etsy'",
                "shortener" in unsupported_reason("https://etsy.me/abc"))

    ok &= check("no locale segment means the default storefront",
                locale_of("https://www.etsy.com/search?q=x") == "")
    ok &= check("/de/ is a locale", locale_of("https://www.etsy.com/de/search?q=x") == "de")
    ok &= check("/ca-fr/ is a locale too",
                locale_of("https://www.etsy.com/ca-fr/search?q=x") == "ca-fr")
    ok &= check("/c/ is NOT a locale, it is a category route",
                locale_of("https://www.etsy.com/c/jewelry") == "")
    ok &= check("locale stripped from the path for routing",
                path_without_locale("https://www.etsy.com/de/c/jewelry") == "/c/jewelry")

    ok &= check("default storefront quotes USD",
                host_currency("https://www.etsy.com/search?q=x") == "USD")
    ok &= check("/de/ quotes EUR", host_currency("https://www.etsy.com/de/search?q=x") == "EUR")
    ok &= check("/uk/ quotes GBP", host_currency("https://www.etsy.com/uk/search?q=x") == "GBP")
    ok &= check("every locale in the table has a currency",
                all(LOCALE_CURRENCY.values()))

    # Page kinds.
    for url, kind in ((SEARCH_URL, "search"), (CATEGORY_URL, "category"),
                      (SHOP_URL, "shop"), (LISTING_URL, "listing"),
                      ("https://www.etsy.com/de/market/handmade_mug", "market"),
                      ("https://www.etsy.com/de/your/purchases", "other")):
        ok &= check("listing_kind(%s) == %s" % (url.split("etsy.com")[-1][:34], kind),
                    listing_kind(url) == kind)

    # Pagination: ?page=N, replacing rather than duplicating.
    ok &= check("page 1 carries no page parameter",
                page_url(SEARCH_URL, 1) == SEARCH_URL)
    ok &= check("page 2 appends ?page=2", page_url(SEARCH_URL, 2).endswith("page=2"))
    ok &= check("other query parameters are preserved",
                "q=handmade+mug" in page_url(SEARCH_URL, 2))
    twice = page_url(page_url(SEARCH_URL, 2), 3)
    ok &= check("an existing page parameter is REPLACED, not duplicated",
                twice.count("page=") == 1 and twice.endswith("page=3"))
    ok &= check("page_number_from_url reads it back",
                page_number_from_url(page_url(SEARCH_URL, 7)) == 7)
    ok &= check("no page parameter reads back as None",
                page_number_from_url(SEARCH_URL) is None)

    # Categories.
    ok &= check("a /c/ URL yields its leaf", category_from_url(CATEGORY_URL) == "mugs")
    ok &= check("a search URL yields its query",
                category_from_url(SEARCH_URL) == "handmade mug")
    ok &= check("a /market/ URL yields its term",
                category_from_url("https://www.etsy.com/de/market/handmade_mug")
                == "handmade mug")
    ok &= check("a shop front yields no category", category_from_url(SHOP_URL) is None)
    ok &= check("shop_from_url reads the seller",
                shop_from_url(SHOP_URL) == "StonehousePotteryOH")

    # Listing ids: the id FOLLOWS /listing/, and the slug is full of other
    # numbers.
    ok &= check("sku from a listing URL", sku_from_url(LISTING_URL) == "519688604")
    ok &= check("a slug full of numbers does not confuse it",
                sku_from_url("https://www.etsy.com/de/listing/4541371530/"
                             "set-aus-2-10-handbemalten-bechern-300-ml")
                == "4541371530")
    ok &= check("a URL with no listing id yields None",
                sku_from_url("https://www.etsy.com/de/c/jewelry") is None)
    ok &= check("None input is handled", sku_from_url(None) is None)

    # The site's own numbers.
    ok &= check("total_pages read from the embedded config",
                total_pages('x "initial_total_pages":20 y') == 20)
    ok &= check("total_pages is None when the site does not say",
                total_pages("<html></html>") is None)
    ok &= check("total_results read from numberOfItems",
                total_results('x "numberOfItems": 201606 y') == 201606)
    ok &= check("a total smaller than what was rendered is not trusted",
                total_results('"numberOfItems": 3', shown=64) is None)
    return ok


# ---------------------------------------------------------------------------
# Page state: DataDome, and what is worth paying for
# ---------------------------------------------------------------------------
def test_page_state():
    group("page state: t=bv vs t=fe vs interstitial vs empty vs content")
    ok = True

    ok &= check("a listing page is content",
                detect_page_state(SEARCH_FIXTURE, 200, SEARCH_URL) == "content")
    ok &= check("a detail page is content",
                detect_page_state(LISTING_FIXTURE, 200, LISTING_URL) == "content")

    # THE `t` PARAMETER IS THE WHOLE DECISION.
    ok &= check("t=bv is blocked (a solve would buy a rejected cookie)",
                detect_page_state(DD_BANNED_FIXTURE, 403, SEARCH_URL) == "blocked")
    ok &= check("t=bv reports its verdict", datadome_verdict(DD_BANNED_FIXTURE) == "bv")
    ok &= check("t=fe is a captcha (the one state worth paying for)",
                detect_page_state(DD_SOLVABLE_FIXTURE, 403, SEARCH_URL) == "captcha")
    ok &= check("t=fe reports its verdict", datadome_verdict(DD_SOLVABLE_FIXTURE) == "fe")

    # The interstitial has not decided yet.
    ok &= check("rt=i is recognised as an interstitial",
                is_datadome_interstitial(DD_INTERSTITIAL_FIXTURE))
    ok &= check("an interstitial is NOT solvable, so it is not a captcha state",
                detect_page_state(DD_INTERSTITIAL_FIXTURE, 403, SEARCH_URL) == "blocked")
    ok &= check("a served page is not an interstitial",
                not is_datadome_interstitial(SEARCH_FIXTURE))

    # The iframe URL the solver needs, HTML-unescaped.
    cu = datadome_captcha_url(DD_SOLVABLE_FIXTURE)
    ok &= check("captchaUrl extracted for DataDomeSliderTask",
                cu and "captcha-delivery.com" in cu)
    ok &= check("captchaUrl is unescaped (&amp; would break the API call)",
                cu and "&amp;" not in cu and "&t=fe" in cu)

    # Empty is a CORRECT answer, distinct from blocked.
    ok &= check("a real page with no listings is empty, not blocked",
                detect_page_state(
                    '<html><body><img src="https://i.etsystatic.com/x.jpg"/>'
                    '<p>Keine Ergebnisse</p></body></html>', 200, SEARCH_URL)
                == "empty")
    ok &= check("no markup at all with a 4xx is blocked",
                detect_page_state("", 403, SEARCH_URL) == "blocked")
    ok &= check("no markup with a 200 is empty",
                detect_page_state("", 200, SEARCH_URL) == "empty")
    ok &= check("a page built out of none of Etsy's assets is blocked",
                detect_page_state("<html><body>MediaMarkt</body></html>", 200,
                                  SEARCH_URL) == "blocked")

    # THE EXTENSION GUARD. The Scraping Browser injects 16 script tags into
    # every page it serves, among them recaptcha and turnstile hunters — both
    # vendors this repo's marker set contains. Without the strip, every
    # successful page over --cdp-endpoint reports as a challenge.
    injected = (SEARCH_FIXTURE.replace(
        "<head>",
        '<head><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo'
        '/content/captcha/turnstile/hunter.js" '
        'data-ts-input="cf-turnstile-response"></script>'
        '<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo'
        '/content/captcha/recaptcha/hunter.js"></script>'))
    ok &= check("an extension-injected cf-turnstile is not the site's",
                detect_bot_challenge(injected, SEARCH_URL) is None)
    ok &= check("a page carrying those injections is still content",
                detect_page_state(injected, 200, SEARCH_URL) == "content")
    ok &= check("the SITE's own turnstile would still be detected",
                detect_bot_challenge(
                    '<html><body><div class="cf-turnstile"></div></body></html>')
                == "turnstile")
    return ok


# ---------------------------------------------------------------------------
# page_flow: the policy all three engines share
# ---------------------------------------------------------------------------
def test_page_flow():
    group("page_flow: shared policy")
    ok = True

    ok &= check("listing readiness anchors on the tile div, not a link",
                "data-listing-id" in page_flow.READY_SELECTOR_LISTING
                and "/listing/" not in page_flow.READY_SELECTOR_LISTING)
    ok &= check("the listing threshold is above 1",
                page_flow.min_matches("listing") > 1)
    ok &= check("a detail page's threshold is 0 (it has one buy box)",
                page_flow.min_matches("product") == 0)
    ok &= check("shop mode uses the listing anchor",
                page_flow.ready_selector("shop") == page_flow.READY_SELECTOR_LISTING)
    ok &= check("every mode has a content timeout",
                all(page_flow.content_timeout_ms(m) > 0
                    for m in ("listing", "product", "shop")))

    # STATE_POLICY as data, so an engine cannot disagree with its twins.
    ok &= check("content is final", not page_flow.should_retry("content"))
    ok &= check("empty is final — retrying it re-confirms a right answer",
                not page_flow.should_retry("empty"))
    ok &= check("empty does not count as blocked",
                not page_flow.counts_as_blocked("empty"))
    ok &= check("blocked IS retried on this site", page_flow.should_retry("blocked"))
    ok &= check("blocked is NOT solved (a t=bv cookie is rejected)",
                not page_flow.should_solve("blocked"))
    ok &= check("captcha is solved", page_flow.should_solve("captcha"))
    ok &= check("captcha counts as blocked", page_flow.counts_as_blocked("captcha"))
    ok &= check("there is a retry budget when no proxy pool exists",
                page_flow.BLOCK_RETRIES_WITHOUT_POOL > 0)

    # The derived next-page selector, and the page=4 / page=40 trap.
    sel = page_flow.next_page_selector(3)
    ok &= check("the selector names the page AFTER the current one",
                "page=4" in sel)
    ok &= check("it leads with the standards-based signal",
                sel.startswith('link[rel="next"]'))
    ok &= check("it cannot match page=40",
                'a[href*="page=4&"]' in sel and 'a[href$="page=4"]' in sel)

    # THE REVIEWS-PAGINATION TRAP: a shop front advertises two "page 2"s.
    hrefs = ["https://www.etsy.com/de/shop/X?ref=items-pagination&page=2",
             "https://www.etsy.com/de/shop/X/reviews?page=2"]
    cands = page_flow.next_page_candidates("https://www.etsy.com/de/shop/X", hrefs)
    ok &= check("the reviews pagination is dropped", len(cands) == 1)
    ok &= check("the items pagination survives", "/reviews" not in cands[0])
    ok &= check("a relative href is resolved before comparison",
                page_flow.pagination_agrees(SEARCH_URL, 1,
                                            ["?q=handmade+mug&ref=pagination&page=2"]))
    ok &= check("tracking parameters do not defeat the comparison",
                page_flow.comparable(SEARCH_URL + "&ref=pagination&ls=a")
                == page_flow.comparable(SEARCH_URL))
    ok &= check("a cursor-style link is reported as a disagreement",
                not page_flow.pagination_agrees(
                    SEARCH_URL, 1,
                    ["https://www.etsy.com/de/search?q=handmade+mug&cursor=abc123"]))
    ok &= check("no advertised link is not a disagreement",
                page_flow.pagination_is_addressable(SEARCH_URL, []))

    # The site's own page bound.
    ok &= check("page_bound honours the site's own count",
                page_flow.page_bound('"initial_total_pages":2', 10) == 2)
    ok &= check("page_bound leaves a larger request alone",
                page_flow.page_bound('"initial_total_pages":50', 3) == 3)
    ok &= check("page_bound passes the request through when unstated",
                page_flow.page_bound("<html></html>", 4) == 4)

    # settle_datadome, driven with a fake driver: an interstitial that
    # resolves, and one that never does.
    seq = [DD_INTERSTITIAL_FIXTURE, None, SEARCH_FIXTURE]
    calls = {"n": 0}

    def content():
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return seq[i]

    out = page_flow.settle_datadome(content, lambda ms: None, rounds=5, pause_ms=1)
    ok &= check("settle_datadome returns the resolved page",
                out == SEARCH_FIXTURE)
    ok &= check("a mid-navigation None keeps it waiting rather than settling",
                calls["n"] >= 3)

    out = page_flow.settle_datadome(lambda: DD_INTERSTITIAL_FIXTURE,
                                    lambda ms: None, rounds=2, pause_ms=1)
    ok &= check("an interstitial that never resolves returns the last markup",
                is_datadome_interstitial(out))
    ok &= check("a page that was never an interstitial costs no waiting",
                page_flow.settle_datadome(lambda: SEARCH_FIXTURE,
                                          lambda ms: (_ for _ in ()).throw(
                                              AssertionError("slept")),
                                          rounds=3, pause_ms=1) == SEARCH_FIXTURE)
    return ok


# ---------------------------------------------------------------------------
# The shop sidecar
# ---------------------------------------------------------------------------
def test_shop_metadata():
    group("a shop run's own facts, which no product row can hold")
    ok = True
    facts = shop_metadata(SHOP_FIXTURE, SHOP_URL)
    ok &= check("shop_name pinned", facts["shop_name"] == "StonehousePotteryOH")
    ok &= check("shop_location pinned", facts["shop_location"] == "Ohio, Vereinigte Staaten")
    ok &= check("shop_rating pinned", facts["shop_rating"] == 5.0)
    ok &= check("shop_review_count pinned (16679)", facts["shop_review_count"] == 16679)
    ok &= check("shop_slogan read", facts["shop_slogan"])

    # A page with no Organization block still names the shop from its URL,
    # because the run knows which shop it asked for.
    bare = shop_metadata(page("<div>nothing</div>"), SHOP_URL)
    ok &= check("a shop page with no Organization data still names the shop",
                bare and bare["shop_name"] == "StonehousePotteryOH")
    ok &= check("a non-shop URL yields no shop facts",
                shop_metadata(SEARCH_FIXTURE, SEARCH_URL) is None
                or shop_metadata(SEARCH_FIXTURE, SEARCH_URL).get("shop_name") is None)

    # It reaches the sidecar, merged at the top level, and cannot overwrite a
    # run field.
    meta = run_meta("complete", "completed", 1, 1, SHOP_URL, SHOP_URL, 39,
                    mode="shop", extra=dict(facts, status="HIJACKED"))
    ok &= check("shop facts appear in the sidecar",
                meta["shop_rating"] == 5.0 and meta["shop_name"])
    ok &= check("a run field cannot be overwritten by extras",
                meta["status"] == "complete")
    return ok

# ---------------------------------------------------------------------------
# The output contract
# ---------------------------------------------------------------------------
def test_output_contract():
    group("the output contract shared across this scraper family")
    ok = True
    names = [f.name for f in fields(Product)]
    # The family prefix, byte-identical and in order, so a consumer written
    # against another repo in this family reads the first sixteen columns
    # unchanged. Site-specific columns go AFTER it.
    family_prefix = ["source", "scraped_at", "url", "sku", "title", "brand",
                     "price", "currency", "original_price", "discount_pct",
                     "rating", "review_count", "in_stock", "image_url",
                     "category", "price_source"]
    ok &= check("the family field prefix is present and in order",
                names[:len(family_prefix)] == family_prefix)
    ok &= check("Etsy's own columns come after it",
                names[len(family_prefix):] ==
                ["page", "position", "is_ad", "shop_id", "shop_rating",
                 "shop_review_count", "price_is_from", "price_max",
                 "free_shipping", "ships_from", "material", "gtin",
                 "description",
                 "images"])
    ok &= check("all three modes map to a row class",
                ROW_CLASS_BY_MODE == {"listing": Product, "product": Product,
                                      "shop": Product})
    ok &= check("all three modes are one row per sku",
                set(UNIQUE_BY_SKU_MODES) == {"listing", "product", "shop"})

    ok &= check("the exit codes are the family's",
                (EXIT_BLOCKED, EXIT_NO_PRODUCTS, EXIT_PARTIAL) == (3, 4, 6))
    ok &= check("an exhausted listing counts as complete",
                "no_new_products" in COMPLETE_STOP_REASONS
                and "pagination_exhausted" in COMPLETE_STOP_REASONS)
    ok &= check("a single-page mode is complete by construction",
                "single_page_mode" in COMPLETE_STOP_REASONS)

    # No defaulted currency anywhere: a row that could not establish one says
    # None rather than claiming EUR, which would be wrong for the four
    # non-euro country sites.
    ok &= check("Product defaults currency to None, not a guess",
                Product().currency is None)
    ok &= check("Product defaults price_source to None",
                Product().price_source is None)
    return ok


def test_writers():
    group("writers, dedupe and the refusal to overwrite good data")
    ok = True
    rows = [Product(sku="1", url="u1", price=1.0),
            Product(sku="2", url="u2", price=2.0)]
    with tempfile.TemporaryDirectory() as d:
        prefix = os.path.join(d, "out")

        # A run that finds nothing writes NOTHING: a consumer cannot tell an
        # empty category from a failed run, and the failure destroys the last
        # known good data.
        save(rows, prefix, "json", allow_empty=False)
        ok &= check("a good run writes its output",
                    os.path.exists(prefix + ".json"))
        before = open(prefix + ".json").read()
        save([], prefix, "json", allow_empty=False)
        ok &= check("an empty run does NOT overwrite the previous good output",
                    open(prefix + ".json").read() == before)
        save([], prefix, "json", allow_empty=True)
        ok &= check("--allow-empty is the opt-out and does overwrite",
                    json.load(open(prefix + ".json")) == [])

        # An empty CSV still carries its header, so a consumer reads a table
        # with no rows instead of failing on a zero-byte file.
        csv_path = os.path.join(d, "empty.csv")
        write_csv([], csv_path, row_cls=Product)
        header = open(csv_path).read().strip().split("\n")[0]
        ok &= check("an empty CSV still carries its header",
                    header.split(",")[:4] == ["source", "scraped_at", "url", "sku"])

        # A list column has to survive CSV without becoming a Python repr.
        csv_path = os.path.join(d, "images.csv")
        write_csv([Product(sku="1", images=["a", "b"])], csv_path, row_cls=Product)
        body = open(csv_path).read()
        ok &= check("a list column is joined, not repr()d in CSV",
                    ("a" + LIST_CSV_SEPARATOR + "b") in body and "['a'" not in body)

    seen = set()
    ok &= check("dedupe drops a repeated sku",
                len(dedupe_by_sku([Product(sku="a"), Product(sku="a")], seen)) == 1)
    # A row with no key is always KEPT: there is nothing to check a duplicate
    # against, and dropping it is a silent data loss rather than a dedupe.
    ok &= check("a row with no sku is kept, not dropped",
                len(dedupe_by_key([Product(sku=None), Product(sku=None)],
                                  set())) == 2)

    meta = run_meta("complete", "completed", 3, 3, "u", "u", 36,
                    pages_failed=[], mode="listing", source="etsy.com")
    ok &= check("the sidecar records status, mode and source",
                meta["status"] == "complete" and meta["mode"] == "listing"
                and meta["source"] == "etsy.com")
    # A count stops being a description once a page can fail while later ones
    # succeed, so the sidecar names WHICH pages failed.
    meta = run_meta("partial", "blocked", 5, 3, "u", "u", 12,
                    pages_failed=[2, 4], mode="listing", source="etsy.com")
    ok &= check("the sidecar names which pages failed, by number",
                meta["pages_failed"] == [2, 4])
    return ok


def test_finish_run():
    group("finish_run: the exit codes all three engines must agree on")
    ok = True
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "run")
        rows = [Product(sku="1", url="u")]

        code = finish_run(rows, p, "json", False, blocked=False,
                          stop_reason="completed", pages_requested=1,
                          pages_completed=1, pages_failed=[], mode="listing",
                          source="etsy.com", start_url="u", final_url="u")
        ok &= check("a complete run exits 0", code == 0)

        code = finish_run([], p + "b", "json", False, blocked=True,
                          stop_reason="blocked_datadome-bv",
                          pages_requested=1, pages_completed=0,
                          pages_failed=[1], mode="listing",
                          source="etsy.com", start_url="u", final_url="u")
        ok &= check("a blocked run exits 3, not 4", code == EXIT_BLOCKED)
        # A FAILED run writes no sidecar: `save` leaves the previous good
        # output in place, and a "failed" sidecar beside good data would
        # contradict it.
        ok &= check("a failed run writes no sidecar beside older good data",
                    not os.path.exists(p + "b.meta.json"))

        code = finish_run([], p + "c", "json", False, blocked=False,
                          stop_reason="completed", pages_requested=1,
                          pages_completed=1, pages_failed=[], mode="listing",
                          source="etsy.com", start_url="u", final_url="u")
        ok &= check("a genuinely empty result exits 4, not 3",
                    code == EXIT_NO_PRODUCTS)

        code = finish_run(rows, p + "d", "json", False, blocked=False,
                          stop_reason="page_load_timeout", pages_requested=5,
                          pages_completed=2, pages_failed=[3], mode="listing",
                          source="etsy.com", start_url="u", final_url="u")
        ok &= check("a run with data that stopped early exits 6 (partial)",
                    code == EXIT_PARTIAL)
        ok &= check("a partial run still writes what it got",
                    os.path.exists(p + "d.json"))
    return ok


def test_diff_refuses_cross_storefront():
    group("diff_runs refuses two storefronts, which `source` cannot catch here")
    ok = True

    def write_run(prefix, rows, mode="shop"):
        with open(prefix + ".json", "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
        with open(prefix + ".meta.json", "w", encoding="utf-8") as f:
            json.dump({"source": "etsy.com", "mode": mode, "status": "complete",
                       "stop_reason": "completed", "pages_requested": 1,
                       "pages_completed": 1, "pages_failed": [],
                       "products": len(rows), "start_url": "u",
                       "final_url": "u", "finished_at": "2026-09-10T00:00:00+00:00"},
                      f)

    def rows_of(fixture, url):
        return [dataclasses_asdict(r) for r in parse_products(fixture, url)]

    with tempfile.TemporaryDirectory() as td:
        us = os.path.join(td, "us")
        de = os.path.join(td, "de")
        us_rows = rows_of(US_SEARCH_FIXTURE, US_SEARCH_URL)
        de_rows = rows_of(SEARCH_FIXTURE, SEARCH_URL)
        write_run(us, us_rows, mode="listing")
        write_run(de, de_rows, mode="listing")

        # THE GAP THIS CLOSES. The sibling repos guard a cross-market diff
        # with `source` — eleven country hostnames. Etsy is one host, so both
        # sidecars say "etsy.com" and that guard silently does not apply.
        # Measured on one shop captured from both exits: 39 of 39 listings
        # reported a price change, at a constant 0.935 ratio, because Etsy
        # converts at a live rate. `--fail-on-change` would have fired on a
        # catalogue that had not moved.
        ok &= check("both sidecars really do say source=etsy.com",
                    json.load(open(us + ".meta.json"))["source"]
                    == json.load(open(de + ".meta.json"))["source"] == "etsy.com")

        done = subprocess.run(
            [sys.executable, "diff_runs.py", "--old", de + ".json",
             "--new", us + ".json"],
            cwd=REPO_ROOT, capture_output=True, text=True)
        ok &= check("a EUR run against a USD run is REFUSED",
                    done.returncode != 0)
        ok &= check("and the refusal names the currencies",
                    "different currencies" in done.stdout)
        ok &= check("and explains why `source` cannot catch it",
                    "etsy.com' for every storefront" in done.stdout
                    or "every storefront" in done.stdout)

        # --force is still the documented escape hatch.
        forced = subprocess.run(
            [sys.executable, "diff_runs.py", "--old", de + ".json",
             "--new", us + ".json", "--force"],
            cwd=REPO_ROOT, capture_output=True, text=True)
        ok &= check("--force still compares them", forced.returncode == 0)

        # AND A SAME-STOREFRONT DIFF STILL WORKS. Refusing everything would
        # pass the check above and break the tool.
        moved = [dict(r) for r in us_rows]
        moved[0]["price"] = round((moved[0]["price"] or 0) + 1.25, 2)
        us2 = os.path.join(td, "us2")
        write_run(us2, moved, mode="listing")
        same = subprocess.run(
            [sys.executable, "diff_runs.py", "--old", us + ".json",
             "--new", us2 + ".json"],
            cwd=REPO_ROOT, capture_output=True, text=True)
        ok &= check("a USD run against a USD run is compared",
                    same.returncode == 0)
        ok &= check("and it finds the one real price change",
                    "1 changed" in same.stdout)

        # A run that straddled two storefronts mid-way is refused on its own.
        mixed = [dict(r) for r in us_rows]
        mixed[0]["currency"] = "EUR"
        mx = os.path.join(td, "mixed")
        write_run(mx, mixed, mode="listing")
        straddled = subprocess.run(
            [sys.executable, "diff_runs.py", "--old", mx + ".json",
             "--new", us + ".json"],
            cwd=REPO_ROOT, capture_output=True, text=True)
        ok &= check("a run holding two currencies is refused too",
                    straddled.returncode != 0
                    and "more than one currency" in straddled.stdout)
    return ok


def test_diff():
    group("diff_runs")
    ok = True
    old = [{"sku": "1", "price": 10.0, "price_source": "jsonld+dom"},
           {"sku": "2", "price": 20.0, "price_source": "jsonld+dom"},
           {"sku": "3", "price": 30.0, "price_source": "jsonld"}]
    new = [{"sku": "1", "price": 11.0, "price_source": "jsonld+dom"},
           {"sku": "3", "price": 30.5, "price_source": "jsonld+dom"},
           {"sku": "4", "price": 40.0, "price_source": "jsonld+dom"}]
    d = diff_products(old, new)
    ok &= check("a real price move is reported as changed",
                any(c["sku"] == "1" for c in d["changed"]))
    ok &= check("a delisted product is reported as removed",
                [r["sku"] for r in d["removed"]] == ["2"])
    ok &= check("a new product is reported as added",
                [r["sku"] for r in d["added"]] == ["4"])
    # A price difference that comes with a price_source difference says
    # something about OUR two snapshots, not about the shop.
    ok &= check("a price move with a source change is not 'changed'",
                not any(c["sku"] == "3" for c in d["changed"]))
    ok &= check("...it is reported separately as source_changed",
                any(c["sku"] == "3" for c in d.get("source_changed", [])))

    # price_is_from is tracked: it moves only when a real price change
    # enters or leaves the 30-day window, so a monitor watching only `price`
    # would miss a product whose current price held while its recent floor
    # moved underneath it.
    ok &= check("price_is_from is a tracked field",
                "price_is_from" in __import__("diff_runs").TRACKED_FIELDS)
    return ok


def test_captcha():
    group("captcha detection and reconciliation")
    ok = True
    from captcha_solver import CaptchaChallenge

    # Format 2: the site's own wrapper element carries the config as
    # attributes, with the execute() call inside a bundled file that never
    # appears as readable inline script.
    widget = ('<captcha-widget data-captcha-type="recaptcha" data-version="v3" '
              'data-sitekey="6LcABCDEFGHIJKLMNOPQRSTUVWXYZ0123" '
              'data-action="submit"></captcha-widget>')
    c = detect_recaptcha_v3(widget, "https://www.etsy.com/")
    ok &= check("a captcha-widget declaring v3 is detected",
                c is not None and c.kind == "recaptcha_v3")

    # A sitekey is at least 20 characters; a short string next to
    # data-sitekey is not one, and treating it as one would send a malformed
    # task to the API and bill for the answer.
    ok &= check("a too-short sitekey is not accepted as a challenge",
                detect_recaptcha_v3('<div data-sitekey="short" '
                                    'class="g-recaptcha"></div>',
                                    "https://www.etsy.com/") is None)
    ok &= check("a page with no reCAPTCHA at all is not a challenge",
                detect_recaptcha_v3(SEARCH_FIXTURE, SEARCH_URL) is None)

    # THE LOADER WINS. A site's own wrapper can declare v3 while the Google
    # loader it actually ships is the v2-invisible signature
    # (render=explicit, size=invisible, a bframe challenge iframe). v3
    # parameters sent for a v2-invisible widget buy a token the site
    # rejects — so the runtime reading is authoritative and the two
    # detectors are reconciled rather than short-circuited.
    static_v3 = CaptchaChallenge(kind="recaptcha_v3", sitekey="6LcABC" + "X" * 20,
                                 action="submit", source="html")
    runtime_v2 = CaptchaChallenge(kind="recaptcha_v2_invisible",
                                  sitekey="6LcABC" + "X" * 20,
                                  source="runtime", size="invisible")
    merged = reconcile_detections(static_v3, runtime_v2)
    ok &= check("when the detectors disagree, the live loader wins",
                merged is not None and merged.kind == "recaptcha_v2_invisible")
    ok &= check("...and the real action from the static markup is kept",
                merged.action == "submit")
    ok &= check("one detector alone is still used when only it fires",
                reconcile_detections(static_v3, None) is static_v3
                and reconcile_detections(None, runtime_v2) is runtime_v2)
    ok &= check("neither firing means no challenge",
                reconcile_detections(None, None) is None)

    # Deliberately absent: no solver for a first-party image captcha. This
    # site has no such page — captured 2026-09-09 from a datacentre exit on
    # five hosts, its refusal is a 403 with no challenge on it at all — so a
    # solver for one would be dead code that looks load-bearing. Pinned so
    # that reintroducing it is a decision rather than a drift.
    import captcha_solver
    ok &= check("no first-party image-captcha solver was ported",
                not [n for n in dir(captcha_solver)
                     if "image" in n.lower() and "captcha" in n.lower()])
    # ...while the DETECTORS stay broad, which is the family's standing
    # policy: which challenge a visitor meets depends on the exit country and
    # on what the address has been doing.
    ok &= check("detection still covers four challenge vendors",
                set(product_parser.BOT_CHALLENGE_MARKERS) >=
                {"recaptcha", "hcaptcha", "turnstile", "datadome"})
    return ok


def _placeholder_reads_unset(raw):
    """Whether env_config would treat `raw` as "not configured".

    Goes through the real rule — `env_config.env_value`, which is where the
    placeholder logic lives — rather than reimplementing it, because a
    reimplementation is what drifts. The variable is set in os.environ
    directly and restored afterwards: `load_env` only fills variables that
    are not already set, so writing a temporary .env would be shadowed by
    whatever the suite has already loaded.
    """
    name = "ETSY_CDP_ENDPOINT"
    saved = os.environ.get(name)
    try:
        os.environ[name] = raw
        with io.StringIO() as buf, redirect_stdout(buf):
            value = env_config.env_value(name)
    finally:
        if saved is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = saved
    return value is None


def test_env_config():
    group("env_config")
    ok = True
    ok &= check("the env keys are this site's, not another repo's",
                set(env_config.ENV_KEYS) ==
                {"TWOCAPTCHA_KEY", "ETSY_CDP_ENDPOINT",
                 "ETSY_PROXY", "ETSY_URL"})

    # .env.example must document exactly the variables the code reads, in
    # both directions. It drifts otherwise, and a documented-but-unread
    # variable is worse than an undocumented one.
    example = os.path.join(REPO_ROOT, ".env.example")
    documented = set()
    if os.path.exists(example):
        for line in open(example, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                documented.add(line.split("=", 1)[0].strip())
    ok &= check(".env.example documents exactly the variables the code reads",
                documented == set(env_config.ENV_KEYS))

    # A variable mapped onto a flag with a non-empty default would be
    # silently inert, because the loader only fills UNSET values: a setting
    # that looks configurable and is not.
    ok &= check("no env variable is mapped onto --out (it has a default)",
                "out" not in env_config.ENV_KEYS.values())

    # A COPIED .env.example MUST READ AS UNSET, and a literal-only check is
    # not enough to make that true. This repo documents its two credentialled
    # URLs the way the vendor does, with the parts you fill in in braces:
    #
    #     ws://{login}-zone-scraping_browser-…-pid-{profileId}:{password}@…
    #     http://{user}:{password}@na.proxy.2captcha.com:2334
    #
    # Before the brace check existed the loader reported both of those as
    # CONFIGURED, so `cp .env.example .env` and a run connected to
    # cb.2captcha.com with the string `{login}-zone-…` as its username and
    # got a 401 — a confusing failure a long way from its cause.
    for raw in ('ws://{login}-zone-scraping_browser-country-us-pid-'
                '{profileId}:{password}@cb.2captcha.com:9222',
                'http://{user}:{password}@na.proxy.2captcha.com:2334',
                'your_2captcha_api_key_here'):
        ok &= check("a placeholder value reads as unset: %s..." % raw[:34],
                    _placeholder_reads_unset(raw))
    # ...and a REAL value still reads as set, or the guard has eaten the
    # feature it was protecting.
    ok &= check("a real value is not mistaken for a placeholder",
                _placeholder_reads_unset(
                    "ws://acct1-zone-scraping_browser-country-us-pid-p1:"
                    "secret@cb.2captcha.com:9222") is False)
    ok &= check("the example's default URL is usable as-is",
                _placeholder_reads_unset(
                    "https://www.etsy.com/search?q=handmade+mug") is False)

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, ".env")
        with open(path, "w", encoding="utf-8") as f:
            f.write("TWOCAPTCHA_KEY=fromfile\n")
            f.write("ETSY_URL=https://www.etsy.com/de/search?q=x\n")
            f.write("NOT_A_REAL_KEY=1\n")

        class A:
            twocaptcha_key = None
            url = None
            cdp_endpoint = None
            proxy = None

        a = A()
        env_config.load_env(path)
        env_config.apply(a, quiet=True)
        ok &= check("a value in .env fills an unset flag",
                    a.twocaptcha_key == "fromfile")

        b = A()
        b.twocaptcha_key = "fromflag"
        env_config.apply(b, quiet=True)
        # A .env must never override something the caller typed.
        ok &= check("an explicit flag beats .env", b.twocaptcha_key == "fromflag")
        # A typo is REPORTED rather than silently ignored.
        ok &= check("an unrecognised variable in .env is reported",
                    "NOT_A_REAL_KEY" in env_config.unknown_keys(path))
    return ok


def test_proxy_pool():
    group("proxy_pool: credentials never reach argv or logs")
    ok = True
    url = "http://user:secret@eu.proxy.2captcha.com:2334"
    masked = mask(url)
    ok &= check("credentials are masked in logs", "secret" not in masked)
    # The host and port are KEPT: which exit a run used is the point of the
    # log and is not the secret.
    ok &= check("...but the host and port survive masking",
                "eu.proxy.2captcha.com:2334" in masked)

    pw = to_playwright(url)
    # A `--proxy-server=` value becomes part of the browser's command line,
    # readable by anything that can run `ps`. The credentials go through the
    # driver's own fields instead.
    ok &= check("the server string handed to the browser has no credentials",
                "secret" not in pw["server"])
    ok &= check("credentials go through the driver's own fields",
                pw["username"] == "user" and pw["password"] == "secret")

    scrubbed, creds = split_credentials(url)
    ok &= check("split_credentials separates the two",
                scrubbed == "http://eu.proxy.2captcha.com:2334"
                and creds == ("user", "secret"))

    pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"])
    ok &= check("a pool reports its size", len(pool) == 3)
    first = pool.current
    pool.advance("test")
    ok &= check("advancing moves to another exit", pool.current != first)
    # `.proxies` hands back a COPY, so a worker building its own pool from it
    # cannot mutate the parent's list. Two threads sharing one mutable list
    # is the bug that makes concurrency stop being worth it.
    copy = pool.proxies
    copy.append("http://d:4")
    ok &= check("the pool hands out a copy of its exits, not the list itself",
                len(pool) == 3)

    # Workers start on DIFFERENT exits, each with its own pool object, so no
    # thread needs a lock: the concurrency is safe by construction rather
    # than by discipline. Tested through the engine's own helper, because
    # that is where the offset actually lives.
    try:
        import playwright_scraper
    except ImportError:
        playwright_scraper = None
    if playwright_scraper is not None:
        exits = [playwright_scraper._worker_pool(pool, i).current
                 for i in range(3)]
        ok &= check("three workers start on three different exits",
                    len(set(exits)) == 3)
        ok &= check("a worker with no pool gets none",
                    playwright_scraper._worker_pool(None, 0) is None)

    # A pool of one is legal and must not rotate itself into an index error.
    one = ProxyPool(["http://only:1"])
    one.advance("nowhere else to go")
    ok &= check("a single-exit pool survives a rotation",
                one.current == "http://only:1")
    ok &= check("an empty pool is refused rather than silently accepted",
                _raises(lambda: ProxyPool([])))

    # This used to assert that "http://host:port:login:pass" — a line from a
    # proxy LIST FILE — "is understood", checking only that parse_proxy_line
    # did not reject it. It returned the string unchanged, so the check
    # passed; the value was never usable, and it blew up several calls later.
    # A test that asserts a function did not complain is not a test that its
    # answer was right.
    #
    # A proxy LIST FILE line pasted where a proxy URL belongs. This is the
    # mistake a new user makes — the file format is
    # scheme://host:port:login:password and the flag wants
    # http://login:password@host:port — and it reached a real CI run.
    #
    # It used to sail through parse_proxy_line (which never looked at the
    # port) and blow up much later inside to_playwright as an uncaught
    # ValueError: exit 1, a crash, where it should be exit 2, bad usage. And
    # the traceback printed the login AND the password into a public CI log.
    from proxy_pool import ProxyError
    pasted = ("http://eu.proxy.2captcha.com:2334:"
              "SOMELOGIN-zone-custom-region-de:SOMEPASSWORD")
    raised = None
    try:
        parse_proxy_line(pasted, source="ETSY_PROXY")
    except ProxyError as exc:
        raised = str(exc)
    ok &= check("a proxy-list line pasted as a URL is refused, not crashed on",
                raised is not None)
    ok &= check("...and the refusal says what the value should look like",
                raised is not None and "login:password@host:port" in raised)
    ok &= check("...and neither the login nor the password is in the message",
                raised is not None
                and "SOMEPASSWORD" not in raised and "SOMELOGIN" not in raised)

    # mask() is the last thing standing between a password and a log, and it
    # is called precisely when the value is already wrong. It read
    # `parsed.port`, which urlparse computes lazily and which RAISES on a
    # malformed authority — so the masker blew up on exactly the input that
    # most needed masking. A masker that raises is worse than a vague one.
    ok &= check("mask() does not raise on a malformed URL",
                "SOMEPASSWORD" not in mask(pasted))
    for junk in ("::::", "not a url", "http://", "://x", ""):
        try:
            mask(junk)
            raised_here = False
        except Exception:
            raised_here = True
        ok &= check("mask(%r) does not raise" % junk, not raised_here)
    ok &= check("mask() still keeps host and port on a good URL",
                mask("http://u:p@h.example:8080") == "http://***:***@h.example:8080")
    return ok





# ---------------------------------------------------------------------------
# The engines
# ---------------------------------------------------------------------------
ENGINES = ("playwright_scraper", "puppeteer_scraper", "selenium_scraper")


def test_engines(skips):
    group("engines: all three must behave identically")
    ok = True
    loaded = {}
    for name in ENGINES:
        try:
            loaded[name] = __import__(name)
        except ImportError as e:
            # Reported, never swallowed: "skipped, engine absent" reads
            # exactly like a passing run, and CI's engine-smoke job fails if
            # this list is non-empty.
            skips.append("%s (%s)" % (name, e))

    for name, mod in loaded.items():
        ok &= check("%s exposes scrape() and parse_args()" % name,
                    hasattr(mod, "scrape") and hasattr(mod, "parse_args"))
        # The engines must reach the shared policy rather than carry copies.
        src = inspect.getsource(mod)
        ok &= check("%s takes its readiness policy from page_flow" % name,
                    "page_flow.ready_selector" in src)
        ok &= check("%s takes its state policy from page_flow" % name,
                    "page_flow.should_retry" in src or "page_flow.classify" in src)
        ok &= check("%s carries no scrolling machinery" % name,
                    "scroll_until_stable" not in src and "page_flow.hydrate" not in src)
        # Credentials never reach a log, in any engine.
        ok &= check("%s masks credentials globally, not just once" % name,
                    "pass@" not in mod._mask_credentials(
                        "a ws://user:pass@h:1/ b ws://user:pass@h:1/"))
        ok &= check("%s refuses a host that is not Etsy" % name,
                    "is_supported_host" in src)
        # All three modes, and the same three in every engine — a mode one
        # engine offers and another does not is the drift page_flow.py and
        # finish_run() exist to prevent, one level up.
        ok &= check("%s offers exactly the listing, product and shop modes" % name,
                    '"listing", "product", "shop"' in src)

    # For "it must pass with no engine installed" to mean anything, each
    # engine has to import its driver at MODULE level — otherwise the module
    # imports cleanly with the library absent, the group never skips, and the
    # CI job that exists to catch that cannot. This drifts back silently, so
    # it is asserted rather than trusted.
    driver_imports = {"playwright_scraper": "playwright",
                      "puppeteer_scraper": "pyppeteer",
                      "selenium_scraper": "selenium"}
    for name, lib in driver_imports.items():
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        top_level = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level.add(node.module.split(".")[0])
        ok &= check("%s imports %s at module level, so an absent library skips"
                    % (name, lib), lib in top_level)
    return ok





# ---------------------------------------------------------------------------
# Repository hygiene
# ---------------------------------------------------------------------------
def test_datadome_frames_beat_the_markup():
    group("the DataDome hand-off is visible in FRAMES, not in the markup")
    ok = True
    from product_parser import datadome_frame_verdict

    # THE MEASUREMENT THIS ENCODES. One live page, 120 seconds:
    #
    #   +3.1s   markup rt='i'   no challenge iframe yet
    #   +5.1s   markup rt='i'   iframe on /interstitial/
    #   +7.1s   markup rt='i'   iframe on /captcha/?t=fe   <- solvable
    #   +120s   markup rt='i'   STILL
    #
    # The device check hands off by NAVIGATING its iframe, and neither the
    # `dd` object nor the iframe's src attribute follows. A detector reading
    # only the HTML therefore reports "interstitial, do not pay" forever — as
    # it did on four of six live attempts, on pages that were solvable.
    INTERSTITIAL = ["https://geo.captcha-delivery.com/interstitial/?cid=X"]
    SOLVABLE = ["https://geo.captcha-delivery.com/captcha/?cid=X&hash=Y&t=fe"]
    BANNED = ["https://geo.captcha-delivery.com/captcha/?cid=X&t=bv"]

    ok &= check("a solvable frame reads t=fe",
                datadome_frame_verdict(SOLVABLE) == "fe")
    ok &= check("an interstitial frame reads i",
                datadome_frame_verdict(INTERSTITIAL) == "i")
    ok &= check("a banned frame reads bv", datadome_frame_verdict(BANNED) == "bv")
    ok &= check("no DataDome frame reads None",
                datadome_frame_verdict(["https://www.etsy.com/x"]) is None)
    ok &= check("an empty list reads None", datadome_frame_verdict([]) is None)
    ok &= check("the SOLVABLE frame wins when both are present (the hand-off)",
                datadome_frame_verdict(INTERSTITIAL + SOLVABLE) == "fe")

    # The whole point: the frames override markup that says otherwise.
    ok &= check("markup alone still reads the interstitial",
                datadome_verdict(DD_INTERSTITIAL_FIXTURE) == "i")
    ok &= check("a t=fe FRAME overrides interstitial markup",
                datadome_verdict(DD_INTERSTITIAL_FIXTURE, SOLVABLE) == "fe")
    ok &= check("and the state becomes captcha, not blocked",
                detect_page_state(DD_INTERSTITIAL_FIXTURE, 403, SEARCH_URL,
                                  frame_urls=SOLVABLE) == "captcha")
    ok &= check("so the run is willing to pay for it, once opted in",
                page_flow.should_pay_for(
                    datadome_verdict(DD_INTERSTITIAL_FIXTURE, SOLVABLE),
                    "always"))
    ok &= check("without frames it would NOT pay (the bug this encodes)",
                not page_flow.should_pay_for(
                    datadome_verdict(DD_INTERSTITIAL_FIXTURE), "always"))
    ok &= check("the captchaUrl comes from the frame, not the stale attribute",
                datadome_captcha_url(DD_INTERSTITIAL_FIXTURE, SOLVABLE)
                == SOLVABLE[0])
    ok &= check("an interstitial with a solvable frame is no longer 'settling'",
                not is_datadome_interstitial(DD_INTERSTITIAL_FIXTURE, SOLVABLE))

    # A served page must not be dragged into a challenge state by a leftover
    # frame reference, and a page with no frames at all still classifies.
    ok &= check("a served page with no frames is content",
                detect_page_state(SEARCH_FIXTURE, 200, SEARCH_URL,
                                  frame_urls=[]) == "content")
    ok &= check("a --dump-html file (no frames available) still classifies",
                detect_page_state(DD_BANNED_FIXTURE, 403, SEARCH_URL) == "blocked")

    # settle_datadome must STOP as soon as the frames say solvable, rather
    # than waiting out its whole budget on markup that never changes.
    rounds = {"n": 0}

    def frames():
        rounds["n"] += 1
        return INTERSTITIAL if rounds["n"] < 3 else SOLVABLE

    out = page_flow.settle_datadome(lambda: DD_INTERSTITIAL_FIXTURE,
                                    lambda ms: None, rounds=20, pause_ms=1,
                                    frames=frames)
    ok &= check("settle_datadome stops when the FRAMES resolve",
                rounds["n"] <= 4 and datadome_verdict(out, SOLVABLE) == "fe")

    # And every engine has to pass its frame list, or it is back to the bug.
    for name in ENGINES:
        src = open(os.path.join(REPO_ROOT, name + ".py"), encoding="utf-8").read()
        ok &= check("%s has a frame-URL primitive" % name,
                    "def _frame_urls(" in src)
        ok &= check("%s passes frames to classify" % name,
                    "frame_urls=_frame_urls(" in src)
        ok &= check("%s passes frames to settle_datadome" % name,
                    "frames=lambda: _frame_urls(" in src)
        ok &= check("%s passes frames when deciding to pay" % name,
                    "datadome_verdict(html, frame_urls)" in src)
        ok &= check("%s takes the captchaUrl from the frames too" % name,
                    "datadome_captcha_url(html, frame_urls)" in src)
    return ok


def test_datadome_solver():
    group("the DataDome solve: what it refuses to pay for, and what it sends")
    ok = True
    from captcha_solver import (DataDomeChallenge, CaptchaUnsolvable,
                                solve_datadome, datadome_proxy_fields,
                                parse_datadome_cookie, _raise_for_datadome_error,
                                TWOCAPTCHA_DATADOME_TASK)

    fe = DataDomeChallenge(
        "https://geo.captcha-delivery.com/captcha/?cid=X&hash=Y&t=fe&e=Z",
        "https://www.etsy.com/de/search?q=x", "Mozilla/5.0 Chrome/140")
    bv = DataDomeChallenge(
        "https://geo.captcha-delivery.com/captcha/?cid=X&t=bv",
        "https://www.etsy.com/de/search?q=x", "Mozilla/5.0 Chrome/140")

    ok &= check("t is read off the challenge URL", (fe.t, bv.t) == ("fe", "bv"))
    ok &= check("only t=fe is solvable", fe.is_solvable and not bv.is_solvable)

    # THE THREE REFUSALS, and none of them reaches the network. Each is a way
    # a run could otherwise spend money on a task that cannot succeed.
    KEY = "k" * 32
    PROXY = "http://user:pass@na.proxy.example:2334"
    for label, ch, key, proxy, want in (
            ("a t=bv page is never sent to the API", bv, KEY, PROXY, CaptchaUnsolvable),
            ("no key is reported, not sent", fe, None, PROXY, RuntimeError),
            ("no proxy is reported, not sent", fe, KEY, None, RuntimeError)):
        try:
            solve_datadome(ch, key, proxy, attempts=1)
            ok &= check(label, False)
        except want as e:
            ok &= check(label, True)
            if want is CaptchaUnsolvable:
                ok &= check("the t=bv refusal says the cookie is not accepted",
                            "not accepted" in str(e))
        except Exception as e:  # noqa: BLE001
            ok &= check("%s (got %s)" % (label, type(e).__name__), False)

    # The no-proxy refusal has to name the CDP case, because that is the
    # configuration a user of this repo is most likely to be in.
    try:
        solve_datadome(fe, KEY, None, attempts=1)
    except RuntimeError as e:
        ok &= check("the no-proxy refusal explains the --cdp-endpoint case",
                    "cdp-endpoint" in str(e))

    # The task fields the API requires.
    fields = datadome_proxy_fields(PROXY)
    ok &= check("proxy host, port and credentials are all sent",
                fields == {"proxyType": "http",
                           "proxyAddress": "na.proxy.example",
                           "proxyPort": 2334,
                           "proxyLogin": "user", "proxyPassword": "pass"})
    ok &= check("socks5 is accepted (the measured gateway speaks it)",
                datadome_proxy_fields("socks5://u:p@h:2333")["proxyType"] == "socks5")
    ok &= check("socks5h is normalised to socks5",
                datadome_proxy_fields("socks5h://u:p@h:2333")["proxyType"] == "socks5")
    ok &= check("an unauthenticated proxy sends no login fields",
                set(datadome_proxy_fields("http://h:8080")) ==
                {"proxyType", "proxyAddress", "proxyPort"})
    ok &= check("no proxy yields {} so the caller can tell it apart",
                datadome_proxy_fields(None) == {})
    ok &= check("a portless proxy is refused rather than sent",
                _raises(lambda: datadome_proxy_fields("http://u:p@hostonly")))
    ok &= check("a scheme 2Captcha does not accept is refused",
                _raises(lambda: datadome_proxy_fields("ftp://u:p@h:21")))
    # The log line has to make the weaker, true claim. "solved" is the
    # vendor's word for something it was measured saying about a challenge
    # that never existed.
    solver_src = inspect.getsource(captcha_solver.solve_datadome)
    ok &= check("the solver says the API RETURNED a cookie, not that it solved",
                "returned a" in solver_src and "decided by the reload" in solver_src)
    ok &= check("the solver documents that `ready` is not evidence",
                "FABRICATED" in solver_src)
    ok &= check("the solver documents that `ip` is the requester, not the exit",
                "REQUESTER" in solver_src)
    ok &= check("the task type is the documented one",
                TWOCAPTCHA_DATADOME_TASK == "DataDomeSliderTask")

    # A PROXY PASSWORD IS A SECRET, and an exception message is a log. This
    # was a real leak: a portless-proxy refusal printed the whole URL.
    for bad in ("http://u:supersecret@hostonly", "socks5://login:supersecret@h"):
        try:
            datadome_proxy_fields(bad)
        except ValueError as e:
            ok &= check("the refusal for %s masks the password" % bad.split("://")[0],
                        "supersecret" not in str(e))
    ok &= check("masking is global, not just the first occurrence",
                "pass@" not in captcha_solver._redact(
                    "a http://u:pass@h1:1 b http://u:pass@h2:2"))
    ok &= check("the host and port survive masking (which exit failed matters)",
                "h1:1" in captcha_solver._redact("http://u:pass@h1:1"))

    # The cookie the API returns arrives Set-Cookie-shaped.
    ok &= check("the cookie name and value are parsed out",
                parse_datadome_cookie(
                    "datadome=ABC.123-xyz; Max-Age=31536000; Domain=.etsy.com; "
                    "Path=/; Secure; SameSite=Lax") == ("datadome", "ABC.123-xyz"))
    ok &= check("a bare name=value works too",
                parse_datadome_cookie("datadome=V") == ("datadome", "V"))
    ok &= check("an unreadable cookie raises rather than half-answering",
                _raises(lambda: parse_datadome_cookie("")))

    # "Not from here" must be a DIFFERENT exception from "try again", because
    # they call for opposite actions: rotate versus retry.
    ok &= check("ERROR_CAPTCHA_UNSOLVABLE is CaptchaUnsolvable (rotate)",
                _raises_type(lambda: _raise_for_datadome_error(
                    {"errorId": 1, "errorCode": "ERROR_CAPTCHA_UNSOLVABLE"}),
                    CaptchaUnsolvable))
    ok &= check("a banned proxy is CaptchaUnsolvable too",
                _raises_type(lambda: _raise_for_datadome_error(
                    {"errorId": 1, "errorCode": "ERROR_PROXY_BANNED"}),
                    CaptchaUnsolvable))
    ok &= check("an unrecognised error is a plain RuntimeError (retry)",
                _raises_type(lambda: _raise_for_datadome_error(
                    {"errorId": 1, "errorCode": "ERROR_NO_SLOT_AVAILABLE"}),
                    RuntimeError)
                and not _raises_type(lambda: _raise_for_datadome_error(
                    {"errorId": 1, "errorCode": "ERROR_NO_SLOT_AVAILABLE"}),
                    CaptchaUnsolvable))
    ok &= check("an error description is redacted before it is raised",
                _no_secret_in(lambda: _raise_for_datadome_error(
                    {"errorId": 1, "errorCode": "ERROR_X",
                     "errorDescription": "failed for clientKey=abcdef123456"}),
                    "abcdef123456"))
    return ok


def _raises_type(fn, exc_type) -> bool:
    """True if `fn()` raises exactly `exc_type` (or a subclass)."""
    try:
        fn()
    except exc_type:
        return True
    except Exception:  # noqa: BLE001 — a different type is a failed check
        return False
    return False


def _no_secret_in(fn, secret: str) -> bool:
    """True if `fn()` raises and the secret is absent from the message."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        return secret not in str(e)
    return False


def test_datadome_policy_is_shared():
    group("all three engines agree on when a DataDome solve is bought")
    ok = True
    ok &= check("only t=fe is worth paying for",
                page_flow.should_pay_for("fe", "always")
                and not any(page_flow.should_pay_for(v, "always")
                            for v in ("bv", "i", "", None)))
    # THE DEFAULT DOES NOT SPEND MONEY, and that is measured rather than
    # cautious: two of two purchases returned cookies Etsy rejected, so a
    # run that paid per blocked page by default would bill for nothing.
    ok &= check("the default declines even a solvable challenge",
                not page_flow.should_pay_for("fe", "when-blocked"))
    ok &= check("--solve-captcha always is the opt-in",
                page_flow.should_pay_for("fe", "always"))
    ok &= check("the opt-in is documented as a measurement",
                page_flow.DATADOME_SOLVE_IS_OPT_IN is True)
    ok &= check("at most one solve is bought per page",
                page_flow.SOLVES_PER_PAGE == 1)

    # A POLICY CONSTANT NOTHING READS IS THE SAME DEFECT AS DEAD CODE.
    # `RETRY_ON_BLOCKED` carried a paragraph of justification and no engine
    # consulted it — they computed their block-retry budget from
    # `BLOCK_RETRIES_WITHOUT_POOL` alone, so setting it False would have
    # changed nothing.
    for f in ENGINE_FILES:
        path = os.path.join(REPO_ROOT, f)
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        ok &= check("%s consults RETRY_ON_BLOCKED, not just "
                    "BLOCK_RETRIES_WITHOUT_POOL" % f,
                    "page_flow.RETRY_ON_BLOCKED" in src
                    and "page_flow.BLOCK_RETRIES_WITHOUT_POOL" in src)

    # `--fp-tags` MUST DEFAULT TO ONE OS-FAMILY TAG. It shipped as
    # "Windows,Chrome,Desktop", which the fingerprint API rejects with HTTP
    # 400 — so --fingerprint failed on every invocation, while
    # fingerprint_client.py's own --tags help said ONE tag all along.
    # Measured against the live API on 2026-09-10: `Windows` succeeds, and
    # `Windows,Chrome,Desktop`, `Chrome` and `Desktop` each 400.
    for f in ENGINE_FILES:
        path = os.path.join(REPO_ROOT, f)
        if not os.path.exists(path):
            continue
        m = re.search(r'--fp-tags"\s*,\s*default="([^"]*)"',
                      open(path, encoding="utf-8").read())
        if m is None:
            continue
        ok &= check("%s's --fp-tags default is ONE tag the API accepts" % f,
                    "," not in m.group(1)
                    and m.group(1) in ("Windows", "Microsoft Windows",
                                       "Android"))
    ok &= check("the cookie domain comes from the page, not from the API",
                page_flow.datadome_cookie_domain(
                    "https://www.etsy.com/de/search?q=x") == ".etsy.com")
    ok &= check("a bare host still yields a dotted domain",
                page_flow.datadome_cookie_domain("https://etsy.com/x") == ".etsy.com")

    # Every engine must reach the paid path through the SHARED policy, and
    # must cap purchases. An engine that spent money on its own terms is the
    # drift page_flow.py exists to prevent, and it would show up as a bill
    # rather than as a failed run.
    for name in ENGINES:
        path = os.path.join(REPO_ROOT, name + ".py")
        src = open(path, encoding="utf-8").read()
        ok &= check("%s has a DataDome solve path" % name,
                    "handle_datadome_if_present" in src)
        ok &= check("%s gates it on page_flow.should_solve" % name,
                    "page_flow.should_solve(state)" in src)
        ok &= check("%s asks page_flow whether to pay" % name,
                    "page_flow.should_pay_for" in src)
        ok &= check("%s passes --solve-captcha into that decision" % name,
                    "should_pay_for(verdict, solve_mode)" in src)
        ok &= check("%s says why when it declines a solvable challenge" % name,
                    "the solve is opt-in" in src)
        ok &= check("%s caps purchases with SOLVES_PER_PAGE" % name,
                    "page_flow.SOLVES_PER_PAGE" in src)
        ok &= check("%s counts purchases outside the retry loop" % name,
                    "solves_bought = 0" in src)
        ok &= check("%s takes the cookie domain from page_flow" % name,
                    "page_flow.datadome_cookie_domain" in src)
        ok &= check("%s reads the browser's OWN user agent" % name,
                    "navigator.userAgent" in src)
        ok &= check("%s treats CaptchaUnsolvable as rotate, not crash" % name,
                    "except CaptchaUnsolvable" in src)
        # THE VERDICT MUST BE VERIFIED, NOT ASSUMED. A task was measured
        # returning `status: ready` with a billable cookie for a FABRICATED
        # captchaUrl, so the API's own answer proves nothing — only the
        # reload does. An engine that logged "solved" and moved on would be
        # reporting success it had not checked, which is this codebase's
        # worst habit.
        ok &= check("%s reports whether the cookie was ACCEPTED" % name,
                    "was accepted" in src and "NOT accepted" in src)
        ok &= check("%s warns rather than informs when it was not" % name,
                    "logger.warning(\n                        \"The solved cookie was NOT accepted" in src
                    or "The solved cookie was NOT accepted" in src)
        ok &= check("%s reloads after setting the cookie" % name,
                    any(m in src for m in ("page.reload", "driver.refresh()",
                                           "page.reload(")))
    return ok


def test_pyppeteer_teardown_noise():
    group("pyppeteer teardown noise is suppressed, and its limit is pinned")
    ok = True
    try:
        import puppeteer_scraper as pyp
    except ImportError:
        return check("pyppeteer engine present (skipped: library absent)", True)

    handler = pyp._AsyncBridge._on_loop_exception.__func__ if hasattr(
        pyp._AsyncBridge._on_loop_exception, "__func__") else pyp._AsyncBridge._on_loop_exception

    class _Loop:
        def __init__(self): self.passed_through = []
        def default_exception_handler(self, context):
            self.passed_through.append(context)

    # Each of these arrives on a run that SUCCEEDED, after the output is
    # written, and four tracebacks under a healthy run is how a reader learns
    # to ignore the log.
    swallowed = [
        {"message": "Task was destroyed but it is pending"},
        {"message": "Future exception was never retrieved",
         "exception": RuntimeError("Protocol error (Target.sendMessageToTarget): "
                                   "No session with given id")},
        {"exception": RuntimeError("Target closed")},
        {"exception": RuntimeError("Connection closed")},
        {"message": "Event loop is closed"},
    ]
    for context in swallowed:
        loop = _Loop()
        handler(loop, context)
        label = (context.get("message") or str(context.get("exception")))[:44]
        ok &= check("teardown noise suppressed: %s" % label,
                    not loop.passed_through)

    # A REAL error must still get through, or the suppression has become a
    # blindfold.
    loop = _Loop()
    handler(loop, {"exception": ValueError("something actually went wrong")})
    ok &= check("a real exception is NOT swallowed", len(loop.passed_through) == 1)

    # The handler reads BOTH fields. It used to read `exception or message`,
    # which meant a context carrying both never had its message inspected —
    # so the asyncio-worded ones kept printing after they were "handled".
    src = inspect.getsource(handler)
    ok &= check("the handler inspects the message as well as the exception",
                'for k in ("exception", "message")' in src)

    # PINNED LIMITATION, not a guard: `Exception ignored in: <coroutine
    # object Connection._recv_loop>` is printed by CPython's garbage
    # collector at interpreter shutdown, after the loop is gone and after the
    # exit code is decided. No loop handler can reach it, and catching it
    # would mean a global unraisable hook that swallows real bugs too. It is
    # documented in TROUBLESHOOTING.md instead; this check makes sure that
    # documentation stays there.
    doc = open(os.path.join(REPO_ROOT, "TROUBLESHOOTING.md"),
               encoding="utf-8").read()
    ok &= check("the shutdown-time traceback is documented rather than hidden",
                "Exception ignored in" in doc and "The run succeeded" in doc)
    return ok


def test_canary_separates_access_from_defect():
    group("the canary fails on defects and only WARNS on access conditions")
    ok = True
    wf_path = os.path.join(REPO_ROOT, ".github", "workflows", "canary.yml")
    wf = open(wf_path, encoding="utf-8").read()

    # The data checks must not run on a blocked or refused run: there is no
    # output file, and a missing file would fail for the wrong reason.
    ok &= check("the data checks are gated on the run having got in",
                "steps.verdict.outputs.tested == 'true'" in wf)

    # Extract the real interpret-the-exit-code script and run it under bash
    # for every code, rather than asserting on the YAML text. What matters is
    # whether the JOB FAILS, and only running it answers that.
    try:
        start = wf.index('          set -e\n          code=')
        end = wf.index('          echo "tested=$tested" >> "$GITHUB_OUTPUT"')
        end += len('          echo "tested=$tested" >> "$GITHUB_OUTPUT"')
    except ValueError:
        return check("the canary's exit-code script could be located", False)
    script = "\n".join(line[10:] if line.startswith(" " * 10) else line
                        for line in wf[start:end].splitlines())

    # WHY EACH CODE LANDS WHERE IT DOES:
    #   0  got in and parsed         -> pass, and the assertions then run
    #   3  blocked before parsing    -> ACCESS. Measured intermittent per
    #      profile on this site, so a daily red badge would be noise.
    #   5  endpoint refused          -> ACCESS, and the commonest cause is an
    #      EXPIRED SECRET. A credential is not forever; failing on it paints
    #      the badge red every day until someone notices.
    #   6  partial                   -> ACCESS, usually a mid-run block.
    #   1  crashed                   -> DEFECT.
    #   2  bad arguments             -> DEFECT (in the workflow itself).
    #   4  served a page, ZERO rows  -> DEFECT, and precisely the regression
    #      this canary exists to catch: the tile anchor moved.
    expected = {0: "pass", 3: "warn", 5: "warn", 6: "warn",
                1: "fail", 2: "fail", 4: "fail", 99: "fail"}
    for code, want in sorted(expected.items()):
        body = script.replace('code="${{ steps.run.outputs.exit_code }}"',
                              'code="%d"' % code)
        with tempfile.TemporaryDirectory() as td:
            out_file = os.path.join(td, "gh_output")
            summary = os.path.join(td, "gh_summary")
            open(out_file, "w").close()
            open(summary, "w").close()
            done = subprocess.run(
                ["bash", "-c", body], capture_output=True, text=True,
                env=dict(os.environ, GITHUB_OUTPUT=out_file,
                         GITHUB_STEP_SUMMARY=summary))
            failed = done.returncode != 0
            warned = "::warning::" in done.stdout
            errored = "::error::" in done.stdout
            wrote_summary = bool(open(summary, encoding="utf-8").read().strip())
            tested = "tested=true" in open(out_file, encoding="utf-8").read()

        if want == "pass":
            got = not failed and not warned and not errored and tested
        elif want == "warn":
            # A warning must NOT read as a pass: it also has to say in the
            # step summary that nothing was actually tested, and it must not
            # claim `tested`.
            got = not failed and warned and wrote_summary and not tested
        else:
            got = failed and errored
        ok &= check("exit %-2d is treated as %s" % (code, want), got)

    ok &= check("the reason access is not a defect is written down",
                "ACCESS CONDITIONS ARE NOT DEFECTS" in wf)

    # NO SCHEDULE, and the reason has to travel with the decision. A daily
    # cron against a credential that does not survive a day gives either a
    # permanently red badge or a permanently green one that tested nothing —
    # and the green is worse, because it reads as "the parser still works".
    # Restoring the cron is a legitimate change the day a long-lived
    # credential exists; this check makes it a decision rather than a habit.
    ok &= check("the canary has no cron schedule",
                not re.search(r"^\s*-\s*cron:", wf, re.M))
    ok &= check("it is dispatchable by hand", "workflow_dispatch:" in wf)
    ok &= check("and the reason the schedule is off is written down",
                "does not survive a day" in wf)
    ok &= check("the expired-secret case is named",
                "expired" in wf.lower() and "refresh" in wf.lower())
    return ok


def test_ci_checks_is_actually_wired_up():
    group("the repo's own checks are RUN, and still catch a real secret")
    ok = True
    script = os.path.join(REPO_ROOT, ".github", "ci_checks.py")
    ok &= check("ci_checks.py exists", os.path.exists(script))
    if not os.path.exists(script):
        return ok

    # IT HAS TO BE INVOKED BY A WORKFLOW. It was not — for the whole of
    # v0.1.0 it sat there implementing three checks that nothing ran, while a
    # second, LOOSER copy of one of them lived inline in tests.yml. Dead code
    # that looks load-bearing is worse than no code, and this is the check
    # that keeps it alive.
    wf_dir = os.path.join(REPO_ROOT, ".github", "workflows")
    workflows = "\n".join(
        open(os.path.join(wf_dir, f), encoding="utf-8").read()
        for f in sorted(os.listdir(wf_dir)) if f.endswith((".yml", ".yaml")))
    ok &= check("a workflow runs ci_checks.py", "ci_checks.py" in workflows)
    ok &= check("the secret check specifically is run",
                "--secret-check" in workflows or "--all" in workflows)

    # AND IT PASSES ON THIS REPO. A check that is always red teaches everyone
    # to ignore checks; this one WAS red, on six documented placeholders.
    done = subprocess.run([sys.executable, script, "--all"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    ok &= check("ci_checks.py --all passes on this repo (exit %d)" % done.returncode,
                done.returncode == 0)
    if done.returncode != 0:
        print("        " + (done.stdout or done.stderr).strip()[-400:])

    # AND IT STILL CATCHES A REAL ONE. Loosening an allowlist until the check
    # passes is the failure mode here, so both directions are asserted: a
    # planted CDP endpoint, a planted 32-hex key and a planted http proxy URL
    # must all be found. The http one matters most — the inline grep this
    # replaced covered only ws:// and would have missed a committed proxy.
    planted = os.path.join(REPO_ROOT, "_secret_probe_delete_me.py")
    # The key is ASSEMBLED rather than written as a literal, because a
    # 32-character hex string sitting in this file is exactly what the check
    # under test flags — and it did, on the first run of this test. The file
    # it writes still gets the whole thing, which is what the probe needs.
    planted_key = "3f8a1c9e4b7d2065" + "af13ce88b409d752"
    try:
        with open(planted, "w", encoding="utf-8") as f:
            f.write(
                'CDP = "ws://acct-zone-scraping_browser-pid-x:'
                'S3cretPassw0rd@cb.2captcha.com:9222"\n'
                'KEY = "%s"\n'
                'PROXY = "http://acct-zone-custom:S3cretPassw0rd'
                '@na.proxy.2captcha.com:2334"\n' % planted_key)
        caught = subprocess.run([sys.executable, script, "--secret-check"],
                                cwd=REPO_ROOT, capture_output=True, text=True)
        out = caught.stdout + caught.stderr
        ok &= check("a planted secret fails the check", caught.returncode != 0)
        ok &= check("the planted ws:// CDP endpoint is named",
                    "_secret_probe_delete_me.py:1" in out)
        ok &= check("the planted 32-hex key is named",
                    "_secret_probe_delete_me.py:2" in out)
        ok &= check("the planted http:// PROXY url is named (the grep this "
                    "replaced missed those)",
                    "_secret_probe_delete_me.py:3" in out)
    finally:
        # Never leave it behind: a test that mutates the working tree is its
        # own defect, and this one would plant a fake secret.
        if os.path.exists(planted):
            os.remove(planted)
    ok &= check("the probe file is cleaned up", not os.path.exists(planted))

    # The pre-publication scan: the same rules over every blob that has EVER
    # existed. A later commit cannot remove what a published tag and a merged
    # PR's refs already hold, so this has to be runnable BEFORE the repo goes
    # public — and it has to be findable, which a check makes it.
    hist = subprocess.run([sys.executable, script, "--history-check"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    ok &= check("--history-check runs and this history is clean",
                hist.returncode == 0)
    ok &= check("it says how many objects it looked at",
                "ever existed" in hist.stdout)
    # NOT in --all, on purpose: it shells out to git once per object, and a
    # dirty history needs a decision rather than a red check on every push.
    every = subprocess.run([sys.executable, script, "--all"],
                           cwd=REPO_ROOT, capture_output=True, text=True)
    ok &= check("--all deliberately excludes the history scan",
                "history check" not in every.stdout)
    return ok


def test_no_capture_leaks():
    group("no credentials or personal data in the committed fixtures")
    ok = True
    # Collected by SUFFIX, which is how this file names its fixtures. An
    # earlier version asked for a "FIX_" PREFIX, matched nothing, and every
    # check below passed against an empty string — 150 KB of committed real
    # captures went unexamined while twelve checks reported green. The
    # non-empty assertion underneath is the actual fix: a corpus check that
    # can silently scan nothing is worse than no corpus check at all.
    names = [k for k, v in sorted(globals().items())
             if k.endswith("_FIXTURE") and isinstance(v, str)]
    fixtures = "\n".join(globals()[k] for k in names)
    ok &= check("the privacy checks below have fixtures to scan "
                "(%d fixtures, %d chars)" % (len(names), len(fixtures)),
                len(names) >= 8 and len(fixtures) > 50000)
    # Guarded with PATTERNS rather than with the literals a previous capture
    # happened to contain, so the NEXT capture is checked too. MediaMarkt's
    # pages embed a front-end configuration blob — a Sentry DSN, a Woosmap
    # public key, a store-code JWT — none of which is needed to test a
    # parser, and none of which belongs in a public repository.
    patterns = {
        "a JWT": r"eyJ[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{10,}",
        "an access token": r"(?:access|auth|bearer)[_\-]?[Tt]oken\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{12,}",
        "an API key": r"(?:api|public|secret|private)[_\-]?[Kk]ey\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{12,}",
        "a Sentry DSN": r"https://[0-9a-f]{16,}@[\w.]*ingest",
        "a session id": r"session[_\-]?[Ii]d\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{8,}",
        "an email address": r"[\w.+-]+@[\w-]+\.[a-z]{2,}",
        "a proxy credential": r"://[^\s/@\"]+:[^\s/@\"]+@",
    }
    for label, pattern in patterns.items():
        hits = re.findall(pattern, fixtures)
        ok &= check("no %s in the fixtures" % label, not hits)

    # Etsy's OWN per-impression material, which a fresh capture brings with
    # it: a click-tracking key, its checksum, and the logging key that ties an
    # impression to a session. Anonymous and expired, and still not something
    # to commit — and a 40-character hex-ish blob in a public repo reads as a
    # credential to every scanner that looks, including this repo's own CI
    # grep. Matched as PATTERNS rather than as the values one capture
    # happened to hold, so the NEXT capture is checked too.
    etsy_session = {
        "a click-tracking key": r"click_key=(?!PLACEHOLDER)[A-Za-z0-9%.-]{12,}",
        "a click checksum": r"click_sum=(?!PLACEHOLDER)[A-Za-z0-9]{6,}",
        "an impression logging key":
            r'data-logging-key="(?!PLACEHOLDER)[A-Za-z0-9:-]{12,}"',
        "a content-source token":
            r"content_source=(?!PLACEHOLDER)[A-Za-z0-9%.-]{12,}",
        "a DataDome session blob": r"'(?:cid|hsh|e|cookie)':'(?!PLACEHOLDER)[^']{16,}'",
    }
    for label, pattern in etsy_session.items():
        hits = re.findall(pattern, fixtures)
        ok &= check("no %s in the fixtures (scrub a new capture before "
                    "committing it)" % label, not hits)

    # The repo-wide grep CI runs, applied here too so a failure is local.
    # Asked of GIT, not of the filesystem. A developer's own `.env` beside
    # the scripts is EXPECTED — it is how the local runs get their key — and
    # `.gitignore` is what keeps it out of the repo. Checking for the file's
    # existence made this red on every machine that had ever run the scraper
    # for real, which is the machine most likely to be running the suite.
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=REPO_ROOT, capture_output=True, text=True).returncode == 0
    ok &= check("no .env file is tracked by git", not tracked)
    return ok


# Wording the family enforces. Four separately-billed 2Captcha products sit
# behind one key, and two of these names were used for a placeholder endpoint
# that no longer exists — an editor reintroducing either costs a support
# ticket, so the check is cheap insurance.
BANNED_PHRASES = (
    "cloud browser",
    "antidetect browser",
    "anti-detect browser",
    "2scraper Antidetect Browser",
    "gate.2prx.com",
    "2prx.com",
    "--antidetect",
    "ANTIDETECT_LOCAL_API",
)

# Flags that must not exist ON THE ENGINES, each for its own reason:
#
#   --antidetect   removed from this family; the endpoint behind it was a
#                  placeholder that never existed.
#   --country      the hostname already decides which country site a run
#                  reads, so a flag could disagree with the URL it was given
#                  and there would be no right answer. (fingerprint_client.py
#                  legitimately HAS a --country: it picks a fingerprint's
#                  locale, which is a different question.)
#   --marketplace  same reasoning, under the sibling repo's name for it.
#   --details      the old pre-family scraper's flag for "also fetch each
#                  product page". That is --mode product now, and a run that
#                  silently multiplies its request count is not a flag.
REMOVED_ENGINE_FLAGS = ("--antidetect", "--marketplace", "--country", "--details")
ENGINE_FILES = ("playwright_scraper.py", "puppeteer_scraper.py",
                "selenium_scraper.py")


def test_wording():
    group("wording and removed flags")
    ok = True
    # Asked of GIT, so the scan reaches the workflows and the issue
    # templates under .github/ — eight shipped files that an os.listdir of
    # the repo ROOT silently missed, including the four a contributor is
    # most likely to paste marketing wording into. Untracked scratch files
    # and .pytest_cache/ are excluded for free by asking git.
    listed = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                            capture_output=True, text=True)
    if listed.returncode == 0 and listed.stdout.strip():
        shipped = [f for f in listed.stdout.split("\n")
                   if f.endswith((".py", ".md", ".txt", ".toml", ".yml", ".yaml"))
                   and os.path.basename(f) != os.path.basename(__file__)]
    else:  # not a git checkout (a release tarball): fall back to the root
        shipped = [f for f in os.listdir(REPO_ROOT)
                   if f.endswith((".py", ".md", ".txt", ".toml", ".yml", ".yaml"))
                   and f != os.path.basename(__file__)]
    ok &= check("the wording scan reaches beyond the repo root",
                any(os.sep in f or "/" in f for f in shipped))
    for phrase in BANNED_PHRASES:
        offenders = []
        for f in shipped:
            try:
                text = open(os.path.join(REPO_ROOT, f), encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            if phrase.lower() in text.lower():
                offenders.append(f)
        ok &= check("no shipped file says %r" % phrase, not offenders)

    for flag in REMOVED_ENGINE_FLAGS:
        offenders = []
        for f in ENGINE_FILES:
            path = os.path.join(REPO_ROOT, f)
            if not os.path.exists(path):
                continue
            text = open(path, encoding="utf-8").read()
            # A prose mention explaining why the flag does NOT exist is fine
            # and is worth keeping; an argparse registration is not.
            if ('add_argument("%s"' % flag) in text or \
                    ("add_argument('%s'" % flag) in text:
                offenders.append(f)
        ok &= check("no engine registers the removed flag %s" % flag,
                    not offenders)

    # The product this repo integrates with, named correctly.
    readme = os.path.join(REPO_ROOT, "README.md")
    if os.path.exists(readme):
        text = open(readme, encoding="utf-8").read()
        ok &= check("the README names the Scraping Browser API",
                    "Scraping Browser API" in text)
        ok &= check("the README does not name a competitor",
                    not re.search(r"brightdata|oxylabs|smartproxy|zyte|scraperapi\.com",
                                  text, re.IGNORECASE))
    return ok


# Names Python provides that are not imports and not assignments.
_MODULE_DUNDERS = {"__file__", "__name__", "__doc__", "__package__",
                   "__spec__", "__loader__", "__builtins__", "__debug__"}


def _undefined_names(path):
    """Names loaded in `path` that are never imported, defined or assigned.

    A deliberately coarse approximation — it pools every binding in the file
    rather than tracking scopes, so it under-reports and never invents a
    problem. That is the right trade here: this exists to catch a name that
    is nowhere at all, and a false positive would be worse than a miss.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    bound = set(dir(builtins)) | _MODULE_DUNDERS
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bound |= {(a.asname or a.name.split(".")[0]) for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            bound |= {(a.asname or a.name) for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global):
            bound |= set(node.names)
    missing = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                and node.id not in bound:
            missing.setdefault(node.id, []).append(node.lineno)
    return missing


class _FakeSession:
    """Stands in for a _BrowserSession: opened, closed, carries a pool."""

    def __init__(self, pool=None):
        self.pool = pool
        self.closed = False

    def open(self):
        return self

    def close(self):
        self.closed = True


class _FakePlaywright:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# A fingerprint in the shape the API actually returns, trimmed to the keys
# this repo reads. Cut from a real `format=chromium` response on 2026-09-09;
# the id and the exact pixel values are the only things changed, and only so
# that nothing here looks like a specific machine.
FIX_FINGERPRINT = {
    "id": 1000000,
    "country": "DE",
    "userAgent": {
        "userAgent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/146.0.0.0 Safari/537.36"),
        "platform": "Windows",
        "mobile": False,
    },
    "intl": {
        "contentLocale": "de-DE",
        "languages": ["de-DE", "de", "en-US", "en"],
        "timeZone": "Europe/Berlin",
    },
    "screen": {"width": 1920, "height": 1080,
               "outerWidth": 1920, "outerHeight": 992,
               "deviceScaleFactor": 1},
}


def test_fingerprint_application():
    group("a fingerprint is applied as the fingerprint describes it")
    ok = True
    import fingerprint_client as fpc

    ua = fpc.fingerprint_user_agent(FIX_FINGERPRINT)
    # The UA used to be read from `userAgent.value`, a key the API returns in
    # NEITHER format. So --fingerprint silently set no user agent at all and
    # the browser kept its own: a German fingerprint's screen and locale
    # wearing a local Chromium's UA, which is precisely the identity mismatch
    # the flag exists to avoid.
    ok &= check("the user agent is found in the shape the API returns",
                ua and ua.startswith("Mozilla/5.0 (Windows NT 10.0"))
    ok &= check("the `raw` format's ua key is understood too",
                fpc.fingerprint_user_agent({"data": {"ua": "UA/1.0"}}) == "UA/1.0")
    ok &= check("a fingerprint with no user agent yields None, not a crash",
                fpc.fingerprint_user_agent({"country": "DE"}) is None)

    kw = fpc.playwright_context_kwargs(FIX_FINGERPRINT)
    ok &= check("the context carries the fingerprint's user agent",
                kw.get("user_agent") == ua)
    # `locale` used to be built as f"en-{country}", giving "en-DE" for a
    # German fingerprint. An English-speaking visitor in Germany is possible,
    # but it is not what this fingerprint describes, and a locale that
    # contradicts the rest of the identity is the mismatch again.
    ok &= check("the locale is the fingerprint's own, not en-<country>",
                kw.get("locale") == "de-DE")
    ok &= check("the timezone is carried, so the browser cannot contradict it",
                kw.get("timezone_id") == "Europe/Berlin")
    # The device pixel ratio, which Playwright takes as its own option and
    # which was dropped on the floor until a live browser was compared
    # against the fingerprint: one stating 1.25 produced a browser reporting
    # `devicePixelRatio === 1`, so the identity contradicted itself on an
    # axis a fingerprinter reads for free.
    ok &= check("fingerprint: the device scale factor is carried",
                kw.get("device_scale_factor") == 1)
    # A viewport exactly equal to the screen is itself a signal, and the
    # fingerprint states its own window size rather than needing one guessed.
    ok &= check("the viewport is the fingerprint's window, not its screen",
                kw.get("viewport") == {"width": 1920, "height": 992}
                and kw.get("screen") == {"width": 1920, "height": 1080})

    # Falling back sensibly when a field is absent, rather than dropping it.
    bare = fpc.playwright_context_kwargs({"country": "FR", "screen":
                                          {"width": 1280, "height": 800}})
    ok &= check("a fingerprint with no intl block still gets a locale",
                bare.get("locale") == "en-FR")
    ok &= check("...and a window smaller than the screen",
                bare["viewport"]["height"] < bare["screen"]["height"])
    ok &= check("a fingerprint with nothing usable yields no kwargs",
                fpc.playwright_context_kwargs({}) == {})

    # Every key this produces must be one Playwright's new_context accepts;
    # an unknown one is a TypeError at launch, on the paid path, at runtime.
    accepted = {"user_agent", "viewport", "screen", "locale", "timezone_id",
                "geolocation", "permissions", "extra_http_headers",
                "device_scale_factor", "is_mobile", "has_touch", "color_scheme"}
    ok &= check("every context kwarg is one Playwright accepts",
                set(kw) <= accepted)
    return ok


def test_credentials_never_reach_a_log():
    group("an API key never reaches a log or an exception message")
    ok = True
    import fingerprint_client as fpc
    import captcha_solver as cs

    # requests puts the FULL URL — query string included — into the text of
    # HTTPError and of every connection error. Both of these modules have an
    # endpoint that takes the key as a query parameter, so an error there
    # echoed a live key to the terminal. It did, once, on a real call.
    # An obviously fake key, and NOT a real one even a revoked one: a
    # 32-hex string in a public repo reads as a live credential to every
    # scanner that looks, including this repo's own CI grep. The word
    # "example" in the name is what tells that grep this line is a fixture.
    example_key = "0123456789abcdef0123456789abcdef"
    for name, module in (("fingerprint_client", fpc), ("captcha_solver", cs)):
        redacted = module._redact(
            "400 Client Error: Bad Request for url: "
            "https://api.2captcha.com/fingerprint/random?format=chromium&"
            "key=%s" % example_key)
        ok &= check("%s redacts a key out of an error message" % name,
                    example_key not in redacted)
        ok &= check("...and keeps the endpoint, which is the useful half",
                    "api.2captcha.com/fingerprint/random" in redacted)
        ok &= check("%s redacts clientKey too" % name,
                    example_key not in module._redact("clientKey=%s" % example_key))
        ok &= check("%s leaves ordinary text alone" % name,
                    module._redact("upstream status 403") == "upstream status 403")
    return ok


def test_concurrent_dispatch(skips):
    group("concurrent page dispatch (threads, stop event, accounting)")
    ok = True
    try:
        import playwright_scraper as eng
    except ImportError as e:
        skips.append("concurrent dispatch (%s)" % e)
        return ok

    # The thread fan-out is the one part of --concurrency that the rest of
    # this suite does not reach, and it is not reachable from a live run in
    # every environment either: page 1 is always fetched alone and decides
    # whether the rest may be addressed, so a blocked page 1 means the
    # workers never start. Driven here with the browser stubbed out, which
    # leaves exactly the concurrency logic under test.
    original = (eng.sync_playwright, eng._BrowserSession, eng._fetch_one_page)

    class Args:
        delay = 0
        mode = "listing"
        out = "x"

    def run(specs, concurrency, rows_for_page, die_on=()):
        fetched, lock = [], threading.Lock()

        def fake_fetch(session, args, pool, page_num, url):
            with lock:
                fetched.append(page_num)
            if page_num in die_on:
                raise RuntimeError("worker blew up on page %d" % page_num)
            outcome = eng.PageOutcome(page_num=page_num, url=url)
            outcome.products = rows_for_page(page_num)
            return outcome

        eng.sync_playwright = lambda: _FakePlaywright()
        eng._BrowserSession = lambda pw, args, pool, **kw: _FakeSession(pool)
        eng._fetch_one_page = fake_fetch
        try:
            results, unattempted, exhausted = eng._fetch_pages_concurrently(
                Args(), None, specs, concurrency)
        finally:
            (eng.sync_playwright, eng._BrowserSession,
             eng._fetch_one_page) = original
        return fetched, results, unattempted, exhausted

    # 1. Every page fetched exactly once, whatever the worker count.
    specs = [(n, "u%d" % n) for n in range(2, 12)]
    fetched, results, unattempted, exhausted = run(
        specs, 4, lambda n: ["row"])
    ok &= check("every queued page is fetched exactly once",
                sorted(fetched) == [n for n, _ in specs])
    ok &= check("every page produces an outcome",
                sorted(o.page_num for o in results) == [n for n, _ in specs])
    ok &= check("nothing is left unattempted when the listing does not end",
                unattempted == [] and not exhausted)

    # 2. Results arrive in whatever order the threads finish, which is
    #    exactly why the caller merges by page number instead of by arrival.
    #    Sorting them must reconstruct the page order.
    ok &= check("outcomes can be put back into page order",
                [o.page_num for o in sorted(results, key=lambda o: o.page_num)]
                == [n for n, _ in specs])

    # 3. The stop event. Asking for 50 pages of a listing that ends at page 5
    #    must not fetch 45 empty ones: workers check the event before taking
    #    more work, so at most (concurrency - 1) extra are already in flight.
    specs = [(n, "u%d" % n) for n in range(2, 51)]
    fetched, results, unattempted, exhausted = run(
        specs, 3, lambda n: [] if n >= 5 else ["row"])
    ok &= check("the end of the listing stops dispatch", exhausted)
    ok &= check("an exhausted listing costs at most (concurrency-1) extra "
                "fetches (%d fetched of 49 queued)" % len(fetched),
                len(fetched) <= 4 + 3)
    ok &= check("the pages never tried are reported, not counted as failed",
                unattempted and all(o.ok for o in results))
    ok &= check("unattempted pages are reported in order",
                unattempted == sorted(unattempted))

    # 4. A worker that dies must not hang the run, and must not swallow the
    #    pages its siblings did fetch.
    specs = [(n, "u%d" % n) for n in range(2, 8)]
    fetched, results, unattempted, exhausted = run(
        specs, 3, lambda n: ["row"], die_on={3})
    ok &= check("a worker that raises does not hang the run",
                len(results) + len(unattempted) + 1 >= len(specs))
    ok &= check("the pages other workers fetched still come back",
                any(o.page_num != 3 for o in results))
    return ok


def test_no_undefined_names():
    group("no engine references a name that does not exist")
    ok = True
    # This exists because of a bug that got all the way to a live run.
    # puppeteer_scraper.py called `detect_page_state(...)` on a line reached
    # only while fetching a page, after the import of that name had been
    # removed. The module imported fine, `--help` worked, `compileall`
    # passed, the whole offline suite passed and CI was green — and the
    # engine died with NameError on its first real page.
    #
    # Byte-compiling proves a file PARSES. It says nothing about whether the
    # names in it resolve, and the paths where they do not are exactly the
    # ones an offline suite cannot execute.
    for name in sorted(f for f in os.listdir(REPO_ROOT) if f.endswith(".py")):
        missing = _undefined_names(os.path.join(REPO_ROOT, name))
        detail = ", ".join("%s (line %d)" % (k, v[0])
                           for k, v in sorted(missing.items()))
        ok &= check("%s references no undefined name%s"
                    % (name, ": " + detail if missing else ""), not missing)
    return ok


def test_dockerfile_copies_what_it_runs():
    group("the Docker image contains every module its entrypoint imports")
    ok = True
    path = os.path.join(REPO_ROOT, "Dockerfile")
    if not os.path.exists(path):
        return check("Dockerfile exists", False)

    # The Dockerfile COPYs an explicit list rather than the whole directory,
    # which is right — the image should not carry the test suite, the
    # fixtures or a stray .env. The cost is that the list can fall behind the
    # imports, and NOTHING else in this repo would notice: CI never builds
    # the image, so a missing module ships and the container dies with
    # ModuleNotFoundError on every invocation, `--help` included.
    #
    # That is not hypothetical. `proxy_pool.py` was missing from this list,
    # and playwright_scraper.py imports it at module level.
    raw = open(path, encoding="utf-8").read()
    joined = re.sub(r"\\\n\s*", " ", raw)          # fold line continuations
    copied = set()
    for line in joined.splitlines():
        if line.startswith("COPY "):
            copied.update(tok for tok in line.split() if tok.endswith(".py"))

    entrypoint = None
    m = re.search(r'ENTRYPOINT\s*\[([^\]]*)\]', joined)
    if m:
        parts = [x.strip().strip('"\'') for x in m.group(1).split(",")]
        entrypoint = next((x for x in parts if x.endswith(".py")), None)
    ok &= check("the Dockerfile names a Python entrypoint", bool(entrypoint))
    if not entrypoint:
        return False
    ok &= check("the entrypoint itself is copied into the image",
                entrypoint in copied)

    # Every LOCAL module the entrypoint reaches, transitively.
    local = {f[:-3] for f in os.listdir(REPO_ROOT) if f.endswith(".py")}

    def reached(module, seen=None):
        seen = seen if seen is not None else set()
        if module in seen:
            return seen
        seen.add(module)
        tree = ast.parse(open(os.path.join(REPO_ROOT, module + ".py"),
                              encoding="utf-8").read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in local:
                    reached(name, seen)
        return seen

    needed = reached(entrypoint[:-3])
    missing = sorted(m + ".py" for m in needed if (m + ".py") not in copied)
    ok &= check("every module the entrypoint imports is COPYed (%s)"
                % (", ".join(missing) if missing else "none missing"),
                not missing)

    # The other direction is a warning, not a failure: diff_runs.py is copied
    # deliberately as a companion tool even though the engine never imports
    # it. But anything copied must at least still EXIST.
    gone = sorted(f for f in copied
                  if not os.path.exists(os.path.join(REPO_ROOT, f)))
    ok &= check("the Dockerfile copies no file that has been deleted (%s)"
                % (", ".join(gone) if gone else "none"), not gone)
    return ok


def test_sample_output():
    group("sample_output is cut from a real run")
    ok = True
    path = os.path.join(REPO_ROOT, "sample_output.json")
    if not os.path.exists(path):
        return check("sample_output.json exists", False)
    rows = json.load(open(path, encoding="utf-8"))
    ok &= check("the sample has rows", len(rows) > 0)
    names = [f.name for f in fields(Product)]
    ok &= check("its columns match the Product schema exactly",
                all(set(r) == set(names) for r in rows))
    text = json.dumps(rows, ensure_ascii=False)
    ok &= check("the sample carries no fabrication markers",
                not re.search(r"example\.com|lorem ipsum|FIXME|TODO|XXXX",
                              text, re.IGNORECASE))
    ok &= check("every sample row has a numeric Etsy listing id",
                all(re.fullmatch(r"\d{5,}", r.get("sku") or "") for r in rows))
    ok &= check("every sample row says which country site it came from",
                all((r.get("source") or "") in HOSTS for r in rows))
    ok &= check("every sample row's URL is a real listing URL",
                all("/listing/" in (r.get("url") or "") for r in rows))
    # A sample that is all ads, or all organic, would hide the column that
    # matters most on this site.
    ok &= check("the sample shows both sponsored and organic rows",
                any(r.get("is_ad") for r in rows)
                and any(r.get("is_ad") is False for r in rows))
    # The sample is what a reader judges the output by, so it has to show the
    # provenance column doing its job rather than a column of nulls.
    ok &= check("the sample shows a real price_source",
                all(r.get("price_source") in ("jsonld", "jsonld+dom", "dom")
                    for r in rows))

    csv_path = os.path.join(REPO_ROOT, "sample_output.csv")
    if os.path.exists(csv_path):
        header = open(csv_path, encoding="utf-8").read().split("\n")[0]
        ok &= check("the sample CSV header matches the schema",
                    header.strip().split(",") == names)
    return ok


# ---------------------------------------------------------------------------



def main() -> int:
    ok = True
    # Checks that could not run because an optional engine library is absent.
    # Reported at the end: a suite that silently skips part of itself and
    # still says "all passed" is the same defect as code that reports success
    # without checking that what it wanted actually happened.
    skips = []

    ok &= test_price_parsing()
    ok &= test_second_locale()
    ok &= test_ads()
    ok &= test_ratings()
    ok &= test_tile_prices()
    ok &= test_listing_values()
    ok &= test_product_detail()
    ok &= test_shop_metadata()
    ok &= test_jsonld_shapes()
    ok &= test_urls()
    ok &= test_page_state()
    ok &= test_page_flow()
    ok &= test_output_contract()
    ok &= test_writers()
    ok &= test_finish_run()
    ok &= test_diff()
    ok &= test_diff_refuses_cross_storefront()
    ok &= test_captcha()
    ok &= test_env_config()
    ok &= test_proxy_pool()
    ok &= test_engines(skips)
    ok &= test_datadome_frames_beat_the_markup()
    ok &= test_datadome_solver()
    ok &= test_datadome_policy_is_shared()
    ok &= test_pyppeteer_teardown_noise()
    ok &= test_canary_separates_access_from_defect()
    ok &= test_ci_checks_is_actually_wired_up()
    ok &= test_no_capture_leaks()
    ok &= test_wording()
    ok &= test_fingerprint_application()
    ok &= test_credentials_never_reach_a_log()
    ok &= test_concurrent_dispatch(skips)
    ok &= test_no_undefined_names()
    ok &= test_dockerfile_copies_what_it_runs()
    ok &= test_sample_output()

    print()
    if _failures:
        print("%d check(s) FAILED:" % len(_failures))
        for f in _failures:
            print("  - %s" % f)
    if skips:
        print("%d engine group(s) SKIPPED — an optional engine library is "
              "absent. CI's engine-smoke job installs all three and fails if "
              "this list is non-empty, because a skip reads exactly like a "
              "passing run:" % len(skips))
        for s in skips:
            print("  - %s" % s)
    print("smoke_test: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
