#!/usr/bin/env python3
"""Test suite for the Memento Devin plugin.

Stdlib-only (unittest) so it runs anywhere the plugin runs — no pytest needed:

    python3 -m unittest discover -s tests -v

Coverage:
  * harvest helpers        — taskKey, outcome detection, rubric
  * Devin ATIF path        — real ATIF-v1.7 transcript → JSONL + outcomes.jsonl
  * judge.py               — rubric scoring discriminates good vs bad replies
  * MCP server             — JSON-RPC initialize / tools/list / error paths
  * microsoft/SkillOpt     — the engine command CONTRACT (argv the server shells out to)
  * optional integration   — runs the real engine IF skillopt_sleep is installed, else skips
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

# Make the plugin modules importable regardless of where the tests are run from.
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

import harvest_devin as hw  # noqa: E402
import judge                    # noqa: E402
import mcp_server               # noqa: E402
import memento_memory           # noqa: E402

FIXTURES = os.path.join(PLUGIN_DIR, "fixtures")


# ── harvest helpers ───────────────────────────────────────────────────────────

class TestTaskKey(unittest.TestCase):
    def test_lang_intent_target(self):
        self.assertEqual(
            hw._normalize_task_key("Implement pagination for the users API in python", "/p"),
            "python:implement:pagination",
        )

    def test_camelcase_target_preferred(self):
        self.assertEqual(
            hw._normalize_task_key("Fix the NPE in OrderService.persist()", "/p"),
            "general:fix:orderservice",
        )

    def test_target_never_the_intent_verb(self):
        key = hw._normalize_task_key("Refactor the auth module", "/p")
        self.assertEqual(key, "general:refactor:auth")
        self.assertNotIn(":refactor:refactor", key)


class TestOutcome(unittest.TestCase):
    def test_zero_failed_is_success(self):
        out = hw._detect_outcome(["Ran suite: BUILD SUCCESS, 142 passed, 0 failed."])
        self.assertTrue(out["success"])
        self.assertEqual(out["evidence"], "BUILD SUCCESS")

    def test_real_failure_detected(self):
        out = hw._detect_outcome(["pytest: 3 failed, 10 passed"])
        self.assertFalse(out["success"])

    def test_no_signal_returns_none(self):
        self.assertIsNone(hw._detect_outcome(["I refactored it; looks cleaner now."]))

    def test_repro_is_trimmed(self):
        out = hw._detect_outcome(
            ["Ran rtk mvn test -Dtest=OrderServiceTest -> BUILD SUCCESS, 142 passed, 0 failed"]
        )
        self.assertEqual(out["reference"]["repro"], "rtk mvn test -Dtest=OrderServiceTest")


class TestRubric(unittest.TestCase):
    def test_judge_fallback_builds_rubric(self):
        fb = hw._judge_rubric_fallback("Refactor OrderService.persist() and rename helper.go")
        self.assertEqual(fb["verifier"], "judge")
        self.assertIsNone(fb["success"])
        self.assertTrue(any("OrderService" in c for c in fb["rubric"]))


# ── cross-platform path resolution (Linux + Windows) ─────────────────────────

class TestCrossPlatformPaths(unittest.TestCase):
    def test_windows_app_data_root(self):
        with mock.patch.object(hw.os, "name", "nt"), \
             mock.patch.dict(hw.os.environ, {"APPDATA": r"C:\Users\me\AppData\Roaming"}):
            roots = hw._app_data_roots("Devin")
        self.assertTrue(any("AppData" in r and r.endswith("Devin") for r in roots),
                        f"no Windows %APPDATA% root in {roots}")

    def test_linux_app_data_root(self):
        env = {k: v for k, v in hw.os.environ.items() if k != "XDG_CONFIG_HOME"}
        with mock.patch.object(hw.os, "name", "posix"), \
             mock.patch.object(hw.sys, "platform", "linux"), \
             mock.patch.dict(hw.os.environ, env, clear=True):
            roots = hw._app_data_roots("Devin")
        self.assertTrue(any(r.endswith(os.path.join(".config", "Devin")) for r in roots),
                        f"no ~/.config root in {roots}")

    def test_uri_to_path_linux(self):
        self.assertEqual(hw._uri_to_path("file:///home/me/proj"), "/home/me/proj")

    def test_uri_to_path_windows(self):
        with mock.patch.object(hw.os, "name", "nt"):
            self.assertEqual(hw._uri_to_path("file:///c%3A/Users/me/proj"),
                             "c:/Users/me/proj")

    def test_env_override_splits_on_pathsep(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            joined = os.pathsep.join([a, b])
            with mock.patch.dict(hw.os.environ, {"MEMENTO_WORKSPACES": joined}):
                ws = hw._detect_workspaces()
            self.assertIn(a, ws)
            self.assertIn(b, ws)


# ── Devin ATIF transcript path (the "works with Devin" guarantee) ─────────────

class TestDevinHarvest(unittest.TestCase):
    def test_hard_signal_transcript(self):
        """The bundled ATIF fixture should yield one gradeable, passing task."""
        with tempfile.TemporaryDirectory() as out:
            n = hw.harvest_devin_transcripts(FIXTURES, out, ["/tmp/proj"])
            self.assertEqual(n, 1)

            outcomes = _read_jsonl(os.path.join(out, "outcomes.jsonl"))
            self.assertEqual(len(outcomes), 1)
            o = outcomes[0]
            self.assertEqual(o["verifier"], "tests")
            self.assertTrue(o["success"])
            self.assertIn("repro", o["reference"])

            # the transcript JSONL must carry the grouping key on the user turn
            session = _find_session_jsonl(out)
            user_turn = next(r for r in session if r["type"] == "user")
            self.assertIn("taskKey", user_turn)

    def test_judge_branch_when_no_result_recorded(self):
        """An ATIF transcript with no test/build result falls back to the judge."""
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
            transcript = {
                "schema_version": "ATIF-v1.7",
                "session_id": "no-signal-001",
                "steps": [
                    {"source": "user", "message": "Refactor the auth module for clarity",
                     "timestamp": "2026-06-20T10:00:00Z"},
                    {"source": "agent", "message": "Extracted helpers and renamed things.",
                     "timestamp": "2026-06-20T10:00:30Z"},
                ],
            }
            with open(os.path.join(src, "t.json"), "w") as f:
                json.dump(transcript, f)
            hw.harvest_devin_transcripts(src, out, ["/tmp/proj"])
            o = _read_jsonl(os.path.join(out, "outcomes.jsonl"))[0]
            self.assertEqual(o["verifier"], "judge")
            self.assertIsNone(o["success"])
            self.assertIn("rubric", o)

    def test_non_atif_files_skipped(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
            with open(os.path.join(src, "other.json"), "w") as f:
                json.dump({"schema_version": "something-else", "steps": []}, f)
            self.assertEqual(hw.harvest_devin_transcripts(src, out, ["/tmp/proj"]), 0)


# ── judge.py ──────────────────────────────────────────────────────────────────

class TestJudge(unittest.TestCase):
    RUBRIC = ["Addresses OrderService",
              "Resolves the reported defect without introducing new errors"]

    def test_discriminates(self):
        good = "I fixed OrderService and resolved the defect without new errors."
        bad = "Sure, I can help."
        self.assertGreater(judge.heuristic_score(good, self.RUBRIC),
                           judge.heuristic_score(bad, self.RUBRIC))

    def test_empty_rubric(self):
        self.assertEqual(judge.heuristic_score("anything", []), 0.0)

    def test_score_in_range(self):
        s = judge.heuristic_score("OrderService defect resolved", self.RUBRIC)
        self.assertTrue(0.0 <= s <= 1.0)


# ── MCP server protocol ───────────────────────────────────────────────────────

class TestMcpProtocol(unittest.TestCase):
    def test_initialize(self):
        resp = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(resp["result"]["protocolVersion"], mcp_server.PROTOCOL_VERSION)
        self.assertIn("serverInfo", resp["result"])

    def test_tools_list_has_expected_tools(self):
        resp = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = resp["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertEqual(names, {"memento_status", "memento_dry_run", "memento_run",
                                 "memento_adopt", "memento_harvest", "memento_auto",
                                 "memory_save", "memory_recall", "memory_list",
                                 "memory_forget", "memory_sessions", "memory_stats",
                                 "memory_dashboard", "memory_related", "memory_graph",
                                 "memory_capture", "memory_consolidate", "memory_pin",
                                 "memory_namespaces", "memory_snapshot",
                                 "memory_restore", "memory_audit",
                                 "memory_learn", "memory_lessons", "memory_brief"})
        for t in tools:
            self.assertIn("inputSchema", t)
        # memory_save advertises its own schema, not the shared engine schema
        save = next(t for t in tools if t["name"] == "memory_save")
        self.assertEqual(set(save["inputSchema"]["required"]), {"title", "content"})

    def test_unknown_method(self):
        resp = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "bogus"})
        self.assertEqual(resp["error"]["code"], -32601)

    def test_unknown_tool(self):
        resp = mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                  "params": {"name": "nope", "arguments": {}}})
        self.assertEqual(resp["error"]["code"], -32602)

    def test_initialized_notification_is_silent(self):
        self.assertIsNone(mcp_server.handle({"method": "notifications/initialized"}))


# ── microsoft/SkillOpt engine contract ────────────────────────────────────────

class TestSkillOptContract(unittest.TestCase):
    """Assert the EXACT command the server shells out to matches the upstream
    `python -m skillopt_sleep <action>` interface — without needing it installed.

    This is the integration contract with microsoft/SkillOpt: if upstream renames
    the module or a flag, this test fails loudly instead of at runtime in the IDE.
    """

    def test_engine_argv(self):
        captured = {}

        def fake_run(cmd, *a, **kw):
            captured["cmd"] = cmd
            return types.SimpleNamespace(stdout="ok", stderr="")

        orig_run, orig_harvest = mcp_server.subprocess.run, mcp_server._run_harvest
        try:
            mcp_server.subprocess.run = fake_run
            mcp_server._run_harvest = lambda: ""  # skip the harvest subprocess
            mcp_server._run_engine("run", {"project": "/p", "backend": "claude", "scope": "all"})
        finally:
            mcp_server.subprocess.run, mcp_server._run_harvest = orig_run, orig_harvest

        cmd = captured["cmd"]
        self.assertEqual(cmd[1:4], ["-m", "skillopt_sleep", "run"])
        # flags the upstream copilot plugin uses
        for flag, val in [("--project", "/p"), ("--backend", "claude"),
                          ("--scope", "all"), ("--source", "claude")]:
            self.assertIn(flag, cmd)
            self.assertEqual(cmd[cmd.index(flag) + 1], val)
        self.assertIn("--claude-home", cmd)

    def test_all_tool_actions_map_to_engine_subcommands(self):
        expected = {"memento_status": "status", "memento_dry_run": "dry-run",
                    "memento_run": "run", "memento_adopt": "adopt", "memento_harvest": "harvest"}
        for name, action in expected.items():
            self.assertEqual(mcp_server._BY_NAME[name]["action"], action)

    def test_memento_auto_action_is_not_an_engine_subcommand(self):
        # 'auto' is handled in-process (run→adopt), never shelled out verbatim.
        self.assertEqual(mcp_server._BY_NAME["memento_auto"]["action"], "auto")


class TestSleepAuto(unittest.TestCase):
    """The fully-automatic run→gate→adopt→report flow (no engine needed)."""

    def _patch(self, engine_calls, before, after, run_out="ok"):
        actions = []

        def fake_engine(action, args):
            actions.append(action)
            engine_calls.append((action, args))
            return run_out if action == "run" else f"[engine] {action} done"

        skills = iter([before, after])
        mcp_server._run_engine = fake_engine
        mcp_server._read_skill = lambda: next(skills)
        return actions

    def setUp(self):
        self._orig = (mcp_server._run_engine, mcp_server._read_skill,
                      mcp_server._run_auto, dict(os.environ))

    def tearDown(self):
        (mcp_server._run_engine, mcp_server._read_skill,
         mcp_server._run_auto, _env) = self._orig
        os.environ.clear()
        os.environ.update(_env)

    def test_runs_then_adopts_and_reports_diff(self):
        os.environ.pop("MEMENTO_AUTO_ADOPT_MIN_SCORE", None)
        calls = []
        self._patch(calls, before="old line\n", after="new line\n",
                    run_out="validation score 0.80")
        out = mcp_server._run_auto({"project": "/p"})
        self.assertEqual([a for a, _ in calls], ["run", "adopt"])
        self.assertIn("skill change report", out)
        self.assertIn("+new line", out)
        self.assertIn("-old line", out)

    def test_threshold_blocks_adopt_below_floor(self):
        os.environ["MEMENTO_AUTO_ADOPT_MIN_SCORE"] = "0.9"
        calls = []
        self._patch(calls, before="x", after="y", run_out="validation score 0.50")
        out = mcp_server._run_auto({})
        self.assertEqual([a for a, _ in calls], ["run"])  # adopt never called
        self.assertIn("NOT adopted", out)

    def test_no_change_reported_when_skill_unchanged(self):
        os.environ.pop("MEMENTO_AUTO_ADOPT_MIN_SCORE", None)
        calls = []
        self._patch(calls, before="same", after="same", run_out="score 0.99")
        out = mcp_server._run_auto({})
        self.assertEqual([a for a, _ in calls], ["run", "adopt"])
        self.assertIn("no change to SKILL.md", out)

    def test_cli_parses_auto_flags_into_args(self):
        captured = {}
        mcp_server._run_auto = lambda args: captured.update(args) or "report"
        with contextlib.redirect_stdout(io.StringIO()):
            rc = mcp_server.run_auto_cli(
                ["--auto", "--project", "/p", "--backend", "claude", "--scope", "all"])
        self.assertEqual(rc, 0)
        self.assertEqual(captured,
                         {"project": "/p", "backend": "claude", "scope": "all"})


class TestMemoryEngine(unittest.TestCase):
    """memento_memory: SQLite store, BM25 search, tiers, redaction, export."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.export = os.path.join(self._dir, "standalone.json")
        self.store = memento_memory.MemoryStore(
            db_path=os.path.join(self._dir, "memory.db"), export_path=self.export)

    def test_save_and_export_is_harvester_compatible(self):
        self.store.save("Use rtk for builds", "Run rtk mvn test.", tier="procedural")
        mems = _read_json(self.export)["mem:memories"]
        self.assertEqual(len(mems), 1)
        self.assertEqual(next(iter(mems.values()))["title"], "Use rtk for builds")
        # the existing harvester reads exactly this shape
        n = hw.harvest_agentmemory(self.export, self._dir, ["/tmp/p"])
        self.assertEqual(n, 1)

    def test_save_requires_both_fields(self):
        with self.assertRaises(ValueError):
            self.store.save("", "content only")

    def test_idempotent_on_identical_content(self):
        self.store.save("t", "c")
        self.store.save("t", "c")
        self.assertEqual(self.store.stats()["total"], 1)

    def test_search_ranks_by_relevance(self):
        self.store.save("Build tip", "use rtk mvn test for the order service")
        self.store.save("Style", "prefer black formatting in python")
        hits = self.store.search("rtk")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["title"], "Build tip")
        self.assertEqual(self.store.search("nomatch"), [])

    def test_tier_filter(self):
        self.store.save("a", "alpha", tier="working")
        self.store.save("b", "beta", tier="semantic")
        self.assertEqual([m["title"] for m in self.store.list(tier="working")], ["a"])

    def test_sessions_grouping(self):
        self.store.save("a", "alpha", session="s1")
        self.store.save("b", "beta", session="s1")
        self.store.save("c", "gamma", session="s2")
        by = {r["session"]: r["n"] for r in self.store.sessions()}
        self.assertEqual(by, {"s1": 2, "s2": 1})

    def test_graph_payload_has_memory_nodes(self):
        self.store.save("Fix", "patch OrderService.persist()")
        g = self.store.graph()
        self.assertTrue(g["entities"] and g["memories"] and g["edges"])
        self.assertIn("title", g["memories"][0])

    def test_secrets_are_redacted(self):
        self.store.save("leak", "token=pypi-AgEIcHlwaS5vcmcABCDEF0123456789xyz")
        m = self.store.list()[0]
        self.assertNotIn("pypi-AgEIc", m["content"])
        self.assertIn("[REDACTED]", m["content"])

    def test_forget_by_id_and_query(self):
        mid = self.store.save("temp", "delete me by id")
        self.store.save("keep", "keep this around")
        self.assertEqual(self.store.forget(mem_id=mid), 1)
        self.assertEqual(self.store.forget(query="keep this"), 1)
        self.assertEqual(self.store.stats()["total"], 0)

    def test_dashboard_api_roundtrip(self):
        import json as _json
        from urllib.request import urlopen, Request
        srv = memento_memory.make_server(self.store, port=0)
        try:
            port = srv.server_address[1]
            t = threading.Thread(target=srv.handle_request)  # serve one POST
            t.start()
            body = _json.dumps({"title": "via web",
                                "content": "added through dashboard"}).encode()
            # reading the response guarantees the save handler finished
            urlopen(Request(f"http://127.0.0.1:{port}/api/memories", data=body,
                            headers={"content-type": "application/json"})).read()
            t.join(timeout=5)
            self.assertEqual(self.store.stats()["total"], 1)
        finally:
            srv.server_close()


