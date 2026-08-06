"""Interactive controller for the meeting transcript -> Notion pipeline.

The controller owns deterministic orchestration and state. Semantic transcript
cleaning and Notion authoring remain explicit hand-off stages for the matching
Codex skills; this module must not guess terminology or write Notion content.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

from src.postprocessing.preparer import prepare_file


DEFAULT_MEETING_ROOT = Path("/Users/user/MeetingRecords")
FOLDER_PATTERN = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{4})_(?P<id>[^/]+)$"
)
SRT_TIMESTAMP_PATTERN = re.compile(
    r"^\d{2}:\d{2}:\d{2},\d{3}\s+-->\s+(\d{2}:\d{2}:\d{2},\d{3})$"
)

PROMPT_PROFILES: dict[str, dict[str, str]] = {
    "inno": {
        "label": "inno（Inno Group／AI Team）",
        "source": "39ddf30c-c71c-81b6-a658-f0ec07fd7e36",
    },
    "whisper": {
        "label": "whisper（土木相關）",
        "source": "whisper-3a3df30c-c71c-8151-95bad39aeb1eb6e1",
    },
    "new": {
        "label": "尚未建立（只產生待補提示詞的 state）",
        "source": "not-registered",
    },
}

PIPELINE_MODES = {
    "full": "預清洗 + 產生技能交接 state",
    "preclean": "只執行預清洗",
    "handoff": "不預清洗，只產生技能交接 state",
}


class PipelineError(RuntimeError):
    """Expected, user-actionable pipeline error."""


@dataclass(frozen=True)
class MeetingCandidate:
    folder: Path
    meeting_id: str
    meeting_date: str
    start_time: str
    raw_transcript: Path
    srt: Path | None
    prepared: Path
    cleaned: Path
    duration_seconds: float | None = None

    @property
    def stem(self) -> str:
        suffix = "_transcription.txt"
        return self.raw_transcript.name[: -len(suffix)]

    @property
    def display(self) -> str:
        end = ""
        if self.duration_seconds is not None:
            end = f"（約 {format_duration(self.duration_seconds)}）"
        status = []
        if self.prepared.exists():
            status.append("prepared")
        if self.cleaned.exists():
            status.append("cleaned")
        status_text = ", ".join(status) if status else "raw only"
        return (
            f"{self.meeting_date} {self.start_time} #{self.meeting_id} "
            f"[{status_text}] {self.raw_transcript.name} {end}"
        )


def format_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    remaining = int(round(seconds - minutes * 60))
    if remaining == 60:
        minutes += 1
        remaining = 0
    return f"{minutes} 分 {remaining} 秒"


def _parse_timestamp(value: str) -> float:
    hours, minutes, seconds_ms = value.split(":")
    seconds, millis = seconds_ms.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def read_srt_duration(path: Path | None) -> float | None:
    if path is None or not path.is_file():
        return None
    last_end: float | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = SRT_TIMESTAMP_PATTERN.match(line.strip())
        if match:
            last_end = _parse_timestamp(match.group(1))
    return last_end


def _candidate_from_folder(folder: Path) -> MeetingCandidate | None:
    match = FOLDER_PATTERN.match(folder.name)
    if not match:
        return None
    raw_files = sorted(
        path
        for path in folder.glob("*_transcription.txt")
        if not path.name.endswith("_prepared.txt")
        and not path.name.endswith("_cleaned.txt")
    )
    if not raw_files:
        return None
    if len(raw_files) > 1:
        raise PipelineError(
            f"Multiple raw transcript files in {folder}: "
            + ", ".join(path.name for path in raw_files)
        )
    raw = raw_files[0]
    stem = raw.name[: -len("_transcription.txt")]
    srt = folder / f"{stem}_transcription.srt"
    return MeetingCandidate(
        folder=folder,
        meeting_id=match.group("id"),
        meeting_date=match.group("date"),
        start_time=match.group("time")[:2] + ":" + match.group("time")[2:],
        raw_transcript=raw,
        srt=srt if srt.is_file() else None,
        prepared=folder / f"{stem}_transcription_prepared.txt",
        cleaned=folder / f"{stem}_transcription_cleaned.txt",
        duration_seconds=read_srt_duration(srt if srt.is_file() else None),
    )


def discover_meetings(root: Path, *, date_filter: str | None = None) -> list[MeetingCandidate]:
    """Discover timestamped meeting workspaces without modifying them."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise PipelineError(f"Meeting records root does not exist: {root}")
    candidates: list[MeetingCandidate] = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir():
            continue
        candidate = _candidate_from_folder(folder)
        if candidate is None:
            continue
        if date_filter and candidate.meeting_date != date_filter:
            continue
        candidates.append(candidate)
    candidates.sort(key=lambda item: (item.meeting_date, item.start_time, item.meeting_id), reverse=True)
    return candidates


