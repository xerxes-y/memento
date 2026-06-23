# SkillOPT-devin

**SkillOpt-Sleep** integration for **Devin** (Cognition).

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

After `sleep_adopt` the evolved skill is synced to
`.devin/skills/skillopt-sleep-learned/SKILL.md` automatically.

---

## Install

**Requirements:** Python ≥ 3.10, Git, Devin CLI.

```bash
git clone https://github.com/xerxes-y/SkillOPT-devin.git
cd SkillOPT-devin
bash install.sh
```

`install.sh` will:
1. Use or clone [microsoft/SkillOpt](https://github.com/microsoft/SkillOpt) to `<project-dir>/../SkillOpt` (or `--skillopt-dir`)
2. Install `skillopt_sleep` (editable) into your Python environment
3. Create `~/.skillopt-sleep-devin/` (runtime data dir)
4. Seed `skillopt-sleep-learned/SKILL.md` into every detected Devin workspace (`.devin/skills/`)
5. Auto-register with **Devin CLI** MCP (`devin mcp add skillopt-sleep`) if the Devin CLI is on PATH

### Devin post-install

MCP registration is automatic if the Devin CLI is installed.
Optionally copy `devin-rules.snippet.md` to `.devin/rules/skillopt-sleep.md` in your workspace so Devin knows to offer the sleep tools.

### Windows

The runtime (`mcp_server.py` + `harvest_devin.py`) is cross-platform and
auto-detects Devin data under `%LOCALAPPDATA%\devin\cli\transcripts` — no extra flags needed.

`install.sh` is bash, so run it from **Git Bash** or **WSL**, or wire it up
manually: add the snippet from `mcp-config.example.json` to your Devin MCP config
(use `python` instead of `python3` and absolute Windows paths in `args`/`env`).

### Manual config

**Devin** — run once in a terminal:

```bash
devin mcp add skillopt-sleep \
  --env "SKILLOPT_SLEEP_REPO=<project-dir>/../SkillOpt" \
  --env "SKILLOPT_DEVIN_CLAUDE_HOME=$HOME/.skillopt-sleep-devin" \
  -- python3 <project-dir>/mcp_server.py
```

---

## Use

Ask Devin:

> *"run the sleep cycle"*, *"what did the last sleep propose?"*, *"adopt it"*

Or call tools directly:

| Tool | What it does |
|---|---|
| `sleep_status` | nights run so far + latest staged proposal |
| `sleep_dry_run` | preview cycle — no staging, no changes |
| `sleep_run` | full cycle; stages a proposal for your review |
| `sleep_adopt` | apply the staged proposal; syncs skill to workspace |
| `sleep_harvest` | debug: list the recurring tasks mined |

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

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SKILLOPT_SLEEP_REPO` | `~/.local/share/SkillOpt` | Path to the SkillOpt repo |
| `SKILLOPT_DEVIN_CLAUDE_HOME` | `~/.skillopt-sleep-devin` | Runtime data dir |
| `SKILLOPT_DEVIN_WORKSPACES` | auto-detected | Colon-separated workspace paths |
| `SKILLOPT_MANAGED_SKILL` | `skillopt-sleep-learned` | Skill name to evolve |

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
SKILLOPT_SLEEP_REPO=~/.local/share/SkillOpt \
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python3 mcp_server.py
```

---

## Project structure

```
SkillOPT-devin/
├── mcp_server.py              MCP server (stdlib-only, stdio) — Devin
├── harvest_devin.py           Transcript generator (Devin ATIF-v1.7 + agentmemory + skills)
├── judge.py                   Reference judge — scores a reply against a rubric (validation gate)
├── fixtures/
│   └── devin_sample.json      Sample ATIF transcript for offline testing
├── tests/
│   └── test_skillopt_sleep.py Test suite (harvest, Devin path, judge, MCP, engine contract)
├── blog-skillopt-sleep.html   Walk-through / use-case blog (PO · QA · Developer)
├── mcp-config.example.json    Devin MCP config snippet
├── devin-rules.snippet.md     Copy to .devin/rules/skillopt-sleep.md
├── seed_skill/
│   └── SKILL.md               Initial skill seed (replaced by sleep_adopt)
├── install.sh                 One-shot installer (Devin auto-detected)
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
Set `SKILLOPT_JUDGE=claude` (+ `ANTHROPIC_API_KEY`) for an LLM judge.

> **Reality check:** the hard-signal path only fires if Devin actually
> records test or build results in its transcripts.  If it doesn't, every task
> falls to the `judge` branch — point `--devin-transcripts` at a real transcript
> dir and inspect `outcomes.jsonl` to find out which case you're in.

Try it on the bundled fixture:

```bash
python3 harvest_devin.py --devin-transcripts fixtures --out-dir /tmp/skillopt-test
cat /tmp/skillopt-test/outcomes.jsonl
```

---

## Contributing / upstream

This plugin is being contributed back to
[microsoft/SkillOpt](https://github.com/microsoft/SkillOpt) as
`plugins/devin/`.  Bug reports and improvements welcome here or upstream.

## License

MIT — same as microsoft/SkillOpt.
