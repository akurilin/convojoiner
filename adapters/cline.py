"""Cline (saoudrizwan.claude-dev VS Code extension) session adapter.

Sessions live in VS Code's global storage at
~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/
with this layout:

    <source>/state/taskHistory.json        # index with per-task cwd
    <source>/tasks/<taskId>/ui_messages.json
    <source>/tasks/<taskId>/api_conversation_history.json
    <source>/tasks/<taskId>/task_metadata.json
    <source>/tasks/<taskId>/settings.json

ui_messages.json is our primary source because it has a ts (epoch ms)
on every record. The api_conversation_history.json has the full LLM
exchange but per-message timestamps are optional.

Older tasks use claude_messages.json instead of ui_messages.json; the
extension auto-migrates on open but we support both names.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

from .base import (
    Event,
    SessionAdapter,
    SessionCandidate,
    first_useful_summary,
    short_id,
    stringify_content,
)


DEFAULT_SOURCE = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Code"
    / "User"
    / "globalStorage"
    / "saoudrizwan.claude-dev"
)

UI_FILENAMES = ("ui_messages.json", "claude_messages.json")

NOISY_SAY_TYPES = frozenset({"api_req_started", "api_req_finished"})


class ClineAdapter(SessionAdapter):
    name: ClassVar[str] = "cline"
    default_source: ClassVar[Path] = DEFAULT_SOURCE
    color: ClassVar[str] = "#6b46c1"

    def discover(self, source: Path, **opts: Any) -> list[SessionCandidate]:
        if not source.exists():
            return []
        history = _load_task_history(source)
        candidates: list[SessionCandidate] = []
        for ui_path in _iter_task_ui_files(source):
            candidate = _scan_task(ui_path, history)
            if candidate:
                candidates.append(candidate)
        return candidates

    def parse_events(self, candidate: SessionCandidate, path: Path) -> list[Event]:
        events: list[Event] = []
        for message in _read_ui_messages(path):
            event = self._message_to_event(candidate, message)
            if event:
                events.append(event)
        return events

    def _message_to_event(
        self, candidate: SessionCandidate, msg: dict[str, Any]
    ) -> Event | None:
        timestamp = _epoch_ms_to_datetime(msg.get("ts"))
        if not timestamp:
            return None

        msg_type = msg.get("type")
        if msg_type not in ("ask", "say"):
            return None

        subtype = msg.get(msg_type)
        text = stringify_content(msg.get("text"))
        reasoning = stringify_content(msg.get("reasoning"))

        if msg_type == "say" and subtype in NOISY_SAY_TYPES:
            return None

        if msg_type == "say" and subtype in {"user_feedback", "task"}:
            if not text:
                return None
            return _event(self, candidate, timestamp,
                          role="user", kind="message", title="User", body=text)

        if (msg_type == "say" and subtype == "text") or (
            msg_type == "ask" and subtype == "followup"
        ):
            if not text:
                return None
            return _event(self, candidate, timestamp,
                          role="assistant", kind="message", title="Assistant", body=text)

        if msg_type == "say" and subtype == "reasoning":
            body = reasoning or text
            if not body:
                return None
            return _event(self, candidate, timestamp,
                          role="assistant", kind="thinking", title="Thinking", body=body)

        if subtype == "command":
            if not text:
                return None
            return _event(
                self, candidate, timestamp,
                role="tool",
                kind="command" if msg_type == "ask" else "tool_result",
                title="Command" if msg_type == "ask" else "Command (approved)",
                body=text,
            )

        if msg_type == "say" and subtype == "command_output":
            if not text:
                return None
            return _event(self, candidate, timestamp,
                          role="tool", kind="tool_result", title="Command output", body=text)

        if subtype == "tool":
            if not text:
                return None
            return _event(
                self, candidate, timestamp,
                role="tool",
                kind="tool_use" if msg_type == "ask" else "tool_result",
                title="Tool" if msg_type == "ask" else "Tool result",
                body=text,
            )

        if (msg_type == "say" and subtype == "error") or (
            msg_type == "ask" and subtype == "api_req_failed"
        ):
            if not text:
                return None
            return _event(self, candidate, timestamp,
                          role="system", kind="status", title="Error",
                          body=text, is_error=True)

        if subtype in {"completion_result", "resume_task"}:
            if not text:
                return None
            title = str(subtype).replace("_", " ").title()
            return _event(self, candidate, timestamp,
                          role="system", kind="status", title=title, body=text)

        # Fallback for any ask/say we haven't explicitly mapped.
        if text:
            title = str(subtype or msg_type).replace("_", " ").title()
            return _event(self, candidate, timestamp,
                          role="system", kind="status", title=title, body=text)

        return None


def _event(
    adapter: ClineAdapter,
    candidate: SessionCandidate,
    timestamp: datetime,
    *,
    role: str,
    kind: str,
    title: str,
    body: str,
    is_error: bool = False,
) -> Event:
    return Event(
        session_id=candidate.lane_id,
        provider=adapter.name,
        timestamp=timestamp,
        role=role,
        kind=kind,
        title=title,
        body=body,
        cwd=candidate.cwd,
        is_error=is_error,
    )


def _load_task_history(source: Path) -> dict[str, dict[str, Any]]:
    history_path = source / "state" / "taskHistory.json"
    if not history_path.exists():
        return {}
    try:
        data = json.loads(history_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, list):
        return {}
    return {
        str(item["id"]): item
        for item in data
        if isinstance(item, dict) and "id" in item
    }


def _iter_task_ui_files(source: Path) -> list[Path]:
    tasks_dir = source / "tasks"
    if not tasks_dir.exists():
        return []
    results: list[Path] = []
    for task_dir in sorted(tasks_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        for filename in UI_FILENAMES:
            candidate = task_dir / filename
            if candidate.exists():
                results.append(candidate)
                break
    return results


def _scan_task(
    ui_path: Path, history: dict[str, dict[str, Any]]
) -> SessionCandidate | None:
    task_id = ui_path.parent.name
    messages = _read_ui_messages(ui_path)
    if not messages:
        return None

    timestamps = [_epoch_ms_to_datetime(m.get("ts")) for m in messages]
    timestamps = [t for t in timestamps if t is not None]
    if not timestamps:
        return None

    hist = history.get(task_id, {})
    cwd = str(hist.get("cwdOnTaskInitialization") or "")

    summary = ""
    for m in messages:
        if m.get("type") == "say" and m.get("say") in {"user_feedback", "task"}:
            text = stringify_content(m.get("text"))
            if text:
                summary = first_useful_summary(text)
                break
    if not summary and hist.get("task"):
        summary = first_useful_summary(str(hist["task"]))

    return SessionCandidate(
        provider=ClineAdapter.name,
        source_path=ui_path,
        session_id=task_id,
        lane_id=f"cline:{task_id}",
        label=f"Cline {short_id(task_id)}",
        cwd=cwd,
        started_at=min(timestamps),
        ended_at=max(timestamps),
        summary=summary,
    )


def _read_ui_messages(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [m for m in data if isinstance(m, dict)]


def _epoch_ms_to_datetime(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None
