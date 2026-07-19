#!/usr/bin/env python3
"""Unit tests for the non-interactive transcription CLI entry."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src.output_manager import DEFAULT_ARTIFACTS, default_outputs_arg
from src.transcription import ArtifactKind, Stage, TranscriptionError, TranscribeResult
from src.transcription.cli import build_parser, main, request_from_args


class TranscriptionCliTests(unittest.TestCase):
    def test_request_from_args_maps_outputs(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--audio",
                "/tmp/a.wav",
                "--output-dir",
                "/tmp/out",
                "--stem",
                "meeting",
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
                "--outputs",
                "txt,srt",
            ]
        )
        request = request_from_args(args)
        self.assertEqual(request.stem, "meeting")
        self.assertEqual(request.outputs, frozenset({ArtifactKind.TXT, ArtifactKind.SRT}))
        self.assertTrue(request.normalize)
        self.assertTrue(request.keep_normalized)
        self.assertIsNone(request.artifact_basename)

    def test_request_from_args_maps_artifact_basename(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--audio",
                "/tmp/a.wav",
                "--output-dir",
                "/tmp/out",
                "--stem",
                "meeting_20260719_120000",
                "--artifact-basename",
                "meeting_20260719_120000",
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
                "--outputs",
                "txt,srt",
                "--no-normalize",
            ]
        )
        request = request_from_args(args)
        self.assertEqual(request.artifact_basename, "meeting_20260719_120000")
        self.assertFalse(request.normalize)

    def test_parser_default_outputs_match_shared_policy(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--audio",
                "/tmp/a.wav",
                "--output-dir",
                "/tmp/out",
                "--stem",
                "meeting",
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
        )
        request = request_from_args(args)
        self.assertEqual(default_outputs_arg(), "json,srt,txt")
        self.assertEqual(request.outputs, DEFAULT_ARTIFACTS)
        self.assertNotIn(ArtifactKind.VTT, request.outputs)

    def test_main_reports_stage_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "a.wav"
            audio.write_bytes(b"x")
            model = root / "m.bin"
            model.write_bytes(b"m")
            cli = root / "whisper-cli"
            cli.write_text("#!/bin/sh\n", encoding="utf-8")

            with patch(
                "src.transcription.cli.transcribe",
                side_effect=TranscriptionError(Stage.NORMALIZE, "boom", exit_code=9),
            ):
                code = main(
                    [
                        "--audio",
                        str(audio),
                        "--output-dir",
                        str(root / "out"),
                        "--stem",
                        "meeting",
                        "--language",
                        "zh",
                        "--model",
                        "small",
                        "--model-path",
                        str(model),
                        "--whisper-cli",
                        str(cli),
                        "--threads",
                        "2",
                        "--quiet-progress",
                    ]
                )
            self.assertEqual(code, 1)

    def test_main_prints_bounded_diagnostic_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "a.wav"
            audio.write_bytes(b"x")
            model = root / "m.bin"
            model.write_bytes(b"m")
            cli = root / "whisper-cli"
            cli.write_text("#!/bin/sh\n", encoding="utf-8")
            stderr = io.StringIO()

            with patch(
                "src.transcription.cli.transcribe",
                side_effect=TranscriptionError(
                    Stage.TRANSCRIBE,
                    "whisper-cli transcription failed",
                    exit_code=1,
                    diagnostic="simulated whisper-cli failure",
                ),
            ):
                with patch("sys.stderr", stderr):
                    code = main(
                        [
                            "--audio",
                            str(audio),
                            "--output-dir",
                            str(root / "out"),
                            "--stem",
                            "meeting",
                            "--language",
                            "zh",
                            "--model",
                            "small",
                            "--model-path",
                            str(model),
                            "--whisper-cli",
                            str(cli),
                            "--threads",
                            "2",
                            "--quiet-progress",
                            "--stream-subprocess",
                        ]
                    )

            self.assertEqual(code, 1)
            err = stderr.getvalue()
            self.assertIn("stage=transcribe", err)
            self.assertIn("subprocess diagnostic (tail)", err)
            self.assertIn("simulated whisper-cli failure", err)
            self.assertNotIn("[*] Transcription OK", err)

    def test_stream_subprocess_uses_streaming_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "a.wav"
            audio.write_bytes(b"x")
            model = root / "m.bin"
            model.write_bytes(b"m")
            cli = root / "whisper-cli"
            cli.write_text("#!/bin/sh\n", encoding="utf-8")
            out_dir = root / "out"
            now = datetime.now(timezone.utc)
            fake_result = TranscribeResult(
                raw_audio_path=audio,
                normalized_audio_path=None,
                artifacts={ArtifactKind.TXT: out_dir / "meeting.txt"},
                model="small",
                language="zh",
                started_at=now,
                finished_at=now,
                output_dir=out_dir,
                stem="meeting",
            )

            with patch("src.transcription.cli.transcribe", return_value=fake_result) as mocked:
                with patch("src.transcription.cli.StreamingSubprocessRunner") as runner_cls:
                    runner_cls.return_value = object()
                    code = main(
                        [
                            "--audio",
                            str(audio),
                            "--output-dir",
                            str(out_dir),
                            "--stem",
                            "meeting",
                            "--language",
                            "zh",
                            "--model",
                            "small",
                            "--model-path",
                            str(model),
                            "--whisper-cli",
                            str(cli),
                            "--threads",
                            "2",
                            "--quiet-progress",
                            "--stream-subprocess",
                        ]
                    )

            self.assertEqual(code, 0)
            runner_cls.assert_called_once_with()
            self.assertIs(mocked.call_args.kwargs["runner"], runner_cls.return_value)

    def test_main_success_lists_requested_artifacts_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "a.wav"
            audio.write_bytes(b"x")
            model = root / "m.bin"
            model.write_bytes(b"m")
            cli = root / "whisper-cli"
            cli.write_text("#!/bin/sh\n", encoding="utf-8")
            out_dir = root / "out"
            srt_path = out_dir / "meeting_transcription.srt"
            now = datetime.now(timezone.utc)
            fake_result = TranscribeResult(
                raw_audio_path=audio,
                normalized_audio_path=None,
                artifacts={ArtifactKind.SRT: srt_path},
                model="small",
                language="zh",
                started_at=now,
                finished_at=now,
                output_dir=out_dir,
                stem="meeting",
            )

            stdout = io.StringIO()
            with patch("src.transcription.cli.transcribe", return_value=fake_result):
                with redirect_stdout(stdout):
                    code = main(
                        [
                            "--audio",
                            str(audio),
                            "--output-dir",
                            str(out_dir),
                            "--stem",
                            "meeting",
                            "--language",
                            "zh",
                            "--model",
                            "small",
                            "--model-path",
                            str(model),
                            "--whisper-cli",
                            str(cli),
                            "--threads",
                            "2",
                            "--outputs",
                            "srt",
                            "--quiet-progress",
                        ]
                    )

            self.assertEqual(code, 0)
            printed = stdout.getvalue()
            self.assertIn(f"[*] Transcription OK: {out_dir}", printed)
            self.assertIn(f"srt: {srt_path}", printed)
            self.assertNotIn("txt:", printed)
            self.assertNotIn("meeting_transcription.txt", printed)


if __name__ == "__main__":
    unittest.main()
