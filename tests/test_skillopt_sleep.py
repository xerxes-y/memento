#!/usr/bin/env python3
"""Test suite for the SkillOpt-Sleep Devin plugin.

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

import importlib.util
import json
import os
import sys
import tempfile
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
            with mock.patch.dict(hw.os.environ, {"SKILLOPT_DEVIN_WORKSPACES": joined}):
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

    def test_tools_list_has_five_tools(self):
        resp = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = resp["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertEqual(names, {"sleep_status", "sleep_dry_run", "sleep_run",
                                 "sleep_adopt", "sleep_harvest"})
        for t in tools:
            self.assertIn("inputSchema", t)

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
        expected = {"sleep_status": "status", "sleep_dry_run": "dry-run",
                    "sleep_run": "run", "sleep_adopt": "adopt", "sleep_harvest": "harvest"}
        for name, action in expected.items():
            self.assertEqual(mcp_server._BY_NAME[name]["action"], action)


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


def _find_session_jsonl(out_dir):
    proj = os.path.join(out_dir, "projects")
    for root, _dirs, files in os.walk(proj):
        for name in files:
            if name.endswith(".jsonl"):
                return _read_jsonl(os.path.join(root, name))
    raise AssertionError("no session jsonl written")


if __name__ == "__main__":
    unittest.main(verbosity=2)
