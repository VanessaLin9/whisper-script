"""Pure checks for the clean-job inbox/outbox contract."""
import json
import tempfile
import unittest
from pathlib import Path

from src.desktop.jobs import JobRejected, build_clean_job, file_sha256, merged_segment_text, stale_reasons, validated_outbox_file
from src.desktop.segments import core_transcript, load_timeline, plan_segments, render_segment


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

    def test_sixty_eight_minutes_cover_every_cue_once(self):
        cues = load_timeline(self._write_srt(68 * 60 * 1000))
        segments = plan_segments(cues)
        self.assertEqual(len(segments), 7)
        self.assertEqual([segment["core_end"] - segment["core_start"] for segment in segments[:6]], [10 * 60 * 1000] * 6)
        self.assertEqual(segments[-1]["core_end"] - segments[-1]["core_start"], 8 * 60 * 1000)
        cores = [cue["id"] for segment in segments for cue in segment["core"]]
        self.assertEqual(cores, [cue["id"] for cue in cues])
        self.assertEqual(core_transcript(segments), "\n".join(cue["text"] for cue in cues))
        self.assertNotIn("[前段上下文", render_segment(segments[0]))
        second = render_segment(segments[1])
        boundary = segments[0]["core"][-1]["text"]
        self.assertIn("[前段上下文", second)
        self.assertIn(boundary, second)
        self.assertNotIn(boundary, _core_only(second))

    def test_long_cue_stays_whole_and_context_is_not_merged(self):
        cues = load_timeline(self._write_srt_blocks([
            (0, 15 * 60 * 1000, "很長的一段"),
            (15 * 60 * 1000, 16 * 60 * 1000, "下一段"),
        ]))
        segments = plan_segments(cues)
        self.assertEqual([[cue["text"] for cue in segment["core"]] for segment in segments], [["很長的一段"], ["下一段"]])
        self.assertEqual(core_transcript(segments), "很長的一段\n下一段")
        rendered = render_segment(segments[1])
        self.assertIn("很長的一段", rendered)
        self.assertIn("下一段", _core_only(rendered))
        self.assertNotIn("很長的一段", _core_only(rendered))

    def test_segment_result_rejects_context_markers(self):
        cues = load_timeline(self._write_srt(60 * 1000))
        segment = plan_segments(cues)[0]
        out = self.outbox / "seg-01.txt"
        out.write_text(render_segment(segment), encoding="utf-8")
        job = {"job_id": "clean-test", "segments": {"items": [{
            "id": "seg-01", "output_order": 1, "outbox_path": str(out),
        }]}}
        result = {"schema_version": 1, "job_id": "clean-test", "stage": "clean", "status": "done", "segments": [{
            "id": "seg-01", "output_order": 1, "path": str(out), "sha256": file_sha256(out),
        }]}
        with self.assertRaisesRegex(JobRejected, "上下文"):
            merged_segment_text(job, result)

    def _write_srt(self, duration_ms: int, cue_ms: int = 30_000) -> Path:
        start = 0
        blocks = []
        number = 1
        while start < duration_ms:
            end = min(start + cue_ms, duration_ms)
            blocks.append((start, end, f"詞{number}"))
            start = end
            number += 1
        return self._write_srt_blocks(blocks)

    def _write_srt_blocks(self, blocks: list[tuple[int, int, str]]) -> Path:
        lines = []
        for number, (start, end, text) in enumerate(blocks, start=1):
            lines.append(f"{number}\n{_clock(start)} --> {_clock(end)}\n{text}")
        path = self.meeting / f"timeline-{len(list(self.meeting.glob('timeline-*.srt')))}.srt"
        path.write_text("\n\n".join(lines) + "\n", encoding="utf-8")
        return path


def _clock(value: int) -> str:
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _core_only(rendered: str) -> str:
    core = rendered.split("[核心｜必須清洗並輸出]\n", 1)[1]
    return core.split("\n\n[後段上下文", 1)[0]


if __name__ == "__main__":
    unittest.main()
