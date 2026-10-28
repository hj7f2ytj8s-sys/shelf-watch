-- =====================================================================
-- Books to Scrape: database schema (Supabase / Postgres 15+)
-- Run once in Supabase: SQL Editor -> New query -> paste -> Run
-- =====================================================================
-- Design in one sentence:
--   Stable facts about a book live in `books`; everything that can change
--   over time (price, stock) is an append-only observation linked to the
--   book AND to the scrape run that produced it.
-- =====================================================================


-- ---------------------------------------------------------------------
-- 1. categories: lookup table (one row per genre, ~50 rows)
-- ---------------------------------------------------------------------
create table public.categories (
  id          bigint generated always as identity primary key,
  name        text not null unique,
  url         text not null unique,
  created_at  timestamptz not null default now()
);


-- ---------------------------------------------------------------------
-- 2. scrape_runs: one row per execution of the scraper
--    Gives every observation provenance ("which run saw this price?")
--    and lets you monitor the scraper itself.
-- ---------------------------------------------------------------------
create table public.scrape_runs (
  id             bigint generated always as identity primary key,
  started_at     timestamptz not null default now(),
  finished_at    timestamptz,
  status         text not null default 'running'
                 check (status in ('running', 'success', 'partial', 'failed')),
  pages_scraped  integer not null default 0,
  books_scraped  integer not null default 0,
  error_count    integer not null default 0,
  is_simulated   boolean not null default false,  -- true = demo run with artificial price changes
  notes          text
);


-- ---------------------------------------------------------------------
-- 3. books: stable attributes, one row per book
--    UPC is the natural key (titles are not unique and get truncated).
-- ---------------------------------------------------------------------
create table public.books (
  id             bigint generated always as identity primary key,
  upc            text not null unique,
  title          text not null,
  category_id    bigint references public.categories(id) on delete set null,
  rating         smallint check (rating between 1 and 5),
  description    text,
  image_url      text,
  product_url    text not null unique,
  first_seen_at  timestamptz not null default now(),
  last_seen_at   timestamptz not null default now()  -- if this stops updating, the book was delisted
);

create index books_category_idx on public.books (category_id);


-- ---------------------------------------------------------------------
-- 4. price_history: one row per book per run (append-only)
--    numeric, never float, for money.
--    unique(book_id, run_id) makes a retried run idempotent.
-- ---------------------------------------------------------------------
create table public.price_history (
  id          bigint generated always as identity primary key,
  book_id     bigint not null references public.books(id) on delete cascade,
  run_id      bigint not null references public.scrape_runs(id) on delete cascade,
  price       numeric(10, 2) not null check (price >= 0),
  currency    char(3) not null default 'GBP',
  scraped_at  timestamptz not null default now(),
  unique (book_id, run_id)
);

create index price_history_book_time_idx on public.price_history (book_id, scraped_at desc);


-- ---------------------------------------------------------------------
-- 5. stock_history: one row per book per run (append-only)
-- ---------------------------------------------------------------------
create table public.stock_history (
  id          bigint generated always as identity primary key,
  book_id     bigint not null references public.books(id) on delete cascade,
  run_id      bigint not null references public.scrape_runs(id) on delete cascade,
  in_stock    boolean not null,
  quantity    integer check (quantity >= 0),
  scraped_at  timestamptz not null default now(),
  unique (book_id, run_id)
);

create index stock_history_book_time_idx on public.stock_history (book_id, scraped_at desc);


-- =====================================================================
-- Views: what the front end reads (keeps query logic out of the UI)
-- security_invoker = the view respects the RLS policies below
-- =====================================================================

-- Current state of every book: latest price + latest stock
create view public.book_latest with (security_invoker = true) as
select
  b.id,
  b.upc,
  b.title,
  c.name        as category,
  b.rating,
  p.price,
  p.currency,
  s.in_stock,
  s.quantity,
  p.scraped_at  as observed_at,
  b.product_url,
  b.image_url
from public.books b
left join public.categories c on c.id = b.category_id
left join lateral (
  select price, currency, scraped_at
  from public.price_history ph
  where ph.book_id = b.id
  order by scraped_at desc
  limit 1
) p on true
left join lateral (
  select in_stock, quantity
  from public.stock_history sh
  where sh.book_id = b.id
  order by scraped_at desc
  limit 1
) s on true;

-- Only the moments a price actually changed
create view public.price_changes with (security_invoker = true) as
select
  t.book_id,
  b.title,
  t.scraped_at,
  t.previous_price,
  t.price,
  t.price - t.previous_price as change
from (
  select
    book_id,
    scraped_at,
    price,
    lag(price) over (partition by book_id order by scraped_at) as previous_price
  from public.price_history
) t
join public.books b on b.id = t.book_id
where t.previous_price is not null
  and t.price <> t.previous_price;


-- =====================================================================
-- Row Level Security
--   Front end (anon key): read-only.
--   Scraper (service_role key, server-side only): bypasses RLS to write.
-- =====================================================================
alter table public.categories    enable row level security;
alter table public.scrape_runs   enable row level security;
alter table public.books         enable row level security;
alter table public.price_history enable row level security;
alter table public.stock_history enable row level security;

create policy "public read" on public.categories    for select to anon, authenticated using (true);
create policy "public read" on public.scrape_runs   for select to anon, authenticated using (true);
create policy "public read" on public.books         for select to anon, authenticated using (true);
create policy "public read" on public.price_history for select to anon, authenticated using (true);
create policy "public read" on public.stock_history for select to anon, authenticated using (true);
