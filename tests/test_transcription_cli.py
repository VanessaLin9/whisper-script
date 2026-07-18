#!/usr/bin/env python3
"""Unit tests for the non-interactive transcription CLI entry."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.transcription import ArtifactKind, Stage, TranscriptionError
from src.transcription.cli import main, request_from_args
from src.transcription.cli import build_parser


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


if __name__ == "__main__":
    unittest.main()
