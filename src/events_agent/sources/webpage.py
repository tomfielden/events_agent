"""Any web page that lists events.

1. Look for schema.org Event data, which most ticketing and venue sites include for
   search engines: JSON-LD first, then microdata (itemprop attributes). Free and exact.
2. If there is none, send the page's text to Claude and ask it to pull out the events.

Source config (all optional):
  {"mode": "auto" | "jsonld" | "llm",
   "category": "family",          # default category for this site
   "venue": "The Fillmore",        # default venue for this site
   "times_are_local": true,        # site labels local times as UTC (+00:00); fix that
   "instructions": "Only the kids' programs"}   # extra hint for Claude
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from pydantic import BaseModel, Field

from events_agent.models import CATEGORIES, Event
from events_agent.sources import USER_AGENT, FetchContext

# About 40k tokens of page text. Long listing pages are cut here; the source's status
# line says so when it happens.
MAX_PAGE_CHARS = 160_000
# Bump when the extraction prompt changes so cached results are redone.
EXTRACT_VERSION = 2


def fetch(source: dict[str, Any], ctx: FetchContext) -> list[Event]:
    url = source["url"]
    if not allowed_by_robots(url, ctx):
        raise RuntimeError("robots.txt asks automated tools not to fetch this page")
    resp = ctx.http.get(url)
    resp.raise_for_status()
    html = resp.text
    cfg = source["config"]
    mode = cfg.get("mode", "auto")

    events: list[Event] = []
    if mode in ("auto", "jsonld"):
        events = parse_jsonld(html, url) or parse_microdata(html, url)
    if not events and mode in ("auto", "llm"):
        events = extract_with_claude(html, url, cfg, ctx)

    start, end = ctx.window
    for ev in events:
        if cfg.get("times_are_local"):
            ev.start = _as_local(ev.start, ctx)
            ev.end = _as_local(ev.end, ctx) if ev.end else None
        if cfg.get("category") and ev.category == "other":
            ev.category = cfg["category"]
        if cfg.get("venue") and not ev.venue:
            ev.venue = cfg["venue"]
    return [ev for ev in events if start <= _day(ev.end or ev.start) and _day(ev.start) <= end]


def _as_local(value: datetime | date, ctx: FetchContext) -> datetime | date:
    """Keep the wall-clock time but treat it as local (for sites that mislabel the zone)."""
    return value.replace(tzinfo=ctx.tz) if isinstance(value, datetime) else value


def _day(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


def allowed_by_robots(url: str, ctx: FetchContext) -> bool:
    parts = urlparse(url)
    robots = RobotFileParser()
    try:
        resp = ctx.http.get(f"{parts.scheme}://{parts.netloc}/robots.txt")
    except Exception:
        return True
    if resp.status_code >= 400:
        return True
    robots.parse(resp.text.splitlines())
    return robots.can_fetch(USER_AGENT, url)


# ---- JSON-LD -----------------------------------------------------------------


def parse_jsonld(html: str, base_url: str) -> list[Event]:
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in _walk(data):
            types = obj.get("@type", [])
            types = types if isinstance(types, list) else [types]
            if any(isinstance(t, str) and t.endswith("Event") for t in types):
                ev = _jsonld_event(obj, base_url, types)
                if ev is not None:
                    events.append(ev)
    return events


def _walk(data: Any) -> list[dict[str, Any]]:
    """Every dict in a JSON-LD blob (handles @graph, lists, and nested itemListElement)."""
    found: list[dict[str, Any]] = []
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            found.append(item)
            stack.extend(v for v in item.values() if isinstance(v, dict | list))
    return found


def _text(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("name") or value.get("url") or value.get("@id") or ""
    return str(value or "").strip()


def parse_when(value: str) -> datetime | date:
    """ISO date or date-time. A bare date stays a date so we don't invent a midnight start."""
    value = value.strip()
    parsed = dateparser.isoparse(value)
    return parsed.date() if len(value) == 10 else parsed


