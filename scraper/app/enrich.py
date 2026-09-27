"""Optional enrichment: find public contact emails on a lead's own website."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin

import httpx

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")
MAILTO_RE = re.compile(r'mailto:([^"\'?>\s]+)', re.I)
JUNK_EMAIL = re.compile(
    r"(\.(png|jpe?g|gif|svg|webp|css|js)$|example\.|domain\.com|email\.com|yourname|"
    r"sentry|wixpress|@2x|godaddy|squarespace|u00|noreply|no-reply)",
    re.I,
)
CONTACT_PATHS = ("/contact", "/contact-us", "/about")
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0 Safari/537.36"
)


def extract_emails(html: str) -> list[str]:
    found = MAILTO_RE.findall(html) + EMAIL_RE.findall(html)
    out: list[str] = []
    for e in found:
        e = e.strip().strip(".").lower()
        if JUNK_EMAIL.search(e) or len(e) > 80:
            continue
        if e not in out:
            out.append(e)
    return out


async def find_emails(client: httpx.AsyncClient, website: str) -> list[str]:
    emails: list[str] = []
    for i, path in enumerate(("",) + CONTACT_PATHS):
        url = website if i == 0 else urljoin(website, path)
        try:
            resp = await client.get(url)
        except (httpx.HTTPError, ValueError):
            if i == 0:
                return []  # site unreachable, don't try sub-pages
            continue
        ctype = resp.headers.get("content-type", "")
        if resp.status_code < 400 and "html" in ctype:
            emails += [e for e in extract_emails(resp.text[:800_000]) if e not in emails]
        if emails:
            break
    return emails[:5]


async def enrich_emails(leads: list[dict], concurrency: int = 8) -> None:
    """Adds `emails` (list) to each lead that has a website. Mutates in place."""
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0),
        follow_redirects=True,
        headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"},
        verify=False,  # many small-business sites have broken certificates
    ) as client:

        async def one(lead: dict) -> None:
            if not lead.get("website"):
                lead["emails"] = []
                return
            async with sem:
                try:
                    lead["emails"] = await asyncio.wait_for(find_emails(client, lead["website"]), 30)
                except asyncio.TimeoutError:
                    lead["emails"] = []

        await asyncio.gather(*(one(l) for l in leads))
