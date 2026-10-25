"""The tools Claude can call during a chat, and the code that runs them.

Each tool's input is a pydantic model: it gives the JSON schema sent to Claude and
validates what comes back before anything runs.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError

from events_agent import sources
from events_agent.config import Settings
from events_agent.models import CATEGORIES
from events_agent.scoring import rank
from events_agent.store import REACTIONS, Store

Category = Literal[CATEGORIES]  # type: ignore[valid-type]
Reaction = Literal[tuple(REACTIONS)]  # type: ignore[valid-type]
SourceKind = Literal[sources.SOURCE_KINDS]  # type: ignore[valid-type]


class AgentContext:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self.tz = ZoneInfo(settings.location.timezone)

    def today(self) -> date:
        return datetime.now(self.tz).date()


# ---- inputs --------------------------------------------------------------------


class SearchEvents(BaseModel):
    query: str | None = Field(
        None, description="Words that must appear in the event (title, description, venue, tags)."
    )
    start_date: str | None = Field(None, description="YYYY-MM-DD, default today")
    end_date: str | None = Field(None, description="YYYY-MM-DD, default 14 days from start")
    categories: list[Category] | None = Field(None, description="Only these categories")
    for_members: list[str] | None = Field(
        None, description="Rank for these family members (names). Default: whole family."
    )
    limit: int = Field(25, ge=1, le=60)


class GetEvent(BaseModel):
    event_id: int


class Empty(BaseModel):
    pass


class RememberPreference(BaseModel):
    member: str | None = Field(None, description="Family member's name; omit for the whole family")
    sentiment: Literal["like", "dislike", "constraint"] = Field(
        description="constraint = practical limits like budget, bedtime, distance, accessibility"
    )
    subject: str = Field(
        description="Short keyword matched against events: a genre, performer, venue, category "
        "or topic, e.g. 'jazz', 'Pixar', 'horror', 'The Fillmore', 'comedy'. One per call."
    )
    detail: str = Field("", description="Anything else worth remembering about it")
    strength: int = Field(2, ge=1, le=3, description="1 mild, 2 normal, 3 strong")


class ForgetPreference(BaseModel):
    preference_id: int


class SetFamilyMember(BaseModel):
    name: str
    notes: str = Field("", description="Age, interests, needs, e.g. 'age 8, into dinosaurs'")


class RecordReaction(BaseModel):
    event_id: int
    reaction: Reaction = Field(  # type: ignore[valid-type]
        description="interested / going / not_interested before the event; "
        "loved_it / liked_it / disliked_it after attending"
    )
    member: str | None = Field(None, description="Whose reaction; omit for the whole family")
    comment: str = ""


class AddSource(BaseModel):
    name: str = Field(description="Short unique name, e.g. 'SF Symphony'")
    kind: SourceKind
    url: str = Field("", description="Page or feed URL; not needed for ticketmaster or tmdb")
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional. webpage: {category, venue, instructions, mode}; ical: {category, "
        "venue}; ticketmaster: {keyword, classificationName, radius}; tmdb: {lists, pages}",
    )
    refresh_now: bool = Field(True, description="Pull events from it right away")


class UpdateSource(BaseModel):
    source_id: int
    action: Literal["enable", "disable", "remove"]


class RefreshSources(BaseModel):
    source_id: int | None = Field(None, description="Only this source; default all enabled sources")


# ---- handlers ------------------------------------------------------------------


def _event_summary(
    ev: dict[str, Any], score: float | None = None, reasons: list[str] | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": ev["id"],
        "title": ev["title"],
        "category": ev["category"],
        "when": ev["start_at"] + (f" to {ev['end_at']}" if ev["end_at"] else ""),
        "venue": ev["venue"],
    }
    for key in ("price", "url"):
        if ev.get(key):
            out[key] = ev[key]
    if ev.get("tags"):
        out["tags"] = ev["tags"][:150]
    if ev.get("description"):
        out["about"] = ev["description"][:220]
    if score is not None:
        out["fit_score"] = round(score, 1)
    if reasons:
        out["why"] = reasons[:4]
    return out


def search_events(ctx: AgentContext, args: SearchEvents) -> Any:
    start = date.fromisoformat(args.start_date) if args.start_date else ctx.today()
    end = date.fromisoformat(args.end_date) if args.end_date else start + timedelta(days=14)
    events = ctx.store.find_events(
        start.isoformat(), end.isoformat(), list(args.categories or []) or None, args.query
    )
    ranked = rank(events, ctx.store.list_preferences(), ctx.store.list_feedback(), args.for_members)
    return {
        "window": f"{start} to {end}",
        "matching_events": len(events),
        "showing": min(len(ranked), args.limit),
        "events": [_event_summary(r.event, r.score, r.reasons) for r in ranked[: args.limit]],
    }


def get_event(ctx: AgentContext, args: GetEvent) -> Any:
    ev = ctx.store.get_event(args.event_id)
    if ev is None:
        raise ValueError(f"no event with id {args.event_id}")
    return {k: v for k, v in ev.items() if v not in ("", None) and k != "external_id"}


def get_profile(ctx: AgentContext, _: Empty) -> Any:
    return profile_snapshot(ctx.store)


def profile_snapshot(store: Store) -> dict[str, Any]:
    return {
        "members": store.list_members(),
        "preferences": [
            {k: p[k] for k in ("id", "member", "sentiment", "subject", "detail", "strength")}
            for p in store.list_preferences()
        ],
        "recent_reactions": [
            {k: f[k] for k in ("member", "reaction", "title", "category", "comment", "start_at")}
            for f in store.list_feedback(limit=25)
        ],
    }


def remember_preference(ctx: AgentContext, args: RememberPreference) -> Any:
    pid = ctx.store.add_preference(
        args.member, args.sentiment, args.subject, args.detail, args.strength
    )
    return {"saved": True, "preference_id": pid}


def forget_preference(ctx: AgentContext, args: ForgetPreference) -> Any:
    return {"removed": ctx.store.remove_preference(args.preference_id)}


def set_family_member(ctx: AgentContext, args: SetFamilyMember) -> Any:
    return {"member_id": ctx.store.upsert_member(args.name, args.notes)}


def record_reaction(ctx: AgentContext, args: RecordReaction) -> Any:
    if ctx.store.get_event(args.event_id) is None:
        raise ValueError(f"no event with id {args.event_id}")
    fid = ctx.store.add_feedback(args.event_id, args.reaction, args.member, args.comment)
    return {"saved": True, "feedback_id": fid}


def list_sources(ctx: AgentContext, _: Empty) -> Any:
    return [
        {
            k: s[k]
            for k in (
                "id",
                "name",
                "kind",
                "url",
                "config",
                "enabled",
                "event_count",
                "last_run_at",
                "last_status",
            )
        }
        for s in ctx.store.list_sources()
    ]


def add_source(ctx: AgentContext, args: AddSource) -> Any:
    if args.kind in ("ical", "webpage") and not args.url.startswith(("http://", "https://")):
        raise ValueError(f"a {args.kind} source needs an http(s) URL")
    sid = ctx.store.add_source(args.name, args.kind, args.url, args.config)
    out: dict[str, Any] = {"source_id": sid}
    if args.refresh_now:
        out["refresh"] = [str(r) for r in sources.refresh(ctx.store, ctx.settings, [sid])]
    return out


def update_source(ctx: AgentContext, args: UpdateSource) -> Any:
    if args.action == "remove":
        return {"removed": ctx.store.remove_source(args.source_id)}
    return {"updated": ctx.store.set_source_enabled(args.source_id, args.action == "enable")}


def refresh_sources(ctx: AgentContext, args: RefreshSources) -> Any:
    ids = [args.source_id] if args.source_id is not None else None
    return [str(r) for r in sources.refresh(ctx.store, ctx.settings, ids)]


# ---- registry ------------------------------------------------------------------


@dataclass
class Tool:
    name: str
    description: str
    model: type[BaseModel]
    handler: Callable[[AgentContext, Any], Any]
    status: str  # shown to the user while the tool runs

    def schema(self) -> dict[str, Any]:
        schema = self.model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": schema,
            # Stream tool input as it is generated; run() validates it before use.
            "eager_input_streaming": True,
        }


TOOLS = [
    Tool(
        "search_events",
        "Search stored upcoming events, ranked by how well they fit the family's saved "
        "preferences and past reactions (fit_score, with reasons). Use broad searches first, "
        "then narrow with query/categories. Results only include sources that have been refreshed.",
        SearchEvents,
        search_events,
        "searching events",
    ),
    Tool(
        "get_event_details", "Full details for one event.", GetEvent, get_event, "looking up event"
    ),
    Tool(
        "get_family_profile",
        "Family members, saved preferences (with ids) and recent reactions to events.",
        Empty,
        get_profile,
        "checking family profile",
    ),
    Tool(
        "remember_preference",
        "Save a like, dislike or practical constraint for a family member or the whole family. "
        "Call this whenever someone states a preference or it is clearly implied, "
        "one subject per call.",
        RememberPreference,
        remember_preference,
        "remembering preference",
    ),
    Tool(
        "forget_preference",
        "Delete a saved preference that is wrong or out of date.",
        ForgetPreference,
        forget_preference,
        "forgetting preference",
    ),
    Tool(
        "set_family_member",
        "Add a family member or update their notes (age, interests, needs).",
        SetFamilyMember,
        set_family_member,
        "updating family",
    ),
    Tool(
        "record_reaction",
        "Save someone's reaction to a specific event: interest before it, or how it went after.",
        RecordReaction,
        record_reaction,
        "saving reaction",
    ),
    Tool(
        "list_sources",
        "List configured event sources and their status.",
        Empty,
        list_sources,
        "listing sources",
    ),
    Tool(
        "add_source",
        "Add a new place to pull events from. kinds: 'webpage' (any page listing events), "
        "'ical' (.ics calendar feed), 'ticketmaster' (Ticketmaster API, local events), "
        "'tmdb' (movies in theaters), 'mcmenamins' (McMenamins theater showtimes; config "
        "{theaters: [page paths]}), 'omsi' (OMSI films, planetarium and events), "
        "'seatengine' (comedy clubs on SeatEngine, url = club site), 'crowdwork' (comedy "
        "theaters on Crowdwork; config {theatre: slug, venue}), 'tribe' (WordPress sites "
        "using The Events Calendar plugin, url = site; try it before 'webpage'). "
        "Prefer an iCal feed when a site offers one.",
        AddSource,
        add_source,
        "adding source",
    ),
    Tool(
        "update_source",
        "Enable, disable or remove a source.",
        UpdateSource,
        update_source,
        "updating source",
    ),
    Tool(
        "refresh_sources",
        "Pull fresh events from sources now. Can take a minute.",
        RefreshSources,
        refresh_sources,
        "refreshing sources",
    ),
]

TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def run(ctx: AgentContext, name: str, raw_input: object) -> tuple[str, bool]:
    """Run a tool. Returns (result text, is_error)."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return f"unknown tool {name}", True
    try:
        args = tool.model.model_validate(raw_input)
    except ValidationError as exc:
        return f"INVALID_INPUT: {exc}", True
    try:
        result = tool.handler(ctx, args)
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}", True
    return json.dumps(result, default=str), False
