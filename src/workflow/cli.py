"""Phase 1 CLI: public Google Drive link → workspace → local transcription.

Progress / diagnostics always go to stderr. With ``--json``, every terminal
result (including argument validation failures) is exactly one JSON object on
stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from src.output_manager import default_outputs_arg
from src.transcription.types import ArtifactKind

from .drive_transcribe import DriveTranscribeWorkflow
from .types import (
    DriveTranscribeRequest,
    DriveTranscribeResult,
    WorkflowError,
    WorkflowProgressEvent,
    WorkflowStage,
)

CLI_STAGE = "cli"


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that can emit JSON validation errors on stdout."""

    def __init__(self, *args: object, json_mode: bool = False, **kwargs: object) -> None:
        self.json_mode = json_mode
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        if self.json_mode:
            print(
                json.dumps(
                    {"ok": False, "stage": CLI_STAGE, "message": message},
                    ensure_ascii=False,
                )
            )
        self.exit(2)


def _parse_outputs(raw: str) -> frozenset[ArtifactKind]:
    kinds: set[ArtifactKind] = set()
    for part in raw.split(","):
        token = part.strip().lower()
        if not token:
            continue
        try:
            kinds.add(ArtifactKind(token))
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"Unsupported output artifact: {token!r}"
            ) from exc
    if not kinds:
        raise argparse.ArgumentTypeError("At least one output artifact is required")
    return frozenset(kinds)


def _parse_meeting_time(raw: str) -> datetime:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "meeting-time must be ISO-8601 (e.g. 2026-07-18T12:34:00+08:00)"
        ) from exc
    if value.tzinfo is None:
        value = value.astimezone()
    return value


def build_parser(*, json_mode: bool = False) -> _ArgumentParser:
    parser = _ArgumentParser(
        description=(
            "Transcribe a public Google Drive audio link into a meeting workspace "
            "(no GUI / OAuth)"
        ),
        json_mode=json_mode,
    )
    parser.add_argument(
        "drive_url",
        nargs="?",
        help="Public Google Drive sharing URL",
    )
    parser.add_argument(
        "--url",
        dest="drive_url_opt",
        help="Public Google Drive sharing URL (alternative to positional)",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="Root directory for meeting workspaces (e.g. MEETING_RECORDS_DIR)",
    )
    parser.add_argument("--language", required=True)
    parser.add_argument("--model", required=True, help="Model name for metadata")
    parser.add_argument("--model-path", required=True, type=Path)
    parser.add_argument("--whisper-cli", required=True, type=Path)
    parser.add_argument("--threads", required=True, type=int)
    parser.add_argument(
        "--outputs",
        default=default_outputs_arg(),
        type=_parse_outputs,
        help="Comma-separated artifacts: txt,srt,vtt,json (default: txt,srt,json; TXT always kept)",
    )
    parser.add_argument("--ffmpeg", type=Path, default=Path("ffmpeg"))
    parser.add_argument(
        "--normalize",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--keep-normalized",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--meeting-time",
        type=_parse_meeting_time,
        default=None,
        help="Optional ISO-8601 meeting time used in the workspace folder name",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON result on stdout (progress stays on stderr)",
    )
    parser.add_argument(
        "--quiet-progress",
        action="store_true",
        help="Do not print workflow stage progress to stderr",
    )
    return parser


def request_from_args(args: argparse.Namespace) -> DriveTranscribeRequest:
    positional = (args.drive_url or "").strip()
    optional = (args.drive_url_opt or "").strip()
    if positional and optional and positional != optional:
        raise ValueError("Conflicting Drive URL: positional and --url differ")
    url = optional or positional
    if not url:
        raise ValueError("Drive URL is required (positional or --url)")
    return DriveTranscribeRequest(
        drive_url=url,
        output_root=args.output_root,
        language=args.language,
        model=args.model,
        model_path=args.model_path,
        whisper_cli=args.whisper_cli,
        threads=args.threads,
        meeting_time=args.meeting_time,
        outputs=args.outputs,
        normalize=args.normalize,
        keep_normalized=args.keep_normalized,
        ffmpeg=args.ffmpeg,
    )


def _progress_printer(event: WorkflowProgressEvent) -> None:
    detail = f" ({event.detail})" if event.detail else ""
    print(
        f"[workflow:{event.stage.value}] {event.status.value}{detail}",
        file=sys.stderr,
    )


def _result_payload(result: DriveTranscribeResult) -> dict[str, object]:
    artifacts = {
        kind.value: str(path) for kind, path in sorted(
            result.artifacts.items(), key=lambda item: item[0].value
        )
    }
    return {
        "ok": True,
        "workspace_dir": str(result.workspace_dir),
        "raw_audio_path": str(result.raw_audio_path),
        "raw_transcript_path": str(result.raw_transcript_path),
        "normalized_audio_path": (
            str(result.normalized_audio_path)
            if result.normalized_audio_path is not None
            else None
        ),
        "artifacts": artifacts,
        "download_filename": result.download_filename,
        "file_id": result.file_id,
        "stem": result.stem,
        "language": result.language,
        "model": result.model,
        "meeting_time": result.meeting_time.isoformat(),
    }


def _error_payload(exc: WorkflowError) -> dict[str, object]:
    return {
        "ok": False,
        "stage": exc.stage.value,
        "message": exc.message,
    }


def _print_human_success(result: DriveTranscribeResult) -> None:
    print("[*] Drive transcription OK")
    print(f"    workspace: {result.workspace_dir}")
    print(f"    raw_audio: {result.raw_audio_path}")
    print(f"    raw_transcript: {result.raw_transcript_path}")
    if result.normalized_audio_path is not None:
        print(f"    normalized: {result.normalized_audio_path}")
    for kind in sorted(result.artifacts, key=lambda item: item.value):
        print(f"    {kind.value}: {result.artifacts[kind]}")


def main(
    argv: list[str] | None = None,
    *,
    workflow: DriveTranscribeWorkflow | None = None,
) -> int:
    argv_list = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in argv_list
    parser = build_parser(json_mode=json_mode)
    args = parser.parse_args(argv_list)
    # Keep parser JSON mode aligned even if flag order was unusual.
    parser.json_mode = bool(args.json)
    try:
        request = request_from_args(args)
    except ValueError as exc:
        parser.error(str(exc))

    on_progress = None if args.quiet_progress else _progress_printer
    runner = workflow or DriveTranscribeWorkflow()

    try:
        result = runner.run(request, on_progress=on_progress)
    except WorkflowError as exc:
        print(
            f"[!] Workflow failed at stage={exc.stage.value}: {exc.message}",
            file=sys.stderr,
        )
        if args.json:
            print(json.dumps(_error_payload(exc), ensure_ascii=False))
        return 1
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        print(f"[!] Unexpected workflow failure: {exc}", file=sys.stderr)
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "stage": WorkflowStage.TRANSCRIBE.value,
                        "message": str(exc) or exc.__class__.__name__,
                    },
                    ensure_ascii=False,
                )
            )
        return 1

    if args.json:
        print(json.dumps(_result_payload(result), ensure_ascii=False))
    else:
        _print_human_success(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
