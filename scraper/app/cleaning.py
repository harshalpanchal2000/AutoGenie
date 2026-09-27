"""Normalisation, dedup keys and filters for scraped leads."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

TRACKING_PARAMS = re.compile(r"^(utm_|gclid|fbclid|msclkid|yclid|mc_|_ga|ref$|source$)", re.I)
FEATURE_ID_RE = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", re.I)
COORDS_RE = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
WS_RE = re.compile(r"\s+")


def clean_text(value: str | None) -> str | None:
    if not value:
        return None
    # Google Maps sprinkles private-use icon glyphs and zero-width chars in text.
    value = re.sub(r"[-​-‏‪-‮]", "", value)
    value = WS_RE.sub(" ", value).strip(" \n\t·,")
    return value or None


def normalize_phone(phone: str | None) -> tuple[str | None, str | None]:
    """Returns (display, digits). digits keeps a leading + for dedup/tel: links."""
    phone = clean_text(phone)
    if not phone:
        return None, None
    digits = re.sub(r"[^\d+]", "", phone)
    digits = digits[0] + digits[1:].replace("+", "") if digits else digits
    if len(re.sub(r"\D", "", digits)) < 6:
        return None, None
    return phone, digits


def unwrap_google_redirect(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.endswith("google.com") and parsed.path == "/url":
        q = parse_qs(parsed.query)
        for key in ("q", "url"):
            if q.get(key):
                return q[key][0]
    return url


def normalize_website(url: str | None) -> tuple[str | None, str | None]:
    """Returns (clean_url, bare_domain)."""
    if not url:
        return None, None
    url = unwrap_google_redirect(url.strip())
    if not re.match(r"^https?://", url, re.I):
        url = "http://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        return None, None
    query = [(k, v) for k, v in parse_qs(parsed.query, keep_blank_values=True).items()
             if not TRACKING_PARAMS.match(k)]
    clean = urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path or "/",
        "",
        urlencode(query, doseq=True),
        "",
    ))
    domain = parsed.netloc.lower().split(":")[0]
    domain = domain[4:] if domain.startswith("www.") else domain
    return clean, domain


def parse_rating(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", text)
    return float(m.group(0).replace(",", ".")) if m else None


def parse_int(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def feature_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = FEATURE_ID_RE.search(url)
    return m.group(1).lower() if m else None


def coords_from_url(url: str | None) -> tuple[float | None, float | None]:
    if not url:
        return None, None
    m = COORDS_RE.search(url)
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)


def _norm_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def lead_key(name: str | None, address: str | None, feature_id: str | None) -> str:
    """Stable identity for a business, used for cross-run dedup."""
    if feature_id:
        return f"fid:{feature_id}"
    raw = f"{_norm_key(name)}|{_norm_key(address)}"
    return "hash:" + hashlib.sha1(raw.encode()).hexdigest()[:20]


def passes_filters(lead: dict, f: dict) -> tuple[bool, str | None]:
    """Returns (ok, reason_if_rejected)."""
    if lead.get("permanently_closed") and not f.get("include_closed"):
        return False, "closed"
    if f.get("require_phone") and not lead.get("phone"):
        return False, "no_phone"
    if f.get("require_website") and not lead.get("website"):
        return False, "no_website"
    if f.get("without_website") and lead.get("website"):
        return False, "has_website"
    min_rating = f.get("min_rating")
    if min_rating is not None and (lead.get("rating") or 0) < min_rating:
        return False, "low_rating"
    max_rating = f.get("max_rating")
    if max_rating is not None and lead.get("rating") is not None and lead["rating"] > max_rating:
        return False, "high_rating"
    min_reviews = f.get("min_reviews")
    if min_reviews is not None and (lead.get("reviews") or 0) < min_reviews:
        return False, "few_reviews"
    return True, None
