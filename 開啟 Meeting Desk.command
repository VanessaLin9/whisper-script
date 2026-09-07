#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$ROOT/.local/Meeting Desk.app"
if [[ ! -x "$APP/Contents/MacOS/MeetingDesk" || "$ROOT/desktop/MeetingDesk.swift" -nt "$APP/Contents/MacOS/MeetingDesk" || "$ROOT/scripts/build-desktop.sh" -nt "$APP/Contents/MacOS/MeetingDesk" ]]; then
    bash "$ROOT/scripts/build-desktop.sh"
fi
open "$APP"
