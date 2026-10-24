# events_agent

A chat agent that finds movies, live shows and events your family might enjoy. It pulls
listings from a growing list of websites and APIs, and it learns each person's likes and
dislikes as you talk to it.

## Quick start

```sh
uv sync
cp .env.example .env          # add your API keys
uv run events-agent init      # writes events_agent.toml and adds Ticketmaster + TMDB sources
# edit events_agent.toml: set [location] to where you live
uv run events-agent refresh   # pull events from all sources
uv run events-agent chat
```

Things to say in chat:

- "We're Tom, Alex, and the kids Maya (9) and Leo (6). Maya loves musicals, Leo is into dinosaurs."
- "What's good for the whole family in the next two weekends?"
- "Something for a date night, nothing loud."
- "We went to #42, the kids loved it." / "Not interested in #17."
- "Keep an eye on https://www.example-theater.org/calendar"
- "Is that show OK for a 6-year-old?" (it can search the web for reviews and age guidance)

Other commands: `events-agent sources`, `events-agent add-source NAME KIND [URL]`,
`events-agent upcoming` (ranked list without calling Claude).

To refresh every night, add a cron entry (`crontab -e`):

```
0 5 * * * cd /Users/tomfielden/Documents/events_agent && ~/.local/bin/uv run events-agent refresh
```

## How it works

```
 sources ──refresh──▶ SQLite (data/events.db) ◀── preferences & reactions
 (APIs, iCal,               │                          ▲
  web pages)                ▼                          │ saved by the agent
                     scorer ranks candidates           │ as you chat
                            │                          │
                            ▼                          │
                   Claude chat agent (tools) ──────────┘
```

- **Sources** (`src/events_agent/sources/`). Each kind is one module with a
  `fetch(source, ctx)` function. The list of sources lives in the database, so you or the
  agent can add more at any time.
  - `ticketmaster`: Ticketmaster Discovery API near your location.
  - `tmdb`: movies now playing and coming soon (treated as running for 5 weeks after release).
  - `ical`: any `.ics` calendar feed (libraries, museums, and many venues publish these).
  - `webpage`: any listings page. It first reads schema.org event data embedded in the page,
    which is exact and free. If the page has none, it sends the page text to Claude and asks
    it to pull out the events (costs one API call per page per refresh).
- **Store** (`store.py`): events, sources, family members, preferences, reactions.
- **Learning** (`scoring.py`): stated likes/dislikes match against event text, and reactions
  to past events lift or lower similar events (same performer/genre tags, venue, category).
  Dislikes count 1.5× a like. Events someone marked "not interested" are hidden.
- **Agent** (`agent.py`, `tools.py`): Claude (Opus 5.5) with tools to search events,
  save preferences and reactions, manage sources, and search the web. If Claude's safety
  checks decline a request, the API automatically retries it on a fallback model
  (`fallbacks: "default"`).

Settings are in `events_agent.toml`: location, search radius, how many days ahead to
look, model, and effort.

## Known limits

- Some sites block automated requests (for example SFJAZZ returns 403). The agent won't
  get around that; use Ticketmaster or an iCal feed for those venues instead.
- Pages that load their listings with JavaScript after the page opens show up empty.
  Look for the site's iCal feed or an underlying listings URL.
- Repeating iCal events (weekly story time, for example) only show their first date.
- Chat history isn't saved between sessions. What it learns about the family is.

## Development

This project uses [uv](https://docs.astral.sh/uv/) for Python, virtualenv, and dependency management.

```sh
uv sync                    # create .venv and install all dependencies
uv run events-agent        # start the chat
uv run pytest              # tests
uv run ruff check .        # lint
uv run ruff format .       # format
uv run mypy src tests      # type check
uv add <package>           # add a runtime dependency
uv add --dev <package>     # add a dev dependency
```

## Project setup history

This is how the development environment was first set up, for reference.

1. **Installed uv** with Astral's official installer (`curl -LsSf https://astral.sh/uv/install.sh | sh`). It installs to `~/.local/bin` and adds that folder to the PATH in `~/.zshrc`. Homebrew was skipped because it no longer ships prebuilt packages for Intel Macs.
2. **Initialized the project** with `uv init --package --name events-agent --python 3.13`. This created:
   - `pyproject.toml`: project metadata and an `events-agent` command that runs `events_agent:main`
   - `src/events_agent/`: the package, in a `src/` layout
   - `.python-version`: pins Python 3.13, which uv downloads and manages without pyenv
3. **Added dev tools** with `uv add --dev ruff mypy pytest`. This created the `.venv` virtual environment and the `uv.lock` lockfile, which is committed so installs are reproducible.
4. **Configured the tools** in `pyproject.toml`:
   - ruff: 100-character lines; lint rules E, F, W, I, B, UP, SIM, RUF
   - mypy: strict mode
   - pytest: runs the tests in `tests/`
5. **Added a starter test** at `tests/test_smoke.py`.
6. **Added VS Code settings** in `.vscode/`: use the `.venv` interpreter, turn on pytest, format with ruff on save, and suggest the Python, Ruff and Mypy extensions.

The existing `.gitignore` already ignored `.venv/` and `.env`.
