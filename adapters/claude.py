"""Claude Code session adapter."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar

from .base import (
    Event,
    SessionAdapter,
    SessionCandidate,
    extract_content_text,
    first_useful_summary,
    iter_jsonl,
    parse_json_timestamp,
    short_id,
    stringify_content,
)


INTERNAL_PREFIXES = (
    "<local-command-caveat>",
    "<command-name>",
    "<system-reminder>",
)


def is_internal_text(text: str) -> bool:
    stripped = text.strip()
    return any(stripped.startswith(prefix) for prefix in INTERNAL_PREFIXES)


class ClaudeAdapter(SessionAdapter):
    name: ClassVar[str] = "claude"
    default_source: ClassVar[Path] = Path.home() / ".claude" / "projects"
    color: ClassVar[str] = "#b05a2a"

    def discover(self, source: Path, **opts: Any) -> list[SessionCandidate]:
        include_subagents = opts.get("include_subagents", True)
        if not source.exists():
            return []
        candidates: list[SessionCandidate] = []
        for path in sorted(source.glob("**/*.jsonl")):
            is_subagent = "/subagents/" in str(path)
            if is_subagent and not include_subagents:
                continue
            candidate = _scan_session(path, is_subagent=is_subagent)
            if candidate:
                candidates.append(candidate)
        return candidates

    def parse_events(self, candidate: SessionCandidate, path: Path) -> list[Event]:
        events: list[Event] = []
        for _, obj in iter_jsonl(path):
            timestamp = parse_json_timestamp(obj.get("timestamp"))
            if not timestamp:
                continue
            cwd = obj.get("cwd") if isinstance(obj.get("cwd"), str) else candidate.cwd
            obj_type = obj.get("type")

            if obj.get("isMeta") or obj_type in ("file-history-snapshot", "attachment"):
                continue

            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            content = message.get("content")

            if obj_type == "system":
                body = stringify_content(obj.get("content"))
                if body:
                    events.append(
                        Event(
                            session_id=candidate.lane_id,
                            provider=self.name,
                            timestamp=timestamp,
                            role="system",
                            kind="status",
                            title=obj.get("subtype") or "System",
                            body=body,
                            cwd=cwd,
                        )
                    )
                continue

            if isinstance(content, str):
                if is_internal_text(content):
                    continue
                role = "user" if obj_type == "user" else "assistant"
                events.append(
                    Event(
                        session_id=candidate.lane_id,
                        provider=self.name,
                        timestamp=timestamp,
                        role=role,
                        kind="message",
                        title=role.title(),
                        body=content,
                        cwd=cwd,
                    )
                )
                continue

            if not isinstance(content, list):
                continue

            for index, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                event_time = timestamp + timedelta(microseconds=index)

                if block_type == "text":
                    text = stringify_content(block.get("text"))
                    if not text or is_internal_text(text):
                        continue
                    role = "user" if obj_type == "user" else "assistant"
                    events.append(
                        Event(
                            session_id=candidate.lane_id,
                            provider=self.name,
                            timestamp=event_time,
                            role=role,
                            kind="message",
                            title=role.title(),
                            body=text,
                            cwd=cwd,
                        )
                    )
                elif block_type == "thinking":
                    thinking = stringify_content(block.get("thinking"))
                    if thinking:
                        events.append(
                            Event(
                                session_id=candidate.lane_id,
                                provider=self.name,
                                timestamp=event_time,
                                role="assistant",
                                kind="thinking",
                                title="Thinking",
                                body=thinking,
                                cwd=cwd,
                            )
                        )
                elif block_type == "tool_use":
                    name = stringify_content(block.get("name")) or "Tool"
                    events.append(
                        Event(
                            session_id=candidate.lane_id,
                            provider=self.name,
                            timestamp=event_time,
                            role="tool",
                            kind=self.classify_tool(name),
                            title=name,
                            body=self.format_tool_input(name, block.get("input")),
                            cwd=cwd,
                            call_id=stringify_content(block.get("id")) or None,
                        )
                    )
                elif block_type == "tool_result":
                    body = stringify_content(block.get("content"))
                    if body:
                        events.append(
                            Event(
                                session_id=candidate.lane_id,
                                provider=self.name,
                                timestamp=event_time,
                                role="tool",
                                kind="tool_result",
                                title="Tool result",
                                body=body,
                                cwd=cwd,
                                call_id=stringify_content(block.get("tool_use_id")) or None,
                                is_error=bool(block.get("is_error")),
                            )
                        )
                elif block_type == "image":
                    events.append(
                        Event(
                            session_id=candidate.lane_id,
                            provider=self.name,
                            timestamp=event_time,
                            role="user" if obj_type == "user" else "assistant",
                            kind="message",
                            title="Image",
                            body="[image block omitted from joined transcript]",
                            cwd=cwd,
                        )
                    )

        return events


def _scan_session(path: Path, is_subagent: bool) -> SessionCandidate | None:
    started_at: datetime | None = None
    ended_at: datetime | None = None
    cwd = ""
    summary = ""
    session_id = path.stem
    parent_session_id = None
    agent_id = None

    if is_subagent:
        agent_id = path.stem.removeprefix("agent-")
        parent_session_id = path.parent.parent.name
        session_id = f"{parent_session_id}/{path.stem}"

    for _, obj in iter_jsonl(path):
        timestamp = parse_json_timestamp(obj.get("timestamp"))
        if timestamp:
            started_at = min(started_at, timestamp) if started_at else timestamp
            ended_at = max(ended_at, timestamp) if ended_at else timestamp

        if not cwd and isinstance(obj.get("cwd"), str):
            cwd = obj["cwd"]

        if obj.get("sessionId") and not is_subagent:
            session_id = str(obj["sessionId"])

        if is_subagent and not agent_id and obj.get("agentId"):
            agent_id = str(obj["agentId"])

        if not summary and obj.get("type") == "user" and not obj.get("isMeta"):
            content = obj.get("message", {}).get("content")
            text = extract_content_text(content)
            if text and not is_internal_text(text):
                summary = first_useful_summary(text)

    if not started_at and not ended_at:
        return None

    if not cwd:
        cwd = _decode_project_folder(path)

    label_id = agent_id or path.stem if is_subagent else session_id
    label = f"Claude {short_id(label_id)}"

    description = ""
    meta_path = path.with_suffix(".meta.json")
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            description = stringify_content(meta.get("description"))
        except (OSError, json.JSONDecodeError):
            pass

    return SessionCandidate(
        provider=ClaudeAdapter.name,
        source_path=path,
        copied_path=None,
        session_id=session_id,
        lane_id=f"claude:{session_id}",
        label=label,
        cwd=cwd,
        started_at=started_at,
        ended_at=ended_at or started_at,
        summary=summary,
        copied_extra_paths=[meta_path] if meta_path.exists() else [],
        extras={
            "is_subagent": is_subagent,
            "parent_session_id": parent_session_id,
            "agent_id": agent_id,
            "description": description,
        },
    )


def _decode_project_folder(path: Path) -> str:
    for parent in path.parents:
        if parent.parent.name == "projects":
            name = parent.name
            if name.startswith("-"):
                return "/" + name[1:].replace("-", "/")
            return name
    return ""
