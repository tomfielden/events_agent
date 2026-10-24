"""Ticketmaster Discovery API: concerts, theater, sports, family shows.

Free key: https://developer.ticketmaster.com/ (set TICKETMASTER_API_KEY).
Optional source config: {"keyword": "...", "classificationName": "music", "radius": 25}
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any

from dateutil import parser as dateparser

from events_agent.models import Event
from events_agent.sources import FetchContext

API = "https://app.ticketmaster.com/discovery/v2/events.json"
PAGE_SIZE = 200
MAX_PAGES = 5  # the API refuses to page past 1000 results

SEGMENT_TO_CATEGORY = {
    "music": "music",
    "sports": "sports",
    "arts & theatre": "theater",
    "film": "movie",
    "miscellaneous": "other",
}


def fetch(source: dict[str, Any], ctx: FetchContext) -> list[Event]:
    key = ctx.settings.ticketmaster_api_key
    if not key:
        raise RuntimeError("TICKETMASTER_API_KEY is not set")
    loc = ctx.settings.location
    cfg = source["config"]
    start, end = ctx.window
    params: dict[str, Any] = {
        "apikey": key,
        "latlong": f"{loc.latitude},{loc.longitude}",
        "radius": cfg.get("radius", loc.radius_miles),
        "unit": "miles",
        "startDateTime": f"{start.isoformat()}T00:00:00Z",
        "endDateTime": f"{end.isoformat()}T23:59:59Z",
        "size": PAGE_SIZE,
        "sort": "date,asc",
    }
    for opt in ("keyword", "classificationName", "segmentName"):
        if cfg.get(opt):
            params[opt] = cfg[opt]

    events: list[Event] = []
    for page in range(MAX_PAGES):
        resp = ctx.http.get(API, params={**params, "page": page})
        resp.raise_for_status()
        data = resp.json()
        events.extend(_to_event(item) for item in data.get("_embedded", {}).get("events", []))
        if page + 1 >= data.get("page", {}).get("totalPages", 0):
            break
    return events


def _to_event(item: dict[str, Any]) -> Event:
    dates = item.get("dates", {}).get("start", {})
    start: datetime | Any
    if dates.get("dateTime"):
        start = dateparser.isoparse(dates["dateTime"])
    elif dates.get("localDate") and dates.get("localTime"):
        start = datetime.combine(
            dateparser.isoparse(dates["localDate"]).date(), time.fromisoformat(dates["localTime"])
        )
    else:
        start = dateparser.isoparse(dates.get("localDate", "1970-01-01")).date()

    cls = (item.get("classifications") or [{}])[0]
    segment = (cls.get("segment") or {}).get("name", "")
    tags = [
        name
        for part in ("genre", "subGenre", "type", "subType")
        if (name := (cls.get(part) or {}).get("name")) and name != "Undefined"
    ]
    category = SEGMENT_TO_CATEGORY.get(segment.lower(), "other")
    if cls.get("family") or any(t.lower() in {"family", "children's theatre"} for t in tags):
        tags.append("family-friendly")

    venue = ((item.get("_embedded") or {}).get("venues") or [{}])[0]
    address = ", ".join(
        p
        for p in (
            (venue.get("address") or {}).get("line1", ""),
            (venue.get("city") or {}).get("name", ""),
        )
        if p
    )
    price = ""
    if item.get("priceRanges"):
        pr = item["priceRanges"][0]
        lo, hi = pr.get("min"), pr.get("max")
        price = f"${lo:.0f}" + (f"-${hi:.0f}" if hi and hi != lo else "") if lo is not None else ""
    images = sorted(item.get("images") or [], key=lambda i: i.get("width", 0), reverse=True)

    attractions = [a.get("name", "") for a in (item.get("_embedded") or {}).get("attractions", [])]
    description = " ".join(p for p in (item.get("info", ""), item.get("pleaseNote", "")) if p) or (
        "Featuring: " + ", ".join(attractions) if attractions else ""
    )

    return Event(
        title=item.get("name", ""),
        start=start,
        category=category,
        description=description,
        venue=venue.get("name", ""),
        address=address,
        url=item.get("url", ""),
        price=price,
        image_url=images[0]["url"] if images else "",
        tags=[*tags, *attractions],
        external_id=item.get("id", ""),
    )
