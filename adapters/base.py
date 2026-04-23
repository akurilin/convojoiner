"""Shared adapter base class, data contracts, and parsing helpers.

Invariant: source session files are strictly read-only. Adapters may only
*read* from `SessionCandidate.source_path` and its sidecars. Never write,
chmod, unlink, move, or otherwise mutate those paths — they are the user's
authoritative conversation history and belong to the originating tool
(Claude Code, Codex, etc.). If a future feature needs to transform content,
copy to a scratch location first and operate on the copy. See CLAUDE.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Iterable


@dataclass
class SessionCandidate:
    provider: str
    source_path: Path
    session_id: str
    lane_id: str
    label: str
    cwd: str
    started_at: datetime | None
    ended_at: datetime | None
    summary: str = ""
    repo_label: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class Event:
    session_id: str
    provider: str
    timestamp: datetime
    role: str
    kind: str
    title: str
    body: str
    cwd: str
    call_id: str | None = None
    is_error: bool = False


class SessionAdapter:
    """Provider adapter. Subclasses declare name/default_source/color and
    implement discover() and parse_events()."""

    name: ClassVar[str]
    default_source: ClassVar[Path]
    color: ClassVar[str]

    def discover(self, source: Path, **opts: Any) -> list[SessionCandidate]:
        raise NotImplementedError

    def parse_events(self, candidate: SessionCandidate, path: Path) -> list[Event]:
        raise NotImplementedError

    def classify_tool(self, name: str) -> str:
        return default_classify_tool(name)

    def format_tool_input(self, name: str, value: Any) -> str:
        return default_format_tool_input(name, value)


def default_classify_tool(name: str) -> str:
    lower = name.lower()
    if lower in {"bash", "exec_command", "shell", "terminal"}:
        return "command"
    if lower in {"edit", "multiedit", "write", "apply_patch"} or "patch" in lower:
        return "file_edit"
    return "tool_use"


def default_format_tool_input(name: str, value: Any) -> str:
    if not isinstance(value, dict):
        return stringify_content(value)

    lower = name.lower()
    if lower in {"bash", "exec_command"}:
        command = value.get("command") or value.get("cmd")
        workdir = value.get("workdir")
        description = value.get("description")
        parts = []
        if description:
            parts.append(f"# {description}")
        if workdir:
            parts.append(f"# cwd: {workdir}")
        if isinstance(command, list):
            parts.append(" ".join(str(part) for part in command))
        elif command:
            parts.append(str(command))
        if parts:
            return "\n".join(parts)

    if lower in {"edit", "multiedit", "write"}:
        file_path = value.get("file_path") or value.get("path")
        pieces = []
        if file_path:
            pieces.append(f"File: {file_path}")
        for key in ("old_string", "new_string", "content", "edits"):
            if key in value:
                pieces.append(f"\n{key}:\n{stringify_content(value[key])}")
        if pieces:
            return "\n".join(pieces)

    return json.dumps(value, indent=2, ensure_ascii=False)


def iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    # Read-only by contract: `path` is a user-owned session file. See module
    # docstring and CLAUDE.md.
    try:
        with path.open("r", encoding="utf-8") as fp:
            for line_number, line in enumerate(fp, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield line_number, json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def parse_json_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def extract_content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in ("text", "input_text", "output_text"):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif block_type == "thinking":
                thinking = block.get("thinking")
                if isinstance(thinking, str):
                    parts.append(thinking)
            elif block_type == "tool_result":
                result_text = stringify_content(block.get("content"))
                if result_text:
                    parts.append(result_text)
        return "\n\n".join(part.strip() for part in parts if part and part.strip())
    return stringify_content(content)


def stringify_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, indent=2, ensure_ascii=False)
        except TypeError:
            return str(value)
    return str(value)


def first_useful_summary(text: str, max_chars: int = 180) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 3].rstrip() + "..."


def short_id(value: str | None) -> str:
    if not value:
        return "session"
    value = value.split("/")[-1]
    return value[:8]


def parse_json_maybe(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value
