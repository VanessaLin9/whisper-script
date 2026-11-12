#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$ROOT/.local/recorder" "$ROOT/.local/swift-cache"
# A stable binary path and embedded usage description give TCC a consistent identity.
xcrun swiftc -parse-as-library -swift-version 5 -O \
  -target "$(uname -m)-apple-macosx14.0" \
  -module-cache-path "$ROOT/.local/swift-cache" \
  "$ROOT/native/recorder/MeetingRecorder.swift" \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist \
  -Xlinker "$ROOT/native/recorder/Info.plist" \
  -o "$ROOT/.local/recorder/meeting-recorder"
codesign --force --sign - --identifier local.whisper-script.meeting-recorder \
  "$ROOT/.local/recorder/meeting-recorder"