def _jsonld_event(obj: dict[str, Any], base_url: str, types: list[str]) -> Event | None:
    try:
        start = parse_when(_text(obj.get("startDate")))
    except (ValueError, TypeError):
        return None
    end = None
    try:
        if obj.get("endDate"):
            end = parse_when(_text(obj["endDate"]))
    except (ValueError, TypeError):
        pass

    location = obj.get("location") or {}
    if isinstance(location, list):
        location = location[0] if location else {}
    venue, address = "", ""
    if isinstance(location, dict):
        venue = _text(location.get("name"))
        addr = location.get("address")
        if isinstance(addr, dict):
            address = ", ".join(
                str(addr[k]) for k in ("streetAddress", "addressLocality") if addr.get(k)
            )
        else:
            address = _text(addr)
    else:
        venue = _text(location)

    offers = obj.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = ""
    if isinstance(offers, dict):
        amount = offers.get("price") or offers.get("lowPrice")
        if amount not in (None, ""):
            price = "Free" if str(amount) in {"0", "0.0", "0.00"} else f"${amount}"

    type_name = next((t for t in types if t != "Event"), "Event")
    category = {
        "MusicEvent": "music",
        "TheaterEvent": "theater",
        "ComedyEvent": "comedy",
        "ChildrensEvent": "family",
        "SportsEvent": "sports",
        "ScreeningEvent": "movie",
        "Festival": "festival",
        "FoodEvent": "food",
        "ExhibitionEvent": "arts",
        "VisualArtsEvent": "arts",
        "DanceEvent": "theater",
        "LiteraryEvent": "talk",
        "EducationEvent": "talk",
    }.get(type_name, "other")

    performers = obj.get("performer") or []
    performers = performers if isinstance(performers, list) else [performers]
    url = _text(obj.get("url"))
    description = BeautifulSoup(_text(obj.get("description")), "html.parser").get_text(" ").strip()
    return Event(
        title=_text(obj.get("name")),
        start=start,
        end=end,
        category=category,
        description=description,
        venue=venue,
        address=address,
        url=urljoin(base_url, url) if url else base_url,
        price=price,
        image_url=_text(obj.get("image")),
        tags=[t for t in (_text(p) for p in performers) if t],
    )


# ---- microdata -----------------------------------------------------------------


def parse_microdata(html: str, base_url: str) -> list[Event]:
    """Events marked up with itemscope/itemtype="http://schema.org/...Event" attributes."""
    soup = BeautifulSoup(html, "html.parser")
    events: list[Event] = []
    seen: set[tuple[str, str]] = set()
    for el in soup.select("[itemscope][itemtype]"):
        if not str(el["itemtype"]).rstrip("/").endswith("Event"):
            continue

        def prop(name: str, scope: Any = el) -> Any:
            return scope.find(attrs={"itemprop": name})

        start_el, name_el = prop("startDate"), prop("name")
        if start_el is None or name_el is None:
            continue
        try:
            start = parse_when(
                str(
                    start_el.get("content")
                    or start_el.get("datetime")
                    or start_el.get_text(strip=True)
                )
            )
        except (ValueError, TypeError):
            continue
        title = name_el.get("content") or name_el.get_text(" ", strip=True)
        if (title, str(start)) in seen:  # feeds often repeat a card in two layouts
            continue
        seen.add((title, str(start)))

        location = prop("location")
        if location is not None:
            loc_name = prop("name", location)
            venue = (loc_name or location).get_text(" ", strip=True)
        else:
            # No location markup: fall back to an element whose class mentions "venue".
            venue_el = el.find(class_=lambda c: bool(c) and "venue" in str(c))
            venue = venue_el.get_text(" ", strip=True) if venue_el else ""
        url_el = prop("url")
        link = url_el.get("href") or url_el.get("content") if url_el is not None else None
        if not link:
            a = el.find("a", href=True)
            link = a["href"] if a else ""
        desc_el = prop("description")
        events.append(
            Event(
                title=str(title),
                start=start,
                venue=venue.strip(" -\u2013\u2014"),  # "- Crystal Ballroom -"
                description=desc_el.get_text(" ", strip=True) if desc_el is not None else "",
                url=urljoin(base_url, str(link)) if link else base_url,
            )
        )
    return events


# ---- Claude extraction ---------------------------------------------------------


