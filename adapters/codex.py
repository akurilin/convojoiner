"""Codex session adapter."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

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


INTERNAL_PREFIXES = (
    "<environment_context>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<apps_instructions>",
    "<skills_instructions>",
    "<plugins_instructions>",
    "# AGENTS.md instructions",
    "<INSTRUCTIONS>",
)

ISO_FILENAME_RE = re.compile(r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})")
EXIT_CODE_RE = re.compile(r"(?:Process exited with code|Exit code)\s+(-?\d+)")


def is_internal_text(text: str) -> bool:
    stripped = text.strip()
    return any(stripped.startswith(prefix) for prefix in INTERNAL_PREFIXES)


class CodexAdapter(SessionAdapter):
    name: ClassVar[str] = "codex"
    default_source: ClassVar[Path] = Path.home() / ".codex" / "sessions"
    color: ClassVar[str] = "#1d6b6b"

    def discover(self, source: Path, **opts: Any) -> list[SessionCandidate]:
        if not source.exists():
            return []
        candidates: list[SessionCandidate] = []
        for path in sorted(source.glob("**/*.jsonl")):
            candidate = _scan_session(path)
            if candidate:
                candidates.append(candidate)
        return candidates

    def parse_events(self, candidate: SessionCandidate, path: Path) -> list[Event]:
        output_call_ids = _collect_response_output_call_ids(path)
        events: list[Event] = []
        for _, obj in iter_jsonl(path):
            timestamp = parse_json_timestamp(obj.get("timestamp"))
            if not timestamp:
                continue
            obj_type = obj.get("type")
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}

            if obj_type == "response_item":
                events.extend(self._parse_response_item(candidate, payload, timestamp))
                continue

            if obj_type != "event_msg":
                continue

            payload_type = payload.get("type")
            if payload_type in ("token_count", "task_started"):
                continue
            if payload_type in ("user_message", "agent_message"):
                continue
            if payload.get("call_id") in output_call_ids and payload_type in (
                "exec_command_end",
                "patch_apply_end",
            ):
                continue

            event = self._parse_event_msg(candidate, payload, timestamp)
            if event:
                events.append(event)
        return events

    def _parse_response_item(
        self, candidate: SessionCandidate, payload: dict[str, Any], timestamp: datetime
    ) -> list[Event]:
        payload_type = payload.get("type")
        events: list[Event] = []

        if payload_type == "message":
            role = stringify_content(payload.get("role")) or "assistant"
            if role in ("developer", "system"):
                return []
            text = extract_content_text(payload.get("content"))
            if not text:
                return []
            if role == "user" and is_internal_text(text):
                return []
            events.append(
                Event(
                    session_id=candidate.lane_id,
                    provider=self.name,
                    timestamp=timestamp,
                    role=role,
                    kind="message",
                    title=role.title(),
                    body=text,
                    cwd=candidate.cwd,
                )
            )
            return events

        if payload_type == "function_call":
            name = stringify_content(payload.get("name")) or "function_call"
            args = parse_json_maybe(payload.get("arguments"))
            events.append(
                Event(
                    session_id=candidate.lane_id,
                    provider=self.name,
                    timestamp=timestamp,
                    role="tool",
                    kind=self.classify_tool(name),
                    title=name,
                    body=self.format_tool_input(name, args),
                    cwd=candidate.cwd,
                    call_id=stringify_content(payload.get("call_id")) or None,
                )
            )
            return events

        if payload_type == "function_call_output":
            output = stringify_content(payload.get("output"))
            if output:
                events.append(
                    Event(
                        session_id=candidate.lane_id,
                        provider=self.name,
                        timestamp=timestamp,
                        role="tool",
                        kind="tool_result",
                        title="Tool result",
                        body=output,
                        cwd=candidate.cwd,
                        call_id=stringify_content(payload.get("call_id")) or None,
                        is_error=_looks_like_error_output(output),
                    )
                )
            return events

        if payload_type == "web_search_call":
            events.append(
                Event(
                    session_id=candidate.lane_id,
                    provider=self.name,
                    timestamp=timestamp,
                    role="tool",
                    kind="tool_use",
                    title="Web search",
                    body=stringify_content(payload),
                    cwd=candidate.cwd,
                )
            )
            return events

        return events

    def _parse_event_msg(
        self, candidate: SessionCandidate, payload: dict[str, Any], timestamp: datetime
    ) -> Event | None:
        payload_type = payload.get("type")
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else candidate.cwd

        if payload_type == "exec_command_end":
            command = payload.get("command")
            body = stringify_content(payload.get("aggregated_output"))
            if not body:
                body = _combine_stdout_stderr(payload)
            title = _command_to_title(command) or "Command"
            return Event(
                session_id=candidate.lane_id,
                provider=self.name,
                timestamp=timestamp,
                role="tool",
                kind="tool_result",
                title=title,
                body=body or "(no output)",
                cwd=cwd,
                call_id=stringify_content(payload.get("call_id")) or None,
                is_error=payload.get("exit_code") not in (None, 0),
            )

        if payload_type == "patch_apply_end":
            body = _combine_stdout_stderr(payload) or stringify_content(payload)
            return Event(
                session_id=candidate.lane_id,
                provider=self.name,
                timestamp=timestamp,
                role="tool",
                kind="file_edit",
                title="Patch applied",
                body=body,
                cwd=cwd,
                call_id=stringify_content(payload.get("call_id")) or None,
                is_error=payload.get("status") not in (None, "completed", "success"),
            )

        if payload_type == "web_search_end":
            action = payload.get("action")
            return Event(
                session_id=candidate.lane_id,
                provider=self.name,
                timestamp=timestamp,
                role="tool",
                kind="tool_result",
                title="Web result",
                body=stringify_content(action or payload),
                cwd=cwd,
                call_id=stringify_content(payload.get("call_id")) or None,
            )

        if payload_type in ("task_complete", "turn_aborted"):
            title = "Turn complete" if payload_type == "task_complete" else "Turn aborted"
            body = stringify_content(payload.get("last_agent_message") or payload)
            return Event(
                session_id=candidate.lane_id,
                provider=self.name,
                timestamp=timestamp,
                role="system",
                kind="status",
                title=title,
                body=body,
                cwd=cwd,
                is_error=payload_type == "turn_aborted",
            )

        return None


def _scan_session(path: Path) -> SessionCandidate | None:
    started_at: datetime | None = None
    ended_at: datetime | None = None
    cwd = ""
    session_id = path.stem
    summary = ""

    inferred = _infer_time_from_filename(path)
    if inferred:
        started_at = inferred
        ended_at = inferred

    for _, obj in iter_jsonl(path):
        timestamp = parse_json_timestamp(obj.get("timestamp"))
        if timestamp:
            started_at = min(started_at, timestamp) if started_at else timestamp
            ended_at = max(ended_at, timestamp) if ended_at else timestamp

        obj_type = obj.get("type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if obj_type == "session_meta":
            if payload.get("id"):
                session_id = str(payload["id"])
            if not cwd and isinstance(payload.get("cwd"), str):
                cwd = payload["cwd"]
            meta_time = parse_json_timestamp(payload.get("timestamp"))
            if meta_time:
                started_at = min(started_at, meta_time) if started_at else meta_time
                ended_at = max(ended_at, meta_time) if ended_at else meta_time

        if not cwd and obj_type == "turn_context" and isinstance(payload.get("cwd"), str):
            cwd = payload["cwd"]

        if not summary and obj_type == "response_item":
            if payload.get("type") == "message" and payload.get("role") == "user":
                text = extract_content_text(payload.get("content"))
                if text and not is_internal_text(text):
                    summary = first_useful_summary(text)

        if not summary and obj_type == "event_msg" and payload.get("type") == "user_message":
            text = stringify_content(payload.get("message"))
            if text and not is_internal_text(text):
                summary = first_useful_summary(text)

    if not started_at and not ended_at:
        return None

    return SessionCandidate(
        provider=CodexAdapter.name,
        source_path=path,
        session_id=session_id,
        lane_id=f"codex:{session_id}",
        label=f"Codex {short_id(session_id)}",
        cwd=cwd,
        started_at=started_at,
        ended_at=ended_at or started_at,
        summary=summary,
    )


def _infer_time_from_filename(path: Path) -> datetime | None:
    match = ISO_FILENAME_RE.search(path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%dT%H-%M-%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _collect_response_output_call_ids(path: Path) -> set[str]:
    call_ids: set[str] = set()
    for _, obj in iter_jsonl(path):
        if obj.get("type") != "response_item":
            continue
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if payload.get("type") == "function_call_output" and payload.get("call_id"):
            call_ids.add(str(payload["call_id"]))
    return call_ids


def _combine_stdout_stderr(payload: dict[str, Any]) -> str:
    parts = []
    stdout = stringify_content(payload.get("stdout"))
    stderr = stringify_content(payload.get("stderr"))
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(stderr)
    return "\n\n".join(parts).strip()


def _command_to_title(command: Any) -> str:
    if isinstance(command, list):
        if len(command) >= 3 and command[0].endswith("zsh") and command[1] == "-lc":
            return str(command[2])
        return " ".join(str(part) for part in command)
    if isinstance(command, str):
        return command
    return ""


def _looks_like_error_output(output: str) -> bool:
    match = EXIT_CODE_RE.search(output)
    if match:
        try:
            return int(match.group(1)) != 0
        except ValueError:
            return False
    return False
