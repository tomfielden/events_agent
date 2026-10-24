"""SQLite storage for sources, events, family members, preferences and feedback."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from events_agent.models import Event, normalize_category, to_local_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,              -- ticketmaster | tmdb | ical | webpage
    url TEXT NOT NULL DEFAULT '',
    config TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    last_status TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    venue TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    start_at TEXT NOT NULL,          -- local wall-clock ISO, see models.to_local_iso
    end_at TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    price TEXT NOT NULL DEFAULT '',
    image_url TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_id, external_id)
);
CREATE INDEX IF NOT EXISTS events_start ON events(start_at);
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS preferences (
    id INTEGER PRIMARY KEY,
    member_id INTEGER REFERENCES members(id) ON DELETE CASCADE,  -- NULL = whole family
    sentiment TEXT NOT NULL,          -- like | dislike | constraint
    subject TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    strength INTEGER NOT NULL DEFAULT 2,  -- 1 mild, 2 normal, 3 strong
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS extract_cache (
    key TEXT PRIMARY KEY,             -- hash of the page text Claude read
    result TEXT NOT NULL,             -- Claude's extracted events, as JSON
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    member_id INTEGER REFERENCES members(id) ON DELETE CASCADE,
    reaction TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""

REACTIONS = {
    # reaction -> weight used by the scorer
    "interested": 1.0,
    "going": 1.5,
    "loved_it": 2.0,
    "liked_it": 1.0,
    "not_interested": -1.0,
    "disliked_it": -1.5,
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path, tz: ZoneInfo) -> None:
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.tz = tz
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ---- sources -------------------------------------------------------------

    def add_source(
        self, name: str, kind: str, url: str = "", config: dict[str, Any] | None = None
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO sources (name, kind, url, config, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, kind, url, json.dumps(config or {}), _now()),
        )
        self.db.commit()
        return int(cur.lastrowid or 0)

    def list_sources(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = (
            "SELECT * FROM sources"
            + (" WHERE enabled = 1" if enabled_only else "")
            + " ORDER BY id"
        )
        rows = []
        for r in self.db.execute(sql):
            row = dict(r)
            row["config"] = json.loads(row["config"])
            row["event_count"] = self.db.execute(
                "SELECT COUNT(*) FROM events WHERE source_id = ?", (row["id"],)
            ).fetchone()[0]
            rows.append(row)
        return rows

    def get_source(self, source_id: int) -> dict[str, Any] | None:
        return next((s for s in self.list_sources() if s["id"] == source_id), None)

    def set_source_enabled(self, source_id: int, enabled: bool) -> bool:
        cur = self.db.execute(
            "UPDATE sources SET enabled = ? WHERE id = ?", (int(enabled), source_id)
        )
        self.db.commit()
        return cur.rowcount > 0

    def remove_source(self, source_id: int) -> bool:
        cur = self.db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        self.db.commit()
        return cur.rowcount > 0

    def mark_source_run(self, source_id: int, status: str) -> None:
        self.db.execute(
            "UPDATE sources SET last_run_at = ?, last_status = ? WHERE id = ?",
            (_now(), status, source_id),
        )
        self.db.commit()

    # ---- events --------------------------------------------------------------

    def upsert_events(self, source_id: int, events: Iterable[Event]) -> tuple[int, int]:
        """Insert or update events. Returns (new, updated)."""
        new = updated = 0
        now = _now()
        for ev in events:
            if not ev.title.strip():
                continue
            values = {
                "title": ev.title.strip()[:300],
                "category": normalize_category(ev.category),
                "description": ev.description.strip()[:4000],
                "venue": ev.venue.strip(),
                "address": ev.address.strip(),
                "start_at": to_local_iso(ev.start, self.tz),
                "end_at": to_local_iso(ev.end, self.tz),
                "url": ev.url,
                "price": ev.price,
                "image_url": ev.image_url,
                "tags": ",".join(t.strip().lower() for t in ev.tags if t.strip()),
            }
            key = ev.key()
            existing = self.db.execute(
                "SELECT id FROM events WHERE source_id = ? AND external_id = ?", (source_id, key)
            ).fetchone()
            if existing:
                sets = ", ".join(f"{k} = :{k}" for k in values)
                self.db.execute(
                    f"UPDATE events SET {sets}, updated_at = :now WHERE id = :id",
                    {**values, "now": now, "id": existing["id"]},
                )
                updated += 1
            else:
                cols = ", ".join(values)
                params = ", ".join(f":{k}" for k in values)
                self.db.execute(
                    f"INSERT INTO events (source_id, external_id, {cols},"
                    " first_seen_at, updated_at)"
                    f" VALUES (:source_id, :key, {params}, :now, :now)",
                    {**values, "source_id": source_id, "key": key, "now": now},
                )
                new += 1
        self.db.commit()
        return new, updated

    def find_events(
        self,
        start: str,
        end: str,
        categories: list[str] | None = None,
        text: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Events starting in [start, end] (local ISO dates or datetimes)."""
        sql = [
            "SELECT e.*, s.name AS source_name FROM events e JOIN sources s ON s.id = e.source_id",
            # Starts in the window, or started earlier and is still running.
            "WHERE s.enabled = 1 AND (e.start_at >= ? OR e.end_at >= ?) AND e.start_at <= ?",
        ]
        args: list[Any] = [start, start, end + "T23:59" if len(end) == 10 else end]
        if categories:
            sql.append(f"AND e.category IN ({','.join('?' * len(categories))})")
            args.extend(categories)
        if text:
            for word in text.split():
                sql.append(
                    "AND (e.title LIKE ? OR e.description LIKE ?"
                    " OR e.venue LIKE ? OR e.tags LIKE ?)"
                )
                args.extend([f"%{word}%"] * 4)
        sql.append("ORDER BY e.start_at LIMIT ?")
        args.append(limit)
        return [dict(r) for r in self.db.execute(" ".join(sql), args)]

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT e.*, s.name AS source_name FROM events e JOIN sources s ON s.id = e.source_id"
            " WHERE e.id = ?",
            (event_id,),
        ).fetchone()
        return dict(row) if row else None

    def prune_past_events(self, before: str) -> int:
        """Delete past events that nobody reacted to."""
        cur = self.db.execute(
            "DELETE FROM events WHERE start_at < ? AND (end_at = '' OR end_at < ?)"
            " AND id NOT IN (SELECT event_id FROM feedback)",
            (before, before),
        )
        self.db.commit()
        return cur.rowcount

    # ---- Claude extraction cache ----------------------------------------------

    def get_extraction(self, key: str) -> str | None:
        row = self.db.execute("SELECT result FROM extract_cache WHERE key = ?", (key,)).fetchone()
        return str(row["result"]) if row else None

    def put_extraction(self, key: str, result: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO extract_cache (key, result, created_at) VALUES (?, ?, ?)",
            (key, result, _now()),
        )
        # Entries for pages that changed are never read again; drop them after a while.
        self.db.execute("DELETE FROM extract_cache WHERE created_at < datetime('now', '-60 days')")
        self.db.commit()

    # ---- family & preferences ------------------------------------------------

    def upsert_member(self, name: str, notes: str = "") -> int:
        row = self.db.execute("SELECT id FROM members WHERE name = ?", (name,)).fetchone()
        if row:
            if notes:
                self.db.execute("UPDATE members SET notes = ? WHERE id = ?", (notes, row["id"]))
                self.db.commit()
            return int(row["id"])
        cur = self.db.execute("INSERT INTO members (name, notes) VALUES (?, ?)", (name, notes))
        self.db.commit()
        return int(cur.lastrowid or 0)

    def member_id(self, name: str | None) -> int | None:
        """Look up a member by name, creating them if new. None/'family' means everyone."""
        if not name or name.strip().lower() in {"family", "everyone", "all", "we", "us"}:
            return None
        return self.upsert_member(name.strip())

    def list_members(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM members ORDER BY id")]

    def add_preference(
        self, member: str | None, sentiment: str, subject: str, detail: str = "", strength: int = 2
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO preferences (member_id, sentiment, subject, detail, strength, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (self.member_id(member), sentiment, subject.strip(), detail, strength, _now()),
        )
        self.db.commit()
        return int(cur.lastrowid or 0)

    def remove_preference(self, pref_id: int) -> bool:
        cur = self.db.execute("DELETE FROM preferences WHERE id = ?", (pref_id,))
        self.db.commit()
        return cur.rowcount > 0

    def list_preferences(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT p.*, COALESCE(m.name, 'family') AS member FROM preferences p"
                " LEFT JOIN members m ON m.id = p.member_id ORDER BY p.id"
            )
        ]

    def add_feedback(
        self, event_id: int, reaction: str, member: str | None, comment: str = ""
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO feedback (event_id, member_id, reaction, comment, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (event_id, self.member_id(member), reaction, comment, _now()),
        )
        self.db.commit()
        return int(cur.lastrowid or 0)

    def list_feedback(self, limit: int = 200) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT f.*, COALESCE(m.name, 'family') AS member, e.title, e.category, e.venue,"
                " e.tags, e.start_at FROM feedback f JOIN events e ON e.id = f.event_id"
                " LEFT JOIN members m ON m.id = f.member_id ORDER BY f.id DESC LIMIT ?",
                (limit,),
            )
        ]
