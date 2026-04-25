#!/usr/bin/env python3
"""Regenerate the GitHub Pages demo site at docs/.

Reuses the same realistic Claude + Codex scenario as the README screenshots,
runs the real multitrack CLI against it, and writes the resulting archive
into docs/ so GitHub Pages can serve it directly. Existing docs/screenshots/
PNGs are left untouched (the CLI only clears page-*.html and replaces
static/ in the output directory).

Run from the repo root with the project venv:

    .venv/bin/python scripts/regenerate_demo_site.py

GitHub Pages setup (one-time, in the repo settings on github.com):

    Settings -> Pages -> Source: Deploy from a branch
    Branch: main, Folder: /docs

Prerequisites:
    - project venv (see CLAUDE.md)
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

from regenerate_screenshots import (  # noqa: E402  (sys.path setup must precede)
    DEMO_SOURCES,
    run_multitrack,
    write_fixtures,
)


def write_nojekyll() -> None:
    """Skip Jekyll processing on GitHub Pages.

    Without this, GH Pages treats the docs/ tree as a Jekyll source. Our
    static archive doesn't need that pipeline, and Jekyll silently skips
    paths starting with `_`, which would silently break links if multitrack
    ever generated such names.
    """
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")


def cleanup_tmp_sources() -> None:
    if DEMO_SOURCES.is_dir():
        shutil.rmtree(DEMO_SOURCES, ignore_errors=True)


def main() -> int:
    print("Writing demo fixtures to /tmp...")
    write_fixtures()
    print(f"Running multitrack against demo dataset, output -> {DOCS_DIR}")
    run_multitrack(DOCS_DIR)
    print("Writing docs/.nojekyll...")
    write_nojekyll()
    print("Cleaning up /tmp source fixtures...")
    cleanup_tmp_sources()
    print()
    print(f"Done. Regenerated demo site at {DOCS_DIR}")
    print("If GitHub Pages is enabled for this repo, push to publish.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
