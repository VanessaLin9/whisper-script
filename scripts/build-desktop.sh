#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${MEETING_PYTHON:-$(command -v python3)}"
APP="$ROOT/.local/Meeting Desk.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$ROOT/.local/swift-cache"
xcrun swiftc -parse-as-library -swift-version 5 -O \
  -target "$(uname -m)-apple-macosx14.0" \
  -module-cache-path "$ROOT/.local/swift-cache" \
  "$ROOT/desktop/MeetingDesk.swift" -o "$APP/Contents/MacOS/MeetingDesk"
"$PYTHON" - "$APP" "$ROOT" "$PYTHON" <<'PY'
import pathlib, plistlib, sys
app, root, python = sys.argv[1:]
plist = {
    "CFBundleName": "Meeting Desk", "CFBundleDisplayName": "Meeting Desk",
    "CFBundleIdentifier": "local.whisper-script.meeting-desk",
    "CFBundleExecutable": "MeetingDesk", "CFBundlePackageType": "APPL",
    "CFBundleVersion": "1", "CFBundleShortVersionString": "0.1.0",
    "LSMinimumSystemVersion": "14.0", "NSHighResolutionCapable": True,
    "MeetingRepo": root, "MeetingPython": python,
    "CFBundleDocumentTypes": [{"CFBundleTypeName": "Audio", "CFBundleTypeRole": "Viewer",
                               "LSItemContentTypes": ["public.audio", "public.movie"]}],
}
with (pathlib.Path(app) / "Contents/Info.plist").open("wb") as stream:
    plistlib.dump(plist, stream)
print(app)
PY
codesign --force --sign - "$APP"
