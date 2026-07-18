#!/usr/bin/env python3
"""Offline deterministic tests for the local transcription core."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from src.transcription import (
    ArtifactKind,
    ProgressStatus,
    Stage,
    TranscribeRequest,
    TranscriptionError,
    transcribe,
)
from src.transcription.subprocess_runner import CommandResult
from src.transcription.whisper import build_whisper_command


@dataclass
class FakeRunner:
    """Command double that writes outputs based on argv without real binaries."""

    fail_normalize: bool = False
    fail_whisper: bool = False
    raise_on_normalize: BaseException | None = None
    raise_on_whisper: BaseException | None = None
    skip_artifact: ArtifactKind | None = None
    calls: list[list[str]] | None = None

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    def run(self, command: Sequence[str], *, cwd: Path | None = None) -> CommandResult:
        del cwd
        argv = list(command)
        self.calls.append(argv)
        binary = Path(argv[0]).name

        if binary == "ffmpeg":
            if self.raise_on_normalize is not None:
                raise self.raise_on_normalize
            if self.fail_normalize:
                return CommandResult(returncode=1, stdout="", stderr="ffmpeg boom")
            output = Path(argv[-1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"RIFF....WAVEfmt fake")
            return CommandResult(returncode=0, stdout="", stderr="")

        if binary == "whisper-cli":
            if self.raise_on_whisper is not None:
                raise self.raise_on_whisper
            if self.fail_whisper:
                return CommandResult(returncode=2, stdout="", stderr="whisper boom")
            output_file = _flag_value(argv, "--output-file")
            assert output_file is not None
            base = Path(output_file)
            base.parent.mkdir(parents=True, exist_ok=True)
            for kind in ArtifactKind:
                flag = f"--output-{kind.value}"
                if flag in argv:
                    if self.skip_artifact == kind:
                        continue
                    path = Path(f"{base}.{kind.value}")
                    path.write_text(f"{kind.value} content\n", encoding="utf-8")
            return CommandResult(returncode=0, stdout="", stderr="")

        return CommandResult(returncode=127, stdout="", stderr=f"unknown binary: {binary}")


def _flag_value(argv: list[str], flag: str) -> str | None:
    for index, item in enumerate(argv):
        if item == flag and index + 1 < len(argv):
            return argv[index + 1]
    return None


class TranscriptionCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.audio = self.root / "source.wav"
        self.audio.write_bytes(b"source-audio")
        self.output_dir = self.root / "out"
        self.whisper_cli = self.root / "whisper-cli"
        self.whisper_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        self.model_path = self.root / "ggml-small.bin"
        self.model_path.write_bytes(b"model")
        self.ffmpeg = Path("ffmpeg")
        self.events: list[tuple[Stage, ProgressStatus]] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _request(self, **overrides: object) -> TranscribeRequest:
        values: dict[str, object] = {
            "audio_path": self.audio,
            "language": "zh",
            "model": "small",
            "model_path": self.model_path,
            "whisper_cli": self.whisper_cli,
            "threads": 2,
            "output_dir": self.output_dir,
            "stem": "meeting",
            "outputs": frozenset(
                {ArtifactKind.TXT, ArtifactKind.SRT, ArtifactKind.VTT, ArtifactKind.JSON}
            ),
            "normalize": True,
            "keep_normalized": True,
            "ffmpeg": self.ffmpeg,
        }
        values.update(overrides)
        return TranscribeRequest(**values)  # type: ignore[arg-type]

    def _on_progress(self, event) -> None:
        self.events.append((event.stage, event.status))

    def test_success_normalize_and_all_artifacts(self) -> None:
        runner = FakeRunner()
        result = transcribe(self._request(), runner=runner, on_progress=self._on_progress)

        self.assertEqual(result.raw_audio_path, self.audio.resolve())
        self.assertIsNotNone(result.normalized_audio_path)
        assert result.normalized_audio_path is not None
        self.assertTrue(result.normalized_audio_path.is_file())
        self.assertEqual(result.model, "small")
        self.assertEqual(result.language, "zh")
        for kind in ArtifactKind:
            self.assertIn(kind, result.artifacts)
            self.assertTrue(result.artifacts[kind].is_file())
        self.assertEqual(self.audio.read_bytes(), b"source-audio")
        self.assertIn((Stage.NORMALIZE, ProgressStatus.STARTED), self.events)
        self.assertIn((Stage.TRANSCRIBE, ProgressStatus.FINISHED), self.events)
        self.assertEqual(self.events[-1], (Stage.VALIDATE_ARTIFACTS, ProgressStatus.FINISHED))

    def test_normalize_false_skips_ffmpeg(self) -> None:
        runner = FakeRunner()
        result = transcribe(self._request(normalize=False), runner=runner)

        self.assertIsNone(result.normalized_audio_path)
        self.assertFalse((self.output_dir / "meeting_norm16k.wav").exists())
        self.assertEqual(Path(runner.calls[0][0]).name, "whisper-cli")
        self.assertEqual(_flag_value(runner.calls[0], "-f"), str(self.audio.resolve()))

    def test_subset_of_outputs(self) -> None:
        runner = FakeRunner()
        result = transcribe(
            self._request(outputs=frozenset({ArtifactKind.TXT, ArtifactKind.SRT})),
            runner=runner,
        )

        self.assertEqual(set(result.artifacts), {ArtifactKind.TXT, ArtifactKind.SRT})
        self.assertFalse((self.output_dir / "meeting_transcription.vtt").exists())
        whisper_cmd = runner.calls[-1]
        self.assertIn("--output-txt", whisper_cmd)
        self.assertIn("--output-srt", whisper_cmd)
        self.assertNotIn("--output-vtt", whisper_cmd)
        self.assertNotIn("--output-json", whisper_cmd)

    def test_output_conflict_refuses_overwrite(self) -> None:
        conflict = self.output_dir / "meeting_transcription.txt"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        conflict.write_text("old\n", encoding="utf-8")
        runner = FakeRunner()

        with self.assertRaises(TranscriptionError) as ctx:
            transcribe(self._request(), runner=runner)

        self.assertEqual(ctx.exception.stage, Stage.CHECK_OUTPUTS)
        self.assertEqual(conflict.read_text(encoding="utf-8"), "old\n")
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.audio.read_bytes(), b"source-audio")

    def test_normalize_failure_keeps_source_and_cleans_partial(self) -> None:
        runner = FakeRunner(fail_normalize=True)

        with self.assertRaises(TranscriptionError) as ctx:
            transcribe(self._request(), runner=runner, on_progress=self._on_progress)

        self.assertEqual(ctx.exception.stage, Stage.NORMALIZE)
        self.assertEqual(self.audio.read_bytes(), b"source-audio")
        self.assertFalse((self.output_dir / "meeting_norm16k.wav").exists())
        self.assertIn((Stage.NORMALIZE, ProgressStatus.FAILED), self.events)

    def test_whisper_failure_keeps_source_and_cleans_outputs(self) -> None:
        runner = FakeRunner(fail_whisper=True)

        with self.assertRaises(TranscriptionError) as ctx:
            transcribe(self._request(), runner=runner)

        self.assertEqual(ctx.exception.stage, Stage.TRANSCRIBE)
        self.assertEqual(self.audio.read_bytes(), b"source-audio")
        self.assertFalse((self.output_dir / "meeting_norm16k.wav").exists())
        self.assertFalse((self.output_dir / "meeting_transcription.txt").exists())

    def test_missing_artifact_after_whisper_fails_validation(self) -> None:
        runner = FakeRunner(skip_artifact=ArtifactKind.JSON)

        with self.assertRaises(TranscriptionError) as ctx:
            transcribe(self._request(), runner=runner)

        self.assertEqual(ctx.exception.stage, Stage.VALIDATE_ARTIFACTS)
        self.assertEqual(self.audio.read_bytes(), b"source-audio")
        self.assertFalse((self.output_dir / "meeting_transcription.txt").exists())

    def test_keep_normalized_false_deletes_norm_after_success(self) -> None:
        runner = FakeRunner()
        result = transcribe(self._request(keep_normalized=False), runner=runner)

        self.assertIsNone(result.normalized_audio_path)
        self.assertFalse((self.output_dir / "meeting_norm16k.wav").exists())
        self.assertTrue((self.output_dir / "meeting_transcription.txt").is_file())

    def test_missing_source_fails_validate_input(self) -> None:
        missing = self.root / "missing.wav"
        with self.assertRaises(TranscriptionError) as ctx:
            transcribe(self._request(audio_path=missing), runner=FakeRunner())
        self.assertEqual(ctx.exception.stage, Stage.VALIDATE_INPUT)

    def test_build_whisper_command_order(self) -> None:
        command = build_whisper_command(
            whisper_cli=Path("whisper-cli"),
            model_path=Path("model.bin"),
            audio_path=Path("a.wav"),
            language="zh",
            threads=4,
            output_base=Path("out/base"),
            outputs=frozenset({ArtifactKind.JSON, ArtifactKind.TXT}),
        )
        self.assertEqual(command[0], "whisper-cli")
        self.assertIn("--output-json", command)
        self.assertIn("--output-txt", command)

    def test_runner_raise_during_normalize_keeps_normalize_stage(self) -> None:
        runner = FakeRunner(raise_on_normalize=FileNotFoundError("ffmpeg missing"))

        with self.assertRaises(TranscriptionError) as ctx:
            transcribe(self._request(), runner=runner, on_progress=self._on_progress)

        self.assertEqual(ctx.exception.stage, Stage.NORMALIZE)
        self.assertIsInstance(ctx.exception.cause, FileNotFoundError)
        self.assertEqual(self.audio.read_bytes(), b"source-audio")
        self.assertIn((Stage.NORMALIZE, ProgressStatus.FAILED), self.events)
        self.assertEqual(len(runner.calls), 1)

    def test_runner_raise_during_whisper_keeps_transcribe_stage(self) -> None:
        runner = FakeRunner(raise_on_whisper=PermissionError("whisper-cli not executable"))

        with self.assertRaises(TranscriptionError) as ctx:
            transcribe(self._request(), runner=runner, on_progress=self._on_progress)

        self.assertEqual(ctx.exception.stage, Stage.TRANSCRIBE)
        self.assertIsInstance(ctx.exception.cause, PermissionError)
        self.assertEqual(self.audio.read_bytes(), b"source-audio")
        self.assertFalse((self.output_dir / "meeting_norm16k.wav").exists())
        self.assertIn((Stage.TRANSCRIBE, ProgressStatus.FAILED), self.events)

    def test_traversal_stem_rejected_before_runner(self) -> None:
        runner = FakeRunner()
        with self.assertRaises(TranscriptionError) as ctx:
            transcribe(self._request(stem="../escape"), runner=runner)

        self.assertEqual(ctx.exception.stage, Stage.VALIDATE_INPUT)
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.root / "escape_norm16k.wav").exists())
        self.assertEqual(self.audio.read_bytes(), b"source-audio")

    def test_absolute_stem_rejected_before_runner(self) -> None:
        runner = FakeRunner()
        absolute_stem = str(self.root / "abs-stem")
        with self.assertRaises(TranscriptionError) as ctx:
            transcribe(self._request(stem=absolute_stem), runner=runner)

        self.assertEqual(ctx.exception.stage, Stage.VALIDATE_INPUT)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.audio.read_bytes(), b"source-audio")


if __name__ == "__main__":
    unittest.main()
