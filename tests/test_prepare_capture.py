from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare_capture", ROOT / "scripts/prepare_capture.py")
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


def wav(path, values):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(struct.pack(f"<{len(values)}h", *values))


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_initial_silence_is_allowed_and_system_copy_is_exact(self):
        source, output = self.root / "system.wav", self.root / "main.wav"
        wav(source, [0] * 48000 + [1000, -1000] * 2400)
        result = capture.prepare(source, None, output)
        self.assertEqual(source.read_bytes(), output.read_bytes())
        self.assertEqual(result["tracks"]["system"]["frames"], 52800)
        with self.assertRaises(FileExistsError):
            capture.prepare(source, None, output)

    def test_header_silence_and_truncation_rejected(self):
        source = self.root / "bad.wav"
        for samples in ([], [0] * 48000, [1] * 48000):
            wav(source, samples)
            with self.assertRaises(ValueError):
                capture.validate_track(source)
        wav(source, [1000] * 48000)
        source.write_bytes(source.read_bytes()[:-20])
        with self.assertRaises(ValueError):
            capture.validate_track(source)

    @unittest.skipUnless(shutil.which("ffmpeg"), "requires FFmpeg for real mixing")
    def test_both_mixes_aligned_tracks_and_retains_provenance(self):
        system, mic, output = [self.root / name for name in ("system.wav", "mic.wav", "main.wav")]
        wav(system, [1000] * 4800 + [0] * 4800)
        wav(mic, [0] * 4800 + [2000] * 4800)
        result = capture.prepare(system, mic, output)
        with wave.open(str(output), "rb") as audio:
            samples = struct.unpack("<9600h", audio.readframes(9600))
        self.assertAlmostEqual(samples[100], 500, delta=2)
        self.assertAlmostEqual(samples[5000], 1000, delta=2)
        manifest = self.root / "capture.json"
        manifest.write_text(json.dumps(result))
        workspace = self.root / "meeting"
        workspace.mkdir()
        meta = workspace / "source_meta.json"
        meta.write_text('{"retained_in_workspace": true, "other": 7}')
        capture.retain(manifest, workspace, "meeting", "2026-11-12T15:00:00+08:00")
        saved = json.loads(meta.read_text())
        self.assertEqual(saved["other"], 7)
        self.assertTrue(saved["retained_in_workspace"])
        self.assertEqual((workspace / "meeting_microphone.wav").read_bytes(), mic.read_bytes())
        with self.assertRaises(FileExistsError):
            capture.retain(manifest, workspace, "meeting", "unused")

    def test_silent_mic_and_invalid_gains_do_not_produce_main(self):
        system, mic, output = [self.root / name for name in ("system.wav", "mic.wav", "main.wav")]
        wav(system, [1000] * 4800)
        wav(mic, [0] * 4800)
        with self.assertRaises(ValueError):
            capture.prepare(system, mic, output)
        for gain in (0, -1, float("nan"), float("inf"), 11):
            with self.assertRaises(ValueError):
                capture.prepare(system, None, output, gain)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
