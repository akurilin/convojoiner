# convojoiner

## Python environment

Always use the project virtual environment at `.venv/`, never the global Python.

- Activate: `source .venv/bin/activate`
- Or invoke directly: `.venv/bin/python`, `.venv/bin/pip`
- Dependencies live in `requirements.txt`. Install with `.venv/bin/pip install -r requirements.txt`.

If `.venv/` is missing, create it with `python3 -m venv .venv` before installing anything.

## Layout

- `convojoiner.py` — CLI entry point and parsing/rendering logic.
- `templates/` — HTML templates loaded at runtime (`page.html`, `index.html`).
- `static/` — CSS and JS copied verbatim into the generated output directory.
