"""Event sources. Each kind is a function that turns one configured source into Events.

Adding a new kind: write a module with a `fetch(source, ctx) -> list[Event]` function and
register it in `_fetchers()` and SOURCE_KINDS below.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from events_agent.config import Settings
from events_agent.models import Event
from events_agent.store import Store

USER_AGENT = "events-agent/0.1 (personal family event finder)"


@dataclass
class FetchContext:
    settings: Settings
    http: httpx.Client
    tz: ZoneInfo
    notes: list[str] = field(default_factory=list)  # warnings for the current source
    store: Store | None = None  # for caching Claude extractions

    @property
    def window(self) -> tuple[date, date]:
        today = datetime.now(self.tz).date()
        return today, today + timedelta(days=self.settings.days_ahead)


Fetcher = Callable[[dict[str, Any], FetchContext], list[Event]]


def _fetchers() -> dict[str, Fetcher]:
    from events_agent.sources import ical, mcmenamins, omsi, ticketmaster, tmdb, webpage

    return {
        "ticketmaster": ticketmaster.fetch,
        "tmdb": tmdb.fetch,
        "ical": ical.fetch,
        "webpage": webpage.fetch,
        "mcmenamins": mcmenamins.fetch,
        "omsi": omsi.fetch,
    }


SOURCE_KINDS = ("ticketmaster", "tmdb", "ical", "webpage", "mcmenamins", "omsi")


@dataclass
class RefreshResult:
    source: str
    ok: bool
    new: int = 0
    updated: int = 0
    message: str = ""

    def __str__(self) -> str:
        if self.ok:
            note = f" ({self.message})" if self.message else ""
            return f"{self.source}: {self.new} new, {self.updated} updated{note}"
        return f"{self.source}: FAILED - {self.message}"


def refresh(
    store: Store, settings: Settings, source_ids: list[int] | None = None
) -> list[RefreshResult]:
    """Pull events from every enabled source (or just the given ones) into the store."""
    fetchers = _fetchers()
    tz = ZoneInfo(settings.location.timezone)
    results: list[RefreshResult] = []
    with httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=30.0, follow_redirects=True
    ) as http:
        ctx = FetchContext(settings=settings, http=http, tz=tz, store=store)
        for source in store.list_sources(enabled_only=source_ids is None):
            if source_ids is not None and source["id"] not in source_ids:
                continue
            fetch = fetchers.get(source["kind"])
            if fetch is None:
                results.append(RefreshResult(source["name"], False, message="unknown kind"))
                continue
            ctx.notes = []
            try:
                events = fetch(source, ctx)
                new, updated = store.upsert_events(source["id"], events)
                result = RefreshResult(source["name"], True, new, updated, "; ".join(ctx.notes))
                status = f"ok: {len(events)} events" + (
                    f" ({result.message})" if result.message else ""
                )
                store.mark_source_run(source["id"], status)
            except Exception as exc:  # one broken site shouldn't stop the others
                first_line = (str(exc).splitlines() or [""])[0]
                result = RefreshResult(
                    source["name"], False, message=f"{type(exc).__name__}: {first_line}"
                )
                store.mark_source_run(source["id"], f"error: {result.message}"[:500])
            results.append(result)
    today = datetime.now(tz).date().isoformat()
    store.prune_past_events(today)
    return results
