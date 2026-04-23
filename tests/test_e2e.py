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
CLAUDE_SECRET_FIXTURE = DATA_DIR / "claude_with_secret" / "projects"


def run_cli(
    *,
    output: Path,
    claude_source: Path,
    codex_source: Path,
    extra: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    args = [
        sys.executable,
        str(REPO_ROOT / "convojoiner.py"),
        "--claude-source", str(claude_source),
        "--codex-source", str(codex_source),
        "--output", str(output),
        "--since", "2019-01-01",
    ]
    if extra:
        args.extend(extra)
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _common_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "output": tmp_path / "archive",
        "missing_claude": tmp_path / "no-claude-here",
        "missing_codex": tmp_path / "no-codex-here",
    }


def test_claude_only_run(tmp_path: Path) -> None:
    p = _common_paths(tmp_path)
    result = run_cli(
        output=p["output"],
        claude_source=CLAUDE_FIXTURE,
        codex_source=p["missing_codex"],
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
    # No Codex content leaked in
    assert "codex-session" not in index
    assert "deployment script" not in index


def test_codex_only_run(tmp_path: Path) -> None:
    p = _common_paths(tmp_path)
    result = run_cli(
        output=p["output"],
        claude_source=p["missing_claude"],
        codex_source=CODEX_FIXTURE,
        extra=["--provider", "codex"],
    )

    assert result.returncode == 0, result.stderr
    assert (p["output"] / "index.html").exists()
    assert (p["output"] / "page-001.html").exists()

    index = (p["output"] / "index.html").read_text(encoding="utf-8")
    assert "write a script to deploy the api" in index
    assert "Codex codex-se" in index  # short_id of "codex-session-bbb22222"
    # No Claude content leaked in
    assert "refactor the auth module" not in index


def test_both_providers_interleaved(tmp_path: Path) -> None:
    p = _common_paths(tmp_path)
    result = run_cli(
        output=p["output"],
        claude_source=CLAUDE_FIXTURE,
        codex_source=CODEX_FIXTURE,
    )

    assert result.returncode == 0, result.stderr
    index = (p["output"] / "index.html").read_text(encoding="utf-8")
    assert "please refactor the auth module" in index
    assert "write a script to deploy the api" in index
    assert "Claude aaa11111" in index
    assert "Codex codex-se" in index


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
        extra=["--provider", "claude"],
    )

    assert result.returncode == 0, result.stderr

    # The secret must appear nowhere in the output; its redaction marker must.
    output_texts = [
        path.read_text(encoding="utf-8")
        for path in p["output"].rglob("*.html")
    ]
    joined = "\n".join(output_texts)
    assert secret not in joined
    assert "[REDACTED:aws-access-key]" in joined


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
        extra=["--provider", "claude", *cli_extra],
    )
    assert result.returncode != 0
    assert expected_error_fragment in (result.stderr + result.stdout).lower()
