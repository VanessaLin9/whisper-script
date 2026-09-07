"""Desktop lifecycle tests: real files + fake local transcription binaries."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.common.cancellation import CancellationController, OperationCancelled
from src.desktop.service import DesktopService, digest, meeting_lock, read_json
from src.output_manager import SourceKind

REPO = Path(__file__).resolve().parents[1]


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home / "repo"
        self.repo.mkdir()
        self.root = self.home / "meetings"
        self.whisper = self.home / "whisper"
        (self.whisper / "models").mkdir(parents=True)
        (self.whisper / "models/ggml-medium.bin").write_bytes(b"model")
        (self.whisper / "build/bin").mkdir(parents=True)
        (self.whisper / "build/bin/whisper-cli").symlink_to(REPO / "tests/fake_bin/whisper-cli")
        self.service = DesktopService(self.repo, {"output_root": str(self.root), "whisper_root": str(self.whisper)})
        self.audio = self.home / "Voice Memo.m4a"
        self.audio.write_bytes(b"original audio")
        self.env = patch.dict(os.environ, {"PATH": str(REPO / "tests/fake_bin") + os.pathsep + os.environ["PATH"]})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.notes = self.home / "notes"
        self.notes.mkdir()
        (self.notes / "test.md").write_text("---\nkey: test\ntitle: 測試提示詞\n---\nprivate vocabulary", encoding="utf-8")
        self.profiles = patch.dict(os.environ, {"PROMPT_NOTES_DIR": str(self.notes)})
        self.profiles.start()
        self.addCleanup(self.profiles.stop)

    def add(self):
        result = self.service.import_audio(str(self.audio), "中文 AI 會議", "2026-09-07 09:30")
        return Path(result["meeting"]["id"])

    def prepared(self):
        folder = self.add()
        self.service.process(str(folder))
        return folder

    def test_medium_default_and_settings_persist(self):
        self.assertEqual(self.service.settings["model"], "medium")
        self.assertEqual(self.service.settings["language"], "zh")
        self.service.save_settings(self.service.settings)
        self.assertEqual(DesktopService(self.repo).settings, self.service.settings)
        self.assertTrue(all(row["ok"] for row in self.service.environment()["checks"]))
        with self.assertRaises(ValueError):
            self.service.save_settings({**self.service.settings, "model": "medium.en"})

    def test_import_retains_copy_and_never_overwrites(self):
        folder = self.add()
        meta = read_json(folder / "source_meta.json")
        self.assertEqual(meta["source_kind"], SourceKind.MANAGED_IMPORT.value)
        self.assertTrue(meta["retained_in_workspace"])
        saved = Path(meta["audio_path_for_core"])
        self.assertEqual(saved.read_bytes(), self.audio.read_bytes())
        with self.assertRaises(Exception):
            self.add()
        self.assertEqual(saved.read_bytes(), b"original audio")
        self.assertEqual(self.audio.read_bytes(), b"original audio")

    def test_import_uses_confirmed_meeting_time(self):
        folder = self.add()
        self.assertTrue(folder.name.startswith("2026-09-07_0930_"))
        self.assertEqual(self.service.row(folder)["date"], "2026-09-07 09:30")
        self.assertIn("+08:00", read_json(folder / "source_meta.json")["meeting_time"])

    def test_transcription_resume_does_not_rerun_whisper(self):
        folder = self.prepared()
        paths = self.service.paths(folder)
        original = {key: digest(path) for key, path in paths.items() if path.exists()}
        with patch.dict(os.environ, {"FAIL_WHISPER": "1"}):
            result = self.service.process(str(folder))
        self.assertTrue(result["meeting"]["prepared"])
        self.assertEqual(original, {key: digest(path) for key, path in paths.items() if path.exists()})
        self.assertEqual(len(read_json(folder / "desktop_state.json")["attempts"]), 2)

    def test_failed_transcription_retries_without_reimport(self):
        folder = self.add()
        with patch.dict(os.environ, {"FAIL_WHISPER": "1"}):
            with self.assertRaises(Exception): self.service.process(str(folder))
        self.assertIn("失敗", self.service.row(folder)["status"])
        self.assertTrue(Path(read_json(folder / "source_meta.json")["audio_path_for_core"]).exists())
        self.assertTrue(self.service.process(str(folder))["meeting"]["prepared"])

    def test_cancellation_keeps_audio_and_can_resume(self):
        folder = self.add()
        controller = CancellationController()
        controller.cancel()
        with self.assertRaises(OperationCancelled): self.service.process(str(folder), controller.token)
        self.assertEqual(read_json(folder / "desktop_state.json")["status"], "cancelled")
        self.assertTrue(self.audio.exists())
        self.assertTrue(self.service.process(str(folder))["meeting"]["prepared"])

    def test_changed_prepared_or_raw_is_blocked(self):
        folder = self.prepared()
        self.service.paths(folder)["raw"].write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "不一致"):
            self.service.handoff(str(folder), "test")

    def test_handoff_import_review_and_summary(self):
        folder = self.prepared()
        paths = self.service.paths(folder)
        packet = self.service.handoff(str(folder), "test")
        self.assertIn("繁體中文", packet["text"])
        self.assertIn("private vocabulary", packet["text"])
        self.assertTrue(Path(packet["path"]).is_file())
        cleaned = self.home / "result.txt"
        cleaned.write_text(paths["prepared"].read_text(encoding="utf-8") + "。", encoding="utf-8")
        result = self.service.import_cleaned(str(folder), str(cleaned))
        self.assertFalse(result["meeting"]["reviewed"])
        with self.assertRaises(ValueError): self.service.handoff(str(folder), "test", summary=True)
        result = self.service.review(str(folder))
        self.assertTrue(result["meeting"]["reviewed"])
        summary = self.service.handoff(str(folder), "test", summary=True)
        self.assertIn("不授權寫入 Notion", summary["text"])
        paths["cleaned"].write_text("externally changed", encoding="utf-8")
        self.assertFalse(self.service.row(folder)["reviewed"])
        with self.assertRaises(ValueError): self.service.handoff(str(folder), "test", summary=True)

    def test_short_cleaned_rejected_without_writing(self):
        folder = self.prepared()
        self.service.handoff(str(folder), "test")
        cleaned = self.home / "summary.txt"
        cleaned.write_text("短摘要", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "20%"):
            self.service.import_cleaned(str(folder), str(cleaned))
        self.assertFalse(self.service.paths(folder)["cleaned"].exists())
        self.assertEqual(cleaned.read_text(encoding="utf-8"), "短摘要")

    def test_revalidate_existing_legacy_cleaned_without_overwrite(self):
        folder = self.prepared()
        paths = self.service.paths(folder)
        paths["cleaned"].write_text(paths["prepared"].read_text(encoding="utf-8"), encoding="utf-8")
        before = digest(paths["cleaned"])
        self.assertFalse(self.service.row(folder)["reviewed"])
        self.service.handoff(str(folder), "test")
        self.service.import_cleaned(str(folder), str(paths["cleaned"]))
        self.service.review(str(folder))
        self.assertEqual(digest(paths["cleaned"]), before)
        self.assertTrue(self.service.row(folder)["reviewed"])

    def test_invalid_profile_and_outside_folder_blocked(self):
        folder = self.prepared()
        with self.assertRaises(ValueError): self.service.handoff(str(folder), "missing")
        with self.assertRaises(ValueError): self.service.folder(str(self.home))
        with self.assertRaises(ValueError): self.service.handoff(str(folder), "new")

    def test_lock_prevents_second_writer(self):
        folder = self.add()
        with meeting_lock(folder):
            with self.assertRaises(ValueError): self.service.process(str(folder))

    def test_bad_workspace_does_not_hide_other_meetings(self):
        good = self.add()
        bad = self.root / "2026-09-07_1000_broken"
        bad.mkdir()
        (bad / "source_meta.json").write_text("broken", encoding="utf-8")
        listing = self.service.meetings()
        self.assertEqual(len(listing["warnings"]), 1)
        self.assertEqual(listing["meetings"][0]["id"], str(good))

    def test_missing_model_preserves_import_for_later(self):
        folder = self.add()
        (self.whisper / "models/ggml-medium.bin").unlink()
        with self.assertRaisesRegex(ValueError, "模型"):
            self.service.process(str(folder))
        self.assertEqual(len(self.service.meetings()["meetings"]), 1)
        self.assertTrue(Path(read_json(folder / "source_meta.json")["audio_path_for_core"]).exists())


if __name__ == "__main__":
    unittest.main()
