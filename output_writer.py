"""
output_writer.py
-----------------
Shared row models + JSON/CSV writers used by all three scrapers.

Three modes, one row shape
--------------------------
    --mode listing   search results, category grids and /market/ pages
                     -> Product
    --mode product   one /listing/<id> detail page -> Product, with the
                     trailing detail-only fields populated
    --mode shop      one /shop/<name> front — the seller's own catalogue,
                     -> Product rows, plus the shop's own facts in the
                     sidecar (see product_parser.shop_metadata)

All three modes yield the SAME class, because on Etsy a detail page is not a
different kind of object from a tile — it is the same listing described more
fully — and a shop front is a listing page that happens to be filtered to one
seller. So there is no second dataclass here (another repo in this family
needs one for reviews; this one does not), and `diff_runs.py` can compare a
listing run against a product run without either side being an artefact.

Why the shop's own facts are NOT a row: a run covers exactly one shop, so a
`Shop` dataclass would produce output files holding a single line, and the
seller's rating and location cannot be attached to a product row without
claiming they describe the product. The sidecar is the honest place for
one-per-run context.

`Product` keeps the family's field order exactly, with the Etsy-specific
columns appended after `price_source`, so a consumer written against another
repo in this family still reads the first sixteen columns unchanged.

Everything below is row-class-agnostic: pass `row_cls` so an empty CSV still
gets the right header for the mode that produced it.
"""

import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# The hostname a row came from. Etsy is ONE host — every market lives on
# www.etsy.com and the locale is a path prefix (/de/, /uk/) — so unlike the
# sibling repo whose eleven country sites each needed naming, this column is
# the same on every row of every run. It is kept because the family's schema
# has it in this position and a consumer reads the columns by name across
# repos; the LOCALE that actually varies is derivable from the row's `url`.
SOURCE_DEFAULT = "etsy.com"


