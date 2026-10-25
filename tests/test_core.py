from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from events_agent import tools
from events_agent.agent import Agent
from events_agent.config import Settings
from events_agent.models import Event
from events_agent.scoring import rank
from events_agent.sources import FetchContext, ical, ticketmaster, webpage
from events_agent.store import Store

TZ = ZoneInfo("America/Los_Angeles")
TODAY = datetime.now(TZ).date()


def day(offset: int) -> date:
    return TODAY + timedelta(days=offset)


@pytest.fixture
def store() -> Store:
    return Store(":memory:", TZ)  # type: ignore[arg-type]


@pytest.fixture
def seeded(store: Store) -> Store:
    sid = store.add_source("test", "webpage", "https://example.com")
    store.upsert_events(
        sid,
        [
            Event(
                "Jazz Night",
                datetime.combine(day(2), datetime.min.time()),
                "music",
                tags=["jazz"],
                venue="SFJAZZ",
            ),
            Event("Horror Movie Marathon", day(3), "movie", tags=["Horror"]),
            Event("Dinosaur Puppet Show", day(5), "family", description="For kids 4-10"),
            Event("Heavy Metal Fest", day(6), "music", tags=["metal"]),
            Event("Old Movie", day(-10), "movie", end=day(10)),  # still in theaters
            Event("Far Future", day(200), "other"),
        ],
    )
    return store


def ctx_for(settings: Settings) -> FetchContext:
    return FetchContext(settings=settings, http=httpx.Client(), tz=TZ)


def test_upsert_dedupes(store: Store) -> None:
    sid = store.add_source("s", "webpage", "https://x")
    ev = Event("Show", day(1), "theater", venue="Hall")
    assert store.upsert_events(sid, [ev]) == (1, 0)
    assert store.upsert_events(sid, [ev]) == (0, 1)


def test_find_events_window_includes_ongoing(seeded: Store) -> None:
    found = seeded.find_events(TODAY.isoformat(), day(14).isoformat())
    titles = {e["title"] for e in found}
    assert "Old Movie" in titles
    assert "Far Future" not in titles
    assert len(found) == 5
    assert [
        e["title"]
        for e in seeded.find_events(TODAY.isoformat(), day(14).isoformat(), text="dinosaur")
    ] == ["Dinosaur Puppet Show"]


def test_rank_uses_preferences_and_reactions(seeded: Store) -> None:
    seeded.add_preference("Sam", "like", "jazz", strength=3)
    seeded.add_preference(None, "dislike", "horror", strength=3)
    events = seeded.find_events(TODAY.isoformat(), day(14).isoformat())
    metal = next(e for e in events if e["title"] == "Heavy Metal Fest")
    seeded.add_feedback(metal["id"], "not_interested", None)

    ranked = rank(events, seeded.list_preferences(), seeded.list_feedback())
    titles = [r.event["title"] for r in ranked]
    assert titles[0] == "Jazz Night"
    assert titles[-1] == "Horror Movie Marathon"
    assert "Heavy Metal Fest" not in titles  # dismissed

    # Sam's like doesn't apply when ranking only for someone else.
    for_alex = rank(events, seeded.list_preferences(), seeded.list_feedback(), ["Alex"])
    assert next(r for r in for_alex if r.event["title"] == "Jazz Night").score == 0


def test_similar_to_loved_event_ranks_up(store: Store) -> None:
    sid = store.add_source("s", "webpage", "https://x")
    store.upsert_events(
        sid,
        [
            Event("Band A live", day(-3), "music", tags=["indie rock"]),
            Event("Band B live", day(4), "music", tags=["indie rock"]),
            Event("Band C live", day(4), "music", tags=["country"]),
        ],
    )
    past = store.find_events(day(-5).isoformat(), day(-1).isoformat())[0]
    store.add_feedback(past["id"], "loved_it", "Dana")
    ranked = rank(
        store.find_events(TODAY.isoformat(), day(10).isoformat()),
        store.list_preferences(),
        store.list_feedback(),
    )
    assert ranked[0].event["title"] == "Band B live"


