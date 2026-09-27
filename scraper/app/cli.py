"""Run the scraper from the command line (no n8n needed), e.g.:

    docker compose run --rm scraper python -m app.cli "plumbers" "Chicago, IL" --limit 5

By default the CLI does NOT touch the seen-leads DB, so test runs don't hide
leads from the real pipeline. Use --use-db to dedupe/mark like the API does.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from .maps import ScrapeOptions, scrape
from .storage import LeadStore


def main() -> None:
    p = argparse.ArgumentParser(description="Scrape Google Maps leads")
    p.add_argument("query", help="niche, e.g. 'plumbers'")
    p.add_argument("location", nargs="?", default=None, help="e.g. 'Chicago, IL'")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--phone", action="store_true", help="only leads with a phone")
    p.add_argument("--has-website", action="store_true")
    p.add_argument("--no-website", action="store_true", help="only leads WITHOUT a website")
    p.add_argument("--min-rating", type=float)
    p.add_argument("--min-reviews", type=int)
    p.add_argument("--emails", action="store_true", help="crawl websites for emails")
    p.add_argument("--use-db", action="store_true", help="dedupe against + update the seen DB")
    p.add_argument("--headed", action="store_true", help="show the browser (needs a display)")
    p.add_argument("--concurrency", type=int, default=int(os.getenv("SCRAPER_CONCURRENCY", "3")))
    args = p.parse_args()

    opts = ScrapeOptions(
        query=args.query,
        location=args.location,
        limit=args.limit,
        filters={
            "require_phone": args.phone,
            "require_website": args.has_website,
            "without_website": args.no_website,
            "min_rating": args.min_rating,
            "min_reviews": args.min_reviews,
        },
        exclude_seen=args.use_db,
        mark_seen=args.use_db,
        enrich_emails=args.emails,
        concurrency=args.concurrency,
        proxy=os.getenv("SCRAPER_PROXY") or None,
        headless=not args.headed,
    )
    store = LeadStore(os.getenv("SCRAPER_DB_PATH", "/data/leads.db")) if args.use_db else None
    result = asyncio.run(scrape(opts, store))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
