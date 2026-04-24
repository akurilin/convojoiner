"""End-to-end happy-path tests.

Spawn the CLI as a subprocess against tiny hand-written JSONL fixtures
in tests/data/ and check the generated archive. These are deliberately
brittle smoke tests — their job is to catch "the pipeline broke" bugs,
not to exhaustively specify output structure. Keep assertions at the
semantic-content level (prompt text appears, session label appears,
filter actually filters) rather than exact HTML/CSS/JSON shape.

If an assertion here breaks because we intentionally changed output
structure, update the assertion. If it breaks because session parsing
regressed, that's the signal we're looking for.

NOTE: these fixtures mirror a plausible subset of Claude/Codex session
JSONL — they're not a schema conformance guarantee. Upstream schema
drift will not be caught here.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixtures import fake_aws_access_key

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(__file__).resolve().parent / "data"
CLAUDE_FIXTURE = DATA_DIR / "claude" / "projects"
CODEX_FIXTURE = DATA_DIR / "codex" / "sessions"
CLINE_FIXTURE = DATA_DIR / "cline"
CLAUDE_SECRET_FIXTURE = DATA_DIR / "claude_with_secret" / "projects"
WORKTREES_FIXTURE = DATA_DIR / "claude_worktrees" / "projects"


def run_cli(
    *,
    output: Path,
    claude_source: Path,
    codex_source: Path,
    cline_source: Path,
    extra: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    args = [
        sys.executable,
        str(REPO_ROOT / "convojoiner.py"),
        "--claude-source",
        str(claude_source),
        "--codex-source",
        str(codex_source),
        "--cline-source",
        str(cline_source),
        "--output",
        str(output),
        "--since",
        "2019-01-01",
    ]
    if extra:
        args.extend(extra)
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _common_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "output": tmp_path / "archive",
        "missing_claude": tmp_path / "no-claude-here",
        "missing_codex": tmp_path / "no-codex-here",
        "missing_cline": tmp_path / "no-cline-here",
    }


def test_claude_only_run(tmp_path: Path) -> None:
    p = _common_paths(tmp_path)
    result = run_cli(
        output=p["output"],
        claude_source=CLAUDE_FIXTURE,
        codex_source=p["missing_codex"],
        cline_source=p["missing_cline"],
        extra=["--provider", "claude"],
    )

    assert result.returncode == 0, result.stderr
    assert (p["output"] / "index.html").exists()
    assert (p["output"] / "page-001.html").exists()
    assert (p["output"] / "static" / "styles.css").exists()
    assert (p["output"] / "static" / "page.js").exists()

    index = (p["output"] / "index.html").read_text(encoding="utf-8")
    assert "please refactor the auth module" in index
    assert "Claude aaa11111" in index  # session label from short_id
    # Other providers' content doesn't leak in
    assert "codex-session" not in index
    assert "deployment script" not in index
    assert "login feature" not in index


def test_codex_only_run(tmp_path: Path) -> None:
    p = _common_paths(tmp_path)
    result = run_cli(
        output=p["output"],
        claude_source=p["missing_claude"],
        codex_source=CODEX_FIXTURE,
        cline_source=p["missing_cline"],
        extra=["--provider", "codex"],
    )

    assert result.returncode == 0, result.stderr
    assert (p["output"] / "index.html").exists()
    assert (p["output"] / "page-001.html").exists()

    index = (p["output"] / "index.html").read_text(encoding="utf-8")
    assert "write a script to deploy the api" in index
    assert "Codex codex-se" in index  # short_id of "codex-session-bbb22222"
    # Other providers' content doesn't leak in
    assert "refactor the auth module" not in index
    assert "login feature" not in index


def test_cline_only_run(tmp_path: Path) -> None:
    p = _common_paths(tmp_path)
    result = run_cli(
        output=p["output"],
        claude_source=p["missing_claude"],
        codex_source=p["missing_codex"],
        cline_source=CLINE_FIXTURE,
        extra=["--provider", "cline"],
    )

    assert result.returncode == 0, result.stderr
    assert (p["output"] / "index.html").exists()
    assert (p["output"] / "page-001.html").exists()

    index = (p["output"] / "index.html").read_text(encoding="utf-8")
    assert "help me implement a login feature" in index
    assert "Cline 17000000" in index  # short_id of taskId "1700000000000"
    # Other providers' content doesn't leak in
    assert "refactor the auth module" not in index
    assert "deployment script" not in index


def test_all_providers_interleaved(tmp_path: Path) -> None:
    p = _common_paths(tmp_path)
    result = run_cli(
        output=p["output"],
        claude_source=CLAUDE_FIXTURE,
        codex_source=CODEX_FIXTURE,
        cline_source=CLINE_FIXTURE,
    )

    assert result.returncode == 0, result.stderr
    index = (p["output"] / "index.html").read_text(encoding="utf-8")
    assert "please refactor the auth module" in index
    assert "write a script to deploy the api" in index
    assert "help me implement a login feature" in index
    assert "Claude aaa11111" in index
    assert "Codex codex-se" in index
    assert "Cline 17000000" in index


def test_redaction_applied_end_to_end(tmp_path: Path) -> None:
    """Copy the placeholder fixture, substitute in a fake AWS key, run the
    pipeline, and verify the secret got redacted before hitting HTML."""
    staged_source = tmp_path / "staged-claude"
    shutil.copytree(CLAUDE_SECRET_FIXTURE, staged_source)

    target_file = next(staged_source.rglob("*.jsonl"))
    secret = fake_aws_access_key()
    target_file.write_text(
        target_file.read_text(encoding="utf-8").replace("<FAKE_AWS_KEY>", secret),
        encoding="utf-8",
    )

    p = _common_paths(tmp_path)
    result = run_cli(
        output=p["output"],
        claude_source=staged_source,
        codex_source=p["missing_codex"],
        cline_source=p["missing_cline"],
        extra=["--provider", "claude"],
    )

    assert result.returncode == 0, result.stderr

    # The secret must appear nowhere in the output; its redaction marker must.
    output_texts = [path.read_text(encoding="utf-8") for path in p["output"].rglob("*.html")]
    joined = "\n".join(output_texts)
    assert secret not in joined
    assert "[REDACTED:aws-access-key]" in joined


def test_repo_folder_prefix_groups_worktrees(tmp_path: Path) -> None:
    """Simulate a main repo, two worktrees (dashed suffix), a decoy repo that
    shares a prefix but has a word-continuation boundary, and a fully unrelated
    project. --repo-folder-prefix /tmp/demo-repo should include the first three
    and exclude the last two."""
    p = _common_paths(tmp_path)
    result = run_cli(
        output=p["output"],
        claude_source=WORKTREES_FIXTURE,
        codex_source=p["missing_codex"],
        cline_source=p["missing_cline"],
        extra=[
            "--provider",
            "claude",
            "--repo-folder-prefix",
            "/tmp/demo-repo",
        ],
    )

    assert result.returncode == 0, result.stderr
    index = (p["output"] / "index.html").read_text(encoding="utf-8")

    # Three included: main repo + two worktrees
    assert "main-branch task" in index
    assert "feature-x-worktree task" in index
    assert "bugfix-worktree task" in index

    # Two excluded: similar-prefix decoy and unrelated project
    assert "decoy-repo task" not in index
    assert "other-project task" not in index


def test_repo_folder_without_prefix_excludes_sibling_worktrees(tmp_path: Path) -> None:
    """Sanity check that plain --repo-folder (strict containment) excludes
    sibling worktrees — this is the behavior that motivated the new flag."""
    p = _common_paths(tmp_path)
    result = run_cli(
        output=p["output"],
        claude_source=WORKTREES_FIXTURE,
        codex_source=p["missing_codex"],
        cline_source=p["missing_cline"],
        extra=[
            "--provider",
            "claude",
            "--repo-folder",
            "/tmp/demo-repo",
        ],
    )

    assert result.returncode == 0, result.stderr
    index = (p["output"] / "index.html").read_text(encoding="utf-8")

    # Only the main repo matches; the worktree siblings are excluded.
    assert "main-branch task" in index
    assert "feature-x-worktree task" not in index
    assert "bugfix-worktree task" not in index
    assert "decoy-repo task" not in index
    assert "other-project task" not in index


def test_pages_embed_full_archive_option_lists(tmp_path: Path) -> None:
    """Each page's embedded JSON should carry the union of providers / repos /
    detail groups across the full archive so URL-driven filter selections
    remain meaningful as the user paginates. Pagination links themselves stay
    bare in the HTML — the JS forwards location.search at runtime."""
    import json
    import re

    p = _common_paths(tmp_path)
    result = run_cli(
        output=p["output"],
        claude_source=CLAUDE_FIXTURE,
        codex_source=CODEX_FIXTURE,
        cline_source=CLINE_FIXTURE,
        extra=["--page-prompts", "1"],
    )
    assert result.returncode == 0, result.stderr

    page_paths = sorted(p["output"].glob("page-*.html"))
    assert len(page_paths) >= 2, "need multiple pages to validate cross-page state"

    seen_provider_unions: set[tuple[str, ...]] = set()
    seen_repo_unions: set[tuple[str, ...]] = set()
    for page_path in page_paths:
        page_html = page_path.read_text(encoding="utf-8")
        match = re.search(
            r'<script id="transcript-data" type="application/json">(.*?)</script>',
            page_html,
            re.DOTALL,
        )
        assert match, f"no transcript-data block in {page_path.name}"
        page_data = json.loads(match.group(1).replace("<\\/", "</"))

        assert "all_providers" in page_data
        assert "all_repos" in page_data
        assert "all_detail_groups" in page_data

        # The union must include providers that don't necessarily appear on
        # this specific page — that's the whole point.
        assert {"claude", "codex", "cline"}.issubset(set(page_data["all_providers"]))
        assert page_data["all_detail_groups"] == [
            "commands",
            "results",
            "patches",
            "web",
            "thinking",
            "status",
            "tools",
        ]

        seen_provider_unions.add(tuple(page_data["all_providers"]))
        seen_repo_unions.add(tuple(page_data["all_repos"]))

        # Pagination anchors should be bare; URL forwarding happens client-side.
        nav_hrefs = re.findall(r'<a[^>]+href="(page-\d+\.html)"', page_html)
        assert nav_hrefs, f"no pagination anchors in {page_path.name}"
        for href in nav_hrefs:
            assert "?" not in href

    # Identical archive-wide unions across every page.
    assert len(seen_provider_unions) == 1
    assert len(seen_repo_unions) == 1


@pytest.mark.parametrize(
    "cli_extra,expected_error_fragment",
    [
        (["--page-prompts", "0"], "page-prompts"),
    ],
)
def test_cli_rejects_invalid_inputs(
    tmp_path: Path, cli_extra: list[str], expected_error_fragment: str
) -> None:
    p = _common_paths(tmp_path)
    result = run_cli(
        output=p["output"],
        claude_source=CLAUDE_FIXTURE,
        codex_source=p["missing_codex"],
        cline_source=p["missing_cline"],
        extra=["--provider", "claude", *cli_extra],
    )
    assert result.returncode != 0
    assert expected_error_fragment in (result.stderr + result.stdout).lower()
