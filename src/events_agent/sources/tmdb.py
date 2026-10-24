"""The Movie Database (TMDB): movies now playing and coming soon to theaters.

Free key: https://www.themoviedb.org/settings/api (set TMDB_API_KEY to either the
v3 API key or the v4 read access token).
Optional source config: {"lists": ["upcoming", "now_playing"], "pages": 3}
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from events_agent.models import Event
from events_agent.sources import FetchContext

API = "https://api.themoviedb.org/3"
# How long a movie is treated as "in theaters" after its release date.
THEATRICAL_RUN = timedelta(days=35)


def fetch(source: dict[str, Any], ctx: FetchContext) -> list[Event]:
    key = ctx.settings.tmdb_api_key
    if not key:
        raise RuntimeError("TMDB_API_KEY is not set")
    # v4 read tokens are long JWTs sent as a bearer header; v3 keys go in the query.
    auth_headers = {"Authorization": f"Bearer {key}"} if len(key) > 40 else {}
    auth_params = {} if auth_headers else {"api_key": key}

    def get(path: str, **params: Any) -> dict[str, Any]:
        resp = ctx.http.get(f"{API}{path}", params={**auth_params, **params}, headers=auth_headers)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    genres = {g["id"]: g["name"] for g in get("/genre/movie/list", language="en-US")["genres"]}
    cfg = source["config"]
    region = ctx.settings.location.country
    _, window_end = ctx.window

    seen: dict[int, Event] = {}
    for list_name in cfg.get("lists", ["now_playing", "upcoming"]):
        for page in range(1, int(cfg.get("pages", 3)) + 1):
            data = get(f"/movie/{list_name}", region=region, language="en-US", page=page)
            for m in data.get("results", []):
                if m["id"] in seen or not m.get("release_date"):
                    continue
                released = date.fromisoformat(m["release_date"])
                if released > window_end:
                    continue
                movie_genres = [genres.get(g, "") for g in m.get("genre_ids", [])]
                seen[m["id"]] = Event(
                    title=m.get("title", ""),
                    start=released,
                    end=released + THEATRICAL_RUN,
                    category="movie",
                    description=m.get("overview", ""),
                    venue="In theaters",
                    url=f"https://www.themoviedb.org/movie/{m['id']}",
                    image_url=(
                        f"https://image.tmdb.org/t/p/w500{m['poster_path']}"
                        if m.get("poster_path")
                        else ""
                    ),
                    tags=[g for g in movie_genres if g]
                    + (["family-friendly"] if "Family" in movie_genres else []),
                    external_id=f"tmdb-{m['id']}",
                )
            if page >= data.get("total_pages", 0):
                break
    return list(seen.values())
