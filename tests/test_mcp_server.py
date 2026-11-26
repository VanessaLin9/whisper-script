"""Meeting Desk MCP adapter tests (SDK tests are optional)."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.desktop.service import ACTION_REGISTRY
from src.mcp_server.registry import (
    ALLOWED_ACTIONS, action_tool_specs, schema_for_action, validate_arguments,
)


class MCPRegistryTests(unittest.TestCase):
    def test_allowlist_and_registry_metadata(self):
        specs = action_tool_specs()
        self.assertEqual([spec["name"] for spec in specs], [name for name in ALLOWED_ACTIONS])
        registry = {item["name"]: item for item in ACTION_REGISTRY}
        for spec in specs:
            action = registry[spec["name"]]
            self.assertEqual(spec["name"], action["name"])
            self.assertTrue(spec["description"].startswith(action["summary"]))
            self.assertEqual(spec["inputSchema"], schema_for_action(action))
        for name in {"review", "settings", "rename", "save_edit", "import", "process", "inspect"}:
            self.assertNotIn(name, [spec["name"] for spec in specs])

    def test_schema_types_defaults_and_annotations(self):
        specs = {spec["name"]: spec for spec in action_tool_specs()}
        jobs = specs["list_jobs"]["inputSchema"]
        self.assertEqual(jobs["properties"]["include_archive"], {"type": "boolean", "default": False})
        self.assertEqual(jobs["properties"]["status"]["anyOf"], [
            {"type": "string"}, {"type": "array", "items": {"type": "string"}}
        ])
        self.assertEqual(jobs["required"], [])
        self.assertTrue(specs["list"]["annotations"]["readOnlyHint"])
        self.assertFalse(specs["handoff"]["annotations"]["readOnlyHint"])
        self.assertFalse(specs["handoff"]["annotations"]["destructiveHint"])
        self.assertTrue(specs["import_cleaned"]["annotations"]["destructiveHint"])
        self.assertIn("唯讀", specs["help"]["description"])
        self.assertIn("寫入", specs["handoff"]["description"])

    def test_new_registry_action_is_not_implicitly_opened(self):
        extra = {"name": "future", "summary": "未來 action", "required": {}, "optional": {},
                 "returns": [], "read_only": True, "long_running": False,
                 "cancellable": False, "handler": "future"}
        self.assertNotIn("future", [item["name"] for item in action_tool_specs([*ACTION_REGISTRY, extra])])

    def test_schema_validation_rejects_unknown_missing_and_wrong_types(self):
        schema = {spec["name"]: spec for spec in action_tool_specs()}["list_jobs"]["inputSchema"]
        with self.assertRaises(ValueError):
            validate_arguments(schema, {"bogus": True})
        with self.assertRaises(ValueError):
            validate_arguments(schema, {"include_archive": "yes"})

        required = {spec["name"]: spec for spec in action_tool_specs()}["handoff"]["inputSchema"]
        with self.assertRaises(ValueError):
            validate_arguments(required, {"folder": "/tmp/meeting"})
        with self.assertRaises(ValueError):
            validate_arguments(required, {"folder": 123, "profile": "test"})
        with self.assertRaises(ValueError):
            validate_arguments(required, ["/tmp/meeting", "test"])

    def test_schema_validation_rejects_action_argument(self):
        schema = {spec["name"]: spec for spec in action_tool_specs()}["help"]["inputSchema"]
        with self.assertRaises(ValueError):
            validate_arguments(schema, {"action": "settings"})


class MCPSDKTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            from mcp.client import ClientSession
            from mcp.client._memory import InMemoryTransport
        except ModuleNotFoundError:
            self.skipTest("未安裝 mcp SDK")
        self.ClientSession = ClientSession
        self.InMemoryTransport = InMemoryTransport
        self.temp = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup)
        self.root = Path(self.temp.name) / "meetings"
        self.config = Path(self.temp.name) / "config"
        self.notes = Path(self.temp.name) / "notes"
        self.root.mkdir()
        self.config.mkdir()
        self.notes.mkdir()
        (self.notes / "test.md").write_text("---\nkey: test\ntitle: 測試\n---\n詞彙", encoding="utf-8")
        self.env = {"MEETING_RECORDS_DIR": str(self.root), "WHISPER_SCRIPT_CONFIG_DIR": str(self.config),
                    "PROMPT_NOTES_DIR": str(self.notes)}
        self.old_env = {key: os.environ.get(key) for key in self.env}
        os.environ.update(self.env)
        (self.config / "desktop_settings.json").write_text(
            json.dumps({"output_root": str(self.root), "whisper_root": str(Path(self.temp.name) / "whisper")}),
            encoding="utf-8",
        )

    async def _cleanup(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    async def _session(self):
        from src.mcp_server.server import create_server
        transport = self.InMemoryTransport(create_server())
        streams = await transport.__aenter__()
        session = self.ClientSession(*streams)
        await session.__aenter__()
        await session.initialize()
        return transport, session

    async def test_initialize_list_and_calls(self):
        transport, session = await self._session()
        try:
            listed = await session.list_tools()
            names = [tool.name for tool in listed.tools]
            self.assertEqual(names, list(ALLOWED_ACTIONS))
            help_result = await session.call_tool("help", {})
            self.assertFalse(help_result.is_error)
            self.assertIn('"actions"', help_result.content[0].text)
            jobs = await session.call_tool("list_jobs", {})
            self.assertFalse(jobs.is_error)
            self.assertEqual(jobs.structured_content["jobs"], [])
        finally:
            await session.__aexit__(None, None, None)
            await transport.__aexit__(None, None, None)

    async def test_handoff_round_trip_in_temp_root(self):
        folder = self.root / "2026-11-26_0900_demo"
        folder.mkdir()
        raw = folder / "demo_transcription.txt"
        prepared = folder / "demo_transcription_prepared.txt"
        srt = folder / "demo_transcription.srt"
        raw.write_text("這是一段會議逐字稿。", encoding="utf-8")
        prepared.write_text(raw.read_text(encoding="utf-8"), encoding="utf-8")
        digest = hashlib.sha256(raw.read_bytes()).hexdigest()
        (prepared.with_suffix(".txt.manifest.json")).write_text(json.dumps({
            "source_sha256": digest,
            "output_sha256": hashlib.sha256(prepared.read_bytes()).hexdigest(),
        }), encoding="utf-8")
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n這是一段會議逐字稿。\n", encoding="utf-8")
        (folder / "source_meta.json").write_text(json.dumps({"safe_stem": "demo"}), encoding="utf-8")
        (folder / "desktop_state.json").write_text(json.dumps({"title": "示範會議", "attempts": []}), encoding="utf-8")
        transport, session = await self._session()
        try:
            result = await session.call_tool("handoff", {"folder": str(folder), "profile": "test"})
            self.assertFalse(result.is_error, result.content[0].text)
            self.assertTrue(Path(result.structured_content["path"]).is_file())
        finally:
            await session.__aexit__(None, None, None)
            await transport.__aexit__(None, None, None)

    async def test_allowlist_and_arguments_are_enforced_before_dispatch(self):
        before = (self.config / "desktop_settings.json").read_text(encoding="utf-8")
        transport, session = await self._session()
        try:
            rejected = ["settings", "review", "rename", "save_edit", "import", "process", "inspect"]
            with patch("src.mcp_server.server.dispatch") as dispatch_mock, \
                    patch("src.mcp_server.server.DesktopService") as service_mock:
                for name in rejected:
                    result = await session.call_tool(name, {"settings": {}} if name == "settings" else {})
                    self.assertTrue(result.is_error, name)
                self.assertFalse(dispatch_mock.called)
                self.assertFalse(service_mock.called)
            self.assertEqual(before, (self.config / "desktop_settings.json").read_text(encoding="utf-8"))

            with patch("src.mcp_server.server.dispatch") as dispatch_mock:
                result = await session.call_tool("help", {"action": "settings", "settings": {}})
                self.assertTrue(result.is_error)
                dispatch_mock.assert_not_called()
                result = await session.call_tool("list_jobs", {"bogus": True})
                self.assertTrue(result.is_error)
                dispatch_mock.assert_not_called()
        finally:
            await session.__aexit__(None, None, None)
            await transport.__aexit__(None, None, None)


class MCPMissingSDKMessageTests(unittest.TestCase):
    def test_cli_has_clear_message_without_sdk(self):
        # This test is only meaningful under a Python interpreter without mcp.
        if any(Path(path, "mcp").is_dir() for path in sys.path if path):
            self.skipTest("目前 Python 環境有 mcp")
        result = subprocess.run([sys.executable, "-m", "src.mcp_server"], input="", text=True,
                                capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mcp SDK", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
