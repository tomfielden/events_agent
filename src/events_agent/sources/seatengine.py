"""Comedy clubs whose sites run on SeatEngine (Helium and many others).

The club's /events page lists every upcoming show with its date. Each show's own page
carries schema.org Event data with one entry per showtime, which the generic JSON-LD
parser reads. Only shows inside the look-ahead window are opened.

Source url: the club's site, e.g. https://portland.heliumcomedy.com
Optional config: {"venue": "Helium Comedy Club", "category": "comedy"}
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from events_agent.models import Event, to_local_iso
from events_agent.sources import FetchContext
from events_agent.sources.webpage import allowed_by_robots, parse_jsonld

PAUSE_SECONDS = 0.3  # one show page at a time


def fetch(source: dict[str, Any], ctx: FetchContext) -> list[Event]:
    parts = urlparse(source["url"])
    base = f"{parts.scheme}://{parts.netloc}"
    listing = f"{base}/events"
    cfg = source["config"]
    resp = ctx.http.get(listing)
    resp.raise_for_status()

    start, end = ctx.window
    today = datetime.now(ctx.tz).date()
    show_pages: list[str] = []
    for item in BeautifulSoup(resp.text, "html.parser").select(".event-list-item"):
        link = item.select_one(".el-header a[href]")
        when = item.select_one(".el-date-range")
        if link is None:
            continue
        first_day = _first_day(when.get_text(" ", strip=True) if when else "", today)
        if first_day is not None and first_day > end:
            continue
        show_pages.append(urljoin(base, str(link["href"])))

    events: list[Event] = []
    for url in dict.fromkeys(show_pages):
        if not allowed_by_robots(url, ctx):
            continue
        page = ctx.http.get(url)
        if page.status_code >= 400:
            ctx.notes.append(f"{url} returned {page.status_code}")
            continue
        for ev in parse_jsonld(page.text, url):
            ev.category = cfg.get("category", "comedy")
            if cfg.get("venue"):
                ev.venue = cfg["venue"]
            ev.external_id = f"{ev.url}|{to_local_iso(ev.start, ctx.tz)}"
            ev.tags.append("comedy")
            events.append(ev)
        time.sleep(PAUSE_SECONDS)

    def day(value: datetime | date) -> date:
        return value.astimezone(ctx.tz).date() if isinstance(value, datetime) else value

    return [ev for ev in events if start <= day(ev.start) <= end]


def _first_day(text: str, today: date) -> date | None:
    """'October 24' or 'May 13 - May 16' -> the first date, in the next year if needed."""
    first = text.split("-")[0].strip()
    try:
        guess = dateparser.parse(first, default=datetime(today.year, 1, 1)).date()
    except (ValueError, OverflowError):
        return None
    return guess.replace(year=today.year + 1) if (today - guess).days > 60 else guess
