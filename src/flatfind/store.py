"""SQLite persistence: every listing ever seen, plus its pipeline status.

Statuses: `filtered` (a required criterion failed at scrape time) or `new`;
`flatfind score` moves new listings to `scored` or `rejected`. Later pipeline
steps (contacted / escalated, ...) own the transitions after that.

Per listing we also keep `facts` (manual / LLM overrides), `geo` (location
lookups) and the latest `evaluation` + `score`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flatfind.models import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'new',
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    data        TEXT NOT NULL,
    raw         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS listings_status ON listings(status);
CREATE TABLE IF NOT EXISTS cache (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    created     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS listings_source ON listings(source, source_id);
"""


# Added after the first release; created on demand for existing databases.
COLUMNS = {"facts": "TEXT NOT NULL DEFAULT '{}'", "geo": "TEXT", "evaluation": "TEXT", "score": "REAL"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(listings)")}
        for name, decl in COLUMNS.items():
            if name not in have:
                self.db.execute(f"ALTER TABLE listings ADD COLUMN {name} {decl}")
        self.db.execute("CREATE INDEX IF NOT EXISTS listings_score ON listings(score)")
        self.db.commit()

    def known_ids(self, source: str) -> set[str]:
        rows = self.db.execute("SELECT source_id FROM listings WHERE source = ?", (source,))
        return {r[0] for r in rows}

    def add(self, listing: Listing, status: str) -> bool:
        """Insert a listing; returns False if it already existed."""
        now = _now()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO listings (id, source, source_id, status, first_seen, last_seen, data, raw)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                listing.id, listing.source, listing.source_id, status, now, now,
                json.dumps(listing.to_dict(), ensure_ascii=False),
                json.dumps(listing.raw, ensure_ascii=False),
            ),
        )
        self.db.commit()
        return cur.rowcount == 1

    def update_data(self, listing: Listing) -> None:
        self.db.execute("UPDATE listings SET data = ? WHERE id = ?",
                        (json.dumps(listing.to_dict(), ensure_ascii=False), listing.id))
        self.db.commit()

    def set_status(self, listing_id: str, status: str) -> None:
        self.db.execute("UPDATE listings SET status = ? WHERE id = ?", (status, listing_id))
        self.db.commit()

    def get(self, listing_id: str) -> tuple[Listing, str] | None:
        r = self.db.execute("SELECT data, raw, status FROM listings WHERE id = ?", (listing_id,)).fetchone()
        if not r:
            return None
        listing = Listing.from_dict(json.loads(r["data"]))
        listing.raw = json.loads(r["raw"])
        return listing, r["status"]

    def list(self, status: str | None = None, limit: int = 50) -> list[tuple[Listing, str]]:
        q = "SELECT data, status FROM listings"
        args: tuple = ()
        if status:
            q += " WHERE status = ?"
            args = (status,)
        q += " ORDER BY first_seen DESC, json_extract(data, '$.published_at') DESC LIMIT ?"
        return [(Listing.from_dict(json.loads(r["data"])), r["status"]) for r in self.db.execute(q, (*args, limit))]

    def counts(self) -> dict[tuple[str, str], int]:
        rows = self.db.execute("SELECT source, status, COUNT(*) FROM listings GROUP BY source, status")
        return {(r[0], r[1]): r[2] for r in rows}

    def get_extra(self, listing_id: str) -> dict:
        """facts overrides, geo facts, evaluation and score of one listing."""
        r = self.db.execute("SELECT facts, geo, evaluation, score FROM listings WHERE id = ?", (listing_id,)).fetchone()
        if not r:
            return {}
        return {
            "facts": json.loads(r["facts"]),
            "geo": json.loads(r["geo"]) if r["geo"] else None,
            "evaluation": json.loads(r["evaluation"]) if r["evaluation"] else None,
            "score": r["score"],
        }

    def set_fact(self, listing_id: str, name: str, value, key: str | None = None) -> None:
        facts = self.get_extra(listing_id)["facts"]
        if key is None:
            facts[name] = value
        else:
            facts.setdefault(name, {})[key] = value
        self.db.execute("UPDATE listings SET facts = ? WHERE id = ?", (json.dumps(facts, ensure_ascii=False), listing_id))
        self.db.commit()

    def save_geo(self, listing_id: str, geo: dict) -> None:
        self.db.execute("UPDATE listings SET geo = ? WHERE id = ?", (json.dumps(geo, ensure_ascii=False), listing_id))
        self.db.commit()

    def save_evaluation(self, listing_id: str, evaluation: dict, status: str) -> None:
        self.db.execute(
            "UPDATE listings SET evaluation = ?, score = ?, status = ? WHERE id = ?",
            (json.dumps(evaluation, ensure_ascii=False, default=str), evaluation["score"], status, listing_id),
        )
        self.db.commit()

    def ids(self, statuses: list[str]) -> list[str]:
        q = f"SELECT id FROM listings WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY first_seen DESC"
        return [r[0] for r in self.db.execute(q, statuses)]

    def ranked(self, limit: int = 20, include_rejected: bool = False) -> list[tuple[Listing, str, float, dict]]:
        """Best first. Rejected listings (score 0) are excluded, or sorted below every passing one."""
        where = "score IS NOT NULL" + ("" if include_rejected else " AND status != 'rejected'")
        q = (f"SELECT data, status, score, evaluation FROM listings WHERE {where}"
             " ORDER BY status = 'rejected', score DESC, first_seen DESC, id LIMIT ?")
        return [(Listing.from_dict(json.loads(r["data"])), r["status"], r["score"], json.loads(r["evaluation"]))
                for r in self.db.execute(q, (limit,))]

    def cache_get(self, key: str):
        r = self.db.execute("SELECT value FROM cache WHERE key = ?", (key,)).fetchone()
        return json.loads(r[0]) if r else None

    def cache_put(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO cache (key, value, created) VALUES (?, ?, ?)",
                        (key, json.dumps(value, ensure_ascii=False), _now()))
        self.db.commit()
