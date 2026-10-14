"""Tests for MCP server profiles, recipe dependencies, registration validation, and closure behavior."""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

from mcp_server import profiles, server
from tests._requires import requires_catalog

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestProfileRegistry(unittest.TestCase):
    def test_valid_profile_names(self):
        self.assertEqual(
            profiles.VALID_PROFILES,
            ("compose", "session", "reference", "debug", "all"),
        )
        self.assertEqual(profiles.DEFAULT_PROFILE, "compose")
        self.assertEqual(profiles.PROFILE_ENV_VAR, "SIDEKICK_MCP_PROFILE")

    def test_get_active_profile_default(self):
        old_env = os.environ.pop(profiles.PROFILE_ENV_VAR, None)
        try:
            self.assertEqual(profiles.get_active_profile_name(), "compose")
        finally:
            if old_env is not None:
                os.environ[profiles.PROFILE_ENV_VAR] = old_env

    def test_get_active_profile_env(self):
        for name in profiles.VALID_PROFILES:
            os.environ[profiles.PROFILE_ENV_VAR] = name
            try:
                self.assertEqual(profiles.get_active_profile_name(), name)
            finally:
                os.environ.pop(profiles.PROFILE_ENV_VAR, None)

    def test_invalid_profile_refusal(self):
        with self.assertRaises(ValueError) as ctx:
            profiles.validate_profile_name("unknown_profile")
        self.assertIn("Unknown MCP profile 'unknown_profile'", str(ctx.exception))
        for p in profiles.VALID_PROFILES:
            self.assertIn(p, str(ctx.exception))

    def test_invalid_profile_type(self):
        with self.assertRaises(ValueError):
            profiles.validate_profile_name(None)  # type: ignore

    def test_profile_tool_sets_uniqueness_and_subsets(self):
        all_tools = profiles.get_profile_tools("all")
        self.assertEqual(len(all_tools), 77)
        self.assertEqual(all_tools, frozenset(profiles.ALL_TOOLS))

        seen_sets = set()
        for p in profiles.VALID_PROFILES:
            tools = profiles.get_profile_tools(p)
            self.assertTrue(len(tools) > 0, f"{p} tool set is empty")
            self.assertTrue(tools.issubset(all_tools), f"{p} contains tools not in 'all'")
            seen_sets.add(tools)
            if p != "all":
                self.assertLess(
                    len(tools),
                    len(all_tools),
                    f"{p} must be strictly smaller than 'all'",
                )
                self.assertLessEqual(
                    len(tools),
                    63,
                    f"{p} ({len(tools)} tools) must be materially smaller than 'all' (77 tools)",
                )
        # All profile tool sets must be distinct
        self.assertEqual(len(seen_sets), len(profiles.VALID_PROFILES))

    def test_compose_core_loop_coverage(self):
        compose_tools = profiles.get_profile_tools("compose")

        # Check category by category
        for cat_name, cat_tools in profiles.COMPOSE_CORE_LOOP_CATEGORIES.items():
            for tool_name in cat_tools:
                self.assertIn(
                    tool_name,
                    compose_tools,
                    f"compose profile missing core loop tool '{tool_name}' in category '{cat_name}'",
                )

        # Explicitly assert required individual tools from the brief + find_audio_refs
        required_tools = [
            # hear/capture
            "hear_the_room", "inventory_session_audio", "capture_loop",
            # inventory/listen
            "listen_to_takes", "read_clip", "read_current_clip", "get_style", "find_phrases",
            # generate/write
            "suggest_structure", "arrange_song", "answer_my_loop", "develop_my_loop",
            "lay_the_song", "three_endings", "export_midi", "send_to_ableton",
            "write_midi_clip", "surgery_clip",
            "create_idea_pack_from_clip", "create_idea_pack_from_live_clip",
            "prepare_idea_pack_audition_lanes", "audition_in_lanes",
            # judge/promote/close
            "score_phrase", "rate_candidates", "judge_answer", "rate", "promote_lane",
            "close_session", "session_report",
            # reference steering in answer_my_loop
            "find_audio_refs",
            # reference jam application tools
            "apply_audio_ref",
            "beat_from_ref",
        ]
        for tool_name in required_tools:
            self.assertIn(tool_name, compose_tools)

    def test_membership_narrow_resolution(self):
        # compose has find_audio_refs
        self.assertIn("find_audio_refs", profiles.get_profile_tools("compose"))
        # session and debug do NOT have get_gold_phrases or whats_missing
        session_tools = profiles.get_profile_tools("session")
        self.assertNotIn("get_gold_phrases", session_tools)
        self.assertNotIn("whats_missing", session_tools)
        self.assertNotIn("bury_or_marry", session_tools)

        debug_tools = profiles.get_profile_tools("debug")
        self.assertNotIn("get_gold_phrases", debug_tools)
        self.assertNotIn("whats_missing", debug_tools)


