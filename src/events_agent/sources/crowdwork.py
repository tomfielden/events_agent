"""Comedy theaters that sell tickets through Crowdwork (Kickstand Comedy and others).

Crowdwork's website sits behind a bot check, but it also publishes each theater's show
list as JSON for the ticket widget venues embed on their own sites. We read that feed.

Config: {"theatre": "kickstandcomedy", "venue": "Kickstand Comedy"}
The theatre slug is the data-theatre value in the venue's embed code.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from events_agent.models import Event
from events_agent.sources import FetchContext

API = "https://crowdwork.com/api/v2/{theatre}/shows"


def fetch(source: dict[str, Any], ctx: FetchContext) -> list[Event]:
    cfg = source["config"]
    theatre = cfg.get("theatre")
    if not theatre:
        raise RuntimeError('crowdwork sources need config {"theatre": "<slug>"}')
    resp = ctx.http.get(API.format(theatre=theatre), headers={"Accept": "application/json"})
    resp.raise_for_status()
    shows = resp.json().get("data") or []
    return [ev for show in shows for ev in to_events(show, cfg, ctx)]


def _price(show: dict[str, Any]) -> str:
    tiers = [t.get("cost") for t in show.get("cost_tiers") or [] if t.get("cost") is not None]
    if tiers:
        # A $0 tier alongside paid ones is usually comps or volunteer seats.
        paid = [t / 100 for t in tiers if t > 0]
        if not paid:
            return "Free"
        lo, hi = min(paid), max(paid)
        return f"${lo:.0f}" + (f"-${hi:.0f}" if hi != lo else "")
    formatted = (show.get("cost") or {}).get("formatted", "")
    return "" if formatted == "Multiple prices" else formatted


def to_events(show: dict[str, Any], cfg: dict[str, Any], ctx: FetchContext) -> list[Event]:
    start, end = ctx.window
    venue = show.get("venue") or cfg.get("venue") or (show.get("theatre") or {}).get("name", "")
    description = show.get("description_short") or BeautifulSoup(
        (show.get("description") or {}).get("body", ""), "html.parser"
    ).get_text(" ", strip=True)
    tags = list((show.get("tags") or {}).get("public") or [])
    recurring = (show.get("recurring") or "").lower()
    if recurring in ("daily", "weekly", "biweekly", "monthly"):
        tags.append(f"{recurring} show")
    if any(t.lower() in {"jam", "jams", "open mic"} for t in tags):
        tags.append("open to participate")
    tags.append("comedy")
    images = show.get("img") or {}

    events: list[Event] = []
    for raw in show.get("dates") or []:
        try:
            when: datetime = dateparser.isoparse(raw)
        except ValueError:
            continue
        if not start <= when.astimezone(ctx.tz).date() <= end:
            continue
        events.append(
            Event(
                title=show.get("name", "").strip(),
                start=when,
                category="comedy",
                description=description.replace("\xa0", " ").strip(),
                venue=str(venue).strip(),
                url=show.get("url", ""),
                price=_price(show),
                image_url=images.get("large") or images.get("url") or "",
                tags=tags,
                external_id=f"crowdwork-{show.get('id')}|{raw}",
            )
        )
    return events
