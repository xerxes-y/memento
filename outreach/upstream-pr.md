# Upstream PR to microsoft/SkillOpt — plugins/devin (READY TO OPEN)

The branch is pushed to your fork. Open the PR here (one click):

**https://github.com/microsoft/SkillOpt/compare/main...xerxes-y:SkillOpt:add-devin-plugin?expand=1**

- **Title:** `Add Devin plugin (plugins/devin): MCP server + ATIF-v1.7 harvest`
- **Body:** paste the section below.
- A **Microsoft CLA bot** will comment — sign it (one-time) for the PR to be mergeable.
- (gh couldn't open it automatically: the current fine-grained token lacks
  "Pull requests: write". The web button bypasses that.)

---

## Summary

Adds a **Devin** (Cognition) plugin under `plugins/devin/` that wires the
`skillopt_sleep` engine into Devin via an MCP server — the same thin-shell
pattern as `plugins/copilot/`.

## What it adds

- **`mcp_server.py`** — stdlib-only stdio MCP server exposing the standard
  `sleep_*` tools (`status`, `dry-run`, `run`, `adopt`, `harvest`). `REPO_ROOT`
  defaults to `../..`, so it finds `skillopt_sleep` automatically when run from
  `plugins/devin/`.
- **`harvest_devin.py`** — the Devin-specific bridge: converts Devin
  **ATIF-v1.7** transcripts (`~/.local/share/devin/cli/transcripts/*.json`),
  agentmemory, and `.devin/skills/*/SKILL.md` into the Claude Code-compatible
  JSONL the engine consumes; enriches with `taskKey` + outcome envelopes (hard
  test/build signal, or a judge rubric) in `outcomes.jsonl`. Workspaces are
  auto-detected from Devin's registry; cross-platform (Linux + Windows) paths.
- **`judge.py`** — reference judge for the deferred/judge branch of the gate.
- **`mcp-config.example.json`**, **`devin-rules.snippet.md`**, **`README.md`**.
- **`plugins/README.md`** — adds Devin to the platform + install tables.

## Why

The reference harness targets the Claude Code transcript format; Devin uses
ATIF-v1.7 and a different workspace layout. This bridges them so the
harvest → mine → replay → validation-gate loop runs on real Devin sessions.

## Notes

- **No changes to `skillopt_sleep`** — shells out to `python -m skillopt_sleep
  <action>` exactly like the other plugins.
- Pure stdlib; default backend `mock` (no API spend).
- Maintained downstream at https://github.com/xerxes-y/memento (also on PyPI as
  `devin-memento`); this PR contributes the convention-matching plugin form.
