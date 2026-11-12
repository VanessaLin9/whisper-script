#!/usr/bin/env bash
# Real native PCM conversion tests; no microphone, system capture, or TCC prompt.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$(uname -s)" != Darwin ] || ! command -v xcrun >/dev/null 2>&1; then
    echo "Skipping native recorder tests (macOS and Command Line Tools required)"
    exit 0
fi
mkdir -p "$ROOT/.local/recorder" "$ROOT/.local/swift-cache"
xcrun swiftc -parse-as-library -swift-version 5 -D RECORDER_TESTS \
    -target "$(uname -m)-apple-macosx14.0" \
    -module-cache-path "$ROOT/.local/swift-cache" \
    "$ROOT/native/recorder/MeetingRecorder.swift" "$ROOT/tests/recorder_audio.swift" \
    -o "$ROOT/.local/recorder/audio-tests"
"$ROOT/.local/recorder/audio-tests"