JSONLD_PAGE = """
<html><head><script type="application/ld+json">
{"@context": "https://schema.org", "@graph": [
  {"@type": "Organization", "name": "Venue Co"},
  {"@type": "MusicEvent", "name": "The Quartet", "startDate": "%s",
   "location": {"@type": "Place", "name": "Hall",
                "address": {"streetAddress": "1 Main St", "addressLocality": "SF"}},
   "offers": {"price": "25"}, "url": "/e/quartet", "performer": [{"name": "The Quartet"}],
   "description": "<p>Strings &amp; more</p>"}
]}
</script></head><body>hi</body></html>
"""


def test_parse_jsonld() -> None:
    start = f"{day(3).isoformat()}T19:30:00-07:00"
    events = webpage.parse_jsonld(JSONLD_PAGE % start, "https://venue.example/events")
    assert len(events) == 1
    ev = events[0]
    assert ev.title == "The Quartet"
    assert ev.category == "music"
    assert ev.venue == "Hall" and ev.address == "1 Main St, SF"
    assert ev.url == "https://venue.example/e/quartet"
    assert ev.price == "$25"
    assert ev.description == "Strings & more"


def test_page_text_keeps_links() -> None:
    html = '<nav>menu</nav><p>Story time <a href="/st">details</a></p><script>x</script>'
    text = webpage.page_text(html, "https://lib.example/")
    assert "menu" not in text and "x" not in text.split()
    assert "<https://lib.example/st>" in text


def test_parse_ical() -> None:
    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:abc
SUMMARY:Lego Club
DTSTART:{day(2):%Y%m%d}T160000
DTEND:{day(2):%Y%m%d}T170000
LOCATION:Main Library
CATEGORIES:Kids,Library
END:VEVENT
BEGIN:VEVENT
UID:old
SUMMARY:Past thing
DTSTART;VALUE=DATE:{day(-30):%Y%m%d}
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n").encode()
    events = ical.parse(ics, {"category": "family"}, ctx_for(Settings()))
    assert [e.title for e in events] == ["Lego Club"]
    assert events[0].category == "family"
    assert events[0].tags == ["Kids", "Library"]


def test_ticketmaster_mapping() -> None:
    item: dict[str, Any] = {
        "id": "tm1",
        "name": "Big Concert",
        "url": "https://tm.example/1",
        "dates": {"start": {"dateTime": "2026-11-07T03:30:00Z"}},
        "classifications": [
            {
                "segment": {"name": "Music"},
                "genre": {"name": "Rock"},
                "subGenre": {"name": "Undefined"},
            }
        ],
        "priceRanges": [{"min": 45.0, "max": 120.0}],
        "_embedded": {
            "venues": [{"name": "Arena", "city": {"name": "SF"}}],
            "attractions": [{"name": "The Band"}],
        },
    }
    ev = ticketmaster._to_event(item)
    assert ev.category == "music"
    assert ev.price == "$45-$120"
    assert ev.tags == ["Rock", "The Band"]
    store = Store(":memory:", TZ)  # type: ignore[arg-type]
    store.upsert_events(store.add_source("tm", "ticketmaster"), [ev])
    # 03:30 UTC on Nov 7 is the evening of Nov 6 in San Francisco.
    assert store.find_events("2026-11-06", "2026-11-06")[0]["start_at"] == "2026-11-06T19:30"


def test_tool_validation_and_search(seeded: Store) -> None:
    ctx = tools.AgentContext(seeded, Settings())
    out, err = tools.run(ctx, "search_events", {"limit": "lots"})
    assert err and out.startswith("INVALID_INPUT")
    out, err = tools.run(
        ctx, "remember_preference", {"member": "Sam", "sentiment": "like", "subject": "puppets"}
    )
    assert not err
    out, err = tools.run(ctx, "search_events", {"categories": ["family", "music"]})
    assert not err
    result = json.loads(out)
    assert result["events"][0]["title"] == "Dinosaur Puppet Show"
    assert "Sam likes puppets" in result["events"][0]["why"]


