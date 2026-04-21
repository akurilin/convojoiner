# Convojoiner

Convojoiner generates a single self-contained HTML page that joins local Claude Code
and Codex session transcripts into one filterable timeline.

The tool treats the original transcript stores as read-only. It discovers matching
sessions from `~/.claude/projects` and `~/.codex/sessions`, copies the selected
JSONL files into a fresh `/tmp/convojoiner-*` directory, parses the copies, and
writes the HTML output from those copied files.

## Usage

Generate an HTML transcript for two worktrees since April 19, 2026:

```bash
python3 convojoiner.py \
  --since 2026-04-19 \
  --timezone Europe/Rome \
  --repo-folder /Users/alex/code/koda \
  --repo-folder /Users/alex/code/koda2 \
  --output ./convojoiner.html
```

Preview what would be selected without copying or writing output:

```bash
python3 convojoiner.py \
  --since 2026-04-19 \
  --timezone Europe/Rome \
  --repo-folder /Users/alex/code/koda \
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

The generated HTML has two views:

- `Lanes`: one column per session or subagent, grouped by minute so concurrent
  work stays visually separated.
- `Feed`: one chronological stream across all providers and sessions.

The page includes client-side filters for provider, day, repo folder, event kind,
session, and search text. It does not load external assets or make network
requests.

## Source Stores

Default source locations:

- Claude Code: `~/.claude/projects`
- Codex: `~/.codex/sessions`

Use `--claude-source` or `--codex-source` to point at copied or alternate stores.
Use `--copy-root` when you want the copied JSONL files in a known directory.
