"""SQLite store of already-delivered leads, so repeat runs only return new ones."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    lead_key     TEXT PRIMARY KEY,
    name         TEXT,
    phone_digits TEXT,
    domain       TEXT,
    address      TEXT,
    query        TEXT,
    first_seen   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_leads_phone  ON leads(phone_digits);
CREATE INDEX IF NOT EXISTS idx_leads_domain ON leads(domain);
"""

# Domains shared by many unrelated businesses must never be used as dedup keys.
SHARED_DOMAINS = {
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "yelp.com", "google.com", "business.site", "sites.google.com", "wixsite.com",
    "godaddysites.com", "square.site", "linktr.ee", "tiktok.com", "youtube.com",
    "homeadvisor.com", "angi.com", "thumbtack.com", "nextdoor.com", "yellowpages.com",
}


class LeadStore:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def is_seen_key(self, key: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM leads WHERE lead_key = ?", (key,)).fetchone()
        return row is not None

    def is_duplicate(self, lead: dict) -> bool:
        """Matches by place key, then phone, then (non-shared) website domain."""
        if self.is_seen_key(lead["lead_key"]):
            return True
        with self._lock:
            if lead.get("phone_digits"):
                if self._conn.execute(
                    "SELECT 1 FROM leads WHERE phone_digits = ?", (lead["phone_digits"],)
                ).fetchone():
                    return True
            domain = lead.get("domain")
            if domain and not is_shared_domain(domain):
                if self._conn.execute("SELECT 1 FROM leads WHERE domain = ?", (domain,)).fetchone():
                    return True
        return False

    def mark_seen(self, leads: list[dict], query: str) -> None:
        now = int(time.time())
        rows = [
            (l["lead_key"], l.get("name"), l.get("phone_digits"),
             None if is_shared_domain(l.get("domain")) else l.get("domain"),
             l.get("address"), query, now)
            for l in leads
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO leads VALUES (?, ?, ?, ?, ?, ?, ?)", rows
            )
            self._conn.commit()

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]

    def clear(self, query: str | None = None) -> int:
        with self._lock:
            if query:
                cur = self._conn.execute("DELETE FROM leads WHERE query = ?", (query,))
            else:
                cur = self._conn.execute("DELETE FROM leads")
            self._conn.commit()
            return cur.rowcount


def is_shared_domain(domain: str | None) -> bool:
    if not domain:
        return False
    return any(domain == d or domain.endswith("." + d) for d in SHARED_DOMAINS)
