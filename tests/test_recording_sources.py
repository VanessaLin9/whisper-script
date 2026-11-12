"""Exercise shell source routing and capture retention without audio permissions."""
import json
import os
from pathlib import Path
import pty
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
FAKE_RECORDER = '''#!/usr/bin/env python3
import os, signal, sys, time, wave, struct, json
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
mode = os.environ.get("CAPTURE_TEST_MODE", "ok")
if mode == "denied":
    print("System audio permission required; no microphone fallback", file=sys.stderr)
    sys.exit(1)
if "--metadata" in args:
    with open(args["--metadata"], "w") as meta:
        json.dump({"recorded_at": "2026-11-12T15:00:00+08:00"}, meta)
for option in ("--output", "--mic-output"):
    if option not in args: continue
    with wave.open(args[option], "wb") as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(48000)
        samples = ([0] * 4800 if mode == "silent" else [1000, -1000] * 2400)
        audio.writeframes(struct.pack("<4800h", *samples))
if mode == "wait":
    def stop(*_):
        # Simulate asynchronous finalization after the shell's wait is interrupted.
        time.sleep(0.15)
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    while True: time.sleep(0.05)
'''


class RecordingSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "project"
        self.repo.mkdir()
        for folder in ("scripts", "src"):
            shutil.copytree(ROOT / folder, self.repo / folder)
        shutil.copy(ROOT / "env_loader.py", self.repo)
        self.fake = self.root / "bin"
        self.fake.mkdir()
        for name in ("ffmpeg", "whisper-cli"):
            shutil.copy(ROOT / "tests/fake_bin" / name, self.fake / name)
            (self.fake / name).chmod(0o755)
        (self.fake / "open").write_text("#!/bin/sh\nexit 0\n")
        (self.fake / "open").chmod(0o755)
        whisper = self.root / "whisper"
        (whisper / "build/bin").mkdir(parents=True)
        (whisper / "models").mkdir()
        shutil.copy(self.fake / "whisper-cli", whisper / "build/bin/whisper-cli")
        (whisper / "models/ggml-small.bin").write_text("model")
        self.records = self.root / "records"
        self.records.mkdir()
        self.base_env = f"WHISPER_ROOT={whisper}\nMEETING_RECORDS_DIR={self.records}\nPREFERRED_MODEL=small\n"
        recorder = self.repo / ".local/recorder/meeting-recorder"
        recorder.parent.mkdir(parents=True)
        recorder.write_text(FAKE_RECORDER)
        recorder.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.fake}:{os.environ['PATH']}", PYTHONDONTWRITEBYTECODE="1")

    def run_script(self, source="system", mode="ok", extra=None):
        (self.repo / ".env").write_text(self.base_env + f"RECORDING_SOURCE={source}\n")
        env = dict(self.env, CAPTURE_TEST_MODE=mode, **(extra or {}))
        return subprocess.run(["bash", str(self.repo / "scripts/record-meeting.sh")],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, env=env, timeout=15)

    def test_system_success_retains_track_and_only_cleans_own_staging(self):
        stage = self.records / ".incoming"
        stage.mkdir()
        sentinel = stage / "previous.wav"
        sentinel.write_text("keep")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout)
        meta = json.loads(next(self.records.glob("*/source_meta.json")).read_text())
        self.assertTrue(meta["retained_in_workspace"])
        self.assertEqual(meta["recording"]["source"], "system")
        self.assertTrue(meta["recording"]["recorded_at"].endswith("+08:00"))
        self.assertTrue(Path(meta["recording"]["tracks"]["system"]["path"]).exists())
        self.assertEqual(list(stage.iterdir()), [sentinel])
        self.assertIn("Transcription complete", result.stdout)

    @unittest.skipUnless(shutil.which("ffmpeg"), "requires FFmpeg for mixing")
    def test_both_keeps_two_tracks_and_transcribes_mixed_main(self):
        import shlex
        real_ffmpeg = shutil.which("ffmpeg")
        stub = self.fake / "ffmpeg-stub"
        (self.fake / "ffmpeg").rename(stub)
        (self.fake / "ffmpeg").write_text(
            "#!/bin/sh\ncase \" $* \" in\n*amix*) exec " + shlex.quote(real_ffmpeg)
            + ' "$@" ;;\n*) exec ' + shlex.quote(str(stub)) + ' "$@" ;;\nesac\n')
        (self.fake / "ffmpeg").chmod(0o755)
        result = self.run_script(source="both")
        self.assertEqual(result.returncode, 0, result.stdout)
        meta = json.loads(next(self.records.glob("*/source_meta.json")).read_text())
        self.assertEqual(set(meta["recording"]["tracks"]), {"system", "microphone"})
        self.assertEqual(meta["recording"]["recorded_at"], "2026-11-12T15:00:00+08:00")
        self.assertEqual(len(list(self.records.glob("*/*_transcription.txt"))), 1)

    def test_permission_failure_never_falls_back(self):
        result = self.run_script(mode="denied")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no microphone fallback", result.stdout)
        self.assertFalse(list(self.records.glob("*/source_meta.json")))
        self.assertTrue(list((self.records / ".incoming").glob("*.log")))

    def test_silent_capture_is_retained_without_transcription(self):
        result = self.run_script(mode="silent")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No usable signal", result.stdout)
        self.assertTrue(list((self.records / ".incoming").glob("*_system.wav")))
        self.assertFalse(list(self.records.glob("*/source_meta.json")))

    def test_core_failure_keeps_native_tracks_in_both_locations(self):
        result = self.run_script(extra={"FAIL_WHISPER": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(list(self.records.glob("*/*_system.wav")))
        self.assertTrue(list((self.records / ".incoming").glob("*_system.wav")))
        self.assertNotIn("Transcription complete", result.stdout)

    def test_invalid_source_fails_before_recording(self):
        result = self.run_script(source="auto")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be mic, system, or both", result.stdout)
        self.assertFalse((self.records / ".incoming").exists())

    def test_interactive_choice_overrides_configured_default(self):
        (self.repo / ".env").write_text(self.base_env + "RECORDING_SOURCE=mic\n")
        master, slave = pty.openpty()
        log = self.root / "interactive.log"
        try:
            with log.open("w") as output:
                proc = subprocess.Popen(["bash", str(self.repo / "scripts/record-meeting.sh")],
                                        stdin=slave, stdout=output, stderr=subprocess.STDOUT,
                                        text=True, env=self.env)
                os.write(master, b"2\n")
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    try: proc.kill()
                    except (PermissionError, ProcessLookupError): pass
                    self.fail("Interactive timeout: " + log.read_text())
            out = log.read_text()
            self.assertEqual(proc.returncode, 0, out)
            self.assertIn("Selected source: system", out)
        finally:
            os.close(master); os.close(slave)

    def test_interrupt_waits_for_native_finalize_and_term_preserves(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            # Reuse root safely: unique capture names avoid collisions.
            (self.repo / ".env").write_text(self.base_env + "RECORDING_SOURCE=system\n")
            proc = subprocess.Popen(["bash", str(self.repo / "scripts/record-meeting.sh")],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    env=dict(self.env, CAPTURE_TEST_MODE="wait"), start_new_session=True)
            try:
                for _ in range(100):
                    if list((self.records / ".incoming").glob("*_system.wav")): break
                    if proc.poll() is not None: break
                    time.sleep(0.03)
                # Ensure fake recorder has installed its signal handler.
                time.sleep(0.1)
                proc.send_signal(sig)
                out, _ = proc.communicate(timeout=15)
                self.assertEqual(proc.returncode, 0 if sig == signal.SIGINT else 143, out)
                if sig == signal.SIGINT: self.assertIn("Transcription complete", out)
                else: self.assertNotIn("Starting transcription from raw", out)
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate()


if __name__ == "__main__":
    unittest.main()
