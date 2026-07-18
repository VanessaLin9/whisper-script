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

from src.transcription.types import ArtifactKind, ProgressStatus
from src.workflow import (
    DriveTranscribeResult,
    WorkflowError,
    WorkflowProgressEvent,
    WorkflowStage,
)
from src.workflow.cli import build_parser, main, request_from_args


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
            fake.run.return_value = DriveTranscribeResult(
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

            def _run(request, *, on_progress=None):
                if on_progress is not None:
                    on_progress(
                        WorkflowProgressEvent(
                            WorkflowStage.DOWNLOAD,
                            ProgressStatus.STARTED,
                        )
                    )
                return fake.run.return_value

            fake.run.side_effect = _run

            out = io.StringIO()
            err = io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = main(_base_argv() + ["--json"], workflow=fake)
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["workspace_dir"], str(workspace))
            self.assertEqual(payload["raw_transcript_path"], str(txt))
            self.assertIn("[workflow:download] started", err.getvalue())
            self.assertNotIn("workflow:download", out.getvalue())

    def test_json_failure_stage_and_nonzero_exit(self) -> None:
        fake = MagicMock()
        fake.run.side_effect = WorkflowError(
            WorkflowStage.DOWNLOAD,
            "not publicly accessible",
        )
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(_base_argv() + ["--json", "--quiet-progress"], workflow=fake)
        self.assertEqual(code, 1)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["stage"], "download")
        self.assertIn("not publicly accessible", payload["message"])
        self.assertIn("stage=download", err.getvalue())

    def test_human_failure_keeps_stdout_clean_of_json(self) -> None:
        fake = MagicMock()
        fake.run.side_effect = WorkflowError(WorkflowStage.WORKSPACE, "conflict")
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(_base_argv() + ["--quiet-progress"], workflow=fake)
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue().strip(), "")
        self.assertIn("stage=workspace", err.getvalue())

    def test_missing_url_exits_nonzero(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                main(
                    [
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
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("Drive URL", err.getvalue())


if __name__ == "__main__":
    unittest.main()
