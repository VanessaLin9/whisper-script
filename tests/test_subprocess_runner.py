#!/usr/bin/env python3
"""Offline tests for streaming / bounded subprocess runners."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

from src.transcription.subprocess_runner import (
    StreamingSubprocessRunner,
    bounded_tail,
)
from src.transcription import ArtifactKind, Stage, TranscriptionError, transcribe
from src.transcription.types import TranscribeRequest


class BoundedTailTests(unittest.TestCase):
    def test_bounded_tail_keeps_suffix(self) -> None:
        self.assertEqual(bounded_tail("abcdef", max_chars=3), "def")
        self.assertEqual(bounded_tail("abc", max_chars=10), "abc")


class StreamingSubprocessRunnerTests(unittest.TestCase):
    def test_streams_progress_and_bounds_retained_tail(self) -> None:
        sink = io.StringIO()
        runner = StreamingSubprocessRunner(max_tail_chars=64, sink=sink)
        # Emit more than the bound so retained diagnostics must truncate.
        script = (
            "import sys\n"
            "for i in range(200):\n"
            "    sys.stdout.write(f'PROGRESS-{i:04d}\\n')\n"
            "    sys.stdout.flush()\n"
            "sys.stderr.write('ERR-TAIL\\n')\n"
            "sys.stderr.flush()\n"
        )
        result = runner.run([sys.executable, "-c", script])

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        live = sink.getvalue()
        self.assertIn("PROGRESS-0000", live)
        self.assertIn("PROGRESS-0199", live)
        self.assertIn("ERR-TAIL", live)
        self.assertLessEqual(len(result.stderr), 64)
        self.assertTrue(result.stderr.endswith("ERR-TAIL\n") or "ERR-TAIL" in result.stderr)
        # Retained buffer must not hold the full live stream.
        self.assertNotIn("PROGRESS-0000", result.stderr)
        self.assertLess(len(result.stderr), len(live))

    def test_failure_diagnostic_observable_via_core(self) -> None:
        sink = io.StringIO()
        runner = StreamingSubprocessRunner(max_tail_chars=256, sink=sink)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio = root / "a.wav"
            audio.write_bytes(b"source")
            model = root / "m.bin"
            model.write_bytes(b"m")
            whisper = root / "whisper-cli"
            whisper.write_text(
                "#!/usr/bin/env bash\n"
                "echo 'whisper progress line' >&2\n"
                "echo 'simulated whisper-cli failure' >&2\n"
                "exit 1\n",
                encoding="utf-8",
            )
            whisper.chmod(0o755)
            request = TranscribeRequest(
                audio_path=audio,
                language="zh",
                model="small",
                model_path=model,
                whisper_cli=whisper,
                threads=1,
                output_dir=root / "out",
                stem="meeting",
                outputs=frozenset({ArtifactKind.TXT, ArtifactKind.SRT}),
                normalize=False,
                artifact_basename="meeting",
            )

            with self.assertRaises(TranscriptionError) as ctx:
                transcribe(request, runner=runner)

            self.assertEqual(ctx.exception.stage, Stage.TRANSCRIBE)
            self.assertIsNotNone(ctx.exception.diagnostic)
            assert ctx.exception.diagnostic is not None
            self.assertIn("simulated whisper-cli failure", ctx.exception.diagnostic)
            self.assertIn("whisper progress line", sink.getvalue())
            self.assertLessEqual(len(ctx.exception.diagnostic), 256)


if __name__ == "__main__":
    unittest.main()
