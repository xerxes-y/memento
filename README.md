# memento

**Memento** integration for **Devin** (Cognition).

Gives Devin a nightly *sleep cycle*: reviews past sessions, mines recurring
patterns, proposes bounded edits to a long-term `SKILL.md`, and gates every
change with a held-out validation score — so only improvements that actually
make Devin better *at your work* get adopted.

> Built on [microsoft/SkillOpt](https://github.com/microsoft/SkillOpt).

---

## How it works

Devin does not write conversation transcripts to disk in a format
the sleep engine understands.  `harvest_devin.py` bridges this by converting
every locally available source into Claude Code-compatible JSONL transcripts:

| Source | Where | What it contributes |
|---|---|---|
| **Devin transcripts** | `~/.local/share/devin/cli/transcripts/*.json` | Native ATIF-v1.7 sessions — real user↔agent turns |
| **agentmemory** | `~/.agentmemory/standalone.json` | Saved memories from the [agentmemory MCP server](https://github.com/agentmemory/agentmemory) |
| **Skill files** | `.devin/skills/*/SKILL.md` | Skill trigger patterns and expected behavior |

Workspaces are **auto-detected** from the Devin registry (nothing to configure):
- Devin: `~/.config/Devin/User/workspaceStorage/*/workspace.json`

After `memento_adopt` the evolved skill is synced to
`.devin/skills/memento-learned/SKILL.md` automatically.

---

## Install

**Requirements:** Python ≥ 3.10, Git, Devin CLI.

```bash
git clone https://github.com/xerxes-y/memento.git
cd memento
bash install.sh
```

`install.sh` will:
1. Use or clone [microsoft/SkillOpt](https://github.com/microsoft/SkillOpt) to `<project-dir>/../SkillOpt` (or `--skillopt-dir`)
2. Install `skillopt_sleep` (editable) into your Python environment
3. Create `~/.memento/` (runtime data dir)
4. Seed `memento-learned/SKILL.md` into every detected Devin workspace (`.devin/skills/`)
5. Auto-register with **Devin CLI** MCP (`devin mcp add memento`) if the Devin CLI is on PATH

### Devin post-install

MCP registration is automatic if the Devin CLI is installed.
Optionally copy `devin-rules.snippet.md` to `.devin/rules/memento.md` in your workspace so Devin knows to offer the sleep tools.

### Windows

The runtime (`mcp_server.py` + `harvest_devin.py`) is cross-platform and
auto-detects Devin data under `%LOCALAPPDATA%\devin\cli\transcripts` — no extra flags needed.

`install.sh` is bash, so run it from **Git Bash** or **WSL**, or wire it up
manually: add the snippet from `mcp-config.example.json` to your Devin MCP config
(use `python` instead of `python3` and absolute Windows paths in `args`/`env`).

### Manual config

**Devin** — run once in a terminal:

```bash
devin mcp add memento \
  --env "MEMENTO_ENGINE_REPO=<project-dir>/../SkillOpt" \
  --env "MEMENTO_HOME=$HOME/.memento" \
  -- python3 <project-dir>/mcp_server.py
```

---

## Add to Devin as an MCP extension (`uvx`, one line)

`memento` ships a console entrypoint (`memento-mcp`), so it runs as a self-contained
package with no clone or path wiring — ideal for Devin's **custom MCP** UI
(*Settings → Connections → MCP servers → Add a custom MCP → STDIO*) or the
`devin mcp add` CLI.

**STDIO config (Devin custom MCP):**

| Field | Value |
|---|---|
| Command | `uvx` |
| Args | `["--from", "git+https://github.com/xerxes-y/memento", "memento-mcp"]` |
| Env | `MEMENTO_ENGINE_REPO`, `MEMENTO_HOME` |

Or via the CLI:

```bash
devin mcp add memento \
  --env "MEMENTO_ENGINE_REPO=$HOME/.local/share/SkillOpt" \
  --env "MEMENTO_HOME=$HOME/.memento" \
  -- uvx --from git+https://github.com/xerxes-y/memento memento-mcp
```

Once published to PyPI the `--from …` drops out — the args become just
`["memento-mcp"]` (`uvx memento-mcp`):

```bash
python3 -m build && python3 -m twine upload dist/*
```

> The optimization engine (`skillopt_sleep`) is loaded at runtime from
> `MEMENTO_ENGINE_REPO` (a local SkillOpt clone), so it works inside the isolated
> `uvx` env without being on PyPI. Point `MEMENTO_ENGINE_REPO` at a clone (or run
> `install.sh` once to create one).

---

## Use

Ask Devin:

> *"run the sleep cycle"*, *"what did the last sleep propose?"*, *"adopt it"*

Or call tools directly:

| Tool | What it does |
|---|---|
| `memento_auto` | **fully automatic** — run + auto-adopt above the validation gate, returns the SKILL.md diff report |
| `memento_status` | nights run so far + latest staged proposal |
| `memento_dry_run` | preview cycle — no staging, no changes |
| `memento_run` | full cycle; stages a proposal for your review |
| `memento_adopt` | apply the staged proposal; syncs skill to workspace |
| `memento_harvest` | debug: list the recurring tasks mined |

Each tool accepts:

| Argument | Values | Default |
|---|---|---|
| `project` | abs path | cwd |
| `backend` | `mock` / `claude` / `codex` | `mock` |
| `scope` | `invoked` / `all` | `invoked` |

`mock` is free (no API calls). For real LLM optimization:
- `backend: "claude"` → set `ANTHROPIC_API_KEY`
- `backend: "codex"` → set `OPENAI_API_KEY`

---

## Run it fully automatically

`memento_auto` runs a cycle **and** adopts the result in one step, gated by the
engine's held-out validation (plus an optional `MEMENTO_AUTO_ADOPT_MIN_SCORE`
floor), then returns a before/after `SKILL.md` diff. Ask Devin *"auto-evolve the
skill"*, or schedule it to run unattended.

**macOS (launchd) — nightly at 02:00:**

```bash
bash install.sh --schedule                       # uses first detected workspace
bash install.sh --schedule --schedule-time 03:30 --schedule-project /path/to/repo
```

This writes `~/Library/LaunchAgents/com.memento.plist` and loads it; logs
go to `~/.memento/memento-auto.log`. Remove with
`launchctl unload <plist> && rm <plist>`.

**Linux / cron** — point a cron entry at the standalone runner:

```cron
0 2 * * *  python3 /path/to/mcp_server.py --auto --project /path/to/repo --backend mock
```

---

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MEMENTO_ENGINE_REPO` | `~/.local/share/SkillOpt` | Path to the SkillOpt repo |
| `MEMENTO_HOME` | `~/.memento` | Runtime data dir |
| `MEMENTO_WORKSPACES` | auto-detected | Colon-separated workspace paths |
| `MEMENTO_MANAGED_SKILL` | `memento-learned` | Skill name to evolve |
| `MEMENTO_AUTO_ADOPT_MIN_SCORE` | unset | Optional floor for `memento_auto`; skip adopt if the parsed validation score is below it (the engine's own gate still applies) |

---

## Verify (no Devin session needed)

Run the test suite (stdlib-only, no pytest required):

```bash
python3 -m unittest discover -s tests -v
```

It covers the harvest helpers, the Devin ATIF transcript path, the judge, the MCP
protocol, and the **microsoft/SkillOpt engine command contract**. The one
integration test that runs the real engine is skipped automatically unless
`skillopt_sleep` is installed (via `install.sh`).

Or smoke-test the MCP server's JSON-RPC directly:

```bash
MEMENTO_ENGINE_REPO=~/.local/share/SkillOpt \
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python3 mcp_server.py
```

---

## Project structure

```
memento/
├── mcp_server.py              MCP server (stdlib-only, stdio) — Devin
├── harvest_devin.py           Transcript generator (Devin ATIF-v1.7 + agentmemory + skills)
├── judge.py                   Reference judge — scores a reply against a rubric (validation gate)
├── fixtures/
│   └── devin_sample.json      Sample ATIF transcript for offline testing
├── tests/
│   └── test_memento.py        Test suite (harvest, Devin path, judge, MCP, engine contract)
├── blog-memento.html   Walk-through / use-case blog (PO · QA · Developer)
├── mcp-config.example.json    Devin MCP config snippet
├── devin-rules.snippet.md     Copy to .devin/rules/memento.md
├── seed_skill/
│   └── SKILL.md               Initial skill seed (replaced by memento_adopt)
├── install.sh                 One-shot installer (Devin auto-detected)
├── pyproject.toml             Packaging — `memento-mcp` console entrypoint (uvx/pip)
└── README.md
```

---

## Outcomes & the validation gate

SkillOpt only improves a skill **where tasks recur and have a checkable
correctness signal**.  A bare transcript has neither, so `harvest_devin.py`
enriches Devin trajectories with two things and writes them to
`<data-dir>/outcomes.jsonl`:

- **`taskKey`** — a stable `<lang>:<intent>:<target>` grouping key (e.g.
  `java:fix:orderservice`) so repeats of the same task collapse into one
  recurring task the gate can replay.
- **an outcome envelope** — the checkable signal:
  - **hard signal** when the agent recorded a test/build result:
    `{"success": true, "verifier": "tests", "evidence": "BUILD SUCCESS",
    "reference": {"repro": "rtk mvn test -Dtest=OrderServiceTest"}}`
  - **deferred (judge)** when no hard signal exists:
    `{"success": null, "verifier": "judge", "rubric": [...]}` — a rubric is
    derived from the task so [`judge.py`](judge.py) (or the engine) can score the
    replay instead.

Score a reply against a rubric:

```bash
echo "<candidate reply>" | python3 judge.py --rubric-inline '["Addresses OrderService", "Resolves the reported defect without introducing new errors"]'
# → 0.5
```

`judge.py` defaults to an offline keyword-coverage heuristic (no API key).
Set `MEMENTO_JUDGE=claude` (+ `ANTHROPIC_API_KEY`) for an LLM judge.

> **Reality check:** the hard-signal path only fires if Devin actually
> records test or build results in its transcripts.  If it doesn't, every task
> falls to the `judge` branch — point `--devin-transcripts` at a real transcript
> dir and inspect `outcomes.jsonl` to find out which case you're in.

Try it on the bundled fixture:

```bash
python3 harvest_devin.py --devin-transcripts fixtures --out-dir /tmp/memento-test
cat /tmp/memento-test/outcomes.jsonl
```

---

## Contributing / upstream

This plugin is being contributed back to
[microsoft/SkillOpt](https://github.com/microsoft/SkillOpt) as
`plugins/devin/`.  Bug reports and improvements welcome here or upstream.

## License

MIT — same as microsoft/SkillOpt.