class TestServerRegistrationValidation(unittest.TestCase):
    def test_server_registration_exact_match(self):
        # Matching exact ALL_TOOLS must pass without error
        profiles.validate_registered_tools(profiles.ALL_TOOLS)

    def test_unclassified_dummy_tool_fails_fast(self):
        extra_registered = list(profiles.ALL_TOOLS) + ["dummy_unclassified_tool"]
        with self.assertRaises(ValueError) as ctx:
            profiles.validate_registered_tools(extra_registered)
        self.assertIn("dummy_unclassified_tool", str(ctx.exception))
        self.assertIn("extra/unclassified", str(ctx.exception))

        # Test via mock FastMCP server instance
        mock_server = MagicMock()
        mock_tool = MagicMock()
        mock_tool.name = "dummy_unclassified_tool"
        mock_server._tool_manager.list_tools.return_value = [
            MagicMock(name=n) for n in profiles.ALL_TOOLS
        ] + [mock_tool]
        # Fix mock tool names
        for idx, n in enumerate(profiles.ALL_TOOLS):
            mock_server._tool_manager.list_tools.return_value[idx].name = n

        with self.assertRaises(ValueError) as ctx:
            profiles.validate_server_registration(mock_server)
        self.assertIn("dummy_unclassified_tool", str(ctx.exception))

    def test_declared_tool_missing_from_server_fails_fast(self):
        missing_registered = [t for t in profiles.ALL_TOOLS if t != "get_style"]
        with self.assertRaises(ValueError) as ctx:
            profiles.validate_registered_tools(missing_registered)
        self.assertIn("get_style", str(ctx.exception))
        self.assertIn("missing from server registration", str(ctx.exception))

        # Test via mock FastMCP server instance
        mock_server = MagicMock()
        mock_server._tool_manager.list_tools.return_value = [
            MagicMock(name=n) for n in missing_registered
        ]
        for idx, n in enumerate(missing_registered):
            mock_server._tool_manager.list_tools.return_value[idx].name = n

        with self.assertRaises(ValueError) as ctx:
            profiles.validate_server_registration(mock_server)
        self.assertIn("get_style", str(ctx.exception))

    def test_duplicate_tool_names_in_registered_tools_fails_fast(self):
        duplicate_sequence = list(profiles.ALL_TOOLS) + [profiles.ALL_TOOLS[0]]
        with self.assertRaises(ValueError) as ctx:
            profiles.validate_registered_tools(duplicate_sequence)
        self.assertIn("Duplicate tools registered on server", str(ctx.exception))
        self.assertIn(profiles.ALL_TOOLS[0], str(ctx.exception))

    def test_duplicate_tool_names_in_fake_manager_server_fails_fast(self):
        class FakeTool:
            def __init__(self, name: str):
                self.name = name

        class FakeToolManager:
            def __init__(self, tool_names: list[str]):
                self._tool_names = tool_names

            def list_tools(self):
                return [FakeTool(name) for name in self._tool_names]

        class FakeServer:
            def __init__(self, tool_names: list[str]):
                self._tool_manager = FakeToolManager(tool_names)

        duplicate_names = list(profiles.ALL_TOOLS) + [profiles.ALL_TOOLS[0]]
        fake_server = FakeServer(duplicate_names)

        with self.assertRaises(ValueError) as ctx:
            profiles.validate_server_registration(fake_server)
        self.assertIn("Duplicate tools registered on server", str(ctx.exception))
        self.assertIn(profiles.ALL_TOOLS[0], str(ctx.exception))


