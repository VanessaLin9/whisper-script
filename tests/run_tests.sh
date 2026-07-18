#!/usr/bin/env bash
# Run offline regression tests (no network, mic, real models, clipboard, or Finder).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

chmod +x tests/fake_bin/ffmpeg tests/fake_bin/whisper-cli \
    tests/test_shell_workflows.sh tests/run_tests.sh \
    scripts/*.sh scripts/lib/common.sh 2>/dev/null || true

echo "Running offline whisper-script regression tests..."
PYTHONPATH="$ROOT" python3 tests/test_organize_recording.py -v
PYTHONPATH="$ROOT" python3 tests/test_transcription_core.py -v
PYTHONPATH="$ROOT" python3 tests/test_transcription_cli.py -v
bash tests/test_shell_workflows.sh
bash tests/test_transcribe_english_wrapper.sh
