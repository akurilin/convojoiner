# Convojoiner

Convojoiner generates a static HTML archive that joins local Claude Code and
Codex session transcripts into one browseable timeline.

The tool treats the original transcript stores as read-only. It discovers matching
sessions from `~/.claude/projects` and `~/.codex/sessions`, copies the selected
JSONL files into a fresh `/tmp/convojoiner-*` directory, parses the copies, and
writes the HTML archive from those copied files.

## Prior art

Inspired by Simon Willison's
[simonw/claude-code-transcripts](https://github.com/simonw/claude-code-transcripts),
which renders a single Claude Code session directory to HTML. Convojoiner extends
that idea in a few directions:

- **Multiple local folders** — scope the archive to one or more repo/worktree
  paths with repeated `--repo-folder` flags.
- **Multiple concurrent sessions** — concurrent Claude and Codex sessions are
  laid out side-by-side in per-minute lanes rather than as a single linear log.
- **Multiple providers** — both Claude Code and Codex are parsed out of the box,
  with the goal of adding more coding-agent formats (OpenCode, Gemini, Amp,
  Cursor, etc.) behind a common adapter.

## Usage

Generate an HTML transcript for two worktrees since April 19, 2026:

```bash
python3 convojoiner.py \
  --since 2026-04-19 \
  --timezone Europe/Rome \
  --repo-folder ~/code/project-a \
  --repo-folder ~/code/project-b \
  --output ./convojoiner
```

Preview what would be selected without copying or writing output:

```bash
python3 convojoiner.py \
  --since 2026-04-19 \
  --timezone Europe/Rome \
  --repo-folder ~/code/project-a \
  --dry-run
```

Include only one provider:

```bash
python3 convojoiner.py --provider codex --since 2026-04-19
python3 convojoiner.py --provider claude --since 2026-04-19
```

Claude Code subagent JSONL files are included by default. Exclude them with:

```bash
python3 convojoiner.py --no-subagents
```

## Output

The generated archive contains:

- `index.html`: an index page with prompt cards, deterministic final-response
  excerpts, tool counts, commit cards extracted from git output, stats, search,
  and links to every transcript page.
- `page-001.html`, `page-002.html`, and so on: precomputed transcript pages,
  each containing a fixed number of user prompt turns. Use `--page-prompts` to
  change the default of 5 prompt turns per page.

Each transcript page renders one column per session or subagent that has events
on that page, grouped by minute so concurrent work stays visually separated
without putting the full archive in one document.

User and assistant messages render expanded by default and are not part of the
detail hide/show filter. Technical details such as commands, results, patches,
web calls, thinking, status, and other tools render as compact expandable rows
and can be hidden by category.

Transcript pages include client-side filters for provider, day, repo folder,
detail category, session, and search text, scoped to that precomputed page. The
archive does not load external assets or make network requests.

## Source Stores

Default source locations:

- Claude Code: `~/.claude/projects`
- Codex: `~/.codex/sessions`

Use `--claude-source` or `--codex-source` to point at copied or alternate stores.
Use `--copy-root` when you want the copied JSONL files in a known directory.

## Secret redaction

All transcript content is passed through a redaction layer before being
written to the archive. It combines Yelp's
[`detect-secrets`](https://github.com/Yelp/detect-secrets) with a handful of
custom detectors for keys the upstream library doesn't cover (Anthropic
`sk-ant-*`, OpenAI project `sk-proj-*`, GitHub fine-grained `github_pat_*`,
Google API / OAuth, Supabase new-format keys). Multi-line PEM private key
blocks are redacted as a whole. Entropy-based and keyword-based detectors are
intentionally disabled because they are noisy on code-containing transcripts.

Redaction counts are summarized at the end of each run. The test suite in
`tests/test_redaction.py` verifies detection for every enabled plugin using
split-literal fixtures so that no complete secret-looking string ever lives in
the repo.