def select_meeting(candidates: list[MeetingCandidate], selection: str) -> MeetingCandidate:
    if not candidates:
        raise PipelineError("No meeting candidates found")
    selection = selection.strip()
    if selection.isdigit():
        index = int(selection) - 1
        if 0 <= index < len(candidates):
            return candidates[index]
    matches = [candidate for candidate in candidates if candidate.meeting_id == selection]
    if len(matches) == 1:
        return matches[0]
    matches = [
        candidate
        for candidate in candidates
        if f"{candidate.meeting_date}_{candidate.start_time.replace(':', '')}" in selection
    ]
    if len(matches) == 1:
        return matches[0]
    raise PipelineError(f"Cannot uniquely select meeting: {selection}")


def choose_from_list(
    title: str,
    options: list[str],
    *,
    input_fn: Callable[[str], str] = input,
) -> int:
    print(title)
    for index, option in enumerate(options, start=1):
        print(f"  {index}. {option}")
    answer = input_fn("請輸入編號：").strip()
    if not answer.isdigit() or not 1 <= int(answer) <= len(options):
        raise PipelineError(f"Invalid choice: {answer}")
    return int(answer) - 1


def choose_prompt_profile(
    *,
    input_fn: Callable[[str], str] = input,
) -> str:
    keys = list(PROMPT_PROFILES)
    index = choose_from_list(
        "選擇提示詞：", [PROMPT_PROFILES[key]["label"] for key in keys], input_fn=input_fn
    )
    return keys[index]


def choose_mode(*, input_fn: Callable[[str], str] = input) -> str:
    keys = list(PIPELINE_MODES)
    index = choose_from_list("選擇流程：", [PIPELINE_MODES[key] for key in keys], input_fn=input_fn)
    return keys[index]


def _state_path(candidate: MeetingCandidate) -> Path:
    return candidate.folder / f"{candidate.stem}_pipeline_state.json"


def build_state(candidate: MeetingCandidate, prompt_profile: str, mode: str) -> dict:
    if prompt_profile not in PROMPT_PROFILES:
        raise PipelineError(f"Unknown prompt profile: {prompt_profile}")
    if mode not in PIPELINE_MODES:
        raise PipelineError(f"Unknown pipeline mode: {mode}")
    end_time = None
    if candidate.duration_seconds is not None:
        start = datetime.strptime(
            f"{candidate.meeting_date} {candidate.start_time}", "%Y-%m-%d %H:%M"
        )
        end_time = (start + timedelta(seconds=candidate.duration_seconds)).strftime("%H:%M")
    prepared_done = candidate.prepared.exists()
    cleaned_done = candidate.cleaned.exists()
    return {
        "schema_version": 1,
        "meeting": {
            "meeting_id": candidate.meeting_id,
            "date": candidate.meeting_date,
            "start_time": candidate.start_time,
            "end_time": end_time,
            "timezone": "Asia/Taipei",
            "folder": str(candidate.folder),
        },
        "prompt_profile": {
            "name": prompt_profile,
            "label": PROMPT_PROFILES[prompt_profile]["label"],
            "source": PROMPT_PROFILES[prompt_profile]["source"],
        },
        "artifacts": {
            "raw_transcript": str(candidate.raw_transcript),
            "srt": str(candidate.srt) if candidate.srt else None,
            "prepared": str(candidate.prepared),
            "cleaned": str(candidate.cleaned),
        },
        "pipeline": {
            "mode": mode,
            "stages": {
                "discover": "completed",
                "preclean": "completed" if prepared_done else "pending",
                "clean_transcript": "completed" if cleaned_done else "pending",
                "notion_worklog": "pending",
            },
            "next_stage": "notion_worklog" if cleaned_done else "clean_transcript",
        },
        "handoff": {
            "clean_skill": "clean-meeting-transcripts",
            "worklog_skill": "standup-worklog",
            "notion_write_requires_confirmation": True,
        },
    }


