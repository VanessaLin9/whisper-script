"""Non-interactive CLI entry for the local transcription core.

Shell wrappers and automation call this module; it does not prompt, copy to
the clipboard, or open Finder.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core import transcribe
from .types import (
    ArtifactKind,
    ProgressEvent,
    ProgressStatus,
    Stage,
    TranscribeRequest,
    TranscriptionError,
)


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one local single-file transcription via the reusable core",
    )
    parser.add_argument("--audio", required=True, type=Path, help="Local audio file path")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--stem", required=True, help="Single filename component for outputs")
    parser.add_argument("--language", required=True)
    parser.add_argument("--model", required=True, help="Model name for metadata (e.g. small)")
    parser.add_argument("--model-path", required=True, type=Path)
    parser.add_argument("--whisper-cli", required=True, type=Path)
    parser.add_argument("--threads", required=True, type=int)
    parser.add_argument(
        "--outputs",
        default="txt,srt,vtt,json",
        type=_parse_outputs,
        help="Comma-separated artifacts: txt,srt,vtt,json",
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
        "--quiet-progress",
        action="store_true",
        help="Do not print stage progress to stderr",
    )
    return parser


def _progress_printer(event: ProgressEvent) -> None:
    detail = f" ({event.detail})" if event.detail else ""
    print(f"[core:{event.stage.value}] {event.status.value}{detail}", file=sys.stderr)


def request_from_args(args: argparse.Namespace) -> TranscribeRequest:
    return TranscribeRequest(
        audio_path=args.audio,
        language=args.language,
        model=args.model,
        model_path=args.model_path,
        whisper_cli=args.whisper_cli,
        threads=args.threads,
        output_dir=args.output_dir,
        stem=args.stem,
        outputs=args.outputs,
        normalize=args.normalize,
        keep_normalized=args.keep_normalized,
        ffmpeg=args.ffmpeg,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    request = request_from_args(args)
    on_progress = None if args.quiet_progress else _progress_printer

    try:
        result = transcribe(request, on_progress=on_progress)
    except TranscriptionError as exc:
        print(f"[!] Transcription failed at stage={exc.stage.value}: {exc.message}", file=sys.stderr)
        if exc.exit_code is not None:
            print(f"    subprocess exit: {exc.exit_code}", file=sys.stderr)
        return 1

    print(f"[*] Transcription OK: {result.output_dir / (result.stem + '_transcription.txt')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