@dataclass
class Product:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    url: str = ""
    # Etsy's listing id. Read from the tile's own `data-listing-id` on a
    # listing page and from the detail page's `sku` field, and the two agree
    # with the id in the URL (519688604 on all three for the captured
    # listing). This is the id Etsy's own URLs, API and seller tools use.
    sku: Optional[str] = None
    title: Optional[str] = None
    # The SHOP, and that is Etsy's own claim rather than an interpretation:
    # `brand.name` in the listing's structured data is the seller's shop name
    # ("StonehousePotteryOH"). On a marketplace of makers the shop is the
    # brand, so this column carries it instead of being null on every row.
    #
    # Two sources, because their coverage is complementary and neither is
    # enough alone (measured on live captures, 2026-09-09): search tiles
    # print the shop name in the markup (63/63) while their JSON-LD covers
    # only 8 of 64 listings; category tiles print no shop name at all (0/64)
    # while their JSON-LD covers 61 of 65. A shop-front run falls back to the
    # name in the URL, which is the same seller for every row by definition.
    brand: Optional[str] = None
    price: Optional[float] = None
    # No guessed default: a row whose currency could not be established says
    # None rather than claiming USD. Etsy quotes at least fourteen
    # currencies across its storefronts and prints a BARE "$" for seven of
    # them (USD, CAD, AUD, NZD, HKD, SGD, MXN), so a defaulted "USD" would be
    # confidently wrong rather than merely unknown. Structured data naming
    # its own currency always wins over a symbol read off the page.
    currency: Optional[str] = None
    # The was-price, from the tile's strikethrough node. Etsy renders exactly
    # ONE kind of struck-through price — unlike the sibling repo, where a
    # second one (the EU Omnibus 30-day low) sits in nearly identical markup
    # and reads as a negative discount. Counted on the live captures: 17 of
    # 63 search tiles and 22 of 64 category tiles carry a strike, all of them
    # a was-price. Null in --mode product: a detail page carries no
    # strikethrough of its own, and the "similar items" carousel's strike
    # prices belong to other listings.
    original_price: Optional[float] = None
    discount_pct: Optional[float] = None
    # The LISTING's own rating — this item's reviews, not its seller's.
    #
    # Null on every listing and shop row, and that is measured rather than
    # missed: Etsy's listing-page JSON-LD carries no `aggregateRating` at all
    # (0 of 105 product nodes across a search page, a category page and a
    # shop front), and the stars printed on a tile are the SHOP's — see
    # `shop_rating`. Populated by --mode product, where the detail page's
    # `aggregateRating` is genuinely per-listing: two listings of the same
    # shop report 825 and 375 reviews while the shop's own page reports
    # 16679.
    rating: Optional[float] = None
    review_count: Optional[int] = None
    in_stock: Optional[bool] = None
    image_url: Optional[str] = None
    category: Optional[str] = None
    # Where `price` came from, because the same column can hold figures of
    # different confidence and nothing else says which:
    #   "dom"        — the rendered tile only. The NORMAL case on this site,
    #                  not a fallback: Etsy's JSON-LD covers 8 of 64 tiles on
    #                  a search page, so most rows have nothing to be
    #                  confirmed against.
    #   "jsonld+dom" — the structured price AND the rendered tile agree. The
    #                  trustworthy read, and the one the canary thresholds —
    #                  per PAGE KIND, because the achievable share differs by
    #                  a factor of eight between search and category.
    #   "jsonld"     — structured data only; no price was found in the tile.
    # diff_runs.py reports a price change that comes with a price_source
    # change as `source_changed`, not `changed`: that says something about
    # our own two snapshots, not about Etsy.
    price_source: Optional[str] = None

    # ---- Etsy-specific, appended so the family prefix above is stable ----
    # Which listing page this row came from (1-based), and its position in
    # that page as the site ordered it. Without `page`, `position` is
    # ambiguous: it restarts at 1 on every page, so a row from page 3 would
    # claim the same position as one from page 1. Both null in
    # --mode product, where there is no page.
    page: Optional[int] = None
    position: Optional[int] = None
    # Whether this row is a SPONSORED placement rather than an organic
    # result. 16 of 64 tiles on the live German search page were ads, mixed
    # into the organic results, so without this column paid placements enter
    # the data as organic ones and any ranking built on the output is wrong.
    #
    # None means the tile did not say — exactly one tile per captured page
    # carries no `ls` parameter, and calling those organic would be a guess
    # presented as a fact. Note a listing can appear BOTH as an ad and
    # organically on one page, so a dedupe on `sku` collapses the pair.
    is_ad: Optional[bool] = None
    # The seller's numeric id, from the tile's own `data-shop-id` (present on
    # 100% of tiles on all three page kinds). Kept beside `brand` rather than
    # instead of it because a shop can be renamed and its id cannot, so this
    # is what a run is safely joinable on.
    shop_id: Optional[str] = None
    # The SELLER's rating and review count, which is what a listing tile
    # actually prints. Kept apart from `rating`/`review_count` because they
    # are a different fact about a different thing, and folding them together
    # would make one column mean the listing on a product run and the shop on
    # a listing run — with nothing in the data to say which.
    #
    # The proof they are the shop's: on every page captured, every seller
    # with more than one listing on it showed the SAME rating and count on
    # all of them — 12 shops across two page kinds, no exceptions. Coverage
    # 62/63 on the live search page and 56/64 on the category page; 0 on a
    # shop front's tiles, where the shop's own figures come from the page's
    # `Organization` data instead and land in the sidecar as well.
    shop_rating: Optional[float] = None
    shop_review_count: Optional[int] = None
    # True when `price` is a MINIMUM rather than the price — a listing with
    # variations, which Etsy prints as "ab 34,00 €" / "from $34.00" and
    # publishes as an `AggregateOffer` with no `price` key at all. 12 of 63
    # live search tiles and 15 of 64 category tiles. Without this column
    # those rows look like ordinary prices and understate the product.
    price_is_from: Optional[bool] = None
    # The top of that range, from `AggregateOffer.highPrice`. Only structured
    # data states it — the tile prints the low end alone — so this is null on
    # a from-price row whose listing had no JSON-LD entry.
    price_max: Optional[float] = None
    # ---- populated by --mode product only; null on a listing run ----
    # Whether shipping is free, from the offer's own
    # `shippingDetails.shippingRate.value == 0`. A FACT, and the reason this
    # is product-mode only: listing tiles carry just a localised badge
    # ("Kostenloser Versand" / "FREE shipping") whose wording differs per
    # storefront, and their JSON-LD has no shipping node at all. A column
    # that is right on German pages and wrong elsewhere is worse than one
    # that is honestly null until the page states it.
    free_shipping: Optional[bool] = None
    # The ISO country the item ships FROM, per `shippingOrigin`. Genuinely
    # useful on a marketplace whose sellers are worldwide: the captured
    # listing ships from US-OH, another from TR.
    ships_from: Optional[str] = None
    # The listing's stated material ("Keramik"), from the detail page's
    # `material` field. A handmade-marketplace field with no equivalent in
    # the sibling repos.
    material: Optional[str] = None
    # A barcode where the seller supplied one. Most handmade listings have
    # none, so this is null far more often than not — kept because where it
    # IS present it is the only field that makes an Etsy row joinable
    # against another retailer's data.
    gtin: Optional[str] = None
    description: Optional[str] = None
    images: Optional[List[str]] = None


