import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "organize_recording.py"
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

    def test_copies_and_prefixes_unlabelled_recording(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "Voice Memo.m4a"
            source.write_bytes(b"audio")
            detected = MODULE.RecordingTime(datetime(2026, 7, 17, 15, 0), "test", False)
            with patch.object(MODULE, "detect_recording_time", return_value=detected):
                result = MODULE.prepare_recording(source, root / "records", assume_yes=True)

            copied = Path(result["audio_file"])
            self.assertEqual(copied.parent.name, "2026-07-17_1500")
            self.assertEqual(copied.name, "2026-07-17_1500_Voice Memo.m4a")
            self.assertTrue(source.exists())
            self.assertEqual(copied.read_bytes(), b"audio")

    def test_standard_name_is_not_prefixed_twice(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "2026-07-17_1500_demo.m4a"
            source.write_bytes(b"audio")
            result = MODULE.prepare_recording(source, root / "records", assume_yes=True)
            self.assertEqual(Path(result["audio_file"]).name, source.name)


if __name__ == "__main__":
    unittest.main()
