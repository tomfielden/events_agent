"""WordPress sites using The Events Calendar plugin (Clinton Street Theater and many
venues, libraries and museums).

The plugin publishes a public REST API at /wp-json/tribe/events/v1/events with date
filters and paging. Its iCal export is capped at about 30 events, so the API is better.
How to spot one: the page source mentions "tribe-events" and links to
/wp-json/tribe/events/v1/.

Source url: the site, e.g. https://cstpdx.com
Optional config: {"category": "movie", "venue": "Clinton Street Theater"}
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bs4 import BeautifulSoup

from events_agent.models import Event, normalize_category
from events_agent.sources import FetchContext
from events_agent.sources.webpage import allowed_by_robots

API_PATH = "/wp-json/tribe/events/v1/events"
MAX_PAGES = 10


def fetch(source: dict[str, Any], ctx: FetchContext) -> list[Event]:
    parts = urlparse(source["url"])
    url: str | None = f"{parts.scheme}://{parts.netloc}{API_PATH}"
    start, end = ctx.window
    params: dict[str, Any] | None = {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "per_page": 50,
    }
    if not allowed_by_robots(str(url), ctx):
        raise RuntimeError("robots.txt disallows the events API on this site")

    events: list[Event] = []
    for _ in range(MAX_PAGES):
        if not url:
            break
        resp = ctx.http.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        events.extend(to_event(item, source["config"], ctx) for item in data.get("events", []))
        url, params = data.get("next_rest_url"), None  # next_rest_url carries the filters
    return events


def _clean(text: str) -> str:
    text = BeautifulSoup(html.unescape(text or ""), "html.parser").get_text(" ", strip=True)
    return " ".join(text.replace("\xa0", " ").split())


def _when(value: str, tz: ZoneInfo, all_day: bool) -> datetime | Any:
    parsed = datetime.fromisoformat(value)
    return parsed.date() if all_day else parsed.replace(tzinfo=tz)


def to_event(item: dict[str, Any], cfg: dict[str, Any], ctx: FetchContext) -> Event:
    try:
        tz = ZoneInfo(item.get("timezone") or "")
    except (ZoneInfoNotFoundError, ValueError):
        tz = ctx.tz
    all_day = bool(item.get("all_day"))
    labels = [c.get("name", "") for c in item.get("categories") or []] + [
        t.get("name", "") for t in item.get("tags") or []
    ]
    category = next(
        (c for c in map(normalize_category, labels) if c != "other"),
        cfg.get("category", "other"),
    )
    venue = item.get("venue") or {}
    venue_name = (venue.get("venue") if isinstance(venue, dict) else "") or cfg.get("venue", "")
    address = (
        ", ".join(p for p in (venue.get("address", ""), venue.get("city", "")) if p)
        if isinstance(venue, dict)
        else ""
    )
    image = item.get("image") or {}
    return Event(
        title=_clean(item.get("title", "")),
        start=_when(item["start_date"], tz, all_day),
        end=_when(item["end_date"], tz, all_day) if item.get("end_date") else None,
        category=category,
        description=_clean(item.get("excerpt") or item.get("description") or "")[:600],
        venue=_clean(venue_name),
        address=address,
        # The event's own page; its ticket link (often on another site) is in the text.
        url=item.get("url", ""),
        price=_clean(item.get("cost", "")),
        image_url=image.get("url", "") if isinstance(image, dict) else "",
        tags=list(dict.fromkeys(_clean(t).lower() for t in labels if t)),
        external_id=f"tribe-{item.get('id')}",
    )
