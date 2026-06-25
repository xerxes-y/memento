#!/usr/bin/env python3
"""Memento — Devin MCP server (stdio, stdlib-only).

Exposes the sleep engine as MCP tools so Devin can drive it.
Speaks JSON-RPC 2.0 over stdio with just the handful of MCP methods Devin
needs.  No third-party deps beyond the SkillOpt repo itself.

Before each tool call this server runs ``harvest_devin.py`` to convert
locally available Devin data (ATIF-v1.7 transcripts, agentmemory memories,
and .devin skill files) into the Claude Code-compatible JSONL transcripts
that the sleep engine consumes.

After ``memento_adopt`` the evolved SKILL.md is also synced back into the active
Devin workspace's ``.devin/skills/`` directory so Devin picks it up immediately.

Tools exposed (identical interface to the Copilot plugin):
  memento_status    show how many nights have run + latest staged proposal
  memento_dry_run   harvest+mine+replay, report only (no staging)
  memento_run       full cycle; stages a reviewed proposal
  memento_adopt     apply the latest staged proposal
  memento_harvest   debug: list mined recurring tasks
  memento_auto      run + auto-adopt above the gate; report the SKILL.md diff
  memory_save       persist a memory (tier/tags/namespace) to the SQLite store
  memory_recall     hybrid search (BM25 + semantic vector, RRF fused)
  memory_list       list recent memories (tier/session filter)
  memory_forget     delete a memory by id or query
  memory_sessions   list sessions with memory counts
  memory_stats      store totals + search backend
  memory_dashboard  start the local web dashboard; return its URL
  memory_related    knowledge-graph neighbours of a memory
  memory_graph      knowledge-graph overview (top entities)
  memory_capture    record a lifecycle event as a working memory
  memory_consolidate  promote reinforced / forget stale memories
  memory_pin        protect a memory from decay/consolidation
  memory_namespaces list scopes; memory_snapshot/restore/audit  governance
  memory_learn      derive lessons from recurring patterns (semantic tier)
  memory_lessons    list derived lessons

Run just the memory dashboard with::

    python mcp_server.py --web [--port 3114]

Configure Devin to launch::

    python plugins/devin/mcp_server.py

with ``MEMENTO_ENGINE_REPO`` set to this repo's root.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import sys

# ── constants ─────────────────────────────────────────────────────────────────

REPO_ROOT = (
    os.environ.get("MEMENTO_ENGINE_REPO")
    or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
)
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
CLAUDE_HOME = os.environ.get(
    "MEMENTO_HOME",
    os.path.expanduser("~/.memento"),
)
MANAGED_SKILL_NAME = os.environ.get("MEMENTO_MANAGED_SKILL", "memento-learned")
# Memory engine lives in memento_memory (SQLite). This path is the
# agentmemory-compatible JSON the engine mirrors to, so the harvester picks up
# saved memories on the next sleep cycle with no extra wiring.
MEMORY_PATH = os.environ.get(
    "MEMENTO_MEMORY_PATH",
    os.path.expanduser("~/.agentmemory/standalone.json"),
)
_MEM_TIERS = ("working", "episodic", "semantic", "procedural")
PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "memento_status",
        "action": "status",
        "description": "Show how many Memento nights have run and the latest staged proposal.",
    },
    {
        "name": "memento_dry_run",
        "action": "dry-run",
        "description": "Preview a sleep cycle (harvest+mine+replay) without staging anything.",
    },
    {
        "name": "memento_run",
        "action": "run",
        "description": "Run a full sleep cycle; stages a reviewed proposal. Nothing live changes until adopt.",
    },
    {
        "name": "memento_adopt",
        "action": "adopt",
        "description": (
            "Apply the latest staged proposal to the managed SKILL.md. "
            "Also syncs the evolved skill into the Devin workspace so Devin picks it up immediately."
        ),
    },
    {
        "name": "memento_harvest",
        "action": "harvest",
        "description": "Debug: list the recurring tasks mined from recent Devin sessions.",
    },
    {
        "name": "memento_auto",
        "action": "auto",
        "description": (
            "Fully automatic: run a sleep cycle and immediately adopt the staged "
            "proposal. Adoption is gated by the engine's held-out validation, plus "
            "an optional MEMENTO_AUTO_ADOPT_MIN_SCORE floor. Returns a before/after "
            "diff report of the SKILL.md change so the user can see what changed."
        ),
    },
    {
        "name": "memory_save",
        "action": "memory_save",
        "description": (
            "Persist a memory to memento's built-in store (SQLite). Secrets are "
            "redacted; memories feed the next sleep cycle automatically — no "
            "external memory MCP needed."
        ),
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short label for the memory."},
                "content": {"type": "string", "description": "The memory text to persist."},
                "tier": {"type": "string", "enum": list(_MEM_TIERS),
                         "description": "Memory tier (default episodic)."},
                "tags": {"type": "string", "description": "Comma-separated tags (optional)."},
                "session": {"type": "string", "description": "Session label (optional)."},
                "namespace": {"type": "string", "description": "Scope (default 'default')."},
            },
            "required": ["title", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_recall",
        "action": "memory_recall",
        "description": ("Search memories by relevance. Default 'hybrid' fuses BM25 "
                        "full-text with semantic vector similarity (RRF)."),
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search text (empty = most recent)."},
                "limit": {"type": "integer", "description": "Max results (default 10)."},
                "tier": {"type": "string", "enum": list(_MEM_TIERS)},
                "mode": {"type": "string", "enum": ["hybrid", "bm25", "vector"]},
                "namespace": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_list",
        "action": "memory_list",
        "description": "List recent memories (optional tier / session filter).",
        "schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max results (default 20)."},
                "tier": {"type": "string", "enum": list(_MEM_TIERS)},
                "session": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_forget",
        "action": "memory_forget",
        "description": "Delete a memory by id, or all memories matching a query.",
        "schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Memory id to delete."},
                "query": {"type": "string", "description": "Delete all matches of this query."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_sessions",
        "action": "memory_sessions",
        "description": "List sessions that have saved memories, with counts.",
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "memory_stats",
        "action": "memory_stats",
        "description": "Memory store stats: totals, per-tier counts, search backend.",
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "memory_dashboard",
        "action": "memory_dashboard",
        "description": "Start the local web dashboard to browse/search/add memories; returns its URL.",
        "schema": {
            "type": "object",
            "properties": {"port": {"type": "integer", "description": "Port (default 3114)."}},
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_related",
        "action": "memory_related",
        "description": "Knowledge-graph: memories sharing an entity with the given memory id.",
        "schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Memory id to find neighbours of."},
                "limit": {"type": "integer"},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_graph",
        "action": "memory_graph",
        "description": "Knowledge-graph overview: top entities and link counts.",
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "memory_capture",
        "action": "memory_capture",
        "description": ("Capture-hook: record an agent lifecycle event "
                        "(SessionStart, PreToolUse, …) as a working memory."),
        "schema": {
            "type": "object",
            "properties": {
                "event": {"type": "string", "description": "Lifecycle event name."},
                "payload": {"type": "string", "description": "Event detail."},
                "session": {"type": "string"},
            },
            "required": ["event", "payload"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_consolidate",
        "action": "memory_consolidate",
        "description": ("Lifecycle: promote reinforced memories up a tier and "
                        "auto-forget stale, never-accessed working memories."),
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "memory_pin",
        "action": "memory_pin",
        "description": "Pin (or unpin) a memory so decay/consolidation never removes it.",
        "schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "pinned": {"type": "boolean", "description": "Default true."},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_namespaces",
        "action": "memory_namespaces",
        "description": "Governance: list memory namespaces (scopes) with counts.",
        "schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "memory_snapshot",
        "action": "memory_snapshot",
        "description": "Governance: write a git-versionable snapshot of all memories.",
        "schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Output path."}},
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_restore",
        "action": "memory_restore",
        "description": "Governance: restore memories from a snapshot file.",
        "schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_audit",
        "action": "memory_audit",
        "description": "Governance: recent audit-log entries (saves, forgets, consolidations).",
        "schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_learn",
        "action": "memory_learn",
        "description": ("Lessons: derive insights from recurring patterns (entities, "
                        "tags, failures) into the semantic tier. Re-run anytime."),
        "schema": {
            "type": "object",
            "properties": {"min_support": {"type": "integer",
                           "description": "Min occurrences for a pattern (default 2)."}},
            "additionalProperties": False,
        },
    },
    {
        "name": "memory_lessons",
        "action": "memory_lessons",
        "description": "Lessons: list derived lessons (semantic insights from patterns).",
        "schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
            "additionalProperties": False,
        },
    },
]
_BY_NAME = {t["name"]: t for t in TOOLS}

_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "project": {
            "type": "string",
            "description": "Project dir to evolve (default: cwd).",
        },
        "backend": {
            "type": "string",
            "enum": ["mock", "claude", "codex"],
            "description": "mock = no API spend (default); claude/codex = real.",
        },
        "scope": {"type": "string", "enum": ["invoked", "all"]},
    },
    "additionalProperties": False,
}

# ── harvest step ──────────────────────────────────────────────────────────────

def _run_harvest() -> str:
    harvester = os.path.join(PLUGIN_DIR, "harvest_devin.py")
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(
            [sys.executable, harvester, "--out-dir", CLAUDE_HOME],
            capture_output=True, text=True, timeout=60, env=env,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        return out + (("\n[harvest stderr]\n" + err) if err else "")
    except Exception as exc:
        return f"[harvest_devin] warning: {exc}"

# ── post-adopt: sync evolved skill into workspace (.devin) ────────────────────

def _sync_skill(project: str) -> str:
    src = os.path.join(CLAUDE_HOME, "skills", MANAGED_SKILL_NAME, "SKILL.md")
    if not os.path.isfile(src):
        return ""
    if not project or not os.path.isdir(project):
        return ""
    synced = []
    dot_root = os.path.join(project, ".devin")
    if os.path.isdir(dot_root):
        dst_dir = os.path.join(dot_root, "skills", MANAGED_SKILL_NAME)
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, "SKILL.md")
        shutil.copy2(src, dst)
        synced.append(dst)
    return ("\n" + "\n".join(f"[sleep] synced evolved skill → {p}" for p in synced)
            if synced else "")

# ── engine call ───────────────────────────────────────────────────────────────

def _run_engine(action: str, args: dict) -> str:
    harvest_out = _run_harvest()

    project = args.get("project") or os.getcwd()
    backend = args.get("backend") or "mock"
    scope = args.get("scope") or "invoked"

    cmd = [
        sys.executable, "-m", "skillopt_sleep", action,
        "--claude-home", CLAUDE_HOME,
        "--project", project,
        "--scope", scope,
        "--backend", backend,
        "--source", "claude",
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3600, env=env,
        )
    except Exception as exc:
        return f"[harvest]\n{harvest_out}\n[error] failed to run engine: {exc}"

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    result = f"[harvest]\n{harvest_out}\n\n[engine]\n{out}"
    if err:
        result += f"\n[stderr]\n{err}"
    if action == "adopt":
        result += _sync_skill(project)
    return result

# ── fully automatic: run → gate → adopt → report diff ─────────────────────────

def _managed_skill_path() -> str:
    return os.path.join(CLAUDE_HOME, "skills", MANAGED_SKILL_NAME, "SKILL.md")


def _read_skill() -> str:
    try:
        with open(_managed_skill_path(), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _extract_score(text: str):
    """Best-effort parse of a validation score from engine stdout.

    Returns a float in roughly [0, 1] or None. Used only to enforce the optional
    MEMENTO_AUTO_ADOPT_MIN_SCORE floor; the engine's own held-out gate is the
    real safety mechanism, so a None here just means "defer to the engine".
    """
    matches = re.findall(r"score[^0-9\-]*([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def _skill_diff(before: str, after: str) -> str:
    if before == after:
        return ""
    return "\n".join(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile="SKILL.md (before)", tofile="SKILL.md (after)", lineterm="",
    ))


def _run_auto(args: dict) -> str:
    """Run a cycle, adopt the staged proposal if it clears the gate, report the diff.

    Two gates apply, narrowest first:
      1. the engine only *stages* a proposal when it strictly improves its
         held-out validation score (upstream behavior, always on);
      2. an optional MEMENTO_AUTO_ADOPT_MIN_SCORE floor enforced here, applied
         only when a score is parseable from the run output.
    """
    raw_floor = os.environ.get("MEMENTO_AUTO_ADOPT_MIN_SCORE")
    try:
        min_score = float(raw_floor) if raw_floor else None
    except ValueError:
        min_score = None

    before = _read_skill()
    run_out = _run_engine("run", args)
    score = _extract_score(run_out)

    if min_score is not None and score is not None and score < min_score:
        return (
            f"{run_out}\n\n[auto] validation score {score:.3f} < threshold "
            f"{min_score:.3f} — proposal NOT adopted."
        )

    adopt_out = _run_engine("adopt", args)
    after = _read_skill()
    diff = _skill_diff(before, after)

    report = [run_out, "", adopt_out, "", "[auto] === skill change report ==="]
    if score is not None:
        line = f"[auto] validation score: {score:.3f}"
        if min_score is not None:
            line += f" (threshold {min_score:.3f})"
        report.append(line)
    if diff:
        report.append(f"[auto] SKILL.md updated ({MANAGED_SKILL_NAME}):\n{diff}")
    else:
        report.append("[auto] no change to SKILL.md "
                      "(engine staged nothing, or the proposal was empty).")
    return "\n".join(report)

# ── memory engine (memento_memory: SQLite + BM25 + tiers + dashboard) ─────────

_MEMORY_STORE = None


def _store():
    """Lazily build (and cache) the memory engine — shared Postgres if
    MEMENTO_DB_URL is set (team mode), else local SQLite."""
    global _MEMORY_STORE
    if _MEMORY_STORE is None:
        import memento_memory
        _MEMORY_STORE = memento_memory.open_store(export_path=MEMORY_PATH)
    return _MEMORY_STORE


def _fmt(rows: list) -> str:
    if not rows:
        return "[memory] no memories found."
    lines = [f"- ({m['tier']}) {m['title']}: {str(m['content']).strip()[:200]}"
             + (f"  #{m['tags']}" if m.get("tags") else "")
             for m in rows]
    return f"[memory] {len(rows)} memory(ies):\n" + "\n".join(lines)


def _memory_save(args: dict) -> str:
    try:
        mid = _store().save(args.get("title"), args.get("content"),
                            tier=args.get("tier"), tags=args.get("tags"),
                            session=args.get("session", ""),
                            namespace=args.get("namespace") or "default")
    except ValueError as exc:
        return f"[memory] error: {exc}"
    return f"[memory] saved ({mid}) in tier '{args.get('tier') or 'episodic'}': {args.get('title')}"


def _memory_recall(args: dict) -> str:
    return _fmt(_store().search(args.get("query"), limit=args.get("limit") or 10,
                                tier=args.get("tier"), namespace=args.get("namespace"),
                                mode=args.get("mode") or "hybrid"))


def _memory_list(args: dict) -> str:
    return _fmt(_store().list(limit=args.get("limit") or 20, tier=args.get("tier"),
                              session=args.get("session")))


def _memory_forget(args: dict) -> str:
    n = _store().forget(mem_id=args.get("id"), query=args.get("query"))
    return f"[memory] forgot {n} memory(ies)."


def _memory_sessions(_args: dict) -> str:
    rows = _store().sessions()
    if not rows:
        return "[memory] no sessions recorded."
    return "[memory] sessions:\n" + "\n".join(
        f"- {r['session']}: {r['n']} memories" for r in rows)


def _memory_stats(_args: dict) -> str:
    s = _store().stats()
    tiers = ", ".join(f"{k}={v}" for k, v in s["by_tier"].items()) or "none"
    mode = s.get("mode", "solo")
    return (f"[memory] {mode} mode ({s.get('backend', 'sqlite')}); "
            f"{s['total']} total ({tiers}); "
            f"bm25={'on' if s['fts'] else 'off (LIKE fallback)'}; db={s['db']}")


def _memory_dashboard(args: dict) -> str:
    import memento_memory
    port = int(args.get("port") or memento_memory.DEFAULT_PORT)
    url = memento_memory.start_dashboard(_store(), port=port)
    return f"[memory] dashboard running at {url}"


def _memory_related(args: dict) -> str:
    rows = _store().related(args.get("id"), limit=args.get("limit") or 10)
    return _fmt(rows)


def _memory_graph(_args: dict) -> str:
    g = _store().graph()
    if not g["entities"]:
        return "[memory] knowledge graph is empty."
    top = ", ".join(f"{e['name']}({e['count']})" for e in g["entities"][:20])
    return (f"[memory] {len(g['entities'])} entities, {len(g['edges'])} links. "
            f"Top: {top}")


def _memory_capture(args: dict) -> str:
    mid = _store().capture(args.get("event") or "Event", args.get("payload") or "",
                           session=args.get("session", ""))
    return f"[memory] captured {args.get('event')} → {mid}"


def _memory_consolidate(_args: dict) -> str:
    r = _store().consolidate()
    return f"[memory] consolidated: promoted {r['promoted']}, forgot {r['forgotten']}."


def _memory_pin(args: dict) -> str:
    ok = _store().pin(args.get("id"), pinned=args.get("pinned", True))
    return f"[memory] {'pinned' if args.get('pinned', True) else 'unpinned'} " + \
           (args.get("id") or "") + ("" if ok else " (not found)")


def _memory_namespaces(_args: dict) -> str:
    rows = _store().namespaces()
    return "[memory] namespaces:\n" + "\n".join(
        f"- {r['namespace']}: {r['n']}" for r in rows) if rows else "[memory] none."


def _memory_snapshot(args: dict) -> str:
    path = args.get("path") or os.path.join(CLAUDE_HOME, "memory-snapshot.json")
    n = _store().snapshot(path)
    return f"[memory] snapshot of {n} memories → {path}"


def _memory_restore(args: dict) -> str:
    path = args.get("path")
    if not path or not os.path.isfile(path):
        return "[memory] error: 'path' to a snapshot file is required."
    return f"[memory] restored {_store().restore(path)} memories from {path}"


def _memory_audit(args: dict) -> str:
    rows = _store().audit_log(limit=args.get("limit") or 20)
    if not rows:
        return "[memory] audit log empty."
    return "[memory] audit:\n" + "\n".join(
        f"- {r['op']} {r['mem_id']} {r['detail']}".rstrip() for r in rows)


def _memory_learn(args: dict) -> str:
    created = _store().learn(min_support=args.get("min_support") or 2)
    if not created:
        return "[memory] no recurring patterns yet — nothing to learn."
    return f"[memory] derived {len(created)} lesson(s):\n" + "\n".join(
        f"- {label}" for _, label in created)


def _memory_lessons(args: dict) -> str:
    rows = _store().lessons(limit=args.get("limit") or 20)
    if not rows:
        return "[memory] no lessons yet — run memory_learn."
    return "[memory] lessons:\n" + "\n".join(
        f"- {m['title']}: {str(m['content']).strip()[:160]}" for m in rows)


_MEMORY_ACTIONS = {
    "memory_save": _memory_save,
    "memory_recall": _memory_recall,
    "memory_list": _memory_list,
    "memory_forget": _memory_forget,
    "memory_sessions": _memory_sessions,
    "memory_stats": _memory_stats,
    "memory_dashboard": _memory_dashboard,
    "memory_related": _memory_related,
    "memory_graph": _memory_graph,
    "memory_capture": _memory_capture,
    "memory_consolidate": _memory_consolidate,
    "memory_pin": _memory_pin,
    "memory_namespaces": _memory_namespaces,
    "memory_snapshot": _memory_snapshot,
    "memory_restore": _memory_restore,
    "memory_audit": _memory_audit,
    "memory_learn": _memory_learn,
    "memory_lessons": _memory_lessons,
}

# ── JSON-RPC / MCP plumbing ───────────────────────────────────────────────────

def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle(req: dict):
    method = req.get("method")
    id_ = req.get("id")
    if method == "initialize":
        return _result(id_, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "memento", "version": "0.1.0"},
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        return _result(id_, {"tools": [
            {"name": t["name"], "description": t["description"],
             "inputSchema": t.get("schema", _TOOL_SCHEMA)}
            for t in TOOLS
        ]})
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        tool = _BY_NAME.get(name)
        if not tool:
            return _error(id_, -32602, f"unknown tool: {name}")
        tool_args = params.get("arguments") or {}
        action = tool["action"]
        handler = _MEMORY_ACTIONS.get(action)
        if action == "auto":
            text = _run_auto(tool_args)
        elif handler:
            text = handler(tool_args)
        else:
            text = _run_engine(action, tool_args)
        return _result(id_, {"content": [{"type": "text", "text": text}]})
    if method == "ping":
        return _result(id_, {})
    return _error(id_, -32601, f"method not found: {method}")


def run_auto_cli(argv) -> int:
    """Standalone, non-MCP entrypoint for scheduled (launchd/cron) runs.

    Usage: python3 mcp_server.py --auto [--project PATH] [--backend mock|claude|codex]
                                        [--scope invoked|all]
    Runs one full auto cycle (run → gate → adopt) and prints the change report.
    """
    args = {}
    it = iter(argv)
    for tok in it:
        if tok == "--auto":
            continue
        if tok in ("--project", "--backend", "--scope"):
            args[tok[2:]] = next(it, "")
    sys.stdout.write(_run_auto(args) + "\n")
    sys.stdout.flush()
    return 0


def run_web_cli(argv) -> int:
    """Standalone entrypoint: serve only the memory dashboard (blocking)."""
    import memento_memory
    port = memento_memory.DEFAULT_PORT
    it = iter(argv)
    for tok in it:
        if tok == "--port":
            port = int(next(it, port))
    try:
        memento_memory.serve_forever(port=port)
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    if "--auto" in sys.argv[1:]:
        return run_auto_cli(sys.argv[1:])
    if "--web" in sys.argv[1:]:
        return run_web_cli(sys.argv[1:])
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