class TestMemoryAdvanced(unittest.TestCase):
    """Phases 2-5: vector/hybrid search, graph, lifecycle, governance."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.store = memento_memory.MemoryStore(
            db_path=os.path.join(self._dir, "memory.db"),
            export_path=os.path.join(self._dir, "standalone.json"))

    def test_update_edits_in_place_and_reindexes(self):
        mid = self.store.save("Build", "uses maven mvn test", tier="working",
                              tags="build")
        ok = self.store.update(mid, content="now uses gradle gradlew check",
                               tier="procedural", tags="build,gradle", actor="qa")
        self.assertTrue(ok)
        m = self.store.get(mid)
        self.assertEqual(m["id"], mid)                 # id stays stable
        self.assertEqual(m["tier"], "procedural")
        self.assertIn("gradle", m["content"])
        # FTS reindexed: searchable by the new term, not the old one
        self.assertTrue(self.store.search("gradlew"))
        self.assertEqual(self.store.search("maven"), [])
        # audit recorded the edit
        self.assertEqual(self.store.audit_log(limit=1)[0]["op"], "edit")

    def test_update_missing_id_returns_false(self):
        self.assertFalse(self.store.update("mem-nope", content="x"))

    def test_update_rejects_emptying_required_field(self):
        mid = self.store.save("T", "body")
        with self.assertRaises(ValueError):
            self.store.update(mid, content="   ")

    # Phase 2
    def test_vector_search_finds_lexically_similar(self):
        self.store.save("Build", "run the test suite with rtk")
        self.store.save("Docs", "write the changelog entry")
        hits = self.store.search("execute tests rtk", mode="vector")
        self.assertTrue(hits and hits[0]["title"] == "Build")

    def test_hybrid_excludes_unrelated(self):
        self.store.save("Build", "rtk mvn test orderservice")
        self.store.save("Style", "prefer black formatting in python")
        titles = [m["title"] for m in self.store.search("orderservice", mode="hybrid")]
        self.assertIn("Build", titles)
        self.assertNotIn("Style", titles)

    # Phase 3
    def test_knowledge_graph_links_shared_entities(self):
        a = self.store.save("Fix", "patch OrderService.persist() bug")
        self.store.save("Test", "add a test for OrderService coverage")
        self.store.save("Other", "unrelated note about weather")
        rel = self.store.related(a)
        self.assertEqual([m["title"] for m in rel], ["Test"])
        g = self.store.graph()
        self.assertTrue(any(e["name"].lower().startswith("orderservice")
                            for e in g["entities"]))

    # Phase 4
    def test_consolidation_promotes_and_forgets(self):
        old = self.store.save("stale", "never accessed working note", tier="working")
        hot = self.store.save("hot", "frequently used note", tier="working")
        for _ in range(3):
            self.store.search("frequently used")          # bump access_count
        # make the stale one look old
        with self.store._connect() as c:
            c.execute("UPDATE memories SET created_ts=? WHERE id=?",
                      (1.0, old))
        r = self.store.consolidate()
        self.assertEqual(r["forgotten"], 1)
        self.assertGreaterEqual(r["promoted"], 1)
        self.assertIsNone(self.store.get(old))
        self.assertEqual(self.store.get(hot)["tier"], "episodic")

    def test_pin_protects_from_consolidation(self):
        mid = self.store.save("keep", "old working note", tier="working")
        self.store.pin(mid)
        with self.store._connect() as c:
            c.execute("UPDATE memories SET created_ts=1.0 WHERE id=?", (mid,))
        self.store.consolidate()
        self.assertIsNotNone(self.store.get(mid))

    def test_capture_hook_creates_working_memory(self):
        self.store.capture("PreToolUse", "ran git status", session="s1")
        rows = self.store.list(tier="working")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "hook")

    # Phase 5
    def test_namespaces_isolate(self):
        self.store.save("a", "alpha", namespace="team-x")
        self.store.save("b", "beta", namespace="team-y")
        self.assertEqual(len(self.store.list(namespace="team-x")), 1)
        self.assertEqual({n["namespace"] for n in self.store.namespaces()},
                         {"team-x", "team-y"})

    def test_snapshot_and_restore_roundtrip(self):
        self.store.save("one", "first memory")
        self.store.save("two", "second memory")
        snap = os.path.join(self._dir, "snap.json")
        self.assertEqual(self.store.snapshot(snap), 2)
        fresh = memento_memory.MemoryStore(
            db_path=os.path.join(self._dir, "fresh.db"),
            export_path=os.path.join(self._dir, "fresh.json"))
        self.assertEqual(fresh.restore(snap), 2)
        self.assertEqual(fresh.stats()["total"], 2)
        self.assertTrue(fresh.search("first"))

    def test_audit_log_records_ops(self):
        self.store.save("x", "audited save")
        ops = {r["op"] for r in self.store.audit_log()}
        self.assertIn("save", ops)

    # Lessons
    def test_learn_derives_lesson_from_recurring_entity(self):
        self.store.save("Fix", "patch OrderService.persist() bug")
        self.store.save("Test", "cover OrderService.persist() edge cases")
        created = self.store.learn(min_support=2)
        self.assertTrue(created)
        lessons = self.store.lessons()
        self.assertTrue(lessons)
        self.assertEqual(lessons[0]["tier"], "semantic")
        self.assertEqual(lessons[0]["tags"], "lesson")
        self.assertTrue(any("OrderService" in m["content"] for m in lessons))

    def test_learn_is_idempotent(self):
        self.store.save("a", "shared WidgetCache here")
        self.store.save("b", "also a WidgetCache there")
        n1 = len(self.store.learn())
        n2 = len(self.store.learn())
        self.assertEqual(n1, n2)
        # re-learning regenerates rather than accumulating
        self.assertEqual(len([m for m in self.store.lessons()
                              if "WidgetCache" in m["content"]]), 1)

    def test_manual_lesson_is_pinned_and_survives_relearn(self):
        mid = self.store.add_lesson("Always rebase", "we squash-merge; rebase first")
        self.assertTrue(self.store.get(mid)["pinned"])
        self.assertEqual(self.store.get(mid)["tier"], "semantic")
        # seed a recurring pattern, then re-derive lessons
        self.store.save("a", "touch WidgetCache")
        self.store.save("b", "again WidgetCache")
        self.store.learn()
        titles = [m["title"] for m in self.store.lessons()]
        self.assertIn("Always rebase", titles)   # manual one kept
        self.assertTrue(any("WidgetCache" in m["content"] for m in self.store.lessons()))

    def test_learn_summarizes_failures(self):
        self.store.capture("PostToolUseFailure", "build error: NPE in checkout")
        self.store.capture("PostToolUseFailure", "test failure in payment flow")
        self.store.learn()
        self.assertTrue(any("failure" in m["title"].lower()
                            for m in self.store.lessons()))

    def test_lessons_excluded_from_pattern_mining(self):
        self.store.save("Fix", "patch OrderService.persist()")
        self.store.save("Test", "test OrderService.persist()")
        self.store.learn()
        # lessons must not feed back into the entity counts on a second pass
        ents = dict(self.store.patterns(min_support=2)["entities"])
        self.assertNotIn("lesson", [e.lower() for e in ents])


class TestMemoryTools(unittest.TestCase):
    """The MCP-facing memory_* handlers in mcp_server."""

    def setUp(self):
        self._orig = mcp_server._MEMORY_STORE
        self._dir = tempfile.mkdtemp()
        mcp_server._MEMORY_STORE = memento_memory.MemoryStore(
            db_path=os.path.join(self._dir, "memory.db"),
            export_path=os.path.join(self._dir, "standalone.json"))

    def tearDown(self):
        mcp_server._MEMORY_STORE = self._orig

    def test_save_then_recall_via_handlers(self):
        out = mcp_server._memory_save({"title": "Build", "content": "use rtk mvn test",
                                       "tier": "procedural", "tags": "build,java"})
        self.assertIn("saved", out)
        self.assertIn("procedural", out)
        recall = mcp_server._memory_recall({"query": "rtk"})
        self.assertIn("Build", recall)
        self.assertIn("procedural", recall)

    def test_stats_handler(self):
        mcp_server._memory_save({"title": "a", "content": "alpha"})
        self.assertIn("1 total", mcp_server._memory_stats({}))

    def test_brief_returns_relevant_memories_and_lessons(self):
        mcp_server._memory_save({"title": "Use Flyway", "content": "add a new V__ migration script",
                                 "tier": "procedural", "tags": "db"})
        mcp_server._store().add_lesson("Gate risky releases", "ship behind a flag")
        out = mcp_server._memory_brief({"task": "add a database migration"})
        self.assertIn("pre-flight briefing", out)
        self.assertIn("Use Flyway", out)              # relevant memory surfaced
        self.assertIn("Gate risky releases", out)     # standing lesson surfaced
        self.assertIn("constraints", out)             # behavioral instruction

    def test_brief_requires_task(self):
        self.assertIn("needs a 'task'", mcp_server._memory_brief({}))


class TestTeamAuthGate(unittest.TestCase):
    """MEMENTO_AUTH gate: the namespace is token-derived, not caller-asserted.

    Keycloak itself is not needed — we patch memento_auth's token accessors so
    the test exercises the mcp_server handlers' enforcement, not the OAuth flow.
    """

    def setUp(self):
        import memento_auth
        self.auth = memento_auth
        self._orig_store = mcp_server._MEMORY_STORE
        self._dir = tempfile.mkdtemp()
        mcp_server._MEMORY_STORE = memento_memory.MemoryStore(
            db_path=os.path.join(self._dir, "memory.db"),
            export_path=os.path.join(self._dir, "standalone.json"))
        # seed one memory in each of two teams (trust-based path)
        mcp_server._memory_save({"title": "A", "content": "alpha secret",
                                 "namespace": "team-alpha"})
        mcp_server._memory_save({"title": "B", "content": "beta secret",
                                 "namespace": "team-beta"})
        self._patches = [
            mock.patch.object(self.auth, "enabled", lambda: True),
            mock.patch.object(self.auth, "teams", lambda: ["team-alpha"]),
            mock.patch.object(self.auth, "actor", lambda: "alice"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        mcp_server._MEMORY_STORE = self._orig_store

    def test_recall_is_forced_to_my_team(self):
        out = mcp_server._memory_recall({"query": "secret"})
        self.assertIn("alpha secret", out)
        self.assertNotIn("beta secret", out)  # other team is invisible

    def test_cross_team_request_is_denied(self):
        for fn in (mcp_server._memory_recall, mcp_server._memory_save,
                   mcp_server._memory_forget):
            out = fn({"query": "x", "title": "x", "content": "x",
                      "id": "x", "namespace": "team-beta"})
            self.assertIn("Not authorized for team 'team-beta'", out)

    def test_save_lands_in_my_team(self):
        out = mcp_server._memory_save({"title": "C", "content": "more alpha"})
        self.assertIn("saved", out)
        self.assertIn("more alpha", mcp_server._memory_recall({"query": "more"}))


_PG_DSN = os.environ.get("MEMENTO_TEST_PG_DSN")


@unittest.skipUnless(
    _PG_DSN and importlib.util.find_spec("psycopg"),
    "set MEMENTO_TEST_PG_DSN (+ pip install psycopg[binary]) to test the Postgres backend")
class TestPostgresBackend(unittest.TestCase):
    """Shared per-team Postgres backend — parity with the SQLite engine.

    Run against the team docker-compose:
        docker compose -f team/docker-compose.yml up -d
        MEMENTO_TEST_PG_DSN=postgresql://memento:memento@localhost:5432/memento \\
          python3 -m unittest discover -s tests
    """
    NS = "memento-test-suite"

    def setUp(self):
        import memento_memory_pg
        self.store = memento_memory_pg.MemoryStorePG(_PG_DSN)
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        for m in self.store.list(namespace=self.NS, limit=1000):
            self.store.forget(mem_id=m["id"])

    def test_save_search_and_isolation(self):
        a = self.store.save("Fix OrderService", "patch OrderService.persist() bug",
                             tier="procedural", namespace=self.NS)
        self.store.save("OrderService test", "cover OrderService.persist() cases",
                        tier="episodic", namespace=self.NS)
        hits = self.store.search("orderservice persist", namespace=self.NS, mode="hybrid")
        self.assertTrue(hits and any(h["id"] == a for h in hits))
        # namespace isolation: other namespaces don't leak in
        self.assertTrue(all(h["namespace"] == self.NS for h in hits))

    def test_graph_lessons_and_stats(self):
        self.store.save("Fix", "patch OrderService.persist()", namespace=self.NS)
        self.store.save("Test", "test OrderService.persist()", namespace=self.NS)
        self.store.learn(namespace=self.NS)
        self.assertTrue(any("OrderService" in m["content"]
                            for m in self.store.lessons()))
        self.assertTrue(self.store.graph()["entities"])
        s = self.store.stats()
        self.assertGreaterEqual(s["total"], 2)
        self.assertTrue(s["backend"].startswith("postgres"))


_HAS_ENGINE = importlib.util.find_spec("skillopt_sleep") is not None


@unittest.skipUnless(_HAS_ENGINE, "microsoft/SkillOpt (skillopt_sleep) not installed — run install.sh")
class TestSkillOptIntegration(unittest.TestCase):
    """Real end-to-end against the installed engine. Skipped unless present."""

    def test_engine_module_runs(self):
        import subprocess
        proc = subprocess.run([sys.executable, "-m", "skillopt_sleep", "--help"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)


# ── helpers ───────────────────────────────────────────────────────────────────

def _read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _find_session_jsonl(out_dir):
    proj = os.path.join(out_dir, "projects")
    for root, _dirs, files in os.walk(proj):
        for name in files:
            if name.endswith(".jsonl"):
                return _read_jsonl(os.path.join(root, name))
    raise AssertionError("no session jsonl written")


if __name__ == "__main__":
    unittest.main(verbosity=2)
