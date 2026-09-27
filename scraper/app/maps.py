"""Google Maps scraper built on Playwright (headless Chromium).

Strategy:
  1. Open the Maps search URL for "<niche> in <location>".
  2. Scroll the results feed and collect place links (cheap).
  3. Skip places already delivered in earlier runs (SQLite) *before* opening them.
  4. Open remaining place pages with a few parallel tabs, extract details,
     clean, dedupe and filter until `limit` leads are accepted.

Google changes class names from time to time. All selectors live in SELECTORS
and EXTRACT_JS so they are easy to patch in one place.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from urllib.parse import quote_plus

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from playwright.async_api import TimeoutError as PWTimeout

from . import cleaning
from .enrich import enrich_emails
from .storage import LeadStore, is_shared_domain

log = logging.getLogger("scraper.maps")

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)

SELECTORS = {
    "feed": 'div[role="feed"]',
    "result_link": 'div[role="feed"] a[href*="/maps/place/"]',
    "place_title": "h1",
    "place_detail_item": "[data-item-id]",
    "consent_buttons": [
        'button[aria-label^="Reject all"]',
        'button[aria-label^="Accept all"]',
        'form[action*="consent"] button',
        'button:has-text("Reject all")',
        'button:has-text("Accept all")',
    ],
}

END_OF_LIST_TEXT = "reached the end of the list"

EXTRACT_JS = r"""
() => {
  const txt = (el) => el ? (el.innerText || el.textContent || '').trim() : null;
  const root = document.querySelector('div[role="main"][aria-label]') || document;
  const q = (s) => root.querySelector(s) || document.querySelector(s);

  const name = txt(q('h1.DUwDvf')) || txt(q('h1'));
  const category = txt(q('button.DkEaL')) || txt(q('button[jsaction*=".category"]'));

  const addrBtn = q('button[data-item-id="address"]');
  const address = addrBtn
    ? (txt(addrBtn.querySelector('.Io6YTe')) || (addrBtn.getAttribute('aria-label') || '').replace(/^Address:\s*/i, ''))
    : null;

  const phoneEl = q('[data-item-id^="phone:tel:"]');
  const phone = phoneEl
    ? (txt(phoneEl.querySelector('.Io6YTe')) || phoneEl.getAttribute('data-item-id').replace('phone:tel:', ''))
    : null;

  const webEl = q('a[data-item-id="authority"]');
  const website = webEl ? webEl.getAttribute('href') : null;

  const ratingEl = q('div.F7nice span[aria-hidden="true"]');
  let reviews = null;
  const reviewsEl = q('div.F7nice span[aria-label*="review"]') || q('button[jsaction*="reviewChart"] span');
  if (reviewsEl) reviews = reviewsEl.getAttribute('aria-label') || txt(reviewsEl);
  if (!reviews) {
    const f = q('div.F7nice');
    const m = f && txt(f).match(/\(([\d,.\s]+)\)/);
    if (m) reviews = m[1];
  }

  const plusEl = q('button[data-item-id="oloc"]');
  const plus_code = plusEl ? (txt(plusEl.querySelector('.Io6YTe')) || plusEl.getAttribute('aria-label')) : null;

  const body = txt(root) || '';
  return {
    name, category, address, phone, website,
    rating: ratingEl ? txt(ratingEl) : null,
    reviews,
    plus_code,
    permanently_closed: /Permanently closed/i.test(body),
    temporarily_closed: /Temporarily closed/i.test(body),
  };
}
"""


class ScrapeBlocked(RuntimeError):
    """Google served a CAPTCHA / 'unusual traffic' page."""


@dataclass
class ScrapeOptions:
    query: str
    location: str | None = None
    limit: int = 20
    filters: dict = field(default_factory=dict)
    exclude_seen: bool = True
    mark_seen: bool = True
    enrich_emails: bool = False
    concurrency: int = 3
    lang: str = "en"
    proxy: str | None = None
    headless: bool = True

    @property
    def search_text(self) -> str:
        return f"{self.query} in {self.location}" if self.location else self.query


@dataclass
class Stats:
    links_found: int = 0
    skipped_seen: int = 0
    skipped_duplicate: int = 0
    filtered: dict = field(default_factory=dict)
    errors: int = 0
    opened: int = 0
    returned: int = 0
    duration_s: float = 0.0


async def _jitter(page: Page, lo: int = 400, hi: int = 1200) -> None:
    await page.wait_for_timeout(random.randint(lo, hi))


def _check_blocked(page: Page) -> None:
    if "/sorry/" in page.url or "recaptcha" in page.url:
        raise ScrapeBlocked(
            "Google returned a CAPTCHA ('unusual traffic'). Wait a while, lower the "
            "volume / concurrency, or set SCRAPER_PROXY."
        )


async def _handle_consent(page: Page) -> None:
    if "consent." not in page.url:
        return
    for sel in SELECTORS["consent_buttons"]:
        btn = page.locator(sel).first
        try:
            if await btn.count() and await btn.is_visible():
                await btn.click()
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(1500)
                return
        except PWTimeout:
            continue
    log.warning("Consent page detected but no button matched; continuing anyway")


async def _new_context(browser: Browser, opts: ScrapeOptions) -> BrowserContext:
    ctx = await browser.new_context(
        locale=f"{opts.lang}-US" if opts.lang == "en" else opts.lang,
        user_agent=UA,
        viewport={"width": 1366, "height": 900},
        timezone_id="UTC",
    )

    async def block_heavy(route):
        if route.request.resource_type in ("image", "media", "font"):
            await route.abort()
        else:
            await route.continue_()

    await ctx.route("**/*", block_heavy)
    return ctx


async def _collect_links(page: Page, opts: ScrapeOptions, target: int) -> list[dict]:
    url = f"https://www.google.com/maps/search/{quote_plus(opts.search_text)}?hl={opts.lang}"
    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    await _handle_consent(page)
    _check_blocked(page)

    try:
        await page.wait_for_selector(
            f'{SELECTORS["feed"]}, {SELECTORS["place_title"]}', timeout=20_000
        )
    except PWTimeout:
        _check_blocked(page)
        log.warning("No results feed found for %r", opts.search_text)
        return []

    # A very specific search can jump straight to a single place page.
    if not await page.locator(SELECTORS["feed"]).count():
        if "/maps/place/" in page.url:
            return [{"href": page.url, "name": None}]
        return []

    feed = page.locator(SELECTORS["feed"]).first
    seen_hrefs: dict[str, dict] = {}
    stale_rounds = 0
    while True:
        links = await page.eval_on_selector_all(
            SELECTORS["result_link"],
            "els => els.map(e => ({href: e.href, name: e.getAttribute('aria-label')}))",
        )
        before = len(seen_hrefs)
        for link in links:
            href = link["href"].split("?")[0]
            seen_hrefs.setdefault(href, {"href": href, "name": link.get("name")})
        if len(seen_hrefs) >= target:
            break
        if await page.get_by_text(END_OF_LIST_TEXT, exact=False).count():
            break
        stale_rounds = stale_rounds + 1 if len(seen_hrefs) == before else 0
        if stale_rounds >= 6:
            break
        await feed.evaluate("el => el.scrollTo(0, el.scrollHeight)")
        await _jitter(page, 900, 1800)

    return list(seen_hrefs.values())[:target]


async def _extract_place(page: Page, href: str) -> dict | None:
    for attempt in range(2):
        try:
            await page.goto(href + "?hl=en", wait_until="domcontentloaded", timeout=40_000)
            _check_blocked(page)
            await page.wait_for_selector(SELECTORS["place_title"], timeout=15_000)
            try:
                await page.wait_for_selector(SELECTORS["place_detail_item"], timeout=5_000)
            except PWTimeout:
                pass  # some places genuinely have no address/phone rows
            await page.wait_for_timeout(600)
            return await page.evaluate(EXTRACT_JS)
        except ScrapeBlocked:
            raise
        except Exception as exc:  # noqa: BLE001 – retry any navigation/eval hiccup
            log.info("place load failed (attempt %d) %s: %s", attempt + 1, href, exc)
            await page.wait_for_timeout(1500)
    return None


def _build_lead(raw: dict, href: str, card_name: str | None, opts: ScrapeOptions) -> dict:
    name = cleaning.clean_text(raw.get("name")) or cleaning.clean_text(card_name)
    address = cleaning.clean_text(raw.get("address"))
    phone, phone_digits = cleaning.normalize_phone(raw.get("phone"))
    website, domain = cleaning.normalize_website(raw.get("website"))
    fid = cleaning.feature_id_from_url(href)
    lat, lng = cleaning.coords_from_url(href)
    return {
        "name": name,
        "category": cleaning.clean_text(raw.get("category")),
        "address": address,
        "phone": phone,
        "phone_digits": phone_digits,
        "website": website,
        "domain": domain,
        "emails": [],
        "rating": cleaning.parse_rating(raw.get("rating")),
        "reviews": cleaning.parse_int(raw.get("reviews")),
        "plus_code": cleaning.clean_text(raw.get("plus_code")),
        "permanently_closed": bool(raw.get("permanently_closed")),
        "temporarily_closed": bool(raw.get("temporarily_closed")),
        "maps_url": href,
        "lat": lat,
        "lng": lng,
        "lead_key": cleaning.lead_key(name, address, fid),
        "query": opts.search_text,
        "scraped_at": int(time.time()),
    }


async def scrape(opts: ScrapeOptions, store: LeadStore | None) -> dict:
    t0 = time.monotonic()
    stats = Stats()
    accepted: list[dict] = []
    run_keys: set[str] = set()
    run_phones: set[str] = set()
    run_domains: set[str] = set()

    scan_target = min(opts.limit * 3 + 10, 200)

    launch_kwargs: dict = {"headless": opts.headless, "args": ["--disable-dev-shm-usage"]}
    if opts.proxy:
        launch_kwargs["proxy"] = {"server": opts.proxy}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kwargs)
        try:
            ctx = await _new_context(browser, opts)
            search_page = await ctx.new_page()
            links = await _collect_links(search_page, opts, scan_target)
            stats.links_found = len(links)

            queue: asyncio.Queue = asyncio.Queue()
            for link in links:
                key = cleaning.lead_key(None, None, cleaning.feature_id_from_url(link["href"]))
                if key.startswith("fid:"):
                    if key in run_keys:
                        continue
                    run_keys.add(key)
                    if opts.exclude_seen and store and store.is_seen_key(key):
                        stats.skipped_seen += 1
                        continue
                queue.put_nowait(link)

            done = asyncio.Event()
            workers = max(1, min(opts.concurrency, queue.qsize()))
            pages = [search_page] + [await ctx.new_page() for _ in range(workers - 1)]

            def accept(lead: dict) -> None:
                if not lead["name"]:
                    stats.errors += 1
                    return
                dup = (
                    (lead["phone_digits"] and lead["phone_digits"] in run_phones)
                    or (lead["domain"] and not is_shared_domain(lead["domain"])
                        and lead["domain"] in run_domains)
                )
                if dup or (opts.exclude_seen and store and store.is_duplicate(lead)):
                    stats.skipped_duplicate += 1
                    return
                ok, reason = cleaning.passes_filters(lead, opts.filters)
                if not ok:
                    stats.filtered[reason] = stats.filtered.get(reason, 0) + 1
                    return
                if lead["phone_digits"]:
                    run_phones.add(lead["phone_digits"])
                if lead["domain"]:
                    run_domains.add(lead["domain"])
                accepted.append(lead)
                if len(accepted) >= opts.limit:
                    done.set()

            async def worker(page: Page) -> None:
                while not done.is_set():
                    try:
                        link = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    stats.opened += 1
                    raw = await _extract_place(page, link["href"])
                    if raw is None:
                        stats.errors += 1
                        continue
                    if not done.is_set():
                        accept(_build_lead(raw, link["href"], link.get("name"), opts))
                    await _jitter(page, 300, 900)

            await asyncio.gather(*(worker(p) for p in pages))
            await ctx.close()
        finally:
            await browser.close()

    if opts.enrich_emails and accepted:
        await enrich_emails(accepted)

    if opts.mark_seen and store and accepted:
        store.mark_seen(accepted, opts.search_text)

    stats.returned = len(accepted)
    stats.duration_s = round(time.monotonic() - t0, 1)
    return {"leads": accepted, "stats": stats.__dict__}
