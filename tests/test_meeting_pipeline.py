import json
import tempfile
import unittest
from pathlib import Path

from src.meeting_pipeline import (
    PROMPT_PROFILES,
    build_state,
    discover_meetings,
    run_pipeline,
    select_meeting,
)


class MeetingPipelineTests(unittest.TestCase):
    def _workspace(self) -> Path:
        root = Path(self.temp_dir.name)
        folder = root / "2026-08-06_1630_11"
        folder.mkdir()
        (folder / "11_transcription.txt").write_text("第一段\n第一段\n第二段\n", encoding="utf-8")
        (folder / "11_transcription.srt").write_text(
            "1\n00:00:00,000 --> 00:00:03,500\n第一段\n", encoding="utf-8"
        )
        return root

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_discover_ignores_prepared_and_cleaned_as_raw_candidates(self):
        root = self._workspace()
        candidate = discover_meetings(root)[0]
        self.assertEqual(candidate.meeting_id, "11")
        self.assertEqual(candidate.start_time, "16:30")
        self.assertEqual(candidate.duration_seconds, 3.5)
        self.assertEqual(select_meeting([candidate], "11"), candidate)

    def test_pipeline_runs_preclean_and_writes_handoff_state(self):
        root = self._workspace()
        candidate = discover_meetings(root)[0]
        state, state_path, result = run_pipeline(
            candidate, prompt_profile="inno", mode="full"
        )
        self.assertEqual(result["status"], "created")
        self.assertTrue(candidate.prepared.exists())
        self.assertTrue(state_path.exists())
        self.assertEqual(state["prompt_profile"]["name"], "inno")
        self.assertEqual(state["pipeline"]["next_stage"], "clean_transcript")
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["handoff"]["clean_skill"], "clean-meeting-transcripts")

    def test_pipeline_is_fail_closed_for_existing_state_without_force(self):
        root = self._workspace()
        candidate = discover_meetings(root)[0]
        run_pipeline(candidate, prompt_profile="whisper", mode="preclean")
        with self.assertRaises(FileExistsError):
            run_pipeline(candidate, prompt_profile="whisper", mode="preclean")

    def test_unknown_prompt_profile_rejected(self):
        root = self._workspace()
        candidate = discover_meetings(root)[0]
        with self.assertRaises(RuntimeError):
            build_state(candidate, "unknown", "full")
        self.assertIn("inno", PROMPT_PROFILES)


if __name__ == "__main__":
    unittest.main()