def write_state(path: Path, state: dict, *, force: bool = False) -> Path:
    path = path.resolve()
    if path.exists() and not force:
        raise FileExistsError(f"Pipeline state already exists: {path}; use --force-state to refresh")
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def run_preclean(candidate: MeetingCandidate) -> dict:
    if candidate.prepared.exists():
        return {"status": "existing", "path": str(candidate.prepared)}
    prepared, manifest, stats = prepare_file(candidate.raw_transcript, candidate.prepared)
    return {
        "status": "created",
        "path": str(prepared),
        "manifest": str(manifest),
        "stats": asdict(stats),
    }


def run_pipeline(
    candidate: MeetingCandidate,
    *,
    prompt_profile: str,
    mode: str,
    force_state: bool = False,
) -> tuple[dict, Path, dict]:
    if mode in {"full", "preclean"}:
        preclean_result = run_preclean(candidate)
    else:
        preclean_result = {"status": "skipped", "path": str(candidate.prepared)}
    state = build_state(candidate, prompt_profile, mode)
    state["pipeline"]["preclean_result"] = preclean_result
    if mode == "preclean":
        state["pipeline"]["next_stage"] = "clean_transcript"
    state_path = _state_path(candidate)
    state["pipeline"]["state_path"] = str(state_path)
    write_state(state_path, state, force=force_state)
    return state, state_path, preclean_result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Interactive meeting transcript pipeline controller")
    parser.add_argument("--root", type=Path, default=Path(os.getenv("MEETING_RECORDS_DIR", DEFAULT_MEETING_ROOT)))
    parser.add_argument("--date", dest="date_filter", help="Only list meetings on YYYY-MM-DD")
    parser.add_argument("--meeting", help="Meeting list number, id, or YYYY-MM-DD_HHMM selector")
    parser.add_argument("--prompt-profile", choices=tuple(PROMPT_PROFILES), help="Skip prompt question")
    parser.add_argument("--mode", choices=tuple(PIPELINE_MODES), help="Skip workflow question")
    parser.add_argument("--force-state", action="store_true", help="Refresh an existing pipeline state")
    parser.add_argument("--yes", action="store_true", help="Skip final confirmation")
    parser.add_argument("--json", action="store_true", help="Print only the final state JSON")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        candidates = discover_meetings(args.root, date_filter=args.date_filter)
        if not candidates:
            raise PipelineError(f"No meeting folders found under {args.root}")
        if args.meeting:
            candidate = select_meeting(candidates, args.meeting)
        else:
            index = choose_from_list("選擇要處理的會議：", [item.display for item in candidates])
            candidate = candidates[index]
        prompt_profile = args.prompt_profile or choose_prompt_profile()
        mode = args.mode or choose_mode()
        print(f"\n已選擇：{candidate.display}")
        print(f"提示詞：{PROMPT_PROFILES[prompt_profile]['label']}")
        print(f"流程：{PIPELINE_MODES[mode]}")
        if not args.yes and input("確認執行？[y/N]：").strip().lower() not in {"y", "yes"}:
            print("已取消")
            return 0
        state, state_path, preclean_result = run_pipeline(
            candidate,
            prompt_profile=prompt_profile,
            mode=mode,
            force_state=args.force_state,
        )
        if args.json:
            print(json.dumps(state, ensure_ascii=False, indent=2))
        else:
            print(f"預清洗：{preclean_result['status']}")
            print(f"Pipeline state：{state_path}")
            print(f"下一階段：{state['pipeline']['next_stage']}")
        return 0
    except (PipelineError, FileExistsError, FileNotFoundError, ValueError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
