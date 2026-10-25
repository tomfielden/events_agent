"""Plain data types shared by sources, the store and the agent."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

CATEGORIES = (
    "movie",
    "music",
    "theater",
    "comedy",
    "family",
    "sports",
    "arts",
    "festival",
    "food",
    "talk",
    "outdoors",
    "other",
)


@dataclass
class Event:
    """One event as a source produces it, before it is stored."""

    title: str
    start: datetime | date
    category: str = "other"
    description: str = ""
    venue: str = ""
    address: str = ""
    end: datetime | date | None = None
    url: str = ""
    price: str = ""
    image_url: str = ""
    tags: list[str] = field(default_factory=list)
    external_id: str = ""

    def key(self) -> str:
        """Stable id within a source: the source's own id, else a hash of the basics."""
        if self.external_id:
            return self.external_id
        basis = (
            f"{self.title.strip().lower()}|{to_local_iso(self.start, None)}|{self.venue.lower()}"
        )
        return hashlib.sha1(basis.encode()).hexdigest()[:16]


def to_local_iso(value: datetime | date | None, tz: ZoneInfo | None) -> str:
    """Store times as local wall-clock ISO strings so they sort and compare as text.

    Dates without a time become "YYYY-MM-DD", which still sorts correctly against
    "YYYY-MM-DDTHH:MM" values.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is not None and tz is not None:
            value = value.astimezone(tz)
        return value.replace(tzinfo=None, second=0, microsecond=0).isoformat(timespec="minutes")
    return value.isoformat()


def normalize_category(value: str) -> str:
    value = (value or "").strip().lower()
    aliases = {
        "film": "movie",
        "films": "movie",
        "movies": "movie",
        "cinema": "movie",
        "concert": "music",
        "concerts": "music",
        "arts & theatre": "theater",
        "theatre": "theater",
        "musical": "theater",
        "dance": "theater",
        "drag": "theater",
        "burlesque": "theater",
        "cabaret": "theater",
        "variety": "theater",
        "variety show": "theater",
        "improv": "comedy",
        "stand up": "comedy",
        "standup": "comedy",
        "kids": "family",
        "children": "family",
        "sport": "sports",
        "museum": "arts",
        "exhibit": "arts",
        "exhibition": "arts",
        "lecture": "talk",
        "talks": "talk",
        "food & drink": "food",
    }
    value = aliases.get(value, value)
    return value if value in CATEGORIES else "other"
