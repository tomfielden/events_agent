"""iCalendar (.ics) feeds. Many venues, libraries and museums publish these.

Source config: {"category": "family", "venue": "Main Library"} (both optional defaults).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from icalendar import Calendar

from events_agent.models import Event
from events_agent.sources import FetchContext


def fetch(source: dict[str, Any], ctx: FetchContext) -> list[Event]:
    resp = ctx.http.get(source["url"])
    resp.raise_for_status()
    return parse(resp.content, source["config"], ctx)


def parse(data: bytes, cfg: dict[str, Any], ctx: FetchContext) -> list[Event]:
    cal = Calendar.from_ical(data)
    start_window, end_window = ctx.window
    events: list[Event] = []
    for comp in cal.walk("VEVENT"):
        dtstart = comp.get("DTSTART")
        if dtstart is None:
            continue
        start: datetime | date = dtstart.dt
        end_prop = comp.get("DTEND")
        end: datetime | date | None = end_prop.dt if end_prop is not None else None
        # Naive times in a feed are local to the venue, i.e. to us.
        if isinstance(start, datetime) and start.tzinfo is None:
            start = start.replace(tzinfo=ctx.tz)
        day = start.date() if isinstance(start, datetime) else start
        last_day = (end.date() if isinstance(end, datetime) else end) or day
        if last_day < start_window or day > end_window:
            continue
        categories = comp.get("CATEGORIES")
        tags: list[str] = []
        if categories is not None:
            for cat in categories if isinstance(categories, list) else [categories]:
                tags.extend(str(c) for c in cat.cats)
        events.append(
            Event(
                title=str(comp.get("SUMMARY", "")),
                start=start,
                end=end,
                category=cfg.get("category", tags[0] if tags else "other"),
                description=str(comp.get("DESCRIPTION", "")),
                venue=str(comp.get("LOCATION", "")) or cfg.get("venue", ""),
                url=str(comp.get("URL", "")),
                tags=tags,
                external_id=f"{comp.get('UID', '')}|{day.isoformat()}" if comp.get("UID") else "",
            )
        )
    return events
