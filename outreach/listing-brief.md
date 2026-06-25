# memento — Marketplace listing brief (one-pager)

**What it is.** An MCP server that gives Devin a nightly *sleep cycle*:
harvest sessions → mine recurring tasks → replay → **held-out validation gate** →
adopt bounded edits to a long-term `SKILL.md`. Devin gets measurably better at a
team's recurring work, with no manual prompt engineering. Built on
[microsoft/SkillOpt](https://github.com/microsoft/SkillOpt).

**Who it helps.** Any Devin team with recurring tasks — developers (code
conventions, build/test patterns), QA (test coverage rules), product (acceptance
criteria). The skill converges on *how this team actually works*.

**Also includes — built-in memory.** stdlib-only engine: hybrid BM25 + semantic
vector search (RRF), 4 memory tiers with auto-consolidation/decay, a knowledge
graph, auto-derived + manual "lessons," namespaces, audit log, snapshots, and a
local web dashboard.

**Install (today).**
- PyPI: `uvx devin-memento`
- `.mcpb` one-click bundle (v0.5.0 release)
- Devin: *Add a custom MCP* → STDIO → `uvx devin-memento`

**Tools.** 24 MCP tools — `memento_run/auto/adopt/...` (sleep cycle) and
`memory_save/recall/learn/graph/...` (memory).

**Safety / privacy posture.**
- Reads **local** Devin transcripts + on-disk skill files; nothing is sent
  anywhere by the server itself.
- **Secret redaction** strips tokens/keys before any memory is stored.
- The validation gate means skills **only improve or stay the same** — never
  regress; every change is staged for review (or auto-adopted only above a
  configurable score floor).
- `mock` backend runs with **no API spend**; `claude`/`codex` backends use the
  user's own key.

**Architecture note (for fit).** Local / STDIO server (not hosted/OAuth). The
optimizer engine (SkillOpt) is loaded from a local clone at
`MEMENTO_ENGINE_REPO`; memory tools work without it. A hosted variant is
possible if the gallery requires remote servers.

**Links.**
- Repo: https://github.com/xerxes-y/memento
- PyPI: https://pypi.org/project/devin-memento/
- Release + `.mcpb`: https://github.com/xerxes-y/memento/releases/tag/v0.5.0
- License: MIT (engine: Apache-2.0 upstream)
