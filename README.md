# Shelf Watch: Books to Scrape price tracker

Class project for Applied Programming & AI (NOVA SBE). A Python scraper collects all 1,000 books from [books.toscrape.com](https://books.toscrape.com), a sandbox site without an API, stores them in a Supabase (Postgres) database that tracks price and stock changes over time, and shows the data in a web front end.

## How it fits together

```
books.toscrape.com  →  scraper.py  →  Supabase (schema.sql)  →  index.html
     (HTML pages)      (requests +      (5 tables, 2 views,      (reads live via
                        BeautifulSoup)   read-only access)        the REST API)
```

## Database design

Stable facts about a book are stored once; everything that can change is stored as a new observation on every scrape run.

| Table | Contents |
|---|---|
| `categories` | One row per genre (50) |
| `books` | Stable attributes; UPC is the natural key |
| `scrape_runs` | One row per scraper execution (status, counts, `is_simulated` flag) |
| `price_history` | One price per book per run, append-only |
| `stock_history` | One stock level per book per run, append-only |

Views: `book_latest` (current state of every book) and `price_changes` (only the moments a price changed, using `lag()`).
Row Level Security: the public key can only read; the scraper writes with the server-side secret key.

books.toscrape.com never changes its prices, so `--simulate` creates a run with artificial price changes to demonstrate the history tracking. Those runs are flagged `is_simulated = true`.

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env          # add your Supabase URL and secret key

python scraper.py --max-books 5           # test: 5 books, writes books.json
python scraper.py                         # full site: 1,000 books (~10 min)
python scraper.py --db                    # scrape and save to Supabase
python scraper.py --from-json books.json --db --simulate   # demo run with price changes
```

Create the tables once by running `schema.sql` in the Supabase SQL Editor.

## Front end

`index.html` is a single file with no build step. Open it in a browser, or publish it with GitHub Pages. It loads live data from Supabase and falls back to an embedded snapshot if the database can't be reached.
