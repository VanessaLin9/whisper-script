"""Split a meeting SRT into core slices with surrounding context.

Cuts stay on cue boundaries. Each cue belongs to one core. Context is copied
from neighboring cues so the cleaner can read across the seam, and merge keeps
only the cores.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.desktop.editing import TIMESTAMP, parse_srt
from src.desktop.jobs import file_sha256
from src.output_manager.workspace import exclusive_write_text


CORE_MS = 10 * 60 * 1000
CONTEXT_MS = 45 * 1000


class SegmentationError(ValueError):
    """The timeline cannot be split safely."""


def format_ms(value: int) -> str:
    value = max(0, value)
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def cue_bounds(cue: dict) -> tuple[int, int]:
    match = TIMESTAMP.fullmatch(cue.get("time", ""))
    if not match:
        raise SegmentationError("沒有可解析的時間軸，無法切段。")
    return _clock_ms(match.group(1)), _clock_ms(match.group(2))


def _clock_ms(value: str) -> int:
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(",")
    return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int(millis)


def load_timeline(path: Path) -> list[dict]:
    if not path.is_file():
        raise SegmentationError("沒有可解析的時間軸，無法切段。")
    try:
        cues = parse_srt(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SegmentationError("沒有可解析的時間軸，無法切段。") from exc
    timed = []
    for cue in cues:
        start, end = cue_bounds(cue)
        if end < start:
            raise SegmentationError("時間軸的結束時間早於開始時間。")
        timed.append({**cue, "start": start, "end": end})
    if not timed:
        raise SegmentationError("沒有可解析的時間軸，無法切段。")
    return timed


def plan_segments(cues: list[dict]) -> list[dict]:
    """Return core ranges of about ten minutes, plus 45 seconds of context."""
    segments = []
    index = 0
    while index < len(cues):
        core_start = cues[index]["start"]
        end_index = index
        while end_index + 1 < len(cues) and cues[end_index + 1]["end"] - core_start <= CORE_MS:
            end_index += 1
        core = cues[index:end_index + 1]
        start_ms = core[0]["start"]
        end_ms = core[-1]["end"]
        context_start = max(0, start_ms - CONTEXT_MS)
        context_end = end_ms + CONTEXT_MS
        before = [cue for cue in cues if cue["end"] > context_start and cue["start"] < start_ms]
        after = [cue for cue in cues if cue["start"] >= end_ms and cue["start"] < context_end]
        segments.append({
            "core": core,
            "before": before,
            "after": after,
            "core_start": start_ms,
            "core_end": end_ms,
            "context_start": context_start,
            "context_end": context_end,
        })
        index = end_index + 1
    return segments


def render_segment(segment: dict) -> str:
    parts = []
    if segment["before"]:
        parts.append("[前段上下文｜僅供理解，不要輸出]\n" + _cue_text(segment["before"]))
    parts.append("[核心｜必須清洗並輸出]\n" + _cue_text(segment["core"]))
    if segment["after"]:
        parts.append("[後段上下文｜僅供理解，不要輸出]\n" + _cue_text(segment["after"]))
    return "\n\n".join(parts) + "\n"


def core_transcript(segments: list[dict]) -> str:
    return "\n".join(_cue_text(segment["core"]) for segment in segments)


def write_job_segments(srt_path: Path, directory: Path, outbox_dir: Path) -> dict:
    cues = load_timeline(srt_path)
    segments = plan_segments(cues)
    directory.mkdir(parents=True, exist_ok=True)
    items = []
    for order, segment in enumerate(segments, start=1):
        name = f"seg-{order:02d}.txt"
        path = directory / name
        exclusive_write_text(path, render_segment(segment))
        items.append({
            "id": f"seg-{order:02d}",
            "output_order": order,
            "path": str(path),
            "sha256": file_sha256(path),
            "core_start": format_ms(segment["core_start"]),
            "core_end": format_ms(segment["core_end"]),
            "context_start": format_ms(segment["context_start"]),
            "context_end": format_ms(segment["context_end"]),
            "outbox_path": str(outbox_dir / name),
        })
    manifest_path = directory / "segments.json"
    exclusive_write_text(manifest_path, json.dumps({"items": items}, ensure_ascii=False, indent=2) + "\n")
    return {
        "directory": str(directory),
        "manifest_path": str(manifest_path),
        "sha256": file_sha256(manifest_path),
        "items": items,
    }


def _cue_text(cues: list[dict]) -> str:
    return "\n".join(cue["text"] for cue in cues)
