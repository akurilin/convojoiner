"""Provider adapter registry.

To add a new provider, create a module here subclassing SessionAdapter
and register an instance of it in ADAPTERS below.
"""

from __future__ import annotations

from .base import (
    Event,
    SessionAdapter,
    SessionCandidate,
    extract_content_text,
    first_useful_summary,
    iter_jsonl,
    parse_json_maybe,
    parse_json_timestamp,
    short_id,
    stringify_content,
)
from .claude import ClaudeAdapter
from .codex import CodexAdapter


ADAPTERS: dict[str, SessionAdapter] = {
    adapter.name: adapter for adapter in (ClaudeAdapter(), CodexAdapter())
}


__all__ = [
    "ADAPTERS",
    "Event",
    "SessionAdapter",
    "SessionCandidate",
    "extract_content_text",
    "first_useful_summary",
    "iter_jsonl",
    "parse_json_maybe",
    "parse_json_timestamp",
    "short_id",
    "stringify_content",
]