class ExtractedEvent(BaseModel):
    title: str
    start: str = Field(description="ISO 8601 local date or date-time, e.g. 2026-11-07T19:30")
    end: str | None = Field(default=None, description="ISO 8601 end, if the page gives one")
    category: str = Field(description="One of: " + ", ".join(CATEGORIES))
    venue: str = ""
    address: str = ""
    description: str = Field(default="", description="One or two sentences from the page")
    url: str = Field(default="", description="Link to the event's own page, if any")
    price: str = ""
    tags: list[str] = Field(default_factory=list, description="Performers, genres, audience")


class ExtractedEvents(BaseModel):
    events: list[ExtractedEvent]


def page_text(html: str, base_url: str) -> str:
    """Readable text of a page, with links kept inline so Claude can return event URLs."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "header", "footer", "nav"]):
        tag.decompose()
    for a in soup.find_all("a", href=True):
        href = str(a.get("href", ""))
        if href and not href.startswith(("#", "javascript:", "mailto:")):
            a.append(f" <{urljoin(base_url, href)}>")
    lines = (line.strip() for line in soup.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


def extract_with_claude(html: str, url: str, cfg: dict[str, Any], ctx: FetchContext) -> list[Event]:
    text = page_text(html, url)
    if len(text) > MAX_PAGE_CHARS:
        ctx.notes.append(f"page text was cut at {MAX_PAGE_CHARS:,} characters; later events missed")
    return extract_from_text(text[:MAX_PAGE_CHARS], url, cfg, ctx)


def extract_from_text(text: str, url: str, cfg: dict[str, Any], ctx: FetchContext) -> list[Event]:
    """Ask Claude to list the events in some page text.

    Results are cached by the text itself, so an unchanged page costs nothing on the next
    refresh.
    """
    cache_key = hashlib.sha256(
        json.dumps([EXTRACT_VERSION, url, cfg.get("instructions", ""), text]).encode()
    ).hexdigest()
    cached = ctx.store.get_extraction(cache_key) if ctx.store else None
    if cached is not None:
        parsed = ExtractedEvents.model_validate_json(cached)
    else:
        parsed = _call_claude(text, url, cfg, ctx)
        if ctx.store:
            ctx.store.put_extraction(cache_key, parsed.model_dump_json())
    return [ev for item in parsed.events if (ev := _to_event(item, url)) is not None]


def _call_claude(text: str, url: str, cfg: dict[str, Any], ctx: FetchContext) -> ExtractedEvents:
    from events_agent.llm import make_client

    today = datetime.now(ctx.tz).date()
    loc = ctx.settings.location
    hint = (
        f"\nExtra instructions for this site: {cfg['instructions']}"
        if cfg.get("instructions")
        else ""
    )
    prompt = (
        f"Below is the text of the web page {url}. List every upcoming event on it that a "
        f"person could attend in or near {loc.city}, {loc.state}. Today is {today.isoformat()}; "
        "when the page omits the year, pick the next occurrence on or after today. Give each "
        "date-time in local time. Skip ads, navigation, past events and anything that is not "
        "a specific dated event. If an event happens on a few separate dates, list each date "
        "as its own event (at most 10). If it runs daily or continuously over a period (a "
        "film's run, an exhibit), return one event whose start is the first day (with the "
        "time if given) and whose end is the last day, and put the schedule in the "
        "description. If there are no dated events, return an empty list."
        f"{hint}\n\n<page>\n{text}\n</page>"
    )
    client = make_client(ctx.settings)
    response = client.messages.parse(
        model=ctx.settings.model,
        max_tokens=16000,
        output_config={"effort": "low"},
        output_format=ExtractedEvents,
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to extract events from this page")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("page has more events than fit in one response; add narrower URLs")
    return response.parsed_output or ExtractedEvents(events=[])


def _to_event(item: ExtractedEvent, page_url: str) -> Event | None:
    try:
        start = parse_when(item.start)
        end = parse_when(item.end) if item.end else None
    except (ValueError, TypeError):
        return None
    return Event(
        title=item.title,
        start=start,
        end=end,
        category=item.category,
        venue=item.venue,
        address=item.address,
        description=item.description,
        url=urljoin(page_url, item.url) if item.url else page_url,
        price=item.price,
        tags=item.tags,
    )
