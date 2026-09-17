"""Private Meeting Desk job queue contract tests."""
import json
import tempfile
import unittest
from pathlib import Path

from src.desktop.editing import file_hash
from src.desktop.jobs import create_job, read_completed_result, read_job, result_payload


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


if __name__ == "__main__":
    unittest.main()
