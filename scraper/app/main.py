"""HTTP API used by n8n: POST /scrape  ->  cleaned, de-duplicated leads + CSV."""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import logging
import os
import re

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .maps import ScrapeBlocked, ScrapeOptions, scrape
from .storage import LeadStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("scraper.api")

API_KEY = os.getenv("SCRAPER_API_KEY", "")
MAX_LIMIT = int(os.getenv("SCRAPER_MAX_LIMIT", "100"))
CONCURRENCY = int(os.getenv("SCRAPER_CONCURRENCY", "3"))
LANG = os.getenv("SCRAPER_LANG", "en")
PROXY = os.getenv("SCRAPER_PROXY") or None

store = LeadStore(os.getenv("SCRAPER_DB_PATH", "/data/leads.db"))
# One browser job at a time: protects a small VPS and keeps Google happy.
job_lock = asyncio.Lock()

app = FastAPI(title="AutoGenie Maps Scraper", version="1.0.0")

CSV_FIELDS = [
    "name", "category", "phone", "website", "emails", "address", "rating",
    "reviews", "maps_url", "lat", "lng", "query",
]


class Filters(BaseModel):
    require_phone: bool = False
    require_website: bool = False
    without_website: bool = False
    min_rating: float | None = None
    max_rating: float | None = None
    min_reviews: int | None = None
    include_closed: bool = False


class ScrapeRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=120, description="Niche, e.g. 'plumbers'")
    location: str | None = Field(None, max_length=120, description="e.g. 'Chicago, IL'")
    limit: int = Field(20, ge=1)
    filters: Filters = Filters()
    exclude_seen: bool = True
    mark_seen: bool = True
    enrich_emails: bool = False
    meta: dict = Field(default_factory=dict, description="Echoed back untouched (chat_id etc.)")


def require_key(x_api_key: str | None = Header(default=None)) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


def to_csv(leads: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for lead in leads:
        writer.writerow({**lead, "emails": "; ".join(lead.get("emails") or [])})
    return buf.getvalue()


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "leads"


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "busy": job_lock.locked()}


@app.get("/stats", dependencies=[Depends(require_key)])
async def stats() -> dict:
    return {"seen_leads": store.count(), "busy": job_lock.locked()}


@app.delete("/seen", dependencies=[Depends(require_key)])
async def clear_seen(query: str | None = None) -> dict:
    """Forget delivered leads (all, or only those of one exact search text)."""
    return {"deleted": store.clear(query)}


@app.post("/scrape", dependencies=[Depends(require_key)])
async def scrape_endpoint(req: ScrapeRequest) -> dict:
    opts = ScrapeOptions(
        query=req.query.strip(),
        location=(req.location or "").strip() or None,
        limit=min(req.limit, MAX_LIMIT),
        filters=req.filters.model_dump(),
        exclude_seen=req.exclude_seen,
        mark_seen=req.mark_seen,
        enrich_emails=req.enrich_emails,
        concurrency=CONCURRENCY,
        lang=LANG,
        proxy=PROXY,
    )
    log.info("scrape requested: %r limit=%d filters=%s", opts.search_text, opts.limit, opts.filters)
    async with job_lock:
        try:
            result = await scrape(opts, store)
        except ScrapeBlocked as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            log.exception("scrape failed")
            raise HTTPException(status_code=500, detail=f"scrape failed: {exc}") from exc

    leads = result["leads"]
    log.info("scrape done: %r -> %s", opts.search_text, result["stats"])
    return {
        "query": opts.query,
        "location": opts.location,
        "search": opts.search_text,
        "meta": req.meta,
        "stats": result["stats"],
        "count": len(leads),
        "leads": leads,
        "csv_filename": f"{slug(opts.search_text)}.csv",
        "csv_base64": base64.b64encode(to_csv(leads).encode("utf-8-sig")).decode() if leads else None,
    }
