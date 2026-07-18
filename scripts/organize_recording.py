#!/usr/bin/env python3
"""Prepare a local recording for transcription via the Output Manager.

Interactive time confirmation stays here (stderr). Local sources are referenced
in place — they are never copied into the meeting workspace.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.output_manager import (
    SourceDescriptor,
    SourceKind,
    WorkspaceError,
    create_workspace,
    plan_workspace,
)


STANDARD_PREFIX = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{4})(?:_|$)"
)


@dataclass(frozen=True)
class RecordingTime:
    value: datetime
    source: str
    requires_confirmation: bool


def parse_standard_prefix(stem: str) -> Optional[RecordingTime]:
    match = STANDARD_PREFIX.match(stem)
    if not match:
        return None
    try:
        value = datetime.strptime(
            f"{match.group('date')} {match.group('time')}", "%Y-%m-%d %H%M"
        )
    except ValueError:
        return None
    return RecordingTime(value, "filename", False)


def parse_datetime(value: str) -> Optional[datetime]:
    candidate = value.strip().replace("Z", "+00:00")
    if not candidate:
        return None
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def ffprobe_creation_time(audio_file: Path) -> Optional[datetime]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format_tags=creation_time",
                "-of",
                "json",
                str(audio_file),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout)
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError):
        return None
    return parse_datetime(payload.get("format", {}).get("tags", {}).get("creation_time", ""))


def detect_recording_time(audio_file: Path) -> RecordingTime:
    from_name = parse_standard_prefix(audio_file.stem)
    if from_name:
        return from_name

    from_metadata = ffprobe_creation_time(audio_file)
    if from_metadata:
        return RecordingTime(from_metadata, "audio metadata", False)

    stat = audio_file.stat()
    birth_time = getattr(stat, "st_birthtime", None)
    if birth_time:
        return RecordingTime(datetime.fromtimestamp(birth_time), "file birth time", False)

    return RecordingTime(
        datetime.fromtimestamp(stat.st_mtime), "file modification time", True
    )


def ask_for_recording_time(detected: RecordingTime) -> RecordingTime:
    print(
        f"[*] Detected recording time: {detected.value:%Y-%m-%d %H:%M} "
        f"({detected.source})",
        file=sys.stderr,
    )
    # Prompt must go to stderr: callers capture stdout as JSON.
    print(
        "Press Enter to accept, or enter YYYY-MM-DD HH:MM: ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    response = sys.stdin.readline().rstrip("\n").strip()
    if not response:
        return detected
    try:
        replacement = datetime.strptime(response, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError("Date/time must use YYYY-MM-DD HH:MM") from exc
    return RecordingTime(replacement, "user input", False)


def prepare_recording(
    audio_file: Path, records_dir: Path, assume_yes: bool = False
) -> dict[str, str]:
    audio_file = audio_file.expanduser().resolve()
    records_dir = records_dir.expanduser().resolve()
    if not audio_file.is_file():
        raise FileNotFoundError(f"Recording not found: {audio_file}")

    detected = detect_recording_time(audio_file)
    if detected.source != "filename" and not assume_yes:
        detected = ask_for_recording_time(detected)
    elif detected.requires_confirmation and assume_yes:
        print(
            "[!] Using file modification time because no stronger timestamp was found.",
            file=sys.stderr,
        )

    source = SourceDescriptor(
        kind=SourceKind.LOCAL_REFERENCE,
        path=audio_file,
        original_name=audio_file.name,
    )
    plan = plan_workspace(records_dir, source, detected.value)
    workspace = create_workspace(plan)

    return {
        "meeting_dir": str(workspace.workspace_dir),
        "audio_file": str(workspace.audio_path),
        "stem": workspace.transcript_stem,
        "recorded_at": detected.value.isoformat(timespec="minutes"),
        "date_source": detected.source,
        "source_kind": workspace.source_kind.value,
        "source_original_path": str(workspace.source_original_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio_file", type=Path)
    parser.add_argument("--records-dir", required=True, type=Path)
    parser.add_argument("--yes", action="store_true", help="accept detected time")
    args = parser.parse_args()
    try:
        result = prepare_recording(args.audio_file, args.records_dir, args.yes)
    except (OSError, ValueError, WorkspaceError) as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
