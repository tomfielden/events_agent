"""OMSI (Oregon Museum of Science and Industry): films, planetarium shows and events.

The What's On page links to one page per event: Empirical Theater films, Kendall
Planetarium and laser shows, OMSI After Dark, Science Pub, Reel Science, kids' workshops.
Each page writes its dates in its own way ("Oct 24 - Nov 5", "Oct 29 + Nov 5 | 5:30 pm +
7 pm", "November | Daily | 4:30 pm"), so Claude reads the top of each page and returns
dated events. Results are cached, so only new or changed pages cost an API call.

Individual film showtimes live only in OMSI's ticketing system, which isn't public, so
film runs are stored as date ranges with a link to buy tickets.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from events_agent.models import Event, to_local_iso
from events_agent.sources import FetchContext
from events_agent.sources.webpage import allowed_by_robots, extract_from_text, page_text

WHATS_ON = "https://omsi.edu/whats-on/"
ADDRESS = "1945 SE Water Ave, Portland (OMSI)"
# Top of each event page: title, dates, venue, audience, tickets and the first paragraphs.
SNIPPET_CHARS = 2500
PAUSE_SECONDS = 0.5  # be gentle: one page at a time

INSTRUCTIONS = (
    "This is one OMSI event page. The lines near the top give the date(s) and times, the "
    "venue (Empirical Theater, Kendall Planetarium, The Museum, or an outside venue such as "
    "a McMenamins pub) and the audience (Little Kids, All Ages, Teens, Adults). Return only "
    "this page's own event, not the related events listed further down. Use the venue as "
    "written; for venues at the museum write e.g. 'OMSI Kendall Planetarium'. Category: "
    "films and planetarium or laser shows are 'movie', lectures and Science Pub are 'talk', "
    "kids' workshops are 'family'. Put the audience in tags (e.g. 'all ages', 'adults 21+', "
    "'little kids') and add 'family-friendly' for kids or all-ages events. Put prices in "
    "price. Use the page's Buy Tickets link as url if it has one."
)


def fetch(source: dict[str, Any], ctx: FetchContext) -> list[Event]:
    listing = source.get("url") or WHATS_ON
    resp = ctx.http.get(listing)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    # Only OMSI's own event pages; ticketing links (tickets.omsi.edu/events/...) are
    # JavaScript pages with nothing to read.
    links = sorted(
        {
            url
            for a in soup.find_all("a", href=True)
            if urlparse(url := urljoin(listing, str(a["href"])).split("#")[0]).netloc
            in ("omsi.edu", "www.omsi.edu")
            and urlparse(url).path.startswith("/events/")
        }
    )

    start, end = ctx.window
    events: list[Event] = []
    errors: list[str] = []
    for url in links:
        if not allowed_by_robots(url, ctx):
            continue
        try:
            page = ctx.http.get(url)
            page.raise_for_status()
            found = extract_from_text(
                event_snippet(page.text, url), url, {"instructions": INSTRUCTIONS}, ctx
            )
        except Exception as exc:  # one odd page shouldn't sink the rest
            errors.append(f"{type(exc).__name__}: {(str(exc).splitlines() or [''])[0]}")
            continue
        for ev in found:
            if ev.venue.lower().removeprefix("omsi").strip() in ("", "the museum", "museum"):
                ev.venue = "OMSI"
            if not ev.address and "omsi" in ev.venue.lower():
                ev.address = ADDRESS
            ev.tags.append("omsi")
            # Stable id: the venue wording can vary between Claude runs.
            ev.external_id = f"omsi|{url}|{to_local_iso(ev.start, ctx.tz)}"
            events.append(ev)
        time.sleep(PAUSE_SECONDS)
    if errors:
        ctx.notes.append(f"{len(errors)} of {len(links)} event pages failed, e.g. {errors[0]}")
    return [ev for ev in events if start <= _day(ev.end or ev.start) and _day(ev.start) <= end]


def _day(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


def event_snippet(html: str, url: str) -> str:
    """The page's header block (title and dates) plus the start of its body text."""
    soup = BeautifulSoup(html, "html.parser")
    hero = soup.select_one("header.omsi-hero")
    hero_text = hero.get_text(" | ", strip=True) if hero else ""
    # Drop query strings from links: some carry per-visit session ids, which would make
    # an unchanged page look new and miss the cache.
    body = re.sub(r"<(https?://[^>?\s]+)\?[^>\s]*>", r"<\1>", page_text(html, url))
    body = body[body.find("Skip to content") :] if "Skip to content" in body else body
    return f"Page header: {hero_text}\n\n{body[:SNIPPET_CHARS]}"
