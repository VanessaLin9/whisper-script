#!/usr/bin/env python3
"""Offline tests for Checkpoint 04.3 Phase 1 Drive workflow CLI."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from src.output_manager.paths import planned_artifact_paths, workspace_dirname
from src.transcription.types import ArtifactKind, ProgressStatus
from src.workflow import (
    DriveTranscribeResult,
    WorkflowError,
    WorkflowProgressEvent,
    WorkflowStage,
)
from src.workflow.cli import CLI_STAGE, build_parser, main, request_from_args


def _base_argv(url: str = "https://drive.google.com/file/d/abc123XYZ_-99/view") -> list[str]:
    return [
        url,
        "--output-root",
        "/tmp/out",
        "--language",
        "zh",
        "--model",
        "small",
        "--model-path",
        "/tmp/model.bin",
        "--whisper-cli",
        "/tmp/whisper-cli",
        "--threads",
        "4",
    ]


def _run_main(argv: list[str], *, workflow: MagicMock | None = None) -> tuple[int | None, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    code: int | None
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(argv, workflow=workflow)
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


class DriveWorkflowCliTests(unittest.TestCase):
    def test_request_from_args_and_default_outputs(self) -> None:
        parser = build_parser()
        args = parser.parse_args(_base_argv() + ["--outputs", "srt"])
        request = request_from_args(args)
        self.assertEqual(
            request.drive_url,
            "https://drive.google.com/file/d/abc123XYZ_-99/view",
        )
        self.assertEqual(request.outputs, frozenset({ArtifactKind.SRT}))
        self.assertTrue(request.normalize)

    def test_url_flag_alternative(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--url",
                "https://drive.google.com/file/d/abc123XYZ_-99/view",
                "--output-root",
                "/tmp/out",
                "--language",
                "zh",
                "--model",
                "small",
                "--model-path",
                "/tmp/m.bin",
                "--whisper-cli",
                "/tmp/w",
                "--threads",
                "2",
            ]
        )
        request = request_from_args(args)
        self.assertIn("abc123XYZ_-99", request.drive_url)

    def test_json_success_stdout_progress_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "2026-07-18_1234_meeting"
            workspace.mkdir()
            audio = workspace / "meeting.m4a"
            audio.write_bytes(b"A")
            txt = workspace / "meeting_transcription.txt"
            txt.write_text("hi\n", encoding="utf-8")
            fake = MagicMock()

            def _run(request, *, on_progress=None):
                if on_progress is not None:
                    on_progress(
                        WorkflowProgressEvent(
                            WorkflowStage.DOWNLOAD,
                            ProgressStatus.STARTED,
                        )
                    )
                return DriveTranscribeResult(
                    workspace_dir=workspace,
                    raw_audio_path=audio,
                    raw_transcript_path=txt,
                    artifacts={ArtifactKind.TXT: txt},
                    normalized_audio_path=None,
                    download_filename="meeting.m4a",
                    file_id="abc123XYZ_-99",
                    meeting_time=datetime(2026, 7, 18, 12, 34, tzinfo=timezone.utc),
                    language="zh",
                    model="small",
                    stem="meeting",
                )

            fake.run.side_effect = _run
            code, out, err = _run_main(_base_argv() + ["--json"], workflow=fake)
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["workspace_dir"], str(workspace))
            self.assertIn("[workflow:download] started", err)
            self.assertNotIn("workflow:download", out)

    def test_json_failure_stage_and_nonzero_exit(self) -> None:
        fake = MagicMock()
        fake.run.side_effect = WorkflowError(
            WorkflowStage.DOWNLOAD,
            "not publicly accessible",
        )
        code, out, err = _run_main(
            _base_argv() + ["--json", "--quiet-progress"], workflow=fake
        )
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["stage"], "download")
        self.assertIn("not publicly accessible", payload["message"])
        self.assertIn("stage=download", err)

    def test_human_failure_keeps_stdout_clean_of_json(self) -> None:
        fake = MagicMock()
        fake.run.side_effect = WorkflowError(WorkflowStage.WORKSPACE, "conflict")
        code, out, err = _run_main(_base_argv() + ["--quiet-progress"], workflow=fake)
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), "")
        self.assertIn("stage=workspace", err)

    def test_json_missing_url_emits_error_object(self) -> None:
        code, out, err = _run_main(
            [
                "--json",
                "--output-root",
                "/tmp/out",
                "--language",
                "zh",
                "--model",
                "small",
                "--model-path",
                "/tmp/m.bin",
                "--whisper-cli",
                "/tmp/w",
                "--threads",
                "2",
            ]
        )
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["stage"], CLI_STAGE)
        self.assertIn("Drive URL", payload["message"])
        self.assertIn("Drive URL", err)

    def test_json_missing_required_option(self) -> None:
        code, out, err = _run_main(
            [
                "--json",
                "https://drive.google.com/file/d/abc123XYZ_-99/view",
                "--language",
                "zh",
                "--model",
                "small",
                "--model-path",
                "/tmp/m.bin",
                "--whisper-cli",
                "/tmp/w",
                "--threads",
                "2",
            ]
        )
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["stage"], CLI_STAGE)
        self.assertIn("output-root", payload["message"])
        self.assertTrue(err.strip())

    def test_json_malformed_meeting_time(self) -> None:
        code, out, err = _run_main(
            _base_argv() + ["--json", "--meeting-time", "not-a-time"]
        )
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["stage"], CLI_STAGE)
        self.assertIn("meeting-time", payload["message"])
        self.assertTrue(err.strip())

    def test_json_unsupported_output(self) -> None:
        code, out, err = _run_main(_base_argv() + ["--json", "--outputs", "txt,docx"])
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["stage"], CLI_STAGE)
        self.assertIn("docx", payload["message"])
        self.assertTrue(err.strip())

    def test_json_conflicting_url_inputs(self) -> None:
        code, out, err = _run_main(
            [
                "--json",
                "https://drive.google.com/file/d/abc123XYZ_-99/view",
                "--url",
                "https://drive.google.com/file/d/otherFileId99/view",
                "--output-root",
                "/tmp/out",
                "--language",
                "zh",
                "--model",
                "small",
                "--model-path",
                "/tmp/m.bin",
                "--whisper-cli",
                "/tmp/w",
                "--threads",
                "2",
            ]
        )
        self.assertEqual(code, 2)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["stage"], CLI_STAGE)
        self.assertIn("Conflicting", payload["message"])
        self.assertTrue(err.strip())


class ReadmeContractTests(unittest.TestCase):
    def test_readme_paths_match_planner_and_record_meeting(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")
        dirname = workspace_dirname("2026-07-17_1500", "safe_stem")
        self.assertEqual(dirname, "2026-07-17_1500_safe_stem")
        artifacts = planned_artifact_paths(
            Path(dirname),
            "safe_stem",
            frozenset({ArtifactKind.TXT, ArtifactKind.SRT, ArtifactKind.JSON}),
        )
        self.assertEqual(
            artifacts[ArtifactKind.TXT].name,
            "safe_stem_transcription.txt",
        )

        self.assertIn("YYYY-MM-DD_HHMM_<safe-stem>/", readme)
        self.assertIn("<safe-stem>_transcription.txt", readme)
        self.assertIn("<safe-stem>_norm16k.wav", readme)
        self.assertNotIn(
            "2026-07-17_1500_<safe-stem>_transcription.txt",
            readme,
        )
        self.assertIn("meeting_YYYYMMDD_HHMMSS.wav", readme)
        self.assertIn("ffmpeg_YYYYMMDD_HHMMSS.log", readme)
        self.assertIn("尚未**遷移到 Output Manager", readme)


if __name__ == "__main__":
    unittest.main()
