#!/usr/bin/env python3
"""Generate a self-contained HTML timeline from multiple coding-agent sessions."""

from __future__ import annotations

import argparse
import dataclasses
import html
import json
import os
import re
import shutil
import tempfile
import webbrowser
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from string import Template
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import markdown

from adapters import (
    ADAPTERS,
    Event,
    SessionCandidate,
    first_useful_summary,
)


SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = SCRIPT_DIR / "templates"
STATIC_DIR = SCRIPT_DIR / "static"
COMMIT_OUTPUT_RE = re.compile(r"\[[\w\-/]+ ([a-f0-9]{7,})\] (.+?)(?:\n|$)", re.I)
DEFAULT_PROMPTS_PER_PAGE = 5
ASSISTANT_INDEX_SUMMARY_CHARS = 900
PROSE_KINDS = frozenset({"message", "thinking"})


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


def parse_args() -> argparse.Namespace:
    provider_names = tuple(ADAPTERS.keys())
    provider_list = ", ".join(provider_names)
    parser = argparse.ArgumentParser(
        description=f"Join coding-agent JSONL sessions ({provider_list}) into one HTML timeline."
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
        choices=provider_names,
        help="Provider to include. Repeatable. Default: all registered providers.",
    )
    for adapter in ADAPTERS.values():
        parser.add_argument(
            f"--{adapter.name}-source",
            type=Path,
            default=adapter.default_source,
            dest=f"{adapter.name}_source",
            help=f"{adapter.name.title()} source. Default: {adapter.default_source}",
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


def adapter_sources(args: argparse.Namespace) -> dict[str, Path]:
    return {
        name: getattr(args, f"{name}_source").expanduser() for name in ADAPTERS
    }


def discover_all(providers: set[str], args: argparse.Namespace) -> list[SessionCandidate]:
    sources = adapter_sources(args)
    candidates: list[SessionCandidate] = []
    opts = {"include_subagents": not args.no_subagents}
    for name in providers:
        adapter = ADAPTERS[name]
        candidates.extend(adapter.discover(sources[name], **opts))
    return candidates


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
    candidates: list[SessionCandidate], copy_root: Path, sources: dict[str, Path]
) -> list[SessionCandidate]:
    copied_candidates: list[SessionCandidate] = []
    for candidate in candidates:
        base_source = sources[candidate.provider]
        dest_path = copy_one_source(
            candidate.source_path, copy_root, candidate.provider, base_source
        )
        copied_extras = [
            copy_one_source(extra, copy_root, candidate.provider, base_source)
            for extra in candidate.copied_extra_paths
            if extra.exists()
        ]
        copied_candidates.append(
            dataclasses.replace(
                candidate, copied_path=dest_path, copied_extra_paths=copied_extras
            )
        )
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
        adapter = ADAPTERS[candidate.provider]
        path = candidate.copied_path or candidate.source_path
        parsed = adapter.parse_events(candidate, path)
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
        extras = candidate.extras
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
                "is_subagent": bool(extras.get("is_subagent")),
                "parent_session_id": extras.get("parent_session_id"),
                "agent_id": extras.get("agent_id"),
                "description": redact_secrets(extras.get("description", "")),
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


def load_template(name: str) -> Template:
    return Template((TEMPLATES_DIR / name).read_text(encoding="utf-8"))


def copy_static_assets(output_dir: Path) -> None:
    dest = output_dir / "static"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(STATIC_DIR, dest)


def generate_provider_styles() -> str:
    """Per-provider color accents, emitted so adding an adapter requires no CSS edits."""
    rules: list[str] = []
    for adapter in ADAPTERS.values():
        rules.append(
            f".lane-header.{adapter.name} {{ border-top: 4px solid {adapter.color}; }}\n"
            f".index-item.{adapter.name} {{ border-left-color: {adapter.color}; }}"
        )
    return "\n".join(rules)


def write_html_archive(output: Path, data: dict[str, Any], prompts_per_page: int) -> Path:
    if prompts_per_page < 1:
        raise SystemExit("--page-prompts must be at least 1")

    output_dir = resolve_output_dir(output)
    if output_dir.exists() and not output_dir.is_dir():
        raise SystemExit(f"Output path exists and is not a directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    for old_page in output_dir.glob("page-*.html"):
        old_page.unlink()
    copy_static_assets(output_dir)

    page_template = load_template("page.html")
    index_template = load_template("index.html")
    provider_styles = generate_provider_styles()

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
        write_page_html(
            output_dir / page_filename(page_num), page_data, page_template, provider_styles
        )

    index_data = build_index_data(data, turns, total_pages, prompts_per_page, session_by_id)
    write_index_html(
        output_dir / "index.html", index_data, total_pages, index_template, provider_styles
    )
    return output_dir


def write_page_html(
    path: Path, data: dict[str, Any], template: Template, provider_styles: str
) -> None:
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    title = f"Koda Timeline - page {data['page']}/{data['total_pages']}"
    html_text = template.substitute(
        page_title=title,
        page_heading=title,
        pagination_html=pagination_html(data["page"], data["total_pages"]),
        provider_styles=provider_styles,
        data_json=data_json,
    )
    path.write_text(html_text, encoding="utf-8")


def write_index_html(
    path: Path,
    index_data: dict[str, Any],
    total_pages: int,
    template: Template,
    provider_styles: str,
) -> None:
    html_text = template.substitute(
        index_items=index_data["items_html"],
        pagination_html=index_pagination_html(total_pages),
        provider_styles=provider_styles,
        prompt_count=index_data["prompt_count"],
        message_count=index_data["message_count"],
        tool_call_count=index_data["tool_call_count"],
        commit_count=index_data["commit_count"],
        page_count=total_pages,
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
                    render_index_commit(
                        commit_hash, commit_title, commit_ts or prompt_event["timestamp"]
                    ),
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
            f"{render_markdown(first_useful_summary(final_assistant, ASSISTANT_INDEX_SUMMARY_CHARS))}"
            "</div>"
        )
    return f"""
<article class="index-item {html.escape(provider)}">
  <a href="{link}">
    <div class="index-item-header">
      <span class="index-item-number">#{prompt_number}</span>
      <time datetime="{html.escape(prompt_event['timestamp'])}">{html.escape(prompt_event['display_time'])}</time>
    </div>
    <div class="index-item-content">{render_markdown(turn["prompt_text"])}{final_html}</div>
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


_MARKDOWN = markdown.Markdown(extensions=["fenced_code"], output_format="html")


def render_markdown(text: str) -> str:
    if not text:
        return ""
    _MARKDOWN.reset()
    return _MARKDOWN.convert(text)


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
        subagent = " subagent" if candidate.extras.get("is_subagent") else ""
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
    providers = set(args.provider or ADAPTERS.keys())

    candidates = discover_all(providers, args)
    selected = select_candidates(candidates, since, until, repo_folders)

    if args.dry_run:
        print_dry_run(selected, display_tz)
        return 0

    copy_root = prepare_copy_root(args.copy_root)
    copied = copy_selected_sources(selected, copy_root, adapter_sources(args))
    events = parse_events_for_candidates(copied, since, until)
    if repo_folders:
        events = [event for event in events if cwd_matches_repo(event.cwd, repo_folders)]

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


if __name__ == "__main__":
    raise SystemExit(main())
