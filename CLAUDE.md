# convojoiner

## Source session files are read-only — never modify or delete them

This tool discovers and parses JSONL session transcripts written by other
programs (Claude Code under `~/.claude/projects`, Codex under
`~/.codex/sessions`, and future adapters). **Those files are the user's
authoritative conversation history and belong to the other tool. Our code
must treat them as strictly read-only.**

When writing or reviewing any logic in this project:

- **Never** call `.write_text`, `.write_bytes`, `.unlink`, `.chmod`, `os.remove`,
  `shutil.move`, `shutil.rmtree`, or any other mutating operation on a path
  that resolves under a `--*-source` directory or on a `SessionCandidate.source_path`.
- Open source files with read-only modes only (`open(path, "r")`).
- If a future feature genuinely requires in-place transformation (e.g.
  decryption, normalization), **copy the files to a scratch location first**
  and operate on the copies. Do not introduce destructive operations on the
  originals.
- When a test needs to mutate a fixture, copy the fixture into `tmp_path`
  first (see `tests/test_e2e.py::test_redaction_applied_end_to_end` for the
  pattern).

This rule exists because losing a session transcript is a high-regret,
irreversible mistake for the user. A corrupted or deleted transcript cannot
be recovered from git, from a cloud backup, or from the originating tool.

## Python environment

Always use the project virtual environment at `.venv/`, never the global Python.

- Activate: `source .venv/bin/activate`
- Or invoke directly: `.venv/bin/python`, `.venv/bin/pip`
- Dependencies live in `requirements.txt`. Install with `.venv/bin/pip install -r requirements.txt`.

If `.venv/` is missing, create it with `python3 -m venv .venv` before installing anything.

## Layout

- `convojoiner.py` — CLI entry point and parsing/rendering logic.
- `adapters/` — provider adapter registry (Claude, Codex, extensible).
- `redaction.py` — secret detection via `detect-secrets` + custom plugins.
- `templates/` — HTML templates loaded at runtime (`page.html`, `index.html`).
- `static/` — CSS and JS copied verbatim into the generated output directory.
- `tests/` — pytest suite.

## Testing

Install dev dependencies: `.venv/bin/pip install -r requirements-dev.txt`.

Run the suite: `.venv/bin/python -m pytest tests/`.

Secret-detection fixtures are assembled from split string literals at runtime
so no complete secret ever appears as a contiguous literal in the repo.
`test_source_has_no_literal_secrets` enforces this — if it fails, fix the
offending fixture split, don't suppress the test.
