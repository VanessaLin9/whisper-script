"""Private Meeting Desk job queue contract tests."""
import json
import tempfile
import unittest
from pathlib import Path

from src.desktop.editing import file_hash
from src.desktop.jobs import (
    create_job,
    new_job_id,
    read_completed_result,
    read_job,
    read_job_manifest,
    result_payload,
    segment_srt,
)


class DesktopJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "MeetingRecords"
        self.meeting = self.root / "2026-09-17_0900_AI"
        self.meeting.mkdir(parents=True)
        self.profile = Path(self.temp.name) / "private-profiles" / "inno.md"
        self.profile.parent.mkdir()
        self.profile.write_text("private vocabulary", encoding="utf-8")
        self.input = self.meeting / "ai_transcription_prepared.txt"
        self.input.write_text("逐字稿內容 Athena API", encoding="utf-8")
        self.srt = self.meeting / "ai_transcription.srt"
        self.srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n逐字稿內容\n", encoding="utf-8")

    def create(self):
        return create_job(
            self.root, self.meeting, "AI 會議", "clean", "inno", "Inno Team",
            self.profile, self.input, "ai_transcription_cleaned.txt", srt_path=self.srt,
        )

    def test_request_is_small_private_and_references_files_by_hash(self):
        record = self.create()
        request = Path(record["request_path"])
        job = read_job(request)
        self.assertEqual(request.parent, (self.root / ".llm_jobs/inbox").resolve())
        self.assertEqual(request.stat().st_mode & 0o777, 0o600)
        self.assertEqual(request.parent.stat().st_mode & 0o777, 0o700)
        self.assertTrue(job["private_local_only"])
        self.assertEqual(job["input"]["path"], str(self.input.resolve()))
        self.assertEqual(job["input"]["sha256"], file_hash(self.input))
        self.assertEqual(job["profile"]["path"], str(self.profile.resolve()))
        serialized = request.read_text(encoding="utf-8")
        self.assertNotIn("private vocabulary", serialized)
        self.assertNotIn("逐字稿內容 Athena API", serialized)

    def test_completed_result_requires_exact_output_path_and_hash(self):
        record = self.create()
        job = read_job(Path(record["request_path"]))
        output = Path(record["expected_output"])
        output.write_text("清洗後逐字稿", encoding="utf-8")
        Path(record["result_path"]).write_text(
            json.dumps(result_payload(job, output), ensure_ascii=False), encoding="utf-8")
        resolved, result, loaded = read_completed_result(record)
        self.assertEqual(resolved, output)
        self.assertEqual(result["status"], "done")
        self.assertEqual(loaded["job_id"], record["job_id"])

        result["output_sha256"] = "tampered"
        Path(record["result_path"]).write_text(json.dumps(result), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash"):
            read_completed_result(record)

    def test_changed_input_or_request_is_rejected(self):
        record = self.create()
        job = read_job(Path(record["request_path"]))
        output = Path(record["expected_output"])
        output.write_text("清洗後逐字稿", encoding="utf-8")
        Path(record["result_path"]).write_text(json.dumps(result_payload(job, output)), encoding="utf-8")
        self.input.write_text("來源已變更", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "來源檔案已變更"):
            read_completed_result(record)

    def test_srt_segmentation_has_unique_cores_and_context_only_overlap(self):
        self.srt.write_text(
            "1\n00:00:10,000 --> 00:00:20,000\n第一段\n\n"
            "2\n00:09:40,000 --> 00:09:50,000\n第一核心尾端\n\n"
            "3\n00:10:05,000 --> 00:10:15,000\n第二核心開頭\n\n"
            "4\n00:19:50,000 --> 00:20:00,000\n第二核心尾端\n\n"
            "5\n00:20:10,000 --> 00:20:20,000\n第三核心\n",
            encoding="utf-8",
        )
        job_id = new_job_id("clean")
        path = segment_srt(self.root, job_id, self.srt)
        manifest = read_job_manifest(path)
        self.assertEqual(len(manifest["segments"]), 3)
        core_ids = [cue for segment in manifest["segments"] for cue in segment["core_cue_ids"]]
        self.assertEqual(core_ids, [0, 1, 2, 3, 4])
        self.assertEqual(len(core_ids), len(set(core_ids)))
        first = Path(manifest["segments"][0]["path"]).read_text(encoding="utf-8")
        second = Path(manifest["segments"][1]["path"]).read_text(encoding="utf-8")
        self.assertIn("第二核心開頭", first.split("[CONTEXT_AFTER]", 1)[1])
        self.assertIn("第二核心開頭", second.split("[CORE]", 1)[1].split("[CONTEXT_AFTER]", 1)[0])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_segment_tampering_invalidates_completed_result(self):
        job_id = new_job_id("clean")
        manifest = segment_srt(self.root, job_id, self.srt)
        record = create_job(
            self.root, self.meeting, "AI 會議", "clean", "inno", "Inno Team",
            self.profile, self.input, "ai_transcription_cleaned.txt", srt_path=self.srt,
            segments_manifest=manifest, job_id=job_id,
        )
        job = read_job(Path(record["request_path"]))
        output = Path(record["expected_output"])
        output.write_text("清洗後逐字稿", encoding="utf-8")
        Path(record["result_path"]).write_text(json.dumps(result_payload(job, output)), encoding="utf-8")
        segment = Path(read_job_manifest(manifest)["segments"][0]["path"])
        segment.write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "切段檔案已變更"):
            read_completed_result(record)


if __name__ == "__main__":
    unittest.main()
