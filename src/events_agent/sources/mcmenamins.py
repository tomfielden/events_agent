"""McMenamins theaters: second-run movies, classics and special screenings.

Each theater's page on mcmenamins.com lists about a week of showtimes plus special
screenings further out. We read those pages directly (their movie calendar only shows one
day at a time, and robots.txt asks bots to stay off the detail and .ics endpoints).

Source config (optional): {"theaters": ["kennedy-school/kennedy-school-theater", ...]}
Default: the Portland-area theaters below.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from dateutil import parser as dateparser

from events_agent.models import Event
from events_agent.sources import FetchContext
from events_agent.sources.webpage import allowed_by_robots

BASE = "https://www.mcmenamins.com"

# Theater page path -> where it is, for people who don't know the venues.
PORTLAND_THEATERS = {
    "bagdad-theater-pub": "Hawthorne, SE Portland",
    "kennedy-school/kennedy-school-theater": "Concordia, NE Portland",
    "mission-theater": "Northwest Portland",
    "st-johns-theater-pub": "St. Johns, North Portland",
    "edgefield/power-station-theater": "Edgefield, Troutdale",
    "grand-lodge/grand-lodge-theater": "Grand Lodge, Forest Grove",
}

TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE)
DATE_ARG_RE = re.compile(r"'(\d{1,2}/\d{1,2}/\d{4})'")
RATING_RE = re.compile(r"\((G|PG|PG-13|R|NC-17|NR|Not Rated)\)", re.IGNORECASE)


def fetch(source: dict[str, Any], ctx: FetchContext) -> list[Event]:
    paths = source["config"].get("theaters") or list(PORTLAND_THEATERS)
    events: dict[str, Event] = {}
    for path in paths:
        url = f"{BASE}/{path.strip('/')}"
        if not allowed_by_robots(url, ctx):
            ctx.notes.append(f"robots.txt disallows {url}")
            continue
        resp = ctx.http.get(url)
        resp.raise_for_status()
        for ev in parse(resp.text, url, PORTLAND_THEATERS.get(path.strip("/"), ""), ctx):
            events.setdefault(ev.key(), ev)  # cards repeat across theater pages
    start, end = ctx.window
    return [ev for ev in events.values() if start <= _day(ev.start) <= end]


def _day(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


def _parse_time(text: str) -> time | None:
    m = TIME_RE.search(text)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)) % 12, int(m.group(2) or 0), m.group(3).lower()
    return time(hour + (12 if ampm == "pm" else 0), minute)


def _card_date(card: Tag, date_text: str, today: date) -> date | None:
    """The page shows 'Saturday, October 24'; the year is in the card's JS links."""
    for a in card.find_all("a", href=True):
        m = DATE_ARG_RE.search(str(a["href"]))
        if m:
            return datetime.strptime(m.group(1), "%m/%d/%Y").date()
    try:
        guess = dateparser.parse(date_text, default=datetime(today.year, 1, 1)).date()
    except (ValueError, OverflowError):
        return None
    # No year given: a date well in the past means next year.
    return guess.replace(year=today.year + 1) if (today - guess).days > 60 else guess


def parse(html: str, page_url: str, area: str, ctx: FetchContext) -> list[Event]:
    soup = BeautifulSoup(html, "html.parser")
    today = datetime.now(ctx.tz).date()
    events: list[Event] = []
    for card in soup.select(".event.movie"):
        title_el = card.select_one(".tm-panel-titlebg h3") or card.select_one("h3")
        date_el = card.select_one(".tm-card-content h3")
        venue_el = card.select_one("h4 span") or card.select_one("h4")
        if not (title_el and date_el and venue_el):
            continue
        title = title_el.get_text(" ", strip=True)
        venue = venue_el.get_text(" ", strip=True).removeprefix("at ").strip()
        if venue == "All Locations":  # chain-wide promos, not things to attend
            continue
        day = _card_date(card, date_el.get_text(" ", strip=True), today)
        if day is None:
            continue

        info_el = card.select_one(".uk-panel-time")
        info = info_el.get_text(" ", strip=True) if info_el else ""
        showtimes = [
            (a.get_text(strip=True), str(a["href"]))
            for a in card.select(".uk-panel-movietimes a[href]")
        ]
        first = _parse_time(showtimes[0][0]) if showtimes else _parse_time(info)
        start: datetime | date = datetime.combine(day, first, ctx.tz) if first else day

        event_link = next(
            (str(a["href"]) for a in card.find_all("a", href=True) if "/events/" in a["href"]),
            "",
        )
        is_movie = "running time" in info.lower() or bool(
            re.search(r"\b(movie|screening|film)\b", f"{title} {info}", re.IGNORECASE)
        )
        tags = ["mcmenamins"]
        if rating := RATING_RE.search(info):
            tags.append(f"rated {rating.group(1).upper()}")
            if rating.group(1).upper() in {"G", "PG"}:
                tags.append("family-friendly")
        if "tour" in title.lower():
            tags.append("tour")

        details = []
        if showtimes:
            details.append("Showtimes: " + ", ".join(t for t, _ in showtimes) + ".")
        if info:
            details.append(info)
        events.append(
            Event(
                title=title,
                start=start,
                category="movie" if is_movie else "other",
                description=" ".join(details),
                venue=venue,
                address=area,
                url=showtimes[0][1] if showtimes else urljoin(page_url, event_link or page_url),
                tags=tags,
                external_id=f"mcm|{venue}|{title}|{day.isoformat()}".lower(),
            )
        )
    return events
