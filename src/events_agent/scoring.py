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


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


# Words that don't identify a show.
COMMON_WORDS = {
    "with",
    "and",
    "the",
    "tour",
    "live",
    "show",
    "presents",
    "special",
    "guest",
    "guests",
    "introducing",
    "featuring",
    "night",
    "world",
    "north",
    "america",
    "american",
    "concert",
    "tribute",
    "event",
    "evening",
    "sold",
    "out",
    "2025",
    "2026",
    "2027",
    "2028",
}


def _key_words(title: str) -> set[str]:
    return {w for w in title.split() if len(w) >= 4 and w not in COMMON_WORDS}


def _same_show(a: tuple[str, str], b: tuple[str, str]) -> bool:
    (title_a, venue_a), (title_b, venue_b) = a, b
    if title_a in title_b or title_b in title_a:
        return True
    same_venue = bool(venue_a and venue_b) and (venue_a in venue_b or venue_b in venue_a)
    return same_venue and bool(_key_words(title_a) & _key_words(title_b))


def dedupe(ranked: list[Scored]) -> list[Scored]:
    """Drop the same show listed by two sources (e.g. Ticketmaster and the venue's site).

    Two listings are the same show if they start at the same minute and either one title
    contains the other ("Caitlin Peluffo" / "Special Event: Caitlin Peluffo"), or they're
    at the same venue and share a distinctive word ("The Aces w/ Gatlin" / "Introducing
    The Aces with special guest Gatlin"). The better-ranked copy is kept.
    """
    kept: list[Scored] = []
    by_start: dict[str, list[tuple[str, str]]] = {}
    for r in ranked:
        me = (_norm(r.event["title"]), _norm(r.event.get("venue") or ""))
        others = by_start.setdefault(r.event["start_at"], [])
        if me[0] and any(_same_show(me, o) for o in others):
            continue
        others.append(me)
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