# ---- agent loop with a fake Claude -------------------------------------------------


class FakeStream:
    def __init__(self, message: Any) -> None:
        self.message = message

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def __iter__(self) -> Any:
        for block in self.message.content:
            if block.type == "text":
                yield SimpleNamespace(type="text", text=block.text)

    def get_final_message(self) -> Any:
        return self.message


def test_agent_runs_tools_then_answers(seeded: Store) -> None:
    script = [
        SimpleNamespace(
            stop_reason="tool_use",
            content=[
                SimpleNamespace(type="text", text="Let me look. "),
                SimpleNamespace(
                    type="tool_use",
                    id="t1",
                    name="remember_preference",
                    input={"member": "Kai", "sentiment": "like", "subject": "jazz"},
                ),
                SimpleNamespace(
                    type="tool_use", id="t2", name="search_events", input={"query": "jazz"}
                ),
            ],
        ),
        SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Try Jazz Night!")]
        ),
    ]
    calls: list[dict[str, Any]] = []

    def fake_stream(**kwargs: Any) -> FakeStream:
        calls.append(kwargs)
        return FakeStream(script[len(calls) - 1])

    agent = Agent(Settings(anthropic_api_key="test"), seeded)
    agent.client = SimpleNamespace(  # type: ignore[assignment]
        beta=SimpleNamespace(messages=SimpleNamespace(stream=fake_stream))
    )
    out: list[str] = []
    statuses: list[str] = []
    agent.send("Kai loves jazz, anything this week?", out.append, statuses.append)

    assert "".join(out) == "Let me look. Try Jazz Night!"
    assert statuses == ["remembering preference", "searching events"]
    assert [p["subject"] for p in seeded.list_preferences()] == ["jazz"]
    # Both tool results went back together in one user message.
    results = agent.messages[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert "Jazz Night" in results[1]["content"]
    assert calls[0]["model"] == "claude-opus-5-5"
    assert calls[0]["fallbacks"] == "default"


MCM_CARD = """
<div class="uk-panel event detailed movie">
  <div class="tm-panel-titlebg">
    <a href="javascript: void showTheaterMovie('Bagdad Theater','{title}','{mdy}');">
    <h3 class="uk-panel-title">{title}</h3></a></div>
  <div class="tm-card-content">
    <a href="javascript: void showTheaterMovie('Bagdad Theater','{title}','{mdy}');">
      <h3 class="uk-panel-title">{weekday}</h3></a>
    <p class="uk-panel-time">{info}</p>
    <div class="uk-panel-movietimes">{times}</div>
    <h4 class="tm-card-title">at <span>{venue}</span></h4>
  </div>
</div>
"""


def test_parse_mcmenamins() -> None:
    from events_agent.sources import mcmenamins

    d = day(2)
    common = {"mdy": f"{d:%m/%d/%Y}", "weekday": f"{d:%A, %B} {d.day}"}
    times = '<div><a href="https://tix.example/1">3:15pm</a></div><div><a href="https://tix.example/2">7pm</a></div>'
    card = MCM_CARD.format(
        title="Casper",
        info="(PG) Running time: 100 minutes",
        times=times,
        venue="Bagdad Theater",
        **common,
    )
    promo = MCM_CARD.format(
        title="Passport Day", info="All day", times="", venue="All Locations", **common
    )
    html = card + card + promo  # the site repeats each card for mobile
    events = {
        e.key(): e
        for e in mcmenamins.parse(
            html, "https://www.mcmenamins.com/bagdad", "Hawthorne", ctx_for(Settings())
        )
    }
    assert len(events) == 1
    ev = next(iter(events.values()))
    assert ev.title == "Casper" and ev.category == "movie" and ev.venue == "Bagdad Theater"
    assert isinstance(ev.start, datetime) and (ev.start.date(), ev.start.hour, ev.start.minute) == (
        d,
        15,
        15,
    )
    assert ev.description.startswith("Showtimes: 3:15pm, 7pm.")
    assert "family-friendly" in ev.tags and ev.url == "https://tix.example/1"


def test_claude_extraction_is_cached(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_claude(text: str, url: str, cfg: dict[str, Any], ctx: FetchContext) -> Any:
        calls.append(text)
        return webpage.ExtractedEvents(
            events=[
                webpage.ExtractedEvent(
                    title="Laser Show",
                    start=f"{day(3)}T16:30",
                    end=day(20).isoformat(),
                    category="movie",
                    venue="OMSI Kendall Planetarium",
                )
            ]
        )

    monkeypatch.setattr(webpage, "_call_claude", fake_claude)
    ctx = FetchContext(settings=Settings(), http=httpx.Client(), tz=TZ, store=store)
    first = webpage.extract_from_text("Laser show daily", "https://omsi.example/e", {}, ctx)
    again = webpage.extract_from_text("Laser show daily", "https://omsi.example/e", {}, ctx)
    changed = webpage.extract_from_text("Laser show, new dates", "https://omsi.example/e", {}, ctx)
    assert len(calls) == 2  # the unchanged page was served from the cache
    assert first[0].title == again[0].title == changed[0].title == "Laser Show"
    assert first[0].end == day(20)


def test_same_show_from_two_sources_is_shown_once(store: Store) -> None:
    tm = store.add_source("tm", "ticketmaster")
    club = store.add_source("club", "seatengine", "https://club.example")
    when = datetime.combine(day(4), datetime.min.time()).replace(hour=19, minute=30)
    store.upsert_events(tm, [Event("Caitlin Peluffo", when, "comedy", venue="Club - Portland")])
    store.upsert_events(
        club,
        [
            Event("Special Event: Caitlin Peluffo", when, "comedy", venue="Club"),
            Event("Caitlin Peluffo", when.replace(hour=21), "comedy", venue="Club"),  # late show
        ],
    )
    ranked = rank(store.find_events(TODAY.isoformat(), day(10).isoformat()), [], [])
    assert len(ranked) == 2


def test_seatengine_first_day_guesses_year() -> None:
    from events_agent.sources.seatengine import _first_day

    assert _first_day("May 13 - May 16", date(2026, 10, 24)) == date(2027, 5, 13)
    assert _first_day("October 24", date(2026, 10, 24)) == date(2026, 10, 24)
    assert _first_day("TBA", date(2026, 10, 24)) is None


def test_crowdwork_show_to_events() -> None:
    from events_agent.sources import crowdwork

    show = {
        "id": 7,
        "name": "Harold Night ",
        "recurring": "Weekly",
        "url": "https://cw.example/e/h",
        "dates": [f"{day(-7)}T20:30:00.000-07:00", f"{day(3)}T20:30:00.000-07:00"],
        "description_short": "Long-form improv\xa0teams.",
        "tags": {"public": ["Improv"]},
        "cost_tiers": [{"cost": 1000}, {"cost": 1500}],
        "venue": "",
        "theatre": {"name": "Kickstand Comedy "},
    }
    events = crowdwork.to_events(show, {}, ctx_for(Settings()))
    assert len(events) == 1  # last week's date is dropped
    ev = events[0]
    assert ev.title == "Harold Night" and ev.venue == "Kickstand Comedy"
    assert ev.price == "$10-$15" and ev.description == "Long-form improv teams."
    assert ev.tags == ["Improv", "weekly show", "comedy"]


def test_tribe_event_mapping() -> None:
    from events_agent.sources import tribe

    item = {
        "id": 10259,
        "title": "GuignolFest 2026: Ghouls &#038; Ghosts",
        "url": "https://cstpdx.com/event/guignolfest/",
        "all_day": False,
        "start_date": f"{day(2)} 13:30:00",
        "end_date": f"{day(2)} 16:00:00",
        "timezone": "America/Los_Angeles",
        "cost": "$20",
        "excerpt": "<p>Short horror&nbsp;films</p>",
        "categories": [{"name": "Film"}],
        "tags": [{"name": "horror"}],
        "venue": {
            "venue": "Clinton Street Theater",
            "address": "2522 SE Clinton St",
            "city": "Portland",
        },
    }
    ev = tribe.to_event(item, {}, ctx_for(Settings()))
    assert ev.title == "GuignolFest 2026: Ghouls & Ghosts"
    assert ev.category == "movie" and ev.tags == ["film", "horror"]
    assert ev.venue == "Clinton Street Theater" and ev.address == "2522 SE Clinton St, Portland"
    assert isinstance(ev.start, datetime) and ev.start.hour == 13 and ev.start.tzinfo is not None
    assert ev.description == "Short horror films" and ev.price == "$20"


MICRODATA_CARD = """
<div class="event" itemscope itemtype="http://schema.org/Event">
  <meta itemprop="startDate" content="{start}">
  <a href="https://tix.example/p/1"><h3 itemprop="name">Theo Katzman</h3></a>
  <div class="event-venue">- Aladdin Theater -</div>
</div>
"""


def test_microdata_with_mislabeled_local_times(monkeypatch: pytest.MonkeyPatch) -> None:
    card = MICRODATA_CARD.format(start=f"{day(2)}T20:00:00+00:00")
    html = card + card  # same show in two layouts
    events = webpage.parse_microdata(html, "https://venue.example/")
    assert len(events) == 1
    assert events[0].venue == "Aladdin Theater" and events[0].url == "https://tix.example/p/1"

    class Resp:
        status_code, text = 200, html

        def raise_for_status(self) -> None:
            pass

    monkeypatch.setattr(webpage, "allowed_by_robots", lambda url, ctx: True)
    ctx = ctx_for(Settings())
    monkeypatch.setattr(ctx.http, "get", lambda url: Resp())
    source = {"url": "https://venue.example/", "config": {"times_are_local": True}}
    ev = webpage.fetch(source, ctx)[0]
    assert isinstance(ev.start, datetime)
    assert ev.start.hour == 20 and ev.start.utcoffset() == TZ.utcoffset(ev.start)


def test_dedupe_same_venue_shared_name(store: Store) -> None:
    a = store.add_source("a", "ticketmaster")
    b = store.add_source("b", "webpage", "https://x")
    when = datetime.combine(day(4), datetime.min.time()).replace(hour=20)
    store.upsert_events(
        a,
        [
            Event("The Aces w/ Gatlin", when, "music", venue="Aladdin Theater"),
            Event(
                "Warren Miller's Days Off", when.replace(hour=13), "movie", venue="Aladdin Theater"
            ),
            Event("Jazz Trio", when, "music", venue="Other Club"),
        ],
    )
    store.upsert_events(
        b,
        [
            Event(
                "Introducing The Aces with special guest Gatlin",
                when,
                "music",
                venue="Aladdin Theater",
            ),
            Event(
                'Warren Miller\'s "Days Off"',
                when.replace(hour=13),
                "movie",
                venue="Aladdin Theater",
            ),
            Event("Jazz Night Live", when, "music", venue="Aladdin Theater"),  # different show
        ],
    )
    titles = sorted(
        r.event["title"]
        for r in rank(store.find_events(TODAY.isoformat(), day(10).isoformat()), [], [])
    )
    assert titles == [
        "Jazz Night Live",
        "Jazz Trio",
        "The Aces w/ Gatlin",
        "Warren Miller's Days Off",
    ]
