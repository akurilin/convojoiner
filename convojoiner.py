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
from typing import Any, Callable
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
COMMIT_OUTPUT_RE = re.compile(r"\[[\w\-/]+ ([a-f0-9]{7,})\] (.+?)(?:\n|$)", re.I)
DEFAULT_PROMPTS_PER_PAGE = 5
ASSISTANT_INDEX_SUMMARY_CHARS = 900

def _redact_db_password(match: re.Match[str]) -> str:
    return f"{match.group(1)}[REDACTED:db-password]{match.group(3)}"


SECRET_PATTERNS: list[tuple[str, re.Pattern[str], Callable[[re.Match[str]], str] | None]] = [
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{90,}"), None),
    ("openai-project-key", re.compile(r"sk-proj-[A-Za-z0-9\-_]{40,}"), None),
    ("openai-key", re.compile(r"sk-[A-Za-z0-9]{48,}"), None),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"), None),
    ("github-pat", re.compile(r"github_pat_[A-Za-z0-9_]{60,}"), None),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), None),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"), None),
    ("google-oauth-secret", re.compile(r"GOCSPX-[A-Za-z0-9_\-]{20,}"), None),
    ("slack-token", re.compile(r"\bxox[bpars]-[A-Za-z0-9-]{10,}"), None),
    ("stripe-live-key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{24,}\b"), None),
    ("supabase-secret", re.compile(r"\bsb_secret_[A-Za-z0-9_\-]{20,}"), None),
    ("supabase-publishable", re.compile(r"\bsb_publishable_[A-Za-z0-9_\-]{20,}"), None),
    ("supabase-access-token", re.compile(r"\bsbp_[A-Za-z0-9]{40,}"), None),
    (
        "pem-private-key",
        re.compile(
            r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"
            r"[\s\S]*?"
            r"-----END (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"
        ),
        None,
    ),
    (
        "db-password",
        re.compile(
            r"(\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?)://[^:@\s/]+:)"
            r"([^@\s/]+)"
            r"(@[^\s\"'<>`]+)"
        ),
        _redact_db_password,
    ),
]

REDACTION_COUNTS: dict[str, int] = {}


def redact_secrets(text: str) -> str:
    if not text:
        return text
    for name, pattern, formatter in SECRET_PATTERNS:
        def replace(match: re.Match[str], _name: str = name, _formatter=formatter) -> str:
            REDACTION_COUNTS[_name] = REDACTION_COUNTS.get(_name, 0) + 1
            if _formatter is not None:
                return _formatter(match)
            return f"[REDACTED:{_name}]"

        text = pattern.sub(replace, text)
    return text


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
        default=Path("convojoiner"),
        help="Output directory. Default: ./convojoiner",
    )
    parser.add_argument(
        "--page-prompts",
        type=int,
        default=DEFAULT_PROMPTS_PER_PAGE,
        help=f"User prompt turns per generated page. Default: {DEFAULT_PROMPTS_PER_PAGE}",
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
                "summary": redact_secrets(candidate.summary),
                "is_subagent": candidate.is_subagent,
                "parent_session_id": candidate.parent_session_id,
                "agent_id": candidate.agent_id,
                "description": redact_secrets(candidate.description),
            }
        )

    html_events = []
    for index, event in enumerate(events, start=1):
        local_dt = event.timestamp.astimezone(display_tz)
        redacted_title = redact_secrets(event.title)
        redacted_body = redact_secrets(event.body)
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
                "title": redacted_title,
                "body": redacted_body,
                "body_html": render_markdown(redacted_body) if event.kind in PROSE_KINDS else "",
                "cwd": event.cwd,
                "call_id": event.call_id,
                "is_error": event.is_error,
            }
        )

    return {
        "generated_at": isoformat_z(datetime.now(timezone.utc)),
        "timezone": timezone_label,
        "repo_folders": repo_folders,
        "sessions": sessions,
        "events": html_events,
    }


def isoformat_z(value: datetime | None) -> str:
    if not value:
        return ""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_output_dir(output: Path) -> Path:
    output = output.expanduser()
    if output.suffix.lower() in {".html", ".htm"}:
        return output.with_suffix("")
    return output


