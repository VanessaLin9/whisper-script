"""Orchestration for one local single-file transcription."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from .artifacts import (
    assert_no_output_conflicts,
    assert_outputs_within_output_dir,
    normalized_audio_path,
    output_base,
    remove_paths,
    validate_requested_artifacts,
)
from .normalize import normalize_audio
from .subprocess_runner import DefaultSubprocessRunner, SubprocessRunner
from .types import (
    ProgressCallback,
    ProgressEvent,
    ProgressStatus,
    Stage,
    TranscribeRequest,
    TranscribeResult,
    TranscriptionError,
)
from .whisper import run_whisper

logger = logging.getLogger(__name__)


def _emit(
    callback: ProgressCallback | None,
    stage: Stage,
    status: ProgressStatus,
    detail: str | None = None,
) -> None:
    if callback is None:
        return
    callback(ProgressEvent(stage=stage, status=status, detail=detail))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _source_fingerprint(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def transcribe(
    request: TranscribeRequest,
    *,
    runner: SubprocessRunner | None = None,
    on_progress: ProgressCallback | None = None,
) -> TranscribeResult:
    """Transcribe one local audio file.

    Success returns :class:`TranscribeResult`. Failure raises
    :class:`TranscriptionError` with a stage. The source audio file is never
    moved or deleted.
    """
    active_runner = runner or DefaultSubprocessRunner()
    started_at = _utc_now()
    created_paths: list[Path] = []
    audio_path = request.audio_path.expanduser().resolve()
    source_fingerprint = None
    active_stage = Stage.VALIDATE_INPUT

    try:
        _emit(on_progress, Stage.VALIDATE_INPUT, ProgressStatus.STARTED)
        if not audio_path.is_file():
            raise TranscriptionError(
                Stage.VALIDATE_INPUT,
                f"Audio file not found: {audio_path}",
            )
        if not request.outputs:
            raise TranscriptionError(
                Stage.VALIDATE_INPUT,
                "At least one output artifact must be requested",
            )
        if not request.whisper_cli.expanduser().exists():
            raise TranscriptionError(
                Stage.VALIDATE_INPUT,
                f"whisper-cli not found: {request.whisper_cli}",
            )
        if not request.model_path.expanduser().is_file():
            raise TranscriptionError(
                Stage.VALIDATE_INPUT,
                f"Model file not found: {request.model_path}",
            )
        if request.threads < 1:
            raise TranscriptionError(
                Stage.VALIDATE_INPUT,
                f"threads must be >= 1, got {request.threads}",
            )
        assert_outputs_within_output_dir(request)
        request.output_dir.mkdir(parents=True, exist_ok=True)
        source_fingerprint = _source_fingerprint(audio_path)
        _emit(on_progress, Stage.VALIDATE_INPUT, ProgressStatus.FINISHED)

        active_stage = Stage.CHECK_OUTPUTS
        _emit(on_progress, Stage.CHECK_OUTPUTS, ProgressStatus.STARTED)
        assert_no_output_conflicts(request)
        _emit(on_progress, Stage.CHECK_OUTPUTS, ProgressStatus.FINISHED)

        whisper_input = audio_path
        normalized_path: Path | None = None
        if request.normalize:
            active_stage = Stage.NORMALIZE
            _emit(on_progress, Stage.NORMALIZE, ProgressStatus.STARTED)
            target = normalized_audio_path(request)
            created_paths.append(target)
            normalized_path = normalize_audio(
                ffmpeg=request.ffmpeg,
                audio_path=audio_path,
                output_path=target,
                runner=active_runner,
            )
            whisper_input = normalized_path
            _emit(on_progress, Stage.NORMALIZE, ProgressStatus.FINISHED, str(normalized_path))
        else:
            logger.info("normalize skipped; using source audio for whisper-cli")

        active_stage = Stage.TRANSCRIBE
        _emit(on_progress, Stage.TRANSCRIBE, ProgressStatus.STARTED)
        base = output_base(request)
        for kind in request.outputs:
            created_paths.append(Path(f"{base}.{kind.value}"))
        run_whisper(
            whisper_cli=request.whisper_cli,
            model_path=request.model_path,
            audio_path=whisper_input,
            language=request.language,
            threads=request.threads,
            output_base=base,
            outputs=request.outputs,
            runner=active_runner,
        )
        _emit(on_progress, Stage.TRANSCRIBE, ProgressStatus.FINISHED)

        active_stage = Stage.VALIDATE_ARTIFACTS
        _emit(on_progress, Stage.VALIDATE_ARTIFACTS, ProgressStatus.STARTED)
        artifacts = validate_requested_artifacts(request)
        _emit(on_progress, Stage.VALIDATE_ARTIFACTS, ProgressStatus.FINISHED)

        if request.normalize and not request.keep_normalized and normalized_path is not None:
            active_stage = Stage.CLEANUP
            _emit(on_progress, Stage.CLEANUP, ProgressStatus.STARTED)
            remove_paths([normalized_path])
            if normalized_path in created_paths:
                created_paths.remove(normalized_path)
            normalized_path = None
            _emit(on_progress, Stage.CLEANUP, ProgressStatus.FINISHED)

        if source_fingerprint != _source_fingerprint(audio_path):
            raise TranscriptionError(
                Stage.VALIDATE_ARTIFACTS,
                f"Source audio was modified during transcription: {audio_path}",
            )

        return TranscribeResult(
            raw_audio_path=audio_path,
            normalized_audio_path=normalized_path,
            artifacts=artifacts,
            model=request.model,
            language=request.language,
            started_at=started_at,
            finished_at=_utc_now(),
            output_dir=request.output_dir,
            stem=request.stem,
        )
    except TranscriptionError as exc:
        _emit(on_progress, exc.stage, ProgressStatus.FAILED, exc.message)
        # Drop only outputs created in this attempt so failures cannot look like
        # success. Never touch the source audio or pre-existing conflict files.
        removable = [path for path in created_paths if path.resolve() != audio_path]
        remove_paths(removable)
        raise
    except Exception as exc:  # pragma: no cover - defensive boundary
        _emit(on_progress, active_stage, ProgressStatus.FAILED, str(exc))
        removable = [path for path in created_paths if path.resolve() != audio_path]
        remove_paths(removable)
        raise TranscriptionError(
            active_stage,
            f"Unexpected transcription failure: {exc}",
            cause=exc,
        ) from exc
