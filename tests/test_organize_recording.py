import importlib.util
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCRIPT = ROOT / "scripts" / "organize_recording.py"
SPEC = importlib.util.spec_from_file_location("organize_recording", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class OrganizeRecordingTests(unittest.TestCase):
    def test_parses_standard_prefix(self):
        result = MODULE.parse_standard_prefix("2026-07-17_1500_meeting")
        self.assertEqual(result.value, datetime(2026, 7, 17, 15, 0))
        self.assertEqual(result.source, "filename")

    def test_invalid_prefix_is_not_accepted(self):
        self.assertIsNone(MODULE.parse_standard_prefix("2026-17-99_9999_meeting"))

    def test_local_source_is_referenced_not_copied(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "Voice Memo.m4a"
            source.write_bytes(b"audio")
            detected = MODULE.RecordingTime(datetime(2026, 7, 17, 15, 0), "test", False)
            with patch.object(MODULE, "detect_recording_time", return_value=detected):
                result = MODULE.prepare_recording(source, root / "records", assume_yes=True)

            self.assertEqual(Path(result["audio_file"]).resolve(), source.resolve())
            self.assertEqual(result["source_kind"], "local_reference")
            meeting_dir = Path(result["meeting_dir"])
            self.assertEqual(meeting_dir.name, "2026-07-17_1500_Voice_Memo")
            self.assertTrue((meeting_dir / "source_meta.json").is_file())
            self.assertTrue(source.exists())
            self.assertEqual(source.read_bytes(), b"audio")
            self.assertFalse(any(meeting_dir.glob("*.m4a")))

    def test_standard_name_still_uses_safe_stem_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "2026-07-17_1500_demo.m4a"
            source.write_bytes(b"audio")
            result = MODULE.prepare_recording(source, root / "records", assume_yes=True)
            self.assertEqual(Path(result["audio_file"]).resolve(), source.resolve())
            self.assertEqual(result["stem"], "2026-07-17_1500_demo")
            self.assertEqual(
                Path(result["meeting_dir"]).name,
                "2026-07-17_1500_2026-07-17_1500_demo",
            )

    def test_time_prompt_keeps_stdout_json_clean(self):
        """Shell captures organizer stdout as JSON; prompts must stay on stderr."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "市民大道八段544–590號.m4a"
            source.write_bytes(b"audio")
            detected = MODULE.RecordingTime(
                datetime(2026, 7, 18, 22, 38), "audio metadata", False
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(MODULE, "detect_recording_time", return_value=detected):
                with patch.object(MODULE.sys, "stdin", io.StringIO("\n")):
                    with patch.object(MODULE.sys, "stdout", stdout):
                        with patch.object(MODULE.sys, "stderr", stderr):
                            result = MODULE.prepare_recording(
                                source, root / "records", assume_yes=False
                            )
                            MODULE.sys.stdout.write(
                                json.dumps(result, ensure_ascii=False) + "\n"
                            )

            payload = stdout.getvalue()
            parsed = json.loads(payload)
            self.assertIn("meeting_dir", parsed)
            self.assertEqual(Path(parsed["audio_file"]).resolve(), source.resolve())
            self.assertIn("Press Enter to accept", stderr.getvalue())
            self.assertNotIn("Press Enter", payload)


if __name__ == "__main__":
    unittest.main()
