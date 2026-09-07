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

    def edit(self, folder, kind, text):
        preview = self.service.preview(str(folder), kind)
        return self.service.save_edit(str(folder), kind, preview["token"], text=text)

    def timeline(self, folder):
        path = self.service.paths(folder)["srt"]
        original = "7\r\n00:00:01,250 --> 00:00:04,500\r\n第一段 API 名稱\r\n\r\n9\r\n00:00:05,000 --> 00:00:08,990\r\n第二段\r\n多行字幕\r\n"
        path.write_bytes(original.encode("utf-8"))
        return path, path.read_bytes()

    def test_rename_updates_display_and_handoff_without_renaming_files(self):
        folder = self.prepared()
        original_files = sorted(p.name for p in folder.iterdir())
        original_hashes = {k: digest(p) for k, p in self.service.paths(folder).items() if p.is_file()}
        self.service.rename(str(folder), "新的 AI 會議名稱", self.service.row(folder)["title"])
        self.assertEqual(self.service.meetings()["meetings"][0]["title"], "新的 AI 會議名稱")
        self.assertEqual(original_files, sorted(p.name for p in folder.iterdir()))
        self.assertEqual(original_hashes, {k: digest(p) for k, p in self.service.paths(folder).items() if p.is_file()})
        self.assertIn("會議：新的 AI 會議名稱", self.service.handoff(str(folder), "test")["text"])
        with self.assertRaisesRegex(ValueError, "已在別處更新"):
            self.service.rename(str(folder), "過期名稱", "中文 AI 會議")
        for invalid in ["  ", "不合法\n名稱", "字" * 201]:
            with self.assertRaises(ValueError): self.service.rename(str(folder), invalid, "新的 AI 會議名稱")

    def test_corrected_text_survives_restart_and_is_used_for_handoff(self):
        folder = self.prepared()
        paths = self.service.paths(folder)
        before = {k: digest(p) for k, p in paths.items() if p.is_file()}
        text = "人工修正 Athena API 名稱，保留繁體中文。"
        result = self.edit(folder, "prepared", text)
        self.assertEqual(result["kind"], "corrected")
        self.assertTrue(result["meeting"]["corrected"])
        restarted = DesktopService(self.repo, self.service.settings)
        self.assertEqual(restarted.preview(str(folder), "corrected")["text"], text)
        packet = restarted.handoff(str(folder), "test")
        self.assertIn(text, packet["text"])
        self.assertEqual(before, {k: digest(p) for k, p in paths.items() if p.is_file()})

    def test_each_correction_preserves_previous_version(self):
        folder = self.prepared()
        self.edit(folder, "raw", "第一次訂正 API")
        first = self.service.preview(str(folder), "corrected")
        self.edit(folder, "corrected", "第二次訂正 API")
        state = read_json(folder / "desktop_state.json")
        self.assertEqual(len(state["edit_history"]), 2)
        self.assertEqual(Path(first["path"]).read_text(), "第一次訂正 API")
        self.assertEqual(self.service.preview(str(folder), "corrected")["text"], "第二次訂正 API")

    def test_stale_editor_cannot_overwrite_newer_correction(self):
        folder = self.prepared()
        opened = self.service.preview(str(folder), "prepared")
        self.edit(folder, "prepared", "較新的訂正")
        with self.assertRaisesRegex(ValueError, "已有新版本"):
            self.service.save_edit(str(folder), "prepared", opened["token"], text="過期修改")
        self.assertEqual(self.service.preview(str(folder), "corrected")["text"], "較新的訂正")

    def test_srt_text_edits_preserve_every_time_number_and_original_byte(self):
        folder = self.prepared()
        original, original_bytes = self.timeline(folder)
        opened = self.service.preview(str(folder), "srt")
        edits = [{"id": c["id"], "text": "訂正 " + c["text"]} for c in opened["cues"]]
        self.service.save_edit(str(folder), "srt", opened["token"], cues=edits)
        saved = self.service.preview(str(folder), "srt")
        self.assertEqual(original.read_bytes(), original_bytes)
        self.assertEqual([(c["number"], c["time"]) for c in opened["cues"]],
                         [(c["number"], c["time"]) for c in saved["cues"]])
        self.assertEqual([c["text"] for c in saved["cues"]], [c["text"] for c in edits])
        packet = self.service.handoff(str(folder), "test")
        self.assertIn("訂正 第一段 API 名稱", packet["text"])
        self.assertIn(saved["path"], packet["files"])

    def test_srt_rejects_times_order_counts_and_timestamp_injection(self):
        folder = self.prepared()
        path, before = self.timeline(folder)
        opened = self.service.preview(str(folder), "srt")
        valid = [{"id": c["id"], "text": c["text"]} for c in opened["cues"]]
        invalid_sets = [
            [{**valid[0], "time": "00:00:00,000 --> 99:00:00,000"}, valid[1]],
            list(reversed(valid)), valid[:1], valid + [valid[0]],
            [{"id": 0, "text": "新增\n\n99\n00:00:00,000 --> 00:00:01,000\n假字幕"}, valid[1]],
            [{"id": 0, "text": ""}, valid[1]],
        ]
        for cues in invalid_sets:
            with self.subTest(cues=cues), self.assertRaises(ValueError):
                self.service.save_edit(str(folder), "srt", opened["token"], cues=cues)
        with self.assertRaises(ValueError):
            self.service.save_edit(str(folder), "srt", opened["token"], text=opened["text"])
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((folder / "manual_revisions").exists())

    def test_bad_srt_remains_readable_but_cannot_be_edited(self):
        folder = self.prepared()  # Fake SRT intentionally contains just "1".
        preview = self.service.preview(str(folder), "srt")
        self.assertFalse(preview["editable"])
        self.assertTrue(preview["text"])
        self.assertIn("格式", preview["edit_error"])

    def test_corrected_input_invalidates_old_handoff_and_confirmation(self):
        folder = self.prepared()
        self.service.handoff(str(folder), "test")
        output = self.home / "clean.txt"
        output.write_text(self.service.preview(str(folder), "prepared")["text"])
        self.service.import_cleaned(str(folder), str(output))
        self.service.review(str(folder))
        self.edit(folder, "prepared", "訂正後內容，有新專有名詞 Athena。")
        self.assertFalse(self.service.row(folder)["reviewed"])
        with self.assertRaises(ValueError): self.service.review(str(folder))
        with self.assertRaises(ValueError): self.service.import_cleaned(str(folder), str(output))
        with self.assertRaises(ValueError): self.service.handoff(str(folder), "test", summary=True)

    def test_cleaned_manual_edit_requires_new_review_and_summary_uses_it(self):
        folder = self.prepared()
        self.service.handoff(str(folder), "test")
        output = self.home / "clean.txt"
        output.write_text(self.service.preview(str(folder), "prepared")["text"])
        self.service.import_cleaned(str(folder), str(output))
        self.service.review(str(folder))
        original_path = self.service.paths(folder)["cleaned"]
        before = original_path.read_bytes()
        edited = output.read_text() + " 加入人工訂正專有名詞 Athena。"
        self.edit(folder, "cleaned", edited)
        self.assertFalse(self.service.row(folder)["reviewed"])
        self.assertEqual(original_path.read_bytes(), before)
        self.service.review(str(folder))
        self.assertIn(edited, self.service.handoff(str(folder), "test", summary=True)["text"])

    def test_new_llm_result_after_input_edit_is_versioned(self):
        folder = self.prepared()
        first = self.home / "clean.txt"
        first.write_text(self.service.preview(str(folder), "prepared")["text"])
        self.service.handoff(str(folder), "test")
        self.service.import_cleaned(str(folder), str(first))
        original = self.service.paths(folder)["cleaned"].read_bytes()
        self.edit(folder, "prepared", "新的來源內容，新模型名稱與 API。")
        self.service.handoff(str(folder), "test")
        first.write_text("新的來源內容，新模型名稱與 API，修正後。")
        self.service.import_cleaned(str(folder), str(first))
        self.assertEqual(self.service.paths(folder)["cleaned"].read_bytes(), original)
        self.assertEqual(self.service.preview(str(folder), "cleaned")["text"], first.read_text())
        self.service.review(str(folder))

    def test_short_manual_cleaned_is_saved_but_cannot_be_approved(self):
        folder = self.prepared()
        cleaned = self.service.paths(folder)["cleaned"]
        cleaned.write_text(self.service.preview(str(folder), "prepared")["text"])
        result = self.edit(folder, "cleaned", "短")
        self.assertTrue(result["warning"])
        self.assertEqual(self.service.preview(str(folder), "cleaned")["text"], "短")
        with self.assertRaises(ValueError): self.service.review(str(folder))

    def test_srt_edit_invalidates_previous_cleaning_packet(self):
        folder = self.prepared()
        self.timeline(folder)
        self.service.handoff(str(folder), "test")
        opened = self.service.preview(str(folder), "srt")
        edits = [{"id": c["id"], "text": "訂正 " + c["text"]} for c in opened["cues"]]
        self.service.save_edit(str(folder), "srt", opened["token"], cues=edits)
        cleaned = self.home / "clean.txt"
        cleaned.write_text(self.service.preview(str(folder), "prepared")["text"])
        with self.assertRaises(ValueError): self.service.import_cleaned(str(folder), str(cleaned))

    def test_noop_edit_does_not_invalidate_handoff(self):
        folder = self.prepared()
        self.service.handoff(str(folder), "test")
        text = self.service.preview(str(folder), "prepared")["text"]
        result = self.edit(folder, "prepared", text)
        self.assertFalse(result["changed"])
        self.assertEqual(read_json(folder / "desktop_state.json")["handoff"]["status"], "ready")

    def test_editing_revision_does_not_allow_external_file_tampering(self):
        folder = self.prepared()
        self.edit(folder, "prepared", "人工訂正內容")
        path = Path(self.service.preview(str(folder), "corrected")["path"])
        path.write_text("外部變更")
        with self.assertRaisesRegex(ValueError, "外部變更"):
            self.service.handoff(str(folder), "test")


if __name__ == "__main__":
    unittest.main()
