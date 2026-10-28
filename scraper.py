"""
Books to Scrape - scraper
=========================

Phase 1 (default): scrape books.toscrape.com and write a JSON file.
Phase 2 (--db):    also write the results into Supabase (schema.sql).

Examples
--------
    python scraper.py --max-books 5            # quick test: 5 books
    python scraper.py --max-pages 2            # first 2 pages (40 books)
    python scraper.py                          # whole site (1,000 books, ~10 min)
    python scraper.py --max-pages 2 --db       # same, and save to Supabase
    python scraper.py --max-pages 2 --db --simulate   # demo run with fake price changes
"""

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://books.toscrape.com/"
HEADERS = {"User-Agent": "Mozilla/5.0 (educational scraper; NOVA SBE Applied Programming & AI)"}
DELAY_SECONDS = 0.5
RATING_WORDS = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
session = requests.Session()
session.headers.update(HEADERS)


def get_soup(url, retries=3):
    """Download a page and return parsed HTML. Retries on network errors."""
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=15)
            response.raise_for_status()
            response.encoding = "utf-8"  # otherwise "£" comes out as "Â£"
            time.sleep(DELAY_SECONDS)    # be polite
            return BeautifulSoup(response.text, "html.parser")
        except requests.RequestException as error:
            if attempt == retries:
                raise
            print(f"  retry {attempt}/{retries} for {url}: {error}")
            time.sleep(2 * attempt)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def parse_listing_page(soup, page_url):
    """Return (list of book detail URLs, URL of next page or None)."""
    book_urls = [
        urljoin(page_url, article.h3.a["href"])
        for article in soup.select("article.product_pod")
    ]
    next_link = soup.select_one("li.next a")
    next_url = urljoin(page_url, next_link["href"]) if next_link else None
    return book_urls, next_url