# Row classes by --mode, so an engine maps its mode to a schema in one place.
# All three modes are Product here; the mapping exists so adding a mode later
# is a one-line change rather than a search for every place that assumed
# Product.
ROW_CLASS_BY_MODE = {"listing": Product, "product": Product, "shop": Product}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku` and
# to hand to diff_runs.py. All three of this repo's modes qualify — a shop
# front lists each of its listings once, exactly as a category does.
UNIQUE_BY_SKU_MODES = ("listing", "product", "shop")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating next-page link then re-parses a page without
    duplicating its rows into the final output. Etsy's own pagination
    does not repeat rows — three consecutive category pages captured on
    2026-09-09 shared 0 skus out of 36 — so this is a guard against a
    re-fetch, not against the site.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.

    Both of this repo's modes are one row per `sku`, so `key` is never
    overridden here — the parameter exists because the rest of the family
    shares this function and one of them needs it.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
#
# On Etsy this code specifically does NOT cover a page with no listings — a URL
# like /de/category/tv-audio-202.html that answers 200 with a real page and
# no product grid, because it is a landing page of sub-categories rather than
# a listing. That is EXIT_NO_PRODUCTS: the request was served exactly as
# asked and simply has no products on it. Reporting it as blocked would send
# a user hunting for a proxy problem that does not exist.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "listing", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are recorded because neither is implied by the repo:
    the same output prefix can hold a listing, product or shop run, from any
    of Etsy's storefronts and therefore in any of a dozen currencies, and a
    consumer that guesses wrong compares prices that were never comparable.
    diff_runs.py refuses a pair whose modes or sources differ.

    `extra` carries facts about the run that are not about any single row.
    `--mode shop` uses it for the SELLER's own name, location, rating and
    review count: a run covers exactly one shop, so those belong to the run
    rather than repeated down a column, and the shop's review count (16679
    on the captured seller) is a different number from its listings' own
    (827 on one of them) — putting them in one column would make the schema
    lie.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `shop_rating` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 products -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 products — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" is kept for the
# engines that still stop on a missing next-link: the first is a property of
# the DATA (a page contributed nothing not already seen, so the listing is
# over), while the second is a property of a CSS SELECTOR and is therefore
# the weaker signal — a renamed attribute looks identical to a short
# catalogue. Etsy publishes NO `link[rel=next]` anywhere in its markup
# AND numbers its pages with `?page=N`, and the two agree, so all three
# layers are real here. See playwright_scraper.py.
#
# "single_page_mode" is complete by construction: --mode product reads one
# page because one page is all there is.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "single_page_mode")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "listing", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    complete = stop_reason in COMPLETE_STOP_REASONS
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all: a challenge outranks "empty result",
        # because it says something stood between the run and the content.
        return EXIT_BLOCKED if blocked else rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
