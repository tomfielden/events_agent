"""Rank candidate events using what we've learned about the family.

This is a cheap first pass, so Claude sees the most promising few dozen events out of
possibly hundreds. Claude makes the final call using the full profile.

Two signals:
- Stated preferences ("Sam loves jazz", "nobody likes horror"): keyword match against the
  event's title, description, venue, tags and category.
- Reactions to past events ("interested", "loved_it", "not_interested"...): events that
  share a category, venue, performer/genre tag or title with a liked event go up, and
  ones like a disliked event go down.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from events_agent.store import REACTIONS

DISLIKE_MULTIPLIER = 1.5  # one dislike outweighs one like: a bad pick costs more than a missed one
HIDDEN = -1000.0


@dataclass
class Scored:
    event: dict[str, Any]
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)


def _tags(event: dict[str, Any]) -> set[str]:
    return {t for t in (event.get("tags") or "").split(",") if t}


def _haystack(event: dict[str, Any]) -> str:
    return " ".join(
        str(event.get(k) or "") for k in ("title", "description", "venue", "tags", "category")
    ).lower()


def _matches(subject: str, event: dict[str, Any], haystack: str) -> bool:
    subject = subject.lower().strip()
    if not subject:
        return False
    if subject == event.get("category"):
        return True
    # Match on a word start so "puppets" finds "Puppet Show" and "jazz" finds "jazzy".
    stem = subject[:-1] if subject.endswith("s") and len(subject) > 3 else subject
    return re.search(rf"\b{re.escape(stem)}", haystack) is not None


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", title.lower()).strip()


def dedupe(ranked: list[Scored]) -> list[Scored]:
    """Drop the same show listed by two sources (e.g. Ticketmaster and the club's own site).

    Two events are the same if they start at the same time and one title contains the
    other ("Caitlin Peluffo" vs "Special Event: Caitlin Peluffo"). The better-ranked copy
    is kept.
    """
    kept: list[Scored] = []
    by_start: dict[str, list[str]] = {}
    for r in ranked:
        start = r.event["start_at"]
        title = _norm_title(r.event["title"])
        others = by_start.setdefault(start, [])
        if title and any(title in o or o in title for o in others):
            continue
        others.append(title)
        kept.append(r)
    return kept


def rank(
    events: list[dict[str, Any]],
    preferences: list[dict[str, Any]],
    feedback: list[dict[str, Any]],
    members: list[str] | None = None,
) -> list[Scored]:
    """Score and sort events, best first. Events the family already dismissed are dropped.

    If `members` is given, only their preferences (plus whole-family ones) count.
    """
    wanted = {m.lower() for m in members} if members else None

    def applies(member: str) -> bool:
        return wanted is None or member == "family" or member.lower() in wanted

    prefs = [
        p for p in preferences if p["sentiment"] in ("like", "dislike") and applies(p["member"])
    ]
    reactions = [f for f in feedback if applies(f["member"]) and f["reaction"] in REACTIONS]
    dismissed = {f["event_id"] for f in reactions if REACTIONS[f["reaction"]] < 0}

    results: list[Scored] = []
    for ev in events:
        if ev["id"] in dismissed:
            continue
        s = Scored(ev)
        hay = _haystack(ev)
        tags = _tags(ev)

        for p in prefs:
            if _matches(p["subject"], ev, hay):
                weight = float(p["strength"])
                if p["sentiment"] == "dislike":
                    weight *= -DISLIKE_MULTIPLIER
                s.score += weight
                s.reasons.append(f"{p['member']} {p['sentiment']}s {p['subject']}")

        for f in reactions:
            if f["event_id"] == ev["id"]:
                s.score += REACTIONS[f["reaction"]]
                s.reasons.append(f"{f['member']} marked this {f['reaction']}")
                continue
            w = REACTIONS[f["reaction"]]
            if w < 0:
                w *= DISLIKE_MULTIPLIER
            shared = tags & _tags(f)
            similarity = 0.0
            if f["title"].lower() == ev["title"].lower():
                similarity += 1.0
            if f["venue"] and f["venue"] == ev.get("venue") and f["venue"] != "In theaters":
                similarity += 0.3
            if f["category"] == ev["category"]:
                similarity += 0.2
            similarity += 0.4 * min(len(shared), 3)
            if similarity >= 0.4:
                s.score += w * similarity
                why = ", ".join(sorted(shared)[:3]) or f["category"]
                s.reasons.append(f"like '{f['title']}' ({f['reaction']}: {why})")

        results.append(s)

    results.sort(key=lambda r: (-r.score, r.event["start_at"]))
    return dedupe(results)