class TestRecipeAndInstructionClosure(unittest.TestCase):
    def test_all_profiles_pass_validation(self):
        # Must not raise
        docstrings = {
            name: getattr(server, name).__doc__ or ""
            for name in profiles.ALL_TOOLS
        }
        profiles.validate_all_profiles(tool_descriptions=docstrings)

    def test_closure_per_profile(self):
        docstrings = {
            name: getattr(server, name).__doc__ or ""
            for name in profiles.ALL_TOOLS
        }
        for p in profiles.VALID_PROFILES:
            with self.subTest(profile=p):
                violations = profiles.validate_profile_closure(p, tool_descriptions=docstrings)
                self.assertEqual(violations, [], f"Closure violations in profile '{p}'")

    def test_recipe_dependencies_pinned(self):
        # 1. listen_to_takes -> inventory_session_audio
        for p in profiles.VALID_PROFILES:
            tools = profiles.get_profile_tools(p)
            if "listen_to_takes" in tools:
                self.assertIn("inventory_session_audio", tools)

        # 2. ears-aware composition/retrieval -> hear_the_room
        ears_tools = [
            "find_phrases", "answer_my_loop", "develop_my_loop",
            "grow_my_loop", "suggest_growth", "audition_leads",
        ]
        for p in profiles.VALID_PROFILES:
            tools = profiles.get_profile_tools(p)
            if any(t in tools for t in ears_tools):
                self.assertIn("hear_the_room", tools)

        # 3. prepare_idea_pack_audition_lanes -> idea-pack creation + judgment
        for p in profiles.VALID_PROFILES:
            tools = profiles.get_profile_tools(p)
            if "prepare_idea_pack_audition_lanes" in tools:
                has_creation = bool({"create_idea_pack_from_clip", "create_idea_pack_from_live_clip"} & tools)
                has_judgment = bool({"rate_candidates", "judge_answer", "rate"} & tools)
                self.assertTrue(has_creation, f"{p} has lanes but no idea-pack creation tool")
                self.assertTrue(has_judgment, f"{p} has lanes but no judgment tool")

        # 4. ref writes -> hearing + judgment/closeout
        for p in profiles.VALID_PROFILES:
            tools = profiles.get_profile_tools(p)
            if "apply_audio_ref" in tools or "beat_from_ref" in tools:
                self.assertIn("hear_the_room", tools)
                has_judgment = bool({"tag_audio_ref", "judge_answer", "rate"} & tools)
                has_closeout = bool({"close_session", "session_report"} & tools)
                self.assertTrue(has_judgment, f"{p} has ref write but no judgment tool")
                self.assertTrue(has_closeout, f"{p} has ref write but no closeout tool")

    def test_instruction_closure_extracts_and_verifies(self):
        for p in profiles.VALID_PROFILES:
            violations = profiles.check_instruction_closure(p)
            self.assertEqual(violations, [])

    def test_tool_description_closure_positive_all_profiles(self):
        docstrings = {
            name: getattr(server, name).__doc__ or ""
            for name in profiles.ALL_TOOLS
        }
        for p in profiles.VALID_PROFILES:
            with self.subTest(profile=p):
                violations = profiles.check_tool_description_closure(p, docstrings)
                self.assertEqual(violations, [], f"Docstring closure violations in '{p}': {violations}")

    def test_negative_closure_checks(self):
        # A hypothetical profile that breaks recipe closure
        broken_tool_registry = dict(profiles.PROFILE_TOOL_REGISTRY)
        # listen_to_takes without inventory_session_audio
        broken_tool_registry["compose"] = tuple(
            t for t in profiles.COMPOSE_TOOLS if t != "inventory_session_audio"
        )
        with patch.dict(profiles.PROFILE_TOOL_REGISTRY, broken_tool_registry):
            violations = profiles.check_recipe_closure("compose")
            self.assertTrue(any("listen_needs_inventory" in v for v in violations))

        # Instructions mentioning an absent tool
        broken_instructions = dict(profiles.PROFILE_INSTRUCTIONS)
        broken_instructions["session"] = "Use analyze_audio_ref and export_midi."
        with patch.dict(profiles.PROFILE_INSTRUCTIONS, broken_instructions):
            violations = profiles.check_instruction_closure("session")
            self.assertTrue(any("analyze_audio_ref" in v for v in violations))

        # Tool docstring mentioning an absent tool
        mock_docs = {
            name: getattr(server, name).__doc__ or ""
            for name in profiles.ALL_TOOLS
        }
        mock_docs["read_clip"] = "Inspects a clip; call analyze_mix_pass to mix."
        violations = profiles.check_tool_description_closure("session", mock_docs)
        self.assertTrue(any("analyze_mix_pass" in v for v in violations))


