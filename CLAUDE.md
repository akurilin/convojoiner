# convojoiner

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
