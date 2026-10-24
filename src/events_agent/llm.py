"""Claude client setup."""

from __future__ import annotations

import anthropic

from events_agent.config import Settings


def make_client(settings: Settings) -> anthropic.Anthropic:
    # With no key in settings the SDK falls back to its own credential lookup
    # (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile).
    if settings.anthropic_api_key:
        return anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return anthropic.Anthropic()