def parse_price(text):
    """'£51.77' -> 51.77"""
    match = re.search(r"\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


def parse_book_page(soup, book_url):
    """Extract all fields from a book's detail page."""
    product = soup.select_one("div.product_main")

    # Product information table: UPC, prices, availability, ...
    table = {
        row.th.get_text(strip=True): row.td.get_text(strip=True)
        for row in soup.select("table.table-striped tr")
    }

    # Rating is stored as a CSS class: <p class="star-rating Three">
    rating_tag = product.select_one("p.star-rating")
    rating_word = next((c for c in rating_tag["class"] if c in RATING_WORDS), None)

    # Availability: "In stock (22 available)"
    availability = table.get("Availability") or product.select_one("p.availability").get_text(strip=True)
    quantity_match = re.search(r"\((\d+) available\)", availability)

    # Description sits in the <p> right after <div id="product_description">
    description_header = soup.select_one("#product_description")
    description = (
        description_header.find_next_sibling("p").get_text(strip=True)
        if description_header else None
    )

    # Breadcrumb: Home > Books > <Category> > <Title>
    category_link = soup.select("ul.breadcrumb li a")[-1]

    image = soup.select_one("#product_gallery img")

    return {
        "upc": table.get("UPC"),
        "title": product.h1.get_text(strip=True),
        "category": {
            "name": category_link.get_text(strip=True),
            "url": urljoin(book_url, category_link["href"]),
        },
        "rating": RATING_WORDS.get(rating_word),
        "description": description,
        "image_url": urljoin(book_url, image["src"]) if image else None,
        "product_url": book_url,
        "price": parse_price(product.select_one("p.price_color").get_text()),
        "currency": "GBP",
        "in_stock": availability.lower().startswith("in stock"),
        "quantity": int(quantity_match.group(1)) if quantity_match else 0,
    }


# ---------------------------------------------------------------------------
# Scraping loop
# ---------------------------------------------------------------------------
def scrape(max_pages=None, max_books=None, base_url=BASE_URL):
    books, errors, pages = [], [], 0
    page_url = base_url

    while page_url:
        if max_pages is not None and pages >= max_pages:
            break
        soup = get_soup(page_url)
        pages += 1
        book_urls, page_url = parse_listing_page(soup, page_url)
        print(f"Page {pages}: {len(book_urls)} books")

        for book_url in book_urls:
            if max_books is not None and len(books) >= max_books:
                return books, errors, pages
            try:
                book = parse_book_page(get_soup(book_url), book_url)
                books.append(book)
                print(f"  [{len(books)}] {book['title'][:60]}  £{book['price']}")
            except Exception as error:  # keep going, log the failure
                errors.append({"url": book_url, "error": str(error)})
                print(f"  ERROR {book_url}: {error}")

    return books, errors, pages


# ---------------------------------------------------------------------------
# Phase 2: Supabase
# ---------------------------------------------------------------------------
def simulate_price_changes(books, share=0.05, max_change=0.10):
    """Demo only: change ~5% of prices by up to +/-10%."""
    for book in random.sample(books, max(1, int(len(books) * share))):
        factor = 1 + random.uniform(-max_change, max_change)
        book["price"] = round(book["price"] * factor, 2)
    return books


def save_to_supabase(books, errors, pages, started_at, simulated):
    from dotenv import load_dotenv
    from supabase import create_client

    load_dotenv()
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY in .env")
    db = create_client(url, key)
    now = datetime.now(timezone.utc).isoformat()

    run = db.table("scrape_runs").insert({
        "started_at": started_at,
        "status": "running",
        "is_simulated": simulated,
    }).execute().data[0]
    run_id = run["id"]

    try:
        # Categories: upsert on name, then build name -> id lookup
        categories = {b["category"]["name"]: b["category"]["url"] for b in books}
        rows = db.table("categories").upsert(
            [{"name": n, "url": u} for n, u in categories.items()], on_conflict="name"
        ).execute().data
        category_ids = {r["name"]: r["id"] for r in rows}

        # Books: upsert on UPC (first_seen_at keeps its original value)
        book_rows = db.table("books").upsert([{
            "upc": b["upc"],
            "title": b["title"],
            "category_id": category_ids[b["category"]["name"]],
            "rating": b["rating"],
            "description": b["description"],
            "image_url": b["image_url"],
            "product_url": b["product_url"],
            "last_seen_at": now,
        } for b in books], on_conflict="upc").execute().data
        book_ids = {r["upc"]: r["id"] for r in book_rows}

        # Observations: one price + one stock row per book for this run
        db.table("price_history").insert([{
            "book_id": book_ids[b["upc"]], "run_id": run_id,
            "price": b["price"], "currency": b["currency"], "scraped_at": now,
        } for b in books]).execute()
        db.table("stock_history").insert([{
            "book_id": book_ids[b["upc"]], "run_id": run_id,
            "in_stock": b["in_stock"], "quantity": b["quantity"], "scraped_at": now,
        } for b in books]).execute()

        status = "success" if not errors else "partial"
    except Exception as error:
        status = "failed"
        errors.append({"url": None, "error": f"database: {error}"})
        print(f"Database error: {error}")

    db.table("scrape_runs").update({
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "pages_scraped": pages,
        "books_scraped": len(books),
        "error_count": len(errors),
    }).eq("id", run_id).execute()
    print(f"Supabase: run {run_id} saved with status '{status}'")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Scrape books.toscrape.com")
    parser.add_argument("--max-pages", type=int, help="stop after N listing pages")
    parser.add_argument("--max-books", type=int, help="stop after N books")
    parser.add_argument("--output", default="books.json", help="JSON output file")
    parser.add_argument("--db", action="store_true", help="also save to Supabase")
    parser.add_argument("--simulate", action="store_true", help="fake price changes (demo, needs --db)")
    parser.add_argument("--base-url", default=BASE_URL, help=argparse.SUPPRESS)  # for local testing
    parser.add_argument("--from-json", help="skip scraping: load this earlier output file into Supabase")
    args = parser.parse_args()

    if args.from_json:
        with open(args.from_json, encoding="utf-8") as f:
            data = json.load(f)
        books = data["books"]
        if args.simulate:
            simulate_price_changes(books)
        save_to_supabase(books, data.get("errors", []), data["pages_scraped"],
                         data["collected_at"] if not args.simulate else datetime.now(timezone.utc).isoformat(),
                         args.simulate)
        return

    started_at = datetime.now(timezone.utc).isoformat()
    books, errors, pages = scrape(args.max_pages, args.max_books, args.base_url)

    if args.simulate:
        simulate_price_changes(books)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump({
            "collected_at": started_at,
            "source": args.base_url,
            "pages_scraped": pages,
            "book_count": len(books),
            "error_count": len(errors),
            "simulated": args.simulate,
            "books": books,
            "errors": errors,
        }, f, ensure_ascii=False, indent=2)
    print(f"\nDone: {len(books)} books, {len(errors)} errors -> {args.output}")

    if args.db:
        save_to_supabase(books, errors, pages, started_at, args.simulate)


if __name__ == "__main__":
    main()
