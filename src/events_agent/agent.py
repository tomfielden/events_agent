"""The chat agent: a streaming tool-use loop over Claude."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

import anthropic

from events_agent.config import Settings
from events_agent.llm import make_client
from events_agent.store import Store
from events_agent.tools import TOOLS, TOOLS_BY_NAME, AgentContext, profile_snapshot, run

# Refusal fallback: if a safety classifier declines a turn, the API re-runs it on a
# suitable model inside the same call instead of failing.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOOL_ROUNDS = 25
MAX_BAD_JSON_RETRIES = 2

SYSTEM_PROMPT = """\
You are a family events concierge. You help one household find movies, concerts, shows, \
festivals and other outings they'll enjoy, and you get better at it by remembering what \
each person likes and dislikes.

How to work:
- Find events with search_events. It only knows about events already pulled from the \
configured sources. If results look thin or stale, offer to refresh sources or add new \
ones. Use web_search for questions the stored events can't answer (reviews, age \
suitability, parking, whether a show suits young kids), not as the main event finder.
- search_events ranks by fit_score using saved preferences. Treat it as a hint: read the \
candidates and use judgment, especially for constraints (budget, bedtimes, distance) the \
score can't apply.
- Recommend a handful of options, not a dump. For each: what it is, when and where, price \
if known, the link, and one line on why it fits this family (or who it fits). Group by \
weekend or by person when that helps. Mention event ids so people can react to them.
- Learn as you go. When someone states or clearly implies a preference ("the kids hated \
the last opera", "Dana is vegetarian", "we never go out on school nights"), save it with \
remember_preference, one subject per call. When someone reacts to a specific event \
("ooh, that one", "we went, it was great"), save it with record_reaction. When you learn \
about a family member (name, age, interests), use set_family_member. Do this quietly: a \
short confirmation is enough.
- When a preference is unclear or two people conflict, ask a brief question rather than guess.
- When someone gives you a site to watch, add it with add_source. Prefer an iCal feed if \
the site has one. Tell them how many events it found; if zero, say so and suggest a more \
specific listings page.
- Keep answers conversational and compact. This is a terminal, so use plain text and \
simple lists, no tables.
"""


def session_context(settings: Settings, store: Store, ctx: AgentContext) -> str:
    loc = settings.location
    sources = store.list_sources()
    source_line = (
        ", ".join(f"{s['name']} ({s['kind']}, {s['event_count']} events)" for s in sources)
        or "none yet, so suggest adding some"
    )
    return (
        f"Today is {ctx.today():%A, %B %d, %Y}. The family lives in {loc.city}, {loc.state} "
        f"(timezone {loc.timezone}) and will travel about {loc.radius_miles} miles.\n"
        f"Configured sources: {source_line}.\n"
        f"What you know about the family so far:\n"
        f"{json.dumps(profile_snapshot(store), default=str)}"
    )


class Agent:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self.ctx = AgentContext(store, settings)
        self.client = make_client(settings)
        self.messages: list[Any] = []
        # Built once per session so the prompt prefix stays byte-identical and cacheable.
        self.system = [
            {"type": "text", "text": SYSTEM_PROMPT},
            {"type": "text", "text": session_context(settings, store, self.ctx)},
        ]
        loc = settings.location
        self.tools: list[Any] = [t.schema() for t in TOOLS] + [
            {
                "type": "web_search_20260209",
                "name": "web_search",
                "max_uses": 5,
                "user_location": {
                    "type": "approximate",
                    "city": loc.city,
                    "region": loc.state,
                    "country": loc.country,
                    "timezone": loc.timezone,
                },
            }
        ]

    def send(
        self,
        text: str,
        on_text: Callable[[str], None],
        on_status: Callable[[str], None],
    ) -> None:
        """Run one user turn to completion, streaming Claude's reply through on_text."""
        self.messages.append({"role": "user", "content": text})
        bad_json = 0
        for _ in range(MAX_TOOL_ROUNDS):
            try:
                response = self._stream_once(on_text, on_status)
                bad_json = 0
            except ValueError:
                # Tool input the SDK couldn't parse at all; the turn never completed,
                # so re-issue it.
                bad_json += 1
                if bad_json > MAX_BAD_JSON_RETRIES:
                    raise
                continue

            # History is append-only: keep every block (thinking, fallback, server tool
            # results) exactly as returned.
            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "pause_turn":
                continue  # a server tool (web search) hit its step limit; let it resume
            if response.stop_reason == "refusal":
                on_text("\n[Claude declined to answer that one.]")
                return
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                return

            results = []
            for block in tool_uses:
                if response.stop_reason == "max_tokens":
                    # The input was cut off mid-generation; don't run a partial call.
                    content, is_error = "Tool input was truncated; try a smaller request.", True
                else:
                    tool = TOOLS_BY_NAME.get(block.name)
                    on_status(tool.status if tool else block.name)
                    content, is_error = run(self.ctx, block.name, block.input)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": content,
                        "is_error": is_error,
                    }
                )
            # All results for one assistant turn go back in a single user message.
            self.messages.append({"role": "user", "content": results})
        on_text("\n[Stopped after too many steps; ask me to continue.]")

    def _stream_once(self, on_text: Callable[[str], None], on_status: Callable[[str], None]) -> Any:
        with self.client.beta.messages.stream(
            model=self.settings.model,
            max_tokens=64000,
            system=self.system,  # type: ignore[arg-type]
            tools=self.tools,
            messages=self.messages,
            output_config=cast(Any, {"effort": self.settings.effort}),
            cache_control={"type": "ephemeral"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            for event in stream:
                if event.type == "text":
                    on_text(event.text)
                elif (
                    event.type == "content_block_start"
                    and event.content_block.type == "server_tool_use"
                ):
                    on_status("searching the web")
            return stream.get_final_message()


def describe_api_error(exc: anthropic.APIError) -> str:
    if isinstance(exc, anthropic.AuthenticationError):
        return "Claude API key missing or invalid. Set ANTHROPIC_API_KEY (see README)."
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limited by the Claude API; wait a moment and try again."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Couldn't reach the Claude API. Check your internet connection."
    if isinstance(exc, anthropic.APIStatusError):
        return f"Claude API error {exc.status_code}: {exc.message}"
    return f"Claude API error: {exc}"
