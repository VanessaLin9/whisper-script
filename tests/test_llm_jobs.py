"""Pure checks for the clean-job inbox/outbox contract."""
import json
import tempfile
import unittest
from pathlib import Path

from src.desktop.jobs import JobRejected, build_clean_job, file_sha256, stale_reasons, validated_outbox_file


class CleanJobContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.meeting = self.root / "2026-09-25_0900_demo"
        self.meeting.mkdir()
        self.paths = {
            "raw": self.meeting / "demo_transcription.txt",
            "prepared": self.meeting / "demo_transcription_prepared.txt",
            "cleaned": self.meeting / "demo_transcription_cleaned.txt",
            "srt": self.meeting / "demo_transcription.srt",
            "input": self.meeting / "demo_transcription_prepared.txt",
            "corrected": self.meeting / "manual_revisions" / "not-created.txt",
        }
        self.paths["prepared"].write_text("準備好的逐字稿內容，足夠做長度比較。", encoding="utf-8")
        self.paths["srt"].write_text("1\n00:00:00,000 --> 00:00:01,000\n準備好的逐字稿\n", encoding="utf-8")
        self.profile = self.root / "inno.md"
        self.profile.write_text("private vocabulary", encoding="utf-8")
        self.outbox = self.root / ".llm_jobs" / "outbox"
        self.outbox.mkdir(parents=True)

    def job(self):
        return build_clean_job(
            job_id="clean-test", meeting_dir=self.meeting, meeting_title="示範會議",
            profile_key="inno", profile_path=self.profile, paths=self.paths,
            outbox_dir=self.outbox, created_at="2026-09-25T09:00:00+08:00",
        )

    def test_job_keeps_paths_and_hashes_only(self):
        document = self.job()
        encoded = json.dumps(document, ensure_ascii=False)
        self.assertNotIn("準備好的逐字稿內容", encoded)
        self.assertNotIn("private vocabulary", encoded)
        self.assertEqual(document["inputs"]["transcript"]["sha256"], file_sha256(self.paths["prepared"]))
        self.assertIsNone(document["inputs"]["vocab"])
        self.assertIsNone(document["segments"])
        self.assertTrue(document["expected_output"]["meeting_path"].endswith("demo_transcription_cleaned.txt"))

    def test_stale_when_profile_or_transcript_changes(self):
        document = self.job()
        self.assertEqual(stale_reasons(
            document, transcript_sha256=document["inputs"]["transcript"]["sha256"],
            srt_sha256=document["inputs"]["srt"]["sha256"],
            profile_sha256=document["profile"]["sha256"], profile_path=document["profile"]["path"],
        ), [])
        self.profile.write_text("changed vocabulary", encoding="utf-8")
        self.assertIn("profile", stale_reasons(
            document, transcript_sha256=document["inputs"]["transcript"]["sha256"],
            srt_sha256=document["inputs"]["srt"]["sha256"],
            profile_sha256=file_sha256(self.profile), profile_path=str(self.profile),
        ))

    def test_result_must_be_the_named_outbox_file(self):
        document = self.job()
        outside = self.paths["raw"]
        outside.write_text("raw", encoding="utf-8")
        with self.assertRaises(JobRejected):
            validated_outbox_file(document, {
                "schema_version": 1, "job_id": "clean-test", "stage": "clean", "status": "done",
                "output": {"path": str(outside), "sha256": file_sha256(outside)},
            }, self.outbox)
        expected = self.outbox / "clean-test.txt"
        expected.write_text("清洗後的逐字稿內容，足夠做長度比較。", encoding="utf-8")
        accepted = validated_outbox_file(document, {
            "schema_version": 1, "job_id": "clean-test", "stage": "clean", "status": "done",
            "output": {"path": str(expected), "sha256": file_sha256(expected)},
        }, self.outbox)
        self.assertEqual(accepted, expected.resolve())


if __name__ == "__main__":
    unittest.main()
