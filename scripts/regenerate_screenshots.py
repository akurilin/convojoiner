#!/usr/bin/env python3
"""Regenerate the README screenshots in docs/screenshots/.

Writes a realistic concurrent Claude + Codex scenario into /tmp, runs the
real multitrack CLI against it, drives `npx agent-browser` to capture two
screenshots (index + page-001), copies them into docs/screenshots/, and
cleans up /tmp.

Run from the repo root with the project venv:

    .venv/bin/python scripts/regenerate_screenshots.py

Prerequisites:
    - project venv (see CLAUDE.md)
    - `npx` on PATH, with network access to fetch agent-browser on first run
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCREENSHOTS_DIR = REPO_ROOT / "docs" / "screenshots"

DEMO_SOURCES = Path("/tmp/multitrack-demo-sources")
DEMO_OUT = Path("/tmp/multitrack-demo-out")
DEMO_INDEX_SHOT = Path("/tmp/multitrack-demo-index.png")
DEMO_TIMELINE_SHOT = Path("/tmp/multitrack-demo-timeline.png")

CWD = "/Users/dev/code/api-service"
BASE = datetime(2026, 4, 15, 10, 0, 0, tzinfo=UTC)
CLAUDE_ID = "7f3a4e21-8c2b-4d91-a17f-3c5e8b9d2014"
CODEX_ID = "019ed2c8-4f7a-7890-b123-c4d5e6f7a8b9"


def t(minutes: float) -> str:
    return (BASE + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


# ---- Claude event builders -------------------------------------------------


def _claude_user(minutes: float, text: str) -> dict:
    return {
        "type": "user",
        "sessionId": CLAUDE_ID,
        "cwd": CWD,
        "timestamp": t(minutes),
        "message": {"content": text},
    }


def _claude_asst(minutes: float, blocks: list[dict]) -> dict:
    return {
        "type": "assistant",
        "sessionId": CLAUDE_ID,
        "cwd": CWD,
        "timestamp": t(minutes),
        "message": {"content": blocks},
    }


def _txt(s: str) -> dict:
    return {"type": "text", "text": s}


def _thinking(s: str) -> dict:
    return {"type": "thinking", "thinking": s}


def _tool_use(tool_id: str, name: str, input_: dict) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": input_}


def _tool_result(tool_use_id: str, content: str, is_error: bool = False) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": is_error,
    }


CLAUDE_EVENTS = [
    _claude_user(
        0,
        "Let's design a JWT auth module for this Express API. Look at the existing routes "
        "under `src/routes/` and give me a plan we can implement.",
    ),
    _claude_asst(
        1,
        [
            _thinking(
                "The user wants an auth module. First I should look at the existing route "
                "structure to understand conventions, then propose middleware that fits."
            ),
            _txt(
                "Good starting point. Let me look at the current route layout before proposing anything."
            ),
            _tool_use(
                "tool_01",
                "Bash",
                {"command": "ls -la src/routes/", "description": "list route files"},
            ),
        ],
    ),
    _claude_asst(
        1.1,
        [
            _tool_result(
                "tool_01",
                "total 24\ndrwxr-xr-x  5 dev  staff  160 Apr 15 09:55 .\n"
                "drwxr-xr-x  8 dev  staff  256 Apr 15 09:40 ..\n"
                "-rw-r--r--  1 dev  staff  742 Apr 15 09:55 index.ts\n"
                "-rw-r--r--  1 dev  staff 1284 Apr 15 09:48 users.ts\n"
                "-rw-r--r--  1 dev  staff  920 Apr 15 09:42 posts.ts",
            )
        ],
    ),
    _claude_asst(
        2,
        [
            _txt(
                "Here's the plan:\n\n"
                "1. **`src/auth/jwt.ts`** — wrap `jsonwebtoken` with `sign(payload)` / `verify(token)` "
                "helpers that read `JWT_SECRET` from env. Fail fast on startup if it's missing.\n"
                "2. **`src/middleware/require-auth.ts`** — Express middleware that pulls the `Authorization: "
                "Bearer …` header, verifies, and attaches `req.user`. Returns 401 on missing/invalid.\n"
                "3. **`src/routes/auth.ts`** — new routes for `POST /login` and `POST /refresh`. "
                "Password check goes through the existing `users` service.\n"
                "4. Wire `auth.ts` into `src/routes/index.ts` and add `requireAuth` to the `posts` "
                "routes that shouldn't be public.\n\n"
                "Want me to implement it now, or tweak the shape first?"
            )
        ],
    ),
    _claude_user(4, "Go for it. Keep the middleware testable."),
    _claude_asst(
        5,
        [
            _txt("Writing `src/auth/jwt.ts` first."),
            _tool_use(
                "tool_02",
                "Write",
                {
                    "file_path": "src/auth/jwt.ts",
                    "content": (
                        'import jwt from "jsonwebtoken";\n\n'
                        "const SECRET = process.env.JWT_SECRET;\n"
                        "if (!SECRET) {\n"
                        '  throw new Error("JWT_SECRET is required");\n'
                        "}\n\n"
                        "export type AuthPayload = { sub: string; email: string };\n\n"
                        "export function sign(payload: AuthPayload): string {\n"
                        '  return jwt.sign(payload, SECRET, { expiresIn: "1h" });\n'
                        "}\n\n"
                        "export function verify(token: string): AuthPayload {\n"
                        "  return jwt.verify(token, SECRET) as AuthPayload;\n"
                        "}\n"
                    ),
                },
            ),
        ],
    ),
    _claude_asst(5.2, [_tool_result("tool_02", "File written: src/auth/jwt.ts (312 bytes)")]),
    _claude_asst(
        6,
        [
            _txt("Now the middleware."),
            _tool_use(
                "tool_03",
                "Write",
                {
                    "file_path": "src/middleware/require-auth.ts",
                    "content": (
                        'import { Request, Response, NextFunction } from "express";\n'
                        'import { verify, AuthPayload } from "../auth/jwt";\n\n'
                        'declare module "express-serve-static-core" {\n'
                        "  interface Request { user?: AuthPayload }\n"
                        "}\n\n"
                        "export function requireAuth(req: Request, res: Response, next: NextFunction) {\n"
                        '  const header = req.header("authorization") ?? "";\n'
                        '  const [scheme, token] = header.split(" ");\n'
                        '  if (scheme !== "Bearer" || !token) {\n'
                        '    return res.status(401).json({ error: "missing bearer token" });\n'
                        "  }\n"
                        "  try {\n"
                        "    req.user = verify(token);\n"
                        "    next();\n"
                        "  } catch {\n"
                        '    res.status(401).json({ error: "invalid token" });\n'
                        "  }\n"
                        "}\n"
                    ),
                },
            ),
        ],
    ),
    _claude_asst(
        6.2, [_tool_result("tool_03", "File written: src/middleware/require-auth.ts (598 bytes)")]
    ),
    _claude_user(
        12,
        "Good. Now add a password reset flow — email with a one-time token that expires in 30 minutes.",
    ),
    _claude_asst(
        13,
        [
            _thinking(
                "Password reset needs its own token type so a reset token can't be used as a session "
                "token. I'll use a different sub-audience on the JWT."
            ),
            _txt(
                "Two pieces: a new token variant with `aud: 'reset'` and a 30-minute expiry, plus "
                "routes for request + confirm.\n\n"
                "```ts\n"
                "// src/auth/reset.ts\n"
                "export function issueResetToken(userId: string) {\n"
                "  return jwt.sign({ sub: userId }, SECRET, {\n"
                '    audience: "reset",\n'
                '    expiresIn: "30m",\n'
                "  });\n"
                "}\n"
                "```\n\n"
                "Writing it now."
            ),
            _tool_use(
                "tool_04",
                "Edit",
                {
                    "file_path": "src/auth/jwt.ts",
                    "old_string": (
                        "export function verify(token: string): AuthPayload {\n"
                        "  return jwt.verify(token, SECRET) as AuthPayload;\n"
                        "}\n"
                    ),
                    "new_string": (
                        "export function verify(token: string): AuthPayload {\n"
                        '  return jwt.verify(token, SECRET, { audience: "session" }) as AuthPayload;\n'
                        "}\n\n"
                        "export function issueReset(userId: string): string {\n"
                        "  return jwt.sign({ sub: userId }, SECRET, {\n"
                        '    audience: "reset",\n'
                        '    expiresIn: "30m",\n'
                        "  });\n"
                        "}\n\n"
                        "export function verifyReset(token: string): { sub: string } {\n"
                        '  return jwt.verify(token, SECRET, { audience: "reset" }) as { sub: string };\n'
                        "}\n"
                    ),
                },
            ),
        ],
    ),
    _claude_asst(13.3, [_tool_result("tool_04", "Edit applied to src/auth/jwt.ts")]),
    _claude_user(22, "Run the linter and commit if it passes."),
    _claude_asst(
        23,
        [_tool_use("tool_05", "Bash", {"command": "npm run lint", "description": "run eslint"})],
    ),
    _claude_asst(
        23.5,
        [
            _tool_result(
                "tool_05",
                "> api-service@1.3.0 lint\n> eslint src --max-warnings 0\n\n"
                "✨ no lint errors, 0 warnings",
            )
        ],
    ),
    _claude_asst(
        24,
        [
            _txt("Lint is clean. Committing."),
            _tool_use(
                "tool_06",
                "Bash",
                {
                    "command": "git add src/auth src/middleware src/routes && git commit -m 'Add JWT auth module and password reset flow'",
                    "description": "commit auth work",
                },
            ),
        ],
    ),
    _claude_asst(
        24.4,
        [
            _tool_result(
                "tool_06",
                "[main a7f2c9e] Add JWT auth module and password reset flow\n"
                " 5 files changed, 142 insertions(+), 3 deletions(-)\n"
                " create mode 100644 src/auth/jwt.ts\n"
                " create mode 100644 src/auth/reset.ts\n"
                " create mode 100644 src/middleware/require-auth.ts\n"
                " create mode 100644 src/routes/auth.ts",
            )
        ],
    ),
]


# ---- Codex event builders --------------------------------------------------


def _codex_meta(minutes: float) -> dict:
    return {
        "type": "session_meta",
        "timestamp": t(minutes),
        "payload": {"id": CODEX_ID, "cwd": CWD, "timestamp": t(minutes)},
    }


def _codex_user(minutes: float, text: str) -> dict:
    return {
        "type": "response_item",
        "timestamp": t(minutes),
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _codex_asst_text(minutes: float, text: str) -> dict:
    return {
        "type": "response_item",
        "timestamp": t(minutes),
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }


def _codex_fncall(minutes: float, call_id: str, name: str, args: dict) -> dict:
    return {
        "type": "response_item",
        "timestamp": t(minutes),
        "payload": {
            "type": "function_call",
            "name": name,
            "arguments": json.dumps(args),
            "call_id": call_id,
        },
    }


def _codex_fnout(minutes: float, call_id: str, output: str) -> dict:
    return {
        "type": "response_item",
        "timestamp": t(minutes),
        "payload": {"type": "function_call_output", "output": output, "call_id": call_id},
    }


CODEX_EVENTS = [
    _codex_meta(2.5),
    _codex_user(3, "Run the test suite and tell me what's breaking."),
    _codex_fncall(
        3.1, "c1", "exec_command", {"command": ["zsh", "-lc", "npm test"], "workdir": CWD}
    ),
    _codex_fnout(
        4,
        "c1",
        "> api-service@1.3.0 test\n> jest --silent\n\n"
        "PASS  tests/users.test.ts\n"
        "FAIL  tests/auth/test_jwt.ts\n"
        "  ● Auth › sign and verify round-trips\n\n"  # Jest's bullet + right-angle output
        "    Cannot find module '../../src/auth/jwt' from 'tests/auth/test_jwt.ts'\n\n"
        "Test Suites: 1 failed, 2 passed, 3 total\n"
        "Tests:       1 failed, 7 passed, 8 total\nProcess exited with code 1",
    ),
    _codex_user(6, "Why is test_jwt.ts failing? Read the test file and explain."),
    _codex_fncall(
        6.1,
        "c2",
        "shell",
        {"command": ["zsh", "-lc", "cat tests/auth/test_jwt.ts"], "workdir": CWD},
    ),
    _codex_fnout(
        7,
        "c2",
        'import { sign, verify } from "../../src/auth/jwt";\n\n'
        'describe("Auth", () => {\n'
        '  it("sign and verify round-trips", () => {\n'
        '    const token = sign({ sub: "u1", email: "a@b.co" });\n'
        '    expect(verify(token).sub).toBe("u1");\n'
        "  });\n"
        "});\n",
    ),
    _codex_asst_text(
        8,
        "The test imports from `../../src/auth/jwt` but that module doesn't exist yet — it's scheduled to "
        "be written in the parallel Claude session. Once the `src/auth/jwt.ts` file lands, the import "
        "resolves and this test should pass without any change on the test side. Nothing to fix here; "
        "this is a sequencing issue between the two agents.",
    ),
    _codex_user(
        15,
        "While Claude writes the code, check what auth-related deps we already have in package.json.",
    ),
    _codex_fncall(
        15.1,
        "c3",
        "exec_command",
        {
            "command": [
                "zsh",
                "-lc",
                "cat package.json | jq '.dependencies | to_entries | map(select(.key | test(\"jwt|auth|bcrypt|passport|crypto\"))) | from_entries'",
            ],
            "workdir": CWD,
        },
    ),
    _codex_fnout(16, "c3", '{\n  "bcryptjs": "^2.4.3",\n  "jsonwebtoken": "^9.0.2"\n}'),
    _codex_asst_text(
        17,
        "Both `jsonwebtoken` and `bcryptjs` are already in `dependencies`. No new packages need to be "
        "installed for the JWT + password-reset work. If the implementation ends up wanting rate "
        "limiting on the reset endpoint, you'd want `express-rate-limit` next — but that's optional.",
    ),
    _codex_user(20, "Summarize everything that's happened so far across both sessions."),
    _codex_asst_text(
        20.5,
        "Current state:\n\n"
        "- **Claude** has written `src/auth/jwt.ts` and `src/middleware/require-auth.ts`, and is "
        "adding a password reset flow (new `issueReset`/`verifyReset` helpers on the JWT module).\n"
        "- **Codex (me)** ran the test suite: 1 failure (`tests/auth/test_jwt.ts` couldn't resolve "
        "the import), which will resolve itself once Claude's files land on disk.\n"
        "- **Deps**: `jsonwebtoken@^9.0.2` and `bcryptjs@^2.4.3` are already in `package.json` — no "
        "install needed.\n\n"
        "Next likely step: Claude commits the auth work, we re-run tests, and confirm the failing "
        "suite now passes.",
    ),
]


# ---- Orchestration ---------------------------------------------------------


def write_fixtures() -> None:
    if DEMO_SOURCES.exists():
        shutil.rmtree(DEMO_SOURCES)
    claude_dir = DEMO_SOURCES / "claude" / "projects" / "-Users-dev-code-api-service"
    codex_dir = DEMO_SOURCES / "codex" / "sessions" / "2026" / "04" / "15"
    claude_dir.mkdir(parents=True)
    codex_dir.mkdir(parents=True)

    claude_file = claude_dir / f"{CLAUDE_ID}.jsonl"
    codex_file = codex_dir / f"rollout-2026-04-15T10-02-30-{CODEX_ID}.jsonl"

    with claude_file.open("w") as f:
        for ev in CLAUDE_EVENTS:
            f.write(json.dumps(ev) + "\n")
    with codex_file.open("w") as f:
        for ev in CODEX_EVENTS:
            f.write(json.dumps(ev) + "\n")


def run_multitrack(output_dir: Path = DEMO_OUT) -> None:
    """Run the CLI against the /tmp demo sources, writing to `output_dir`.

    The CLI itself only clears `page-*.html` and replaces `static/` inside the
    output dir, so unrelated files (e.g. docs/screenshots/) survive. Callers
    that want a fully clean slate (the screenshot script) should rmtree the
    target before calling.

    `--timezone UTC` is pinned so the resulting display_time/day fields don't
    depend on the local TZ of whoever runs this — a regen on a laptop in
    Europe must produce the same output as one in California.
    """
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "multitrack.py"),
            "--claude-source",
            str(DEMO_SOURCES / "claude" / "projects"),
            "--codex-source",
            str(DEMO_SOURCES / "codex" / "sessions"),
            "--cline-source",
            "/tmp/multitrack-demo-no-cline",
            "--since",
            "2026-04-14",
            "--timezone",
            "UTC",
            "-o",
            str(output_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise SystemExit("multitrack run failed")


def _browser(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["npx", "-y", "agent-browser", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def capture_screenshots() -> None:
    closed = _browser("close", "--all")
    if closed.returncode != 0 and "No such file or directory" in closed.stderr:
        raise SystemExit(
            "npx (or Node) isn't on PATH. Install Node, then re-run:\n"
            "    npm i -g agent-browser  # optional; npx will fetch it otherwise"
        )

    for url, dest in (
        (f"file://{DEMO_OUT}/index.html", DEMO_INDEX_SHOT),
        (f"file://{DEMO_OUT}/page-001.html", DEMO_TIMELINE_SHOT),
    ):
        opened = _browser("open", url)
        if opened.returncode != 0:
            print(opened.stderr, file=sys.stderr)
            raise SystemExit(f"agent-browser failed to open {url}")
        sized = _browser("set", "viewport", "1680", "1050")
        if sized.returncode != 0:
            print(sized.stderr, file=sys.stderr)
            raise SystemExit("agent-browser failed to set viewport")
        shot = _browser("screenshot", "--full", str(dest))
        if shot.returncode != 0:
            print(shot.stderr, file=sys.stderr)
            raise SystemExit(f"agent-browser failed to screenshot {url}")

    _browser("close", "--all")


def copy_to_repo() -> None:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DEMO_INDEX_SHOT, SCREENSHOTS_DIR / "index.png")
    shutil.copy2(DEMO_TIMELINE_SHOT, SCREENSHOTS_DIR / "timeline.png")


def cleanup_tmp() -> None:
    for path in (DEMO_SOURCES, DEMO_OUT, DEMO_INDEX_SHOT, DEMO_TIMELINE_SHOT):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()


def main() -> int:
    print("Writing demo fixtures to /tmp...")
    write_fixtures()
    print("Running multitrack to generate HTML...")
    if DEMO_OUT.exists():
        shutil.rmtree(DEMO_OUT)
    run_multitrack(DEMO_OUT)
    print("Capturing screenshots via agent-browser...")
    capture_screenshots()
    print("Copying screenshots into docs/screenshots/...")
    copy_to_repo()
    print("Cleaning up /tmp artifacts...")
    cleanup_tmp()
    print()
    print(f"Done. Updated {SCREENSHOTS_DIR / 'index.png'}")
    print(f"      Updated {SCREENSHOTS_DIR / 'timeline.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
