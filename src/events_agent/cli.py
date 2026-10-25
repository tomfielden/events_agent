"""Command line: chat, refresh, and source management."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic

from events_agent import sources
from events_agent.config import CONFIG_FILE, Settings, load_settings
from events_agent.scoring import rank
from events_agent.store import Store

CONFIG_TEMPLATE = """\
# Settings for events-agent. API keys go in .env, not here.

model = "claude-opus-5-5"
effort = "medium"      # low | medium | high: how hard Claude thinks on each chat turn
days_ahead = 60        # how far ahead sources look

[location]
city = "San Francisco"
state = "CA"
country = "US"
latitude = 37.7749
longitude = -122.4194
radius_miles = 30
timezone = "America/Los_Angeles"
"""

DIM, RESET = ("\033[2m", "\033[0m") if sys.stdout.isatty() else ("", "")


def open_store(settings: Settings) -> Store:
    return Store(settings.db_path, ZoneInfo(settings.location.timezone))


def cmd_init(settings: Settings, _: argparse.Namespace) -> None:
    path = Path(CONFIG_FILE)
    if path.exists():
        print(f"{path} already exists; leaving it alone.")
    else:
        path.write_text(CONFIG_TEMPLATE)
        print(f"Wrote {path}. Edit [location] to match where you live.")
    store = open_store(settings)
    have = {s["kind"] for s in store.list_sources()}
    for kind, name, key in (
        ("ticketmaster", "Ticketmaster", settings.ticketmaster_api_key),
        ("tmdb", "Movies (TMDB)", settings.tmdb_api_key),
    ):
        if kind in have:
            continue
        store.add_source(name, kind)
        note = "" if key else f" (needs {kind.upper()}_API_KEY in .env before it will work)"
        print(f"Added source: {name}{note}")
    print("Next: run `events-agent refresh`, then `events-agent chat`.")


def cmd_refresh(settings: Settings, args: argparse.Namespace) -> None:
    store = open_store(settings)
    if not store.list_sources():
        print("No sources yet. Run `events-agent init` or add one with `events-agent add-source`.")
        return
    for result in sources.refresh(store, settings, args.source):
        print(result)


def cmd_sources(settings: Settings, _: argparse.Namespace) -> None:
    store = open_store(settings)
    for s in store.list_sources():
        state = "on " if s["enabled"] else "off"
        print(f"[{s['id']}] {state} {s['name']} ({s['kind']}) {s['url']}")
        print(
            f"      {s['event_count']} events; last run {s['last_run_at'] or 'never'}: "
            f"{s['last_status'] or ''}"
        )


def cmd_add_source(settings: Settings, args: argparse.Namespace) -> None:
    if args.kind not in sources.SOURCE_KINDS:
        sys.exit(f"kind must be one of {', '.join(sources.SOURCE_KINDS)}")
    store = open_store(settings)
    try:
        config = json.loads(args.options) if args.options else {}
    except json.JSONDecodeError as exc:
        sys.exit(f"--options must be JSON: {exc}")
    sid = store.add_source(args.name, args.kind, args.url or "", config)
    print(f"Added source {sid}. Fetching...")
    for result in sources.refresh(store, settings, [sid]):
        print(result)


def cmd_upcoming(settings: Settings, args: argparse.Namespace) -> None:
    """Ranked list without calling Claude: a quick check of what the sources found."""
    store = open_store(settings)
    today = datetime.now(ZoneInfo(settings.location.timezone)).date()
    events = store.find_events(today.isoformat(), (today + timedelta(days=args.days)).isoformat())
    ranked = rank(events, store.list_preferences(), store.list_feedback())
    for r in ranked[: args.limit]:
        ev = r.event
        print(
            f"{r.score:+5.1f}  [{ev['id']}] {ev['start_at']:<16}  {ev['category']:<8} "
            f"{ev['title'][:60]}  @ {ev['venue'][:30]}"
        )
    print(f"{DIM}{len(events)} events in the next {args.days} days{RESET}")


def cmd_chat(settings: Settings, _: argparse.Namespace) -> None:
    from events_agent.agent import Agent, describe_api_error

    store = open_store(settings)
    agent = Agent(settings, store)
    print("Events concierge. Ask what's on, tell me what you like, or give me a site to watch.")
    print(f"{DIM}Type 'quit' to leave.{RESET}\n")

    def on_text(text: str) -> None:
        print(text, end="", flush=True)

    def on_status(status: str) -> None:
        print(f"\n{DIM}· {status}…{RESET}", flush=True)

    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.lower() in {"quit", "exit", "bye"}:
            break
        if not line:
            continue
        print()
        try:
            agent.send(line, on_text, on_status)
        except anthropic.APIError as exc:
            print(f"\n{describe_api_error(exc)}")
        except TypeError as exc:
            if "authentication" not in str(exc):
                raise
            print("No Claude credentials found. Put ANTHROPIC_API_KEY=... in .env (see README).")
            break
        except KeyboardInterrupt:
            print("\n[interrupted]")
        print("\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="events-agent", description=__doc__)
    parser.add_argument("--config", type=Path, help=f"settings file (default {CONFIG_FILE})")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("chat", help="talk to the concierge (default)")
    sub.add_parser("init", help="write a settings file and add the built-in API sources")
    p = sub.add_parser("refresh", help="pull new events from sources")
    p.add_argument("--source", type=int, action="append", help="source id (repeatable)")
    sub.add_parser("sources", help="list sources")
    p = sub.add_parser("add-source", help="add a source and fetch it")
    p.add_argument("name")
    p.add_argument("kind", help=" | ".join(sources.SOURCE_KINDS))
    p.add_argument("url", nargs="?")
    p.add_argument(
        "--options", help='source settings as JSON, e.g. \'{"theatre": "kickstandcomedy"}\''
    )
    p = sub.add_parser("upcoming", help="ranked upcoming events, without Claude")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--limit", type=int, default=30)

    args = parser.parse_args(argv)
    settings = load_settings(args.config)
    commands = {
        "init": cmd_init,
        "refresh": cmd_refresh,
        "sources": cmd_sources,
        "add-source": cmd_add_source,
        "upcoming": cmd_upcoming,
        "chat": cmd_chat,
    }
    commands[args.command or "chat"](settings, args)
