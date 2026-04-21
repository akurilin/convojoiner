#!/usr/bin/env python3
"""Generate a self-contained HTML timeline from Claude Code and Codex sessions."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import tempfile
import textwrap
import webbrowser
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_CLAUDE_SOURCE = Path.home() / ".claude" / "projects"
DEFAULT_CODEX_SOURCE = Path.home() / ".codex" / "sessions"
INTERNAL_CLAUDE_PREFIXES = (
    "<local-command-caveat>",
    "<command-name>",
    "<system-reminder>",
)
INTERNAL_CODEX_PREFIXES = (
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


@dataclass
class SessionCandidate:
    provider: str
    source_path: Path
    copied_path: Path | None
    session_id: str
    lane_id: str
    label: str
    cwd: str
    started_at: datetime | None
    ended_at: datetime | None
    summary: str = ""
    is_subagent: bool = False
    parent_session_id: str | None = None
    agent_id: str | None = None
    description: str = ""
    repo_label: str = ""
    copied_extra_paths: list[Path] = field(default_factory=list)


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Join Claude Code and Codex JSONL sessions into one HTML timeline."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("convojoiner.html"),
        help="Output HTML file. Default: ./convojoiner.html",
    )
    parser.add_argument(
        "--since",
        help="Include events at or after this date/datetime. YYYY-MM-DD uses local midnight.",
    )
    parser.add_argument(
        "--until",
        help=(
            "Include events before this date/datetime. YYYY-MM-DD includes that full local day."
        ),
    )
    parser.add_argument(
        "--repo-folder",
        action="append",
        default=[],
        help=(
            "Repo/worktree folder to include. Can be passed more than once; subfolders match."
        ),
    )
    parser.add_argument(
        "--provider",
        action="append",
        choices=("claude", "codex"),
        help="Provider to include. Repeatable. Default: both.",
    )
    parser.add_argument(
        "--claude-source",
        type=Path,
        default=DEFAULT_CLAUDE_SOURCE,
        help=f"Claude Code projects source. Default: {DEFAULT_CLAUDE_SOURCE}",
    )
    parser.add_argument(
        "--codex-source",
        type=Path,
        default=DEFAULT_CODEX_SOURCE,
        help=f"Codex sessions source. Default: {DEFAULT_CODEX_SOURCE}",
    )
    parser.add_argument(
        "--copy-root",
        type=Path,
        help="Directory for read-only source copies. Default: fresh /tmp/convojoiner-* dir.",
    )
    parser.add_argument(
        "--timezone",
        default="local",
        help="Display/filter timezone, e.g. Europe/Rome. Default: local system timezone.",
    )
    parser.add_argument(
        "--no-subagents",
        action="store_true",
        help="Exclude Claude Code subagent JSONL files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show selected sessions without copying sources or writing HTML.",
    )
    parser.add_argument("--open", action="store_true", help="Open the generated HTML file.")
    return parser.parse_args()


def get_display_tz(name: str) -> timezone:
    if name == "local":
        return datetime.now().astimezone().tzinfo or timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise SystemExit(f"Unknown timezone: {name}") from exc


def parse_cli_datetime(value: str | None, display_tz: timezone, is_until: bool) -> datetime | None:
    if not value:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            parsed_date = date.fromisoformat(value)
            local_dt = datetime.combine(parsed_date, time.min, tzinfo=display_tz)
            if is_until:
                local_dt += timedelta(days=1)
            return local_dt.astimezone(timezone.utc)

        normalized = value.replace("Z", "+00:00")
        parsed_dt = datetime.fromisoformat(normalized)
        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=display_tz)
        return parsed_dt.astimezone(timezone.utc)
    except ValueError as exc:
        raise SystemExit(f"Invalid datetime: {value}") from exc


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


def infer_time_from_codex_filename(path: Path) -> datetime | None:
    match = ISO_FILENAME_RE.search(path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%dT%H-%M-%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def iter_jsonl(path: Path):
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


def is_internal_claude_text(text: str) -> bool:
    stripped = text.strip()
    return any(stripped.startswith(prefix) for prefix in INTERNAL_CLAUDE_PREFIXES)


def is_internal_codex_text(text: str) -> bool:
    stripped = text.strip()
    return any(stripped.startswith(prefix) for prefix in INTERNAL_CODEX_PREFIXES)


def first_useful_summary(text: str, max_chars: int = 180) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 3].rstrip() + "..."


def discover_claude_sessions(source: Path, include_subagents: bool) -> list[SessionCandidate]:
    if not source.exists():
        return []
    candidates: list[SessionCandidate] = []
    for path in sorted(source.glob("**/*.jsonl")):
        if "/subagents/" in str(path):
            if not include_subagents:
                continue
            candidate = scan_claude_session(path, is_subagent=True)
        else:
            candidate = scan_claude_session(path, is_subagent=False)
        if candidate:
            candidates.append(candidate)
    return candidates


def scan_claude_session(path: Path, is_subagent: bool) -> SessionCandidate | None:
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
            if text and not is_internal_claude_text(text):
                summary = first_useful_summary(text)

    if not started_at and not ended_at:
        return None

    if not cwd:
        cwd = decode_claude_project_folder(path)

    label = f"Claude {short_id(session_id)}"
    if is_subagent:
        label = f"Claude agent {short_id(agent_id or path.stem)}"

    description = ""
    meta_path = path.with_suffix(".meta.json")
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            description = stringify_content(meta.get("description"))
            agent_type = stringify_content(meta.get("agentType"))
            if agent_type:
                label = f"Claude {agent_type} {short_id(agent_id or path.stem)}"
        except (OSError, json.JSONDecodeError):
            pass

    return SessionCandidate(
        provider="claude",
        source_path=path,
        copied_path=None,
        session_id=session_id,
        lane_id=f"claude:{session_id}",
        label=label,
        cwd=cwd,
        started_at=started_at,
        ended_at=ended_at or started_at,
        summary=summary,
        is_subagent=is_subagent,
        parent_session_id=parent_session_id,
        agent_id=agent_id,
        description=description,
        copied_extra_paths=[meta_path] if meta_path.exists() else [],
    )


def decode_claude_project_folder(path: Path) -> str:
    for parent in path.parents:
        if parent.parent.name == "projects":
            name = parent.name
            if name.startswith("-"):
                return "/" + name[1:].replace("-", "/")
            return name
    return ""


def discover_codex_sessions(source: Path) -> list[SessionCandidate]:
    if not source.exists():
        return []
    candidates: list[SessionCandidate] = []
    for path in sorted(source.glob("**/*.jsonl")):
        candidate = scan_codex_session(path)
        if candidate:
            candidates.append(candidate)
    return candidates


def scan_codex_session(path: Path) -> SessionCandidate | None:
    started_at: datetime | None = None
    ended_at: datetime | None = None
    cwd = ""
    session_id = path.stem
    summary = ""

    inferred = infer_time_from_codex_filename(path)
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
                if text and not is_internal_codex_text(text):
                    summary = first_useful_summary(text)

        if not summary and obj_type == "event_msg" and payload.get("type") == "user_message":
            text = stringify_content(payload.get("message"))
            if text and not is_internal_codex_text(text):
                summary = first_useful_summary(text)

    if not started_at and not ended_at:
        return None

    return SessionCandidate(
        provider="codex",
        source_path=path,
        copied_path=None,
        session_id=session_id,
        lane_id=f"codex:{session_id}",
        label=f"Codex {short_id(session_id)}",
        cwd=cwd,
        started_at=started_at,
        ended_at=ended_at or started_at,
        summary=summary,
    )


def short_id(value: str | None) -> str:
    if not value:
        return "session"
    value = value.split("/")[-1]
    return value[:8]


def session_overlaps_range(
    candidate: SessionCandidate, since: datetime | None, until: datetime | None
) -> bool:
    start = candidate.started_at
    end = candidate.ended_at or start
    if not start or not end:
        return False
    if since and end < since:
        return False
    if until and start >= until:
        return False
    return True


def normalize_repo_folders(values: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        path = os.path.abspath(os.path.expanduser(value))
        normalized.append(path.rstrip(os.sep) or os.sep)
    return normalized


def cwd_matches_repo(cwd: str, repo_folders: list[str]) -> bool:
    if not repo_folders:
        return True
    if not cwd:
        return False
    cwd_abs = os.path.abspath(os.path.expanduser(cwd)).rstrip(os.sep) or os.sep
    for repo in repo_folders:
        try:
            if os.path.commonpath([cwd_abs, repo]) == repo:
                return True
        except ValueError:
            continue
    return False


def repo_label_for(cwd: str, repo_folders: list[str]) -> str:
    if cwd:
        cwd_abs = os.path.abspath(os.path.expanduser(cwd)).rstrip(os.sep) or os.sep
        for repo in repo_folders:
            try:
                if os.path.commonpath([cwd_abs, repo]) == repo:
                    return repo
            except ValueError:
                continue
        return cwd_abs
    return "(unknown repo)"


def select_candidates(
    candidates: list[SessionCandidate],
    since: datetime | None,
    until: datetime | None,
    repo_folders: list[str],
) -> list[SessionCandidate]:
    selected = []
    for candidate in candidates:
        if not session_overlaps_range(candidate, since, until):
            continue
        if not cwd_matches_repo(candidate.cwd, repo_folders):
            continue
        candidate.repo_label = repo_label_for(candidate.cwd, repo_folders)
        selected.append(candidate)
    return selected


def prepare_copy_root(copy_root: Path | None) -> Path:
    if copy_root:
        copy_root.mkdir(parents=True, exist_ok=True)
        return copy_root
    return Path(tempfile.mkdtemp(prefix="convojoiner-", dir="/tmp"))


def copy_selected_sources(
    candidates: list[SessionCandidate], copy_root: Path, claude_source: Path, codex_source: Path
) -> list[SessionCandidate]:
    copied_candidates: list[SessionCandidate] = []
    for candidate in candidates:
        base_source = claude_source if candidate.provider == "claude" else codex_source
        dest_path = copy_one_source(candidate.source_path, copy_root, candidate.provider, base_source)
        copied = SessionCandidate(**{**candidate.__dict__, "copied_path": dest_path})
        copied.copied_extra_paths = []
        for extra_path in candidate.copied_extra_paths:
            if extra_path.exists():
                copied.copied_extra_paths.append(
                    copy_one_source(extra_path, copy_root, candidate.provider, base_source)
                )
        copied_candidates.append(copied)
    return copied_candidates


def copy_one_source(path: Path, copy_root: Path, provider: str, base_source: Path) -> Path:
    try:
        relative = path.relative_to(base_source)
    except ValueError:
        relative = Path(path.name)
    dest = copy_root / "sources" / provider / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.chmod(dest.stat().st_mode | 0o200)
        dest.unlink()
    shutil.copy2(path, dest)
    dest.chmod(dest.stat().st_mode & ~0o222)
    return dest


def parse_events_for_candidates(
    candidates: list[SessionCandidate], since: datetime | None, until: datetime | None
) -> list[Event]:
    events: list[Event] = []
    by_lane = {candidate.lane_id: candidate for candidate in candidates}
    for candidate in candidates:
        path = candidate.copied_path or candidate.source_path
        if candidate.provider == "claude":
            parsed = parse_claude_events(candidate, path)
        else:
            parsed = parse_codex_events(candidate, path)
        for event in parsed:
            if since and event.timestamp < since:
                continue
            if until and event.timestamp >= until:
                continue
            if event.session_id not in by_lane:
                continue
            events.append(event)
    events.sort(key=lambda event: (event.timestamp, event.provider, event.session_id))
    return events


def parse_claude_events(candidate: SessionCandidate, path: Path) -> list[Event]:
    events: list[Event] = []
    for line_number, obj in iter_jsonl(path):
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
                        provider="claude",
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
            if is_internal_claude_text(content):
                continue
            role = "user" if obj_type == "user" else "assistant"
            events.append(
                Event(
                    session_id=candidate.lane_id,
                    provider="claude",
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
                if not text or is_internal_claude_text(text):
                    continue
                role = "user" if obj_type == "user" else "assistant"
                events.append(
                    Event(
                        session_id=candidate.lane_id,
                        provider="claude",
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
                            provider="claude",
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
                tool_input = block.get("input")
                events.append(
                    Event(
                        session_id=candidate.lane_id,
                        provider="claude",
                        timestamp=event_time,
                        role="tool",
                        kind=tool_kind(name),
                        title=name,
                        body=format_tool_input(name, tool_input),
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
                            provider="claude",
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
                        provider="claude",
                        timestamp=event_time,
                        role="user" if obj_type == "user" else "assistant",
                        kind="message",
                        title="Image",
                        body="[image block omitted from joined transcript]",
                        cwd=cwd,
                    )
                )

    return events


def parse_codex_events(candidate: SessionCandidate, path: Path) -> list[Event]:
    output_call_ids = collect_codex_response_output_call_ids(path)
    events: list[Event] = []
    for line_number, obj in iter_jsonl(path):
        timestamp = parse_json_timestamp(obj.get("timestamp"))
        if not timestamp:
            continue
        obj_type = obj.get("type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}

        if obj_type == "response_item":
            events.extend(parse_codex_response_item(candidate, payload, timestamp))
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

        event = parse_codex_event_msg(candidate, payload, timestamp)
        if event:
            events.append(event)
    return events


def collect_codex_response_output_call_ids(path: Path) -> set[str]:
    call_ids: set[str] = set()
    for _, obj in iter_jsonl(path):
        if obj.get("type") != "response_item":
            continue
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if payload.get("type") == "function_call_output" and payload.get("call_id"):
            call_ids.add(str(payload["call_id"]))
    return call_ids


def parse_codex_response_item(
    candidate: SessionCandidate, payload: dict[str, Any], timestamp: datetime
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
        if role == "user" and is_internal_codex_text(text):
            return []
        title = role.title()
        kind = "message"
        events.append(
            Event(
                session_id=candidate.lane_id,
                provider="codex",
                timestamp=timestamp,
                role=role,
                kind=kind,
                title=title,
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
                provider="codex",
                timestamp=timestamp,
                role="tool",
                kind=tool_kind(name),
                title=name,
                body=format_tool_input(name, args),
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
                    provider="codex",
                    timestamp=timestamp,
                    role="tool",
                    kind="tool_result",
                    title="Tool result",
                    body=output,
                    cwd=candidate.cwd,
                    call_id=stringify_content(payload.get("call_id")) or None,
                    is_error=looks_like_error_output(output),
                )
            )
        return events

    if payload_type == "web_search_call":
        events.append(
            Event(
                session_id=candidate.lane_id,
                provider="codex",
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


def parse_codex_event_msg(
    candidate: SessionCandidate, payload: dict[str, Any], timestamp: datetime
) -> Event | None:
    payload_type = payload.get("type")
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else candidate.cwd

    if payload_type == "exec_command_end":
        command = payload.get("command")
        body = stringify_content(payload.get("aggregated_output"))
        if not body:
            body = combine_stdout_stderr(payload)
        title = command_to_title(command) or "Command"
        return Event(
            session_id=candidate.lane_id,
            provider="codex",
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
        body = combine_stdout_stderr(payload) or stringify_content(payload)
        return Event(
            session_id=candidate.lane_id,
            provider="codex",
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
            provider="codex",
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
            provider="codex",
            timestamp=timestamp,
            role="system",
            kind="status",
            title=title,
            body=body,
            cwd=cwd,
            is_error=payload_type == "turn_aborted",
        )

    return None


def parse_json_maybe(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def tool_kind(name: str) -> str:
    lower = name.lower()
    if lower in {"bash", "exec_command", "shell", "terminal"}:
        return "command"
    if lower in {"edit", "multiedit", "write", "apply_patch"} or "patch" in lower:
        return "file_edit"
    return "tool_use"


def format_tool_input(name: str, value: Any) -> str:
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


def combine_stdout_stderr(payload: dict[str, Any]) -> str:
    parts = []
    stdout = stringify_content(payload.get("stdout"))
    stderr = stringify_content(payload.get("stderr"))
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(stderr)
    return "\n\n".join(parts).strip()


def command_to_title(command: Any) -> str:
    if isinstance(command, list):
        if len(command) >= 3 and command[0].endswith("zsh") and command[1] == "-lc":
            return str(command[2])
        return " ".join(str(part) for part in command)
    if isinstance(command, str):
        return command
    return ""


def looks_like_error_output(output: str) -> bool:
    match = EXIT_CODE_RE.search(output)
    if match:
        try:
            return int(match.group(1)) != 0
        except ValueError:
            return False
    return False


def event_with_repo_filter(event: Event, repo_folders: list[str]) -> bool:
    return cwd_matches_repo(event.cwd, repo_folders)


def build_html_data(
    candidates: list[SessionCandidate],
    events: list[Event],
    copy_root: Path,
    display_tz: timezone,
    timezone_label: str,
    repo_folders: list[str],
) -> dict[str, Any]:
    active_session_ids = {event.session_id for event in events}
    sessions = []
    for candidate in sorted(
        candidates, key=lambda c: (c.started_at or datetime.max.replace(tzinfo=timezone.utc))
    ):
        if candidate.lane_id not in active_session_ids:
            continue
        sessions.append(
            {
                "id": candidate.lane_id,
                "provider": candidate.provider,
                "session_id": candidate.session_id,
                "label": candidate.label,
                "cwd": candidate.cwd,
                "repo": candidate.repo_label or repo_label_for(candidate.cwd, repo_folders),
                "started_at": isoformat_z(candidate.started_at),
                "ended_at": isoformat_z(candidate.ended_at),
                "summary": candidate.summary,
                "is_subagent": candidate.is_subagent,
                "parent_session_id": candidate.parent_session_id,
                "agent_id": candidate.agent_id,
                "description": candidate.description,
                "source_path": str(candidate.source_path),
                "copied_path": str(candidate.copied_path) if candidate.copied_path else "",
            }
        )

    html_events = []
    for index, event in enumerate(events, start=1):
        local_dt = event.timestamp.astimezone(display_tz)
        html_events.append(
            {
                "id": f"event-{index}",
                "session_id": event.session_id,
                "provider": event.provider,
                "timestamp": isoformat_z(event.timestamp),
                "display_time": local_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "display_minute": local_dt.strftime("%Y-%m-%d %H:%M"),
                "day": local_dt.strftime("%Y-%m-%d"),
                "role": event.role,
                "kind": event.kind,
                "title": event.title,
                "body": event.body,
                "cwd": event.cwd,
                "call_id": event.call_id,
                "is_error": event.is_error,
            }
        )

    return {
        "generated_at": isoformat_z(datetime.now(timezone.utc)),
        "timezone": timezone_label,
        "copy_root": str(copy_root),
        "repo_folders": repo_folders,
        "sessions": sessions,
        "events": html_events,
    }


def isoformat_z(value: datetime | None) -> str:
    if not value:
        return ""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def write_html(output: Path, data: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html_text = HTML_TEMPLATE.replace("__DATA_JSON__", data_json)
    output.write_text(html_text, encoding="utf-8")


def print_dry_run(candidates: list[SessionCandidate], display_tz: timezone) -> None:
    print(f"Selected {len(candidates)} sessions")
    for candidate in sorted(
        candidates, key=lambda c: (c.started_at or datetime.max.replace(tzinfo=timezone.utc))
    ):
        start = (
            candidate.started_at.astimezone(display_tz).strftime("%Y-%m-%d %H:%M:%S")
            if candidate.started_at
            else "unknown"
        )
        subagent = " subagent" if candidate.is_subagent else ""
        print(
            f"{start}  {candidate.provider:<6}{subagent:<9}  "
            f"{candidate.cwd or '(unknown cwd)'}  {candidate.source_path}"
        )
        if candidate.summary:
            print(f"    {candidate.summary}")


def main() -> int:
    args = parse_args()
    display_tz = get_display_tz(args.timezone)
    since = parse_cli_datetime(args.since, display_tz, is_until=False)
    until = parse_cli_datetime(args.until, display_tz, is_until=True)
    repo_folders = normalize_repo_folders(args.repo_folder)
    providers = set(args.provider or ("claude", "codex"))

    candidates: list[SessionCandidate] = []
    if "claude" in providers:
        candidates.extend(
            discover_claude_sessions(args.claude_source.expanduser(), not args.no_subagents)
        )
    if "codex" in providers:
        candidates.extend(discover_codex_sessions(args.codex_source.expanduser()))

    selected = select_candidates(candidates, since, until, repo_folders)

    if args.dry_run:
        print_dry_run(selected, display_tz)
        return 0

    copy_root = prepare_copy_root(args.copy_root)
    copied = copy_selected_sources(
        selected,
        copy_root,
        args.claude_source.expanduser(),
        args.codex_source.expanduser(),
    )
    events = parse_events_for_candidates(copied, since, until)
    if repo_folders:
        events = [event for event in events if event_with_repo_filter(event, repo_folders)]

    data = build_html_data(copied, events, copy_root, display_tz, args.timezone, repo_folders)
    write_html(args.output, data)

    print(f"Copied source files under: {copy_root}")
    print(f"Sessions: {len(data['sessions'])}")
    print(f"Events: {len(data['events'])}")
    print(f"Wrote: {args.output.resolve()}")

    if args.open:
        webbrowser.open(args.output.resolve().as_uri())
    return 0


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Convojoiner Timeline</title>
<style>
:root {
  --bg: #f5f5f5;
  --card: #ffffff;
  --text: #212121;
  --muted: #6f7782;
  --line: #d8dde4;
  --user-bg: #e3f2fd;
  --user-border: #1976d2;
  --assistant-bg: #ffffff;
  --assistant-border: #8f98a3;
  --thinking-bg: #fff8e1;
  --thinking-border: #f5b400;
  --tool-bg: #f3e5f5;
  --tool-border: #8e24aa;
  --result-bg: #e8f5e9;
  --error-bg: #ffebee;
  --code-bg: #263238;
  --code-text: #d7e8d0;
  --claude: #b05a2a;
  --codex: #1d6b6b;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  line-height: 1.45;
}
.topbar {
  background: rgba(245, 245, 245, 0.96);
  border-bottom: 1px solid var(--line);
}
.topbar-inner { max-width: 1680px; margin: 0 auto; padding: 14px 16px; }
h1 { font-size: 1.35rem; margin: 0 0 8px; }
.summary { color: var(--muted); font-size: 0.9rem; }
.controls {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
  margin-top: 12px;
}
.control-group {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 8px;
}
.control-title {
  color: var(--muted);
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  margin-bottom: 6px;
  text-transform: uppercase;
}
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.control-group .chips {
  max-height: 94px;
  overflow-y: auto;
  padding-right: 2px;
}
#session-filter { max-height: 130px; }
.chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 3px 8px;
  background: #fafafa;
  font-size: 0.82rem;
  max-width: 100%;
}
.chip span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
input[type="search"], select {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 7px 9px;
  background: #fff;
  color: var(--text);
  font: inherit;
}
button {
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #fff;
  color: var(--text);
  cursor: pointer;
  font: inherit;
  padding: 6px 10px;
}
button:hover { background: #f0f3f6; }
main { max-width: 1680px; margin: 0 auto; padding: 16px; }
.session-title { font-weight: 700; font-size: 0.9rem; }
.session-meta { color: var(--muted); font-size: 0.78rem; margin-top: 3px; }
.pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 12px 0;
}
.pager-info {
  color: var(--muted);
  font-size: 0.88rem;
  min-width: 220px;
  text-align: center;
}
.pager button:disabled {
  color: var(--muted);
  cursor: default;
  opacity: 0.55;
}
.timeline-scroll { overflow-x: auto; padding-bottom: 32px; }
.lane-grid {
  display: grid;
  gap: 8px;
  align-items: start;
}
.lane-header, .time-cell { background: var(--bg); }
.lane-header {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 8px;
  min-height: 70px;
}
.lane-header.claude { border-top: 4px solid var(--claude); }
.lane-header.codex { border-top: 4px solid var(--codex); }
.time-cell {
  color: var(--muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.78rem;
  padding: 9px 4px;
  text-align: right;
}
.lane-cell {
  min-height: 28px;
  border-top: 1px solid rgba(0, 0, 0, 0.04);
}
.event-card {
  margin-bottom: 8px;
  overflow: hidden;
  border-radius: 8px;
  border: 1px solid var(--line);
  border-left: 4px solid var(--assistant-border);
  background: var(--assistant-bg);
  box-shadow: 0 1px 2px rgba(0,0,0,0.05);
}
.event-card.user { background: var(--user-bg); border-left-color: var(--user-border); }
.event-card.assistant { border-left-color: var(--assistant-border); }
.event-card.system { border-left-color: #607d8b; opacity: 0.88; }
.event-card.tool_use, .event-card.command, .event-card.file_edit {
  background: var(--tool-bg);
  border-left-color: var(--tool-border);
}
.event-card.thinking { background: var(--thinking-bg); border-left-color: var(--thinking-border); }
.event-card.tool_result { background: var(--result-bg); border-left-color: #43a047; }
.event-card.error { background: var(--error-bg); border-left-color: #c62828; }
.event-head {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  padding: 7px 10px;
  background: rgba(0,0,0,0.035);
  font-size: 0.78rem;
}
.event-title {
  font-weight: 700;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.event-time {
  color: var(--muted);
  flex: 0 0 auto;
  text-decoration: none;
}
.event-body {
  padding: 10px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.event-body pre {
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  background: var(--code-bg);
  color: var(--code-text);
  border-radius: 6px;
  padding: 10px;
  font-size: 0.8rem;
  line-height: 1.45;
}
.event-body.collapsed {
  max-height: 260px;
  overflow: hidden;
  position: relative;
}
.event-body.collapsed::after {
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 58px;
  background: linear-gradient(to bottom, transparent, rgba(255,255,255,0.92));
}
.tool_use .event-body.collapsed::after,
.command .event-body.collapsed::after,
.file_edit .event-body.collapsed::after { background: linear-gradient(to bottom, transparent, #f3e5f5); }
.tool_result .event-body.collapsed::after { background: linear-gradient(to bottom, transparent, #e8f5e9); }
.error .event-body.collapsed::after { background: linear-gradient(to bottom, transparent, #ffebee); }
.expand {
  display: block;
  width: calc(100% - 20px);
  margin: 0 10px 10px;
  font-size: 0.8rem;
}
.dense .event-body { padding: 7px; font-size: 0.88rem; }
.dense .event-head { padding: 5px 8px; }
.empty {
  padding: 24px;
  border: 1px dashed var(--line);
  border-radius: 8px;
  background: var(--card);
  color: var(--muted);
}
@media (max-width: 760px) {
  .controls { grid-template-columns: 1fr; }
  .lane-header, .time-cell { top: 0; position: static; }
  main { padding: 10px; }
}
</style>
</head>
<body>
<div class="topbar">
  <div class="topbar-inner">
    <h1>Convojoiner Timeline</h1>
    <div class="summary" id="summary"></div>
    <div class="controls">
      <div class="control-group">
        <div class="control-title">Search</div>
        <input id="search-input" type="search" placeholder="Search transcript">
      </div>
      <div class="control-group">
        <div class="control-title">Page Size</div>
        <select id="page-size-select">
          <option value="100">100 events</option>
          <option value="250" selected>250 events</option>
          <option value="500">500 events</option>
          <option value="1000">1000 events</option>
        </select>
      </div>
      <div class="control-group">
        <div class="control-title">Provider</div>
        <div class="chips" id="provider-filter"></div>
      </div>
      <div class="control-group">
        <div class="control-title">Day</div>
        <div class="chips" id="day-filter"></div>
      </div>
      <div class="control-group">
        <div class="control-title">Repo</div>
        <div class="chips" id="repo-filter"></div>
      </div>
      <div class="control-group">
        <div class="control-title">Kind</div>
        <div class="chips" id="kind-filter"></div>
      </div>
      <div class="control-group">
        <div class="control-title">Session</div>
        <div class="chips" id="session-filter"></div>
      </div>
      <div class="control-group">
        <div class="control-title">Display</div>
        <div class="chips">
          <label class="chip"><input type="checkbox" id="dense-toggle"><span>Dense</span></label>
        </div>
      </div>
    </div>
  </div>
</div>
<main id="app"></main>
<script id="transcript-data" type="application/json">__DATA_JSON__</script>
<script>
const data = JSON.parse(document.getElementById("transcript-data").textContent);
const state = { query: "", dense: false, page: 1, pageSize: 250 };
const sessionById = new Map(data.sessions.map(session => [session.id, session]));

function unique(values) {
  return Array.from(new Set(values.filter(Boolean))).sort();
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  }[char]));
}

function makeCheckboxes(containerId, name, values, labeler = value => value) {
  const container = document.getElementById(containerId);
  container.innerHTML = values.map(value => `
    <label class="chip" title="${escapeHtml(value)}">
      <input type="checkbox" name="${name}" value="${escapeHtml(value)}" checked>
      <span>${escapeHtml(labeler(value))}</span>
    </label>
  `).join("");
  container.querySelectorAll("input").forEach(input => input.addEventListener("change", () => {
    state.page = 1;
    render();
  }));
}

function selectedValues(name) {
  return new Set(Array.from(document.querySelectorAll(`input[name="${name}"]:checked`)).map(input => input.value));
}

function initFilters() {
  makeCheckboxes("provider-filter", "provider", unique(data.sessions.map(s => s.provider)));
  makeCheckboxes("day-filter", "day", unique(data.events.map(e => e.day)));
  makeCheckboxes("repo-filter", "repo", unique(data.sessions.map(s => s.repo)), value => value.split("/").filter(Boolean).slice(-2).join("/") || value);
  makeCheckboxes("kind-filter", "kind", unique(data.events.map(e => e.kind)), value => value.replaceAll("_", " "));
  makeCheckboxes("session-filter", "session", data.sessions.map(s => s.id), value => sessionById.get(value)?.label || value);

  document.getElementById("search-input").addEventListener("input", event => {
    state.query = event.target.value.toLowerCase().trim();
    state.page = 1;
    render();
  });
  document.getElementById("page-size-select").addEventListener("change", event => {
    state.pageSize = Number(event.target.value);
    state.page = 1;
    render();
  });
  document.getElementById("dense-toggle").addEventListener("change", event => {
    state.dense = event.target.checked;
    render();
  });
}

function filteredEvents() {
  const providers = selectedValues("provider");
  const days = selectedValues("day");
  const repos = selectedValues("repo");
  const kinds = selectedValues("kind");
  const sessions = selectedValues("session");
  return data.events.filter(event => {
    const session = sessionById.get(event.session_id);
    if (!session) return false;
    if (!providers.has(event.provider)) return false;
    if (!days.has(event.day)) return false;
    if (!repos.has(session.repo)) return false;
    if (!kinds.has(event.kind)) return false;
    if (!sessions.has(event.session_id)) return false;
    if (state.query) {
      const haystack = [
        event.title,
        event.body,
        event.role,
        event.kind,
        session.label,
        session.cwd,
        session.repo
      ].join("\n").toLowerCase();
      if (!haystack.includes(state.query)) return false;
    }
    return true;
  });
}

function render() {
  document.body.classList.toggle("dense", state.dense);
  const events = filteredEvents();
  const page = paginateEvents(events);
  const activeSessionIds = unique(page.events.map(event => event.session_id));
  const activeSessions = data.sessions.filter(session => activeSessionIds.includes(session.id));
  document.getElementById("summary").textContent =
    `${events.length} matching events · ${activeSessions.length} sessions on this page · sources copied to ${data.copy_root}`;
  const app = document.getElementById("app");
  if (!events.length) {
    app.innerHTML = `<div class="empty">No events match the current filters.</div>`;
    return;
  }
  app.innerHTML = renderPager(page) + renderLanes(activeSessions, page.events) + renderPager(page);
  wirePaginationButtons();
  wireExpandButtons();
}

function paginateEvents(events) {
  const pageCount = Math.max(1, Math.ceil(events.length / state.pageSize));
  state.page = Math.max(1, Math.min(state.page, pageCount));
  const start = (state.page - 1) * state.pageSize;
  const end = Math.min(start + state.pageSize, events.length);
  return {
    events: events.slice(start, end),
    page: state.page,
    pageCount,
    start,
    end,
    total: events.length
  };
}

function renderPager(page) {
  return `
    <nav class="pager" aria-label="Pagination">
      <button type="button" data-page-action="first" ${page.page === 1 ? "disabled" : ""}>First</button>
      <button type="button" data-page-action="prev" ${page.page === 1 ? "disabled" : ""}>Prev</button>
      <div class="pager-info">Page ${page.page} of ${page.pageCount} · ${page.start + 1}-${page.end} of ${page.total}</div>
      <button type="button" data-page-action="next" ${page.page === page.pageCount ? "disabled" : ""}>Next</button>
      <button type="button" data-page-action="last" data-page-count="${page.pageCount}" ${page.page === page.pageCount ? "disabled" : ""}>Last</button>
    </nav>
  `;
}

function wirePaginationButtons() {
  document.querySelectorAll("[data-page-action]").forEach(button => {
    button.addEventListener("click", () => {
      const action = button.dataset.pageAction;
      if (action === "first") state.page = 1;
      if (action === "prev") state.page -= 1;
      if (action === "next") state.page += 1;
      if (action === "last") state.page = Number(button.dataset.pageCount || state.page);
      render();
      window.scrollTo({ top: 0, behavior: "auto" });
    });
  });
}

function renderLanes(sessions, events) {
  if (!sessions.length) {
    return `<div class="empty">No sessions have events on this page.</div>`;
  }
  const columns = `112px repeat(${sessions.length}, minmax(280px, 420px))`;
  const byMinute = new Map();
  events.forEach(event => {
    if (!byMinute.has(event.display_minute)) byMinute.set(event.display_minute, []);
    byMinute.get(event.display_minute).push(event);
  });
  const minutes = Array.from(byMinute.keys()).sort();
  const headers = `<div></div>${sessions.map(session => `
    <div class="lane-header ${escapeHtml(session.provider)}">
      <div class="session-title">${escapeHtml(session.label)}</div>
      <div class="session-meta">${escapeHtml(session.cwd)}</div>
    </div>
  `).join("")}`;
  const rows = minutes.map(minute => {
    const minuteEvents = byMinute.get(minute);
    const cells = sessions.map(session => {
      const cellEvents = minuteEvents.filter(event => event.session_id === session.id);
      return `<div class="lane-cell">${cellEvents.map(renderEventCard).join("")}</div>`;
    }).join("");
    return `<div class="time-cell">${escapeHtml(minute)}</div>${cells}`;
  }).join("");
  return `<div class="timeline-scroll"><div class="lane-grid" style="grid-template-columns: ${columns}">${headers}${rows}</div></div>`;
}

function renderEventCard(event) {
  const preKinds = new Set(["command", "tool_use", "tool_result", "file_edit", "status"]);
  const body = preKinds.has(event.kind)
    ? `<pre>${escapeHtml(event.body)}</pre>`
    : escapeHtml(event.body);
  const classes = [
    "event-card",
    event.role,
    event.kind,
    event.provider,
    event.is_error ? "error" : ""
  ].join(" ");
  return `
    <article class="${classes}" id="${escapeHtml(event.id)}">
      <div class="event-head">
        <div class="event-title">${escapeHtml(event.title)}</div>
        <a class="event-time" href="#${escapeHtml(event.id)}">${escapeHtml(event.display_time.split(" ")[1] || event.display_time)}</a>
      </div>
      <div class="event-body collapsed">${body}</div>
      <button class="expand" type="button">Show more</button>
    </article>
  `;
}

function wireExpandButtons() {
  document.querySelectorAll(".event-card").forEach(card => {
    const body = card.querySelector(".event-body");
    const button = card.querySelector(".expand");
    if (!body || !button) return;
    if (body.scrollHeight <= 270) {
      body.classList.remove("collapsed");
      button.style.display = "none";
      return;
    }
    button.addEventListener("click", () => {
      const collapsed = body.classList.toggle("collapsed");
      button.textContent = collapsed ? "Show more" : "Show less";
    });
  });
}

initFilters();
render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
