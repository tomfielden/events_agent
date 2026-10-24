"""Settings, loaded from events_agent.toml and environment variables.

API keys come from the environment (or a .env file in the working directory) so they
never end up in the settings file.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_FILE = "events_agent.toml"


@dataclass
class Location:
    city: str = "San Francisco"
    state: str = "CA"
    country: str = "US"
    latitude: float = 37.7749
    longitude: float = -122.4194
    radius_miles: int = 30
    timezone: str = "America/Los_Angeles"


@dataclass
class Settings:
    location: Location = field(default_factory=Location)
    data_dir: Path = Path("data")
    model: str = "claude-opus-5-5"
    # Effort for chat turns. Extraction from web pages always runs at "low".
    effort: str = "medium"
    days_ahead: int = 60
    anthropic_api_key: str | None = None
    ticketmaster_api_key: str | None = None
    tmdb_api_key: str | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "events.db"


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=value lines; existing environment variables win."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.split(" #", 1)[0].strip().strip("'\"")  # drop trailing comments
        if value:
            os.environ.setdefault(key, value)


def load_settings(config_path: Path | None = None) -> Settings:
    _load_dotenv(Path(".env"))
    path = config_path or Path(os.environ.get("EVENTS_AGENT_CONFIG", CONFIG_FILE))
    raw: dict[str, object] = {}
    if path.exists():
        raw = tomllib.loads(path.read_text())

    settings = Settings()
    loc = raw.get("location")
    if isinstance(loc, dict):
        settings.location = Location(**{**settings.location.__dict__, **loc})
    for key in ("model", "effort"):
        if isinstance(raw.get(key), str):
            setattr(settings, key, raw[key])
    if isinstance(raw.get("days_ahead"), int):
        settings.days_ahead = int(str(raw["days_ahead"]))
    if isinstance(raw.get("data_dir"), str):
        settings.data_dir = Path(str(raw["data_dir"]))

    settings.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    settings.ticketmaster_api_key = os.environ.get("TICKETMASTER_API_KEY")
    settings.tmdb_api_key = os.environ.get("TMDB_API_KEY")
    return settings