@requires_catalog
class TestSubprocessProfileImports(unittest.TestCase):
    def _run_subprocess(self, env_profile: str | None) -> subprocess.CompletedProcess:
        snippet = """
import json, os, sys
sys.path.insert(0, %r)
from mcp_server.server import mcp
import mcp_server.server as server

tools = [t.name for t in mcp._tool_manager.list_tools()]
print('REGISTERED_COUNT:', len(tools))
print('REGISTERED_TOOLS:', json.dumps(sorted(tools)))
print('INSTRUCTIONS:', mcp._mcp_server.instructions or '')
# Check direct callable functions
assert callable(server.get_style)
assert callable(server.export_midi)
assert callable(server.analyze_mix_pass)
print('DIRECT_CALLS_OK')
""" % ROOT
        env = dict(os.environ)
        if env_profile is None:
            env.pop("SIDEKICK_MCP_PROFILE", None)
        else:
            env["SIDEKICK_MCP_PROFILE"] = env_profile

        return subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=env,
            timeout=60,
        )

    def test_default_profile_without_env(self):
        proc = self._run_subprocess(None)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("DIRECT_CALLS_OK", proc.stdout)

        compose_tools = sorted(profiles.get_profile_tools("compose"))
        self.assertIn(f"REGISTERED_COUNT: {len(compose_tools)}", proc.stdout)
        self.assertIn(f"REGISTERED_TOOLS: {json.dumps(compose_tools)}", proc.stdout)

    def test_all_five_profiles_in_subprocesses(self):
        for p in profiles.VALID_PROFILES:
            with self.subTest(profile=p):
                proc = self._run_subprocess(p)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("DIRECT_CALLS_OK", proc.stdout)

                expected_tools = sorted(profiles.get_profile_tools(p))
                self.assertIn(f"REGISTERED_COUNT: {len(expected_tools)}", proc.stdout)
                self.assertIn(f"REGISTERED_TOOLS: {json.dumps(expected_tools)}", proc.stdout)
                self.assertIn(profiles.get_profile_instructions(p), proc.stdout)

    def test_invalid_profile_subprocess_fails_fast(self):
        proc = self._run_subprocess("invalid_profile_name")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Unknown MCP profile 'invalid_profile_name'", proc.stderr)
        for p in profiles.VALID_PROFILES:
            self.assertIn(p, proc.stderr)

    def test_pruned_functions_remain_callable_in_python(self):
        snippet = """
import os, sys
sys.path.insert(0, %r)
import mcp_server.server as server

# export_midi is not registered in session profile MCP tool list
assert "export_midi" not in [t.name for t in server.mcp._tool_manager.list_tools()]
# But direct Python call works
res = server.get_style()
assert isinstance(res, dict)
print('DIRECT_CALL_SUCCESS')
""" % ROOT
        env = dict(os.environ)
        env["SIDEKICK_MCP_PROFILE"] = "session"
        proc = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=env,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("DIRECT_CALL_SUCCESS", proc.stdout)


class TestPublicSchemasAndSignatures(unittest.TestCase):
    def test_all_60_tool_signatures_accessible(self):
        for name in profiles.ALL_TOOLS:
            fn = getattr(server, name, None)
            self.assertIsNotNone(fn, f"Function {name} missing from server module")
            self.assertTrue(callable(fn), f"{name} is not callable")
            sig = inspect.signature(fn)
            self.assertIsInstance(sig, inspect.Signature)


class TestMcpJsonConfig(unittest.TestCase):
    def test_mcp_json_contents(self):
        mcp_json_path = os.path.join(ROOT, ".mcp.example.json")
        with open(mcp_json_path, encoding="utf-8") as f:
            config = json.load(f)

        servers = config.get("mcpServers", {})
        self.assertIn("style-sidekick", servers)
        self.assertIn("producer-pal", servers)

        # style-sidekick must launch with compose profile
        sidekick = servers["style-sidekick"]
        self.assertIn("env", sidekick)
        self.assertEqual(
            sidekick["env"].get("SIDEKICK_MCP_PROFILE"),
            "compose",
        )

        # Producer Pal pin must remain exactly producer-pal@2.3.0 with json and midi-json args
        # (moved from 2.2.0 on 2026-09-30). His Sets carry the 2.3.0 device; the code speaks
        # 2.4.0 too, and the pin moves to 2.4.0 once the Sets carry the 2.4 device (README).
        pp = servers["producer-pal"]
        self.assertEqual(pp.get("command"), "npx")
        self.assertEqual(
            pp.get("args"),
            ["-y", "producer-pal@2.3.0", "-f", "json", "-n", "midi-json"],
        )


if __name__ == "__main__":
    unittest.main()