def write_html_archive(output: Path, data: dict[str, Any], prompts_per_page: int) -> Path:
    if prompts_per_page < 1:
        raise SystemExit("--page-prompts must be at least 1")

    output_dir = resolve_output_dir(output)
    if output_dir.exists() and not output_dir.is_dir():
        raise SystemExit(f"Output path exists and is not a directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    for old_page in output_dir.glob("page-*.html"):
        old_page.unlink()

    turns = build_turns(data["events"])
    if data["events"] and not turns:
        turns = [synthetic_turn(data["events"])]

    total_pages = max(1, (len(turns) + prompts_per_page - 1) // prompts_per_page)
    session_by_id = {session["id"]: session for session in data["sessions"]}
    for page_num in range(1, total_pages + 1):
        start = (page_num - 1) * prompts_per_page
        end = start + prompts_per_page
        page_turns = turns[start:end]
        page_events = sorted(
            [event for turn in page_turns for event in turn["events"]],
            key=lambda event: (event["timestamp"], event["provider"], event["session_id"]),
        )
        page_session_ids = {event["session_id"] for event in page_events}
        page_data = {
            **data,
            "sessions": [
                session for session in data["sessions"] if session["id"] in page_session_ids
            ],
            "events": page_events,
            "page": page_num,
            "total_pages": total_pages,
            "total_events": len(data["events"]),
        }
        write_page_html(output_dir / page_filename(page_num), page_data)

    index_data = build_index_data(data, turns, total_pages, prompts_per_page, session_by_id)
    write_index_html(output_dir / "index.html", data, index_data, total_pages)
    return output_dir


def write_page_html(path: Path, data: dict[str, Any]) -> None:
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html_text = (
        PAGE_TEMPLATE.replace("__DATA_JSON__", data_json)
        .replace("__PAGE_TITLE__", f"Koda Timeline - page {data['page']}/{data['total_pages']}")
        .replace("__PAGE_HEADING__", f"Koda Timeline - page {data['page']}/{data['total_pages']}")
        .replace("__PAGINATION_HTML__", pagination_html(data["page"], data["total_pages"]))
    )
    path.write_text(html_text, encoding="utf-8")


def write_index_html(
    path: Path, data: dict[str, Any], index_data: dict[str, Any], total_pages: int
) -> None:
    html_text = (
        INDEX_TEMPLATE.replace("__INDEX_ITEMS__", index_data["items_html"])
        .replace("__PAGINATION_HTML__", index_pagination_html(total_pages))
        .replace("__PROMPT_COUNT__", str(index_data["prompt_count"]))
        .replace("__MESSAGE_COUNT__", str(index_data["message_count"]))
        .replace("__TOOL_CALL_COUNT__", str(index_data["tool_call_count"]))
        .replace("__COMMIT_COUNT__", str(index_data["commit_count"]))
        .replace("__PAGE_COUNT__", str(total_pages))
    )
    path.write_text(html_text, encoding="utf-8")


def build_turns(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    events_by_session: dict[str, list[dict[str, Any]]] = {}
    for event in sorted(events, key=lambda e: (e["timestamp"], e["provider"], e["session_id"])):
        events_by_session.setdefault(event["session_id"], []).append(event)

    for session_events in events_by_session.values():
        current: dict[str, Any] | None = None
        preamble: list[dict[str, Any]] = []
        for event in session_events:
            is_user_prompt = event["kind"] == "message" and event["role"] == "user"
            if not is_user_prompt:
                if current:
                    current["events"].append(event)
                else:
                    preamble.append(event)
                continue

            if current:
                turns.append(current)
            turn = {
                "timestamp": event["timestamp"],
                "session_id": event["session_id"],
                "prompt_event": event,
                "prompt_text": event["body"],
                "events": [*preamble, event],
                "is_prompt": True,
            }
            preamble = []
            current = turn

        if current:
            turns.append(current)
        elif preamble:
            turns.append(synthetic_turn(preamble))

    return sorted(turns, key=lambda turn: (turn["timestamp"], turn["session_id"]))


def synthetic_turn(events: list[dict[str, Any]]) -> dict[str, Any]:
    first = events[0]
    return {
        "timestamp": first["timestamp"],
        "session_id": first["session_id"],
        "prompt_event": first,
        "prompt_text": "Session activity",
        "events": events,
        "is_prompt": False,
    }


def build_index_data(
    data: dict[str, Any],
    turns: list[dict[str, Any]],
    total_pages: int,
    prompts_per_page: int,
    session_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    timeline_items: list[tuple[str, int, str]] = []
    prompt_count = 0
    commit_count = 0
    message_count = sum(
        1
        for event in data["events"]
        if event["kind"] == "message" and event["role"] in {"user", "assistant"}
    )
    tool_call_count = sum(
        1
        for event in data["events"]
        if event["kind"] in {"command", "tool_use", "file_edit"}
    )

    for turn_index, turn in enumerate(turns):
        page_num = min(total_pages, (turn_index // prompts_per_page) + 1)
        prompt_event = turn["prompt_event"]
        session = session_by_id.get(turn["session_id"], {})
        if turn.get("is_prompt"):
            prompt_count += 1
            item_html = render_index_turn(prompt_count, page_num, turn, session)
            timeline_items.append((turn["timestamp"], prompt_count * 2, item_html))

        for commit_hash, commit_title, commit_ts in extract_commits(turn["events"]):
            commit_count += 1
            timeline_items.append(
                (
                    commit_ts or prompt_event["timestamp"],
                    prompt_count * 2 + 1,
                    render_index_commit(commit_hash, commit_title, commit_ts or prompt_event["timestamp"]),
                )
            )

    timeline_items.sort(key=lambda item: (item[0], item[1]))
    return {
        "prompt_count": prompt_count,
        "message_count": message_count,
        "tool_call_count": tool_call_count,
        "commit_count": commit_count,
        "items_html": "\n".join(item[2] for item in timeline_items),
    }


def render_index_turn(
    prompt_number: int, page_num: int, turn: dict[str, Any], session: dict[str, Any]
) -> str:
    prompt_event = turn["prompt_event"]
    link = f"{page_filename(page_num)}#{html.escape(prompt_event['id'])}"
    provider = session.get("provider", prompt_event["provider"])
    repo = session.get("repo", "")
    session_label = session.get("label", prompt_event["session_id"])
    final_assistant = final_assistant_text(turn["events"])
    tool_stats = format_detail_stats(turn["events"])
    stats_parts = [part for part in [tool_stats, f"{provider} · {session_label}", repo] if part]
    stats_html = (
        f'<div class="index-item-stats">{html.escape(" · ".join(stats_parts))}</div>'
        if stats_parts
        else ""
    )
    final_html = ""
    if final_assistant:
        final_html = (
            '<div class="index-item-response">'
            '<div class="index-item-response-label">Final response</div>'
            f"{render_text_html(first_useful_summary(final_assistant, ASSISTANT_INDEX_SUMMARY_CHARS))}"
            "</div>"
        )
    return f"""
<article class="index-item {html.escape(provider)}">
  <a href="{link}">
    <div class="index-item-header">
      <span class="index-item-number">#{prompt_number}</span>
      <time datetime="{html.escape(prompt_event['timestamp'])}">{html.escape(prompt_event['display_time'])}</time>
    </div>
    <div class="index-item-content">{render_text_html(turn["prompt_text"])}{final_html}</div>
  </a>
  {stats_html}
</article>""".strip()


def render_index_commit(commit_hash: str, commit_title: str, timestamp: str) -> str:
    return f"""
<article class="index-commit">
  <div class="index-commit-header">
    <span class="index-commit-hash">{html.escape(commit_hash[:7])}</span>
    <time datetime="{html.escape(timestamp)}">{html.escape(timestamp)}</time>
  </div>
  <div class="index-commit-msg">{html.escape(commit_title)}</div>
</article>""".strip()


def extract_commits(events: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    commits: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for event in events:
        if event["kind"] not in {"tool_result", "status"}:
            continue
        for match in COMMIT_OUTPUT_RE.finditer(event.get("body") or ""):
            commit_hash, title = match.group(1), match.group(2).strip()
            key = (commit_hash, title)
            if key in seen:
                continue
            seen.add(key)
            commits.append((commit_hash, title, event["timestamp"]))
    return commits


def final_assistant_text(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        if event["kind"] == "message" and event["role"] == "assistant" and event.get("body"):
            return event["body"]
    return ""


def format_detail_stats(events: list[dict[str, Any]]) -> str:
    labels = {
        "command": "commands",
        "tool_use": "tools",
        "tool_result": "results",
        "file_edit": "patches",
        "thinking": "thinking",
        "status": "status",
    }
    counts: dict[str, int] = {}
    for event in events:
        label = labels.get(event["kind"])
        if not label:
            continue
        counts[label] = counts.get(label, 0) + 1
    return " · ".join(f"{count} {label}" for label, count in sorted(counts.items()))


PROSE_KINDS = frozenset({"message", "thinking"})


def render_markdown(text: str) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = re.match(r"^```(\w*)\s*$", line)
        if fence:
            lang = fence.group(1)
            i += 1
            code_lines: list[str] = []
            while i < len(lines) and not re.match(r"^```\s*$", lines[i]):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            code_html = html.escape("\n".join(code_lines))
            lang_attr = f' class="lang-{html.escape(lang)}"' if lang else ""
            out.append(f"<pre><code{lang_attr}>{code_html}</code></pre>")
            continue
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{render_inline_md(heading.group(2))}</h{level}>")
            i += 1
            continue
        if line.startswith(">"):
            quote_lines: list[str] = []
            while i < len(lines) and lines[i].startswith(">"):
                quote_lines.append(re.sub(r"^>\s?", "", lines[i]))
                i += 1
            quote_text = "\n".join(quote_lines).strip()
            out.append(f"<blockquote><p>{render_inline_md(quote_text)}</p></blockquote>")
            continue
        if re.match(r"^\s*[-*+]\s+", line):
            items: list[str] = []
            while i < len(lines) and re.match(r"^\s*[-*+]\s+", lines[i]):
                items.append(re.sub(r"^\s*[-*+]\s+", "", lines[i]))
                i += 1
            out.append(
                "<ul>"
                + "".join(f"<li>{render_inline_md(item)}</li>" for item in items)
                + "</ul>"
            )
            continue
        if re.match(r"^\s*\d+[.)]\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\s*\d+[.)]\s+", lines[i]):
                items.append(re.sub(r"^\s*\d+[.)]\s+", "", lines[i]))
                i += 1
            out.append(
                "<ol>"
                + "".join(f"<li>{render_inline_md(item)}</li>" for item in items)
                + "</ol>"
            )
            continue
        if not line.strip():
            i += 1
            continue
        para_lines: list[str] = []
        while i < len(lines) and lines[i].strip() and not _is_markdown_block_start(lines[i]):
            para_lines.append(lines[i])
            i += 1
        out.append(f"<p>{render_inline_md(chr(10).join(para_lines))}</p>")
    return "".join(out)


def _is_markdown_block_start(line: str) -> bool:
    return bool(
        re.match(r"^```", line)
        or re.match(r"^#{1,6}\s", line)
        or line.startswith(">")
        or re.match(r"^\s*[-*+]\s", line)
        or re.match(r"^\s*\d+[.)]\s", line)
    )


def render_inline_md(text: str) -> str:
    if not text:
        return ""
    code_spans: list[str] = []

    def stash_code(match: re.Match[str]) -> str:
        code_spans.append(html.escape(match.group(1)))
        return f"\x00CODE{len(code_spans) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", stash_code, text)
    text = html.escape(text)
    text = re.sub(r"\*\*([^*\n]+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__([^_\n]+?)__", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"(?<![_\w])_(?!\s)([^_\n]+?)(?<!\s)_(?![_\w])", r"<em>\1</em>", text)
    text = re.sub(
        r"\[([^\]\n]+)\]\(([^)\s]+)\)",
        lambda m: f'<a href="{m.group(2)}" rel="noopener">{m.group(1)}</a>',
        text,
    )
    text = re.sub(
        r"\x00CODE(\d+)\x00",
        lambda m: f"<code>{code_spans[int(m.group(1))]}</code>",
        text,
    )
    return text


def render_text_html(text: str) -> str:
    return render_markdown(text)


def page_filename(page_num: int) -> str:
    return f"page-{page_num:03d}.html"


def pagination_html(current_page: int, total_pages: int) -> str:
    pieces = ['<nav class="pagination" aria-label="Pagination">']
    pieces.append('<a href="index.html">Index</a>')
    if current_page > 1:
        pieces.append(f'<a href="{page_filename(current_page - 1)}">&larr; Prev</a>')
    else:
        pieces.append('<span class="disabled">&larr; Prev</span>')
    for page_num in range(1, total_pages + 1):
        if page_num == current_page:
            pieces.append(f'<span class="current">{page_num}</span>')
        else:
            pieces.append(f'<a href="{page_filename(page_num)}">{page_num}</a>')
    if current_page < total_pages:
        pieces.append(f'<a href="{page_filename(current_page + 1)}">Next &rarr;</a>')
    else:
        pieces.append('<span class="disabled">Next &rarr;</span>')
    pieces.append("</nav>")
    return "".join(pieces)


def index_pagination_html(total_pages: int) -> str:
    pieces = ['<nav class="pagination" aria-label="Pagination">']
    pieces.append('<span class="current">Index</span>')
    pieces.append('<span class="disabled">&larr; Prev</span>')
    for page_num in range(1, total_pages + 1):
        pieces.append(f'<a href="{page_filename(page_num)}">{page_num}</a>')
    if total_pages:
        pieces.append('<a href="page-001.html">Next &rarr;</a>')
    else:
        pieces.append('<span class="disabled">Next &rarr;</span>')
    pieces.append("</nav>")
    return "".join(pieces)


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
    output_dir = write_html_archive(args.output, data, args.page_prompts)
    index_path = output_dir / "index.html"

    print(f"Copied source files under: {copy_root}")
    print(f"Sessions: {len(data['sessions'])}")
    print(f"Events: {len(data['events'])}")
    if REDACTION_COUNTS:
        summary = ", ".join(f"{name}={count}" for name, count in sorted(REDACTION_COUNTS.items()))
        total = sum(REDACTION_COUNTS.values())
        print(f"Redacted {total} secret match(es): {summary}")
    print(f"Wrote: {index_path.resolve()}")

    if args.open:
        webbrowser.open(index_path.resolve().as_uri())
    return 0


PAGE_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__PAGE_TITLE__</title>
<link rel="icon" href="data:,">
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
h1 a { color: inherit; text-decoration: none; }
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
.chips { display: flex; flex-wrap: wrap; gap: 6px; align-content: flex-start; align-items: flex-start; }
.control-group .chips {
  height: 141px;
  overflow-y: auto;
  padding-right: 2px;
}
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
.pagination {
  display: flex;
  justify-content: center;
  gap: 8px;
  margin: 14px 0;
  flex-wrap: wrap;
}
.pagination a,
.pagination span {
  padding: 5px 10px;
  border-radius: 6px;
  text-decoration: none;
  font-size: 0.85rem;
}
.pagination a {
  background: var(--card);
  color: var(--user-border);
  border: 1px solid var(--user-border);
}
.pagination a:hover { background: var(--user-bg); }
.pagination .current {
  background: var(--user-border);
  color: #fff;
}
.pagination .disabled {
  color: var(--muted);
  border: 1px solid #ddd;
}
.timeline-scroll { padding-bottom: 32px; overflow: visible; }
.lane-grid {
  display: grid;
  gap: 8px;
  align-items: start;
  min-width: min-content;
}
.corner-cell {
  position: sticky;
  top: 0;
  z-index: 6;
  background: var(--bg);
}
.lane-header, .time-cell { background: var(--bg); }
.lane-header {
  position: sticky;
  top: 0;
  z-index: 5;
  cursor: pointer;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 8px 26px 8px 8px;
  min-height: 70px;
  text-align: left;
  font: inherit;
  color: inherit;
  transition: background 0.12s ease;
}
.lane-header:hover { background: #eef2f6; }
.lane-header:focus-visible { outline: 2px solid var(--user-border); outline-offset: 1px; }
.lane-header.claude { border-top: 4px solid var(--claude); }
.lane-header.codex { border-top: 4px solid var(--codex); }
.lane-header .lane-toggle-icon {
  position: absolute;
  top: 6px;
  right: 8px;
  color: var(--muted);
  font-size: 1.05rem;
  line-height: 1;
}
.lane-header:hover .lane-toggle-icon { color: var(--user-border); }
.lane-header.collapsed {
  padding: 6px 2px;
  min-height: 52px;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}
.lane-header.collapsed .lane-toggle-icon {
  position: static;
  font-size: 1.1rem;
}
.lane-cell-collapsed {
  min-height: 0;
  border-top: none;
}
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
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 7px 10px;
  background: rgba(0,0,0,0.035);
  font-size: 0.78rem;
}
.event-title {
  min-width: 0;
  font-weight: 700;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.event-actions {
  display: flex;
  align-items: center;
  flex: 0 0 auto;
  gap: 6px;
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
.event-body p { margin: 0 0 8px; }
.event-body p:last-child { margin-bottom: 0; }
.event-body ul, .event-body ol { margin: 0 0 8px; padding-left: 22px; }
.event-body li { margin-bottom: 3px; }
.event-body li:last-child { margin-bottom: 0; }
.event-body code {
  background: #ececec;
  color: inherit;
  padding: 1px 6px;
  border-radius: 5px;
  font-size: 0.88em;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.event-body pre code {
  background: transparent;
  color: inherit;
  padding: 0;
  border-radius: 0;
  font-size: inherit;
}
.event-body h1, .event-body h2, .event-body h3,
.event-body h4, .event-body h5, .event-body h6 {
  margin: 4px 0 6px;
  font-size: 1rem;
  line-height: 1.3;
}
.event-body h1 { font-size: 1.15rem; }
.event-body h2 { font-size: 1.08rem; }
.event-body blockquote {
  margin: 0 0 8px;
  padding: 4px 10px;
  border-left: 3px solid var(--line);
  color: var(--muted);
}
.event-body a { color: var(--user-border); }
.detail-collapsed {
  box-shadow: none;
}
.detail-collapsed .event-head {
  padding-block: 5px;
}
.detail-collapsed .event-title {
  font-weight: 600;
}
.expand {
  flex: 0 0 auto;
  padding: 2px 7px;
  font-size: 0.72rem;
  line-height: 1.25;
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
    <h1><a href="index.html">__PAGE_HEADING__</a></h1>
    <div class="summary" id="summary"></div>
    <div class="controls">
      <div class="control-group">
        <div class="control-title">Search</div>
        <input id="search-input" type="search" placeholder="Search transcript">
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
        <div class="control-title">Details</div>
        <div class="chips" id="detail-filter"></div>
      </div>
      <div class="control-group">
        <div class="control-title">Display</div>
        <div class="chips">
          <label class="chip"><input type="checkbox" id="dense-toggle"><span>Dense</span></label>
          <label class="chip"><input type="checkbox" id="expand-details-toggle"><span>Expand details</span></label>
        </div>
      </div>
    </div>
  </div>
</div>
__PAGINATION_HTML__
<main id="app"></main>
__PAGINATION_HTML__
<script id="transcript-data" type="application/json">__DATA_JSON__</script>
<script>
const data = JSON.parse(document.getElementById("transcript-data").textContent);
const state = { query: "", dense: false, expandDetails: false, collapsedSessions: new Set() };
const sessionById = new Map(data.sessions.map(session => [session.id, session]));
const DETAIL_GROUPS = [
  { id: "commands", label: "Commands" },
  { id: "results", label: "Results" },
  { id: "patches", label: "Patches" },
  { id: "web", label: "Web" },
  { id: "thinking", label: "Thinking" },
  { id: "status", label: "Status" },
  { id: "tools", label: "Other tools" }
];
const detailGroupById = new Map(DETAIL_GROUPS.map(group => [group.id, group]));

function unique(values) {
  return Array.from(new Set(values.filter(Boolean))).sort();
}

function isCoreEvent(event) {
  return event.kind === "message" && (event.role === "user" || event.role === "assistant");
}

function detailGroupForEvent(event) {
  if (isCoreEvent(event)) return "core";
  const title = String(event.title || "").toLowerCase();
  if (event.kind === "command") return "commands";
  if (event.kind === "file_edit") return "patches";
  if (event.kind === "tool_result" && title.includes("web")) return "web";
  if (event.kind === "tool_use" && title.includes("web")) return "web";
  if (event.kind === "tool_result") return "results";
  if (event.kind === "thinking") return "thinking";
  if (event.kind === "status") return "status";
  return "tools";
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
  const detailGroups = DETAIL_GROUPS
    .map(group => group.id)
    .filter(id => data.events.some(event => detailGroupForEvent(event) === id));
  makeCheckboxes("detail-filter", "detail", detailGroups, value => detailGroupById.get(value)?.label || value);

  document.getElementById("search-input").addEventListener("input", event => {
    state.query = event.target.value.toLowerCase().trim();
    render();
  });
  document.getElementById("dense-toggle").addEventListener("change", event => {
    state.dense = event.target.checked;
    render();
  });
  document.getElementById("expand-details-toggle").addEventListener("change", event => {
    state.expandDetails = event.target.checked;
    applyDetailExpansion();
  });
}

function filteredEvents() {
  const providers = selectedValues("provider");
  const days = selectedValues("day");
  const repos = selectedValues("repo");
  const details = selectedValues("detail");
  return data.events.filter(event => {
    const session = sessionById.get(event.session_id);
    if (!session) return false;
    if (!providers.has(event.provider)) return false;
    if (!days.has(event.day)) return false;
    if (!repos.has(session.repo)) return false;
    if (!isCoreEvent(event) && !details.has(detailGroupForEvent(event))) return false;
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
  const activeSessionIds = unique(events.map(event => event.session_id));
  const activeSessions = data.sessions.filter(session => activeSessionIds.includes(session.id));
  document.getElementById("summary").textContent =
    `${events.length} events on page ${data.page} of ${data.total_pages} · ${activeSessions.length} sessions on this page`;
  const app = document.getElementById("app");
  if (!events.length) {
    app.innerHTML = `<div class="empty">No events match the current filters.</div>`;
    return;
  }
  app.innerHTML = renderLanes(activeSessions, events);
  wireLaneHeaders();
  wireExpandButtons();
}

function renderLanes(sessions, events) {
  if (!sessions.length) {
    return `<div class="empty">No sessions have events on this page.</div>`;
  }
  const laneCols = sessions
    .map(session => state.collapsedSessions.has(session.id) ? "28px" : "minmax(min(100%, 320px), 800px)")
    .join(" ");
  const columns = `112px ${laneCols}`;
  const byMinute = new Map();
  events.forEach(event => {
    if (!byMinute.has(event.display_minute)) byMinute.set(event.display_minute, []);
    byMinute.get(event.display_minute).push(event);
  });
  const minutes = Array.from(byMinute.keys()).sort();
  const headers = `<div class="corner-cell"></div>${sessions.map(session => {
    const collapsed = state.collapsedSessions.has(session.id);
    const classes = `lane-header ${escapeHtml(session.provider)}${collapsed ? " collapsed" : ""}`;
    const toggleIcon = collapsed
      ? `<span class="lane-toggle-icon" aria-hidden="true">›</span>`
      : `<span class="lane-toggle-icon" aria-hidden="true">‹</span>`;
    const title = collapsed
      ? toggleIcon
      : `<div class="session-title">${escapeHtml(session.label)}</div>
         <div class="session-meta">${escapeHtml(session.cwd)}</div>
         ${toggleIcon}`;
    const aria = collapsed ? "false" : "true";
    return `
    <button type="button" class="${classes}" data-session-id="${escapeHtml(session.id)}" aria-expanded="${aria}" title="${escapeHtml(session.label)}${collapsed ? " (click to expand)" : " (click to collapse)"}">
      ${title}
    </button>
  `;
  }).join("")}`;
  const rows = minutes.map(minute => {
    const minuteEvents = byMinute.get(minute);
    const cells = sessions.map(session => {
      const collapsed = state.collapsedSessions.has(session.id);
      if (collapsed) {
        return `<div class="lane-cell lane-cell-collapsed"></div>`;
      }
      const cellEvents = minuteEvents.filter(event => event.session_id === session.id);
      return `<div class="lane-cell">${cellEvents.map(renderEventCard).join("")}</div>`;
    }).join("");
    return `<div class="time-cell">${escapeHtml(minute)}</div>${cells}`;
  }).join("");
  return `<div class="timeline-scroll"><div class="lane-grid" style="grid-template-columns: ${columns}">${headers}${rows}</div></div>`;
}

function wireLaneHeaders() {
  document.querySelectorAll(".lane-header[data-session-id]").forEach(header => {
    header.addEventListener("click", () => {
      const sessionId = header.dataset.sessionId;
      if (state.collapsedSessions.has(sessionId)) {
        state.collapsedSessions.delete(sessionId);
      } else {
        state.collapsedSessions.add(sessionId);
      }
      render();
    });
  });
}

function renderEventCard(event) {
  const preKinds = new Set(["command", "tool_use", "tool_result", "file_edit", "status"]);
  const isCore = isCoreEvent(event);
  const detailGroup = detailGroupForEvent(event);
  const expanded = isCore || state.expandDetails;
  const body = preKinds.has(event.kind)
    ? `<pre>${escapeHtml(event.body)}</pre>`
    : (event.body_html || escapeHtml(event.body));
  const classes = [
    "event-card",
    isCore ? "core-event" : "detail-event",
    expanded ? "detail-expanded" : "detail-collapsed",
    `detail-${detailGroup}`,
    event.role,
    event.kind,
    event.provider,
    event.is_error ? "error" : ""
  ].join(" ");
  const detailsToggle = isCore ? "" : `<button class="expand" type="button" aria-expanded="${expanded ? "true" : "false"}">${expanded ? "Hide details" : "Show details"}</button>`;
  return `
    <article class="${classes}" id="${escapeHtml(event.id)}">
      <div class="event-head">
        <div class="event-title">${escapeHtml(event.title)}</div>
        <div class="event-actions">
          <a class="event-time" href="#${escapeHtml(event.id)}">${escapeHtml(event.display_time.split(" ")[1] || event.display_time)}</a>
          ${detailsToggle}
        </div>
      </div>
      <div class="event-body"${expanded ? "" : " hidden"}>${body}</div>
    </article>
  `;
}

function setDetailCardExpanded(card, expanded) {
  const body = card.querySelector(".event-body");
  const button = card.querySelector(".expand");
  if (!body || !button) return;
  card.classList.toggle("detail-collapsed", !expanded);
  card.classList.toggle("detail-expanded", expanded);
  body.hidden = !expanded;
  button.textContent = expanded ? "Hide details" : "Show details";
  button.setAttribute("aria-expanded", expanded ? "true" : "false");
}

function applyDetailExpansion() {
  document.querySelectorAll(".event-card.detail-event").forEach(card => {
    setDetailCardExpanded(card, state.expandDetails);
  });
}

function wireExpandButtons() {
  document.querySelectorAll(".event-card.detail-event").forEach(card => {
    const button = card.querySelector(".expand");
    if (!button) return;
    setDetailCardExpanded(card, state.expandDetails);
    button.addEventListener("click", () => {
      setDetailCardExpanded(card, card.classList.contains("detail-collapsed"));
    });
  });
}

initFilters();
render();
</script>
</body>
</html>
"""


INDEX_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Koda Timeline - Index</title>
<link rel="icon" href="data:,">
<style>
:root {
  --bg: #f5f5f5;
  --card: #ffffff;
  --text: #212121;
  --muted: #6f7782;
  --line: #d8dde4;
  --user-bg: #e3f2fd;
  --user-border: #1976d2;
  --assistant-border: #8f98a3;
  --commit-bg: #fff3e0;
  --commit-border: #ff9800;
  --claude: #b05a2a;
  --codex: #1d6b6b;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 24px 16px;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  line-height: 1.5;
}
.container { max-width: 980px; margin: 0 auto; }
.header-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  border-bottom: 2px solid var(--user-border);
  padding-bottom: 10px;
  margin-bottom: 24px;
}
h1 { margin: 0; font-size: 1.55rem; }
.search {
  display: flex;
  align-items: center;
  gap: 8px;
}
.search input {
  width: min(360px, 70vw);
  border: 1px solid #9aa4ae;
  border-radius: 6px;
  padding: 7px 10px;
  background: #fff;
  color: var(--text);
  font: inherit;
}
.search button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: 6px;
  background: var(--user-border);
  color: #fff;
  padding: 8px 12px;
  cursor: pointer;
}
.pagination {
  display: flex;
  justify-content: center;
  gap: 8px;
  margin: 24px 0;
  flex-wrap: wrap;
}
.pagination a,
.pagination span {
  padding: 7px 12px;
  border-radius: 6px;
  text-decoration: none;
  font-size: 0.9rem;
}
.pagination a {
  background: var(--card);
  color: var(--user-border);
  border: 1px solid var(--user-border);
}
.pagination a:hover { background: var(--user-bg); }
.pagination .current {
  background: var(--user-border);
  color: #fff;
}
.pagination .disabled {
  color: var(--muted);
  border: 1px solid #ddd;
}
.stats {
  color: var(--muted);
  margin: 0 0 22px;
  font-size: 1rem;
}
.index-item,
.index-commit {
  margin-bottom: 14px;
  overflow: hidden;
  border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.1);
}
.index-item {
  background: var(--user-bg);
  border-left: 4px solid var(--user-border);
}
.index-item.claude { border-left-color: var(--claude); }
.index-item.codex { border-left-color: var(--codex); }
.index-item a,
.index-commit a {
  display: block;
  color: inherit;
  text-decoration: none;
}
.index-item a:hover { background: rgba(25, 118, 210, 0.08); }
.index-item-header,
.index-commit-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 8px 14px;
  background: rgba(0,0,0,0.035);
  font-size: 0.85rem;
}
.index-item-number {
  color: var(--user-border);
  font-weight: 700;
}
.index-item-content { padding: 14px; }
.index-item-content p {
  margin: 0 0 10px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.index-item-content p:last-child { margin-bottom: 0; }
.index-item-content ul,
.index-item-content ol { margin: 0 0 10px; padding-left: 22px; }
.index-item-content li { margin-bottom: 3px; }
.index-item-content code {
  background: #ececec;
  color: inherit;
  padding: 1px 6px;
  border-radius: 5px;
  font-size: 0.9em;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.index-item-content pre {
  margin: 0 0 10px;
  padding: 10px;
  background: rgba(0,0,0,0.06);
  border-radius: 6px;
  overflow-x: auto;
  white-space: pre-wrap;
}
.index-item-content pre code {
  background: transparent;
  padding: 0;
}
.index-item-content h1, .index-item-content h2, .index-item-content h3,
.index-item-content h4, .index-item-content h5, .index-item-content h6 {
  margin: 4px 0 6px;
  font-size: 1rem;
}
.index-item-content a { color: var(--user-border); }
.index-item-response {
  margin-top: 12px;
  padding: 12px;
  background: var(--card);
  border-left: 3px solid var(--assistant-border);
  border-radius: 6px;
}
.index-item-response-label {
  margin-bottom: 6px;
  color: var(--muted);
  font-size: 0.74rem;
  font-weight: 700;
  text-transform: uppercase;
}
.index-item-stats {
  padding: 8px 14px 11px;
  color: var(--muted);
  border-top: 1px solid rgba(0,0,0,0.06);
  font-size: 0.85rem;
}
.index-commit {
  padding: 0;
  background: var(--commit-bg);
  border-left: 4px solid var(--commit-border);
}
.index-commit-hash {
  color: #e65100;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-weight: 700;
}
.index-commit-msg {
  padding: 10px 14px 12px;
  color: #5d4037;
}
time { color: var(--muted); font-size: 0.82rem; text-align: right; }
.hidden { display: none; }
@media (max-width: 680px) {
  body { padding: 12px 8px; }
  .header-row { align-items: stretch; }
  .search, .search input { width: 100%; }
}
</style>
</head>
<body>
<div class="container">
  <div class="header-row">
    <h1>Koda Timeline</h1>
    <div class="search">
      <input id="search-input" type="search" placeholder="Search..." aria-label="Search index">
      <button id="search-button" type="button" aria-label="Search">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"></circle><path d="m21 21-4.35-4.35"></path></svg>
      </button>
    </div>
  </div>
  __PAGINATION_HTML__
  <p class="stats">__PROMPT_COUNT__ prompts &middot; __MESSAGE_COUNT__ messages &middot; __TOOL_CALL_COUNT__ tool calls &middot; __COMMIT_COUNT__ commits &middot; __PAGE_COUNT__ pages</p>
  <div id="index-items">__INDEX_ITEMS__</div>
  __PAGINATION_HTML__
</div>
<script>
const searchInput = document.getElementById("search-input");
const searchButton = document.getElementById("search-button");
function filterIndex() {
  const query = searchInput.value.trim().toLowerCase();
  document.querySelectorAll(".index-item, .index-commit").forEach(item => {
    item.classList.toggle("hidden", query && !item.textContent.toLowerCase().includes(query));
  });
}
searchInput.addEventListener("input", filterIndex);
searchButton.addEventListener("click", filterIndex);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
