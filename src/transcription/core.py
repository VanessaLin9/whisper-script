"""Orchestration for one local single-file transcription."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from src.common import CancellationToken, OperationCancelled

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


def _throw_if_cancelled(
    cancellation: CancellationToken | None,
    stage: Stage,
) -> None:
    if cancellation is not None:
        cancellation.throw_if_cancelled(stage.value)


def transcribe(
    request: TranscribeRequest,
    *,
    runner: SubprocessRunner | None = None,
    on_progress: ProgressCallback | None = None,
    cancellation: CancellationToken | None = None,
) -> TranscribeResult:
    """Transcribe one local audio file.

    Success returns :class:`TranscribeResult`. Failure raises
    :class:`TranscriptionError` with a stage. Cancellation raises
    :class:`OperationCancelled`. The source audio file is never moved or
    deleted.
    """
    active_runner = runner or DefaultSubprocessRunner()
    started_at = _utc_now()
    created_paths: list[Path] = []
    audio_path = request.audio_path.expanduser().resolve()
    source_fingerprint = None
    active_stage = Stage.VALIDATE_INPUT

    try:
        _throw_if_cancelled(cancellation, Stage.VALIDATE_INPUT)
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
        _throw_if_cancelled(cancellation, Stage.CHECK_OUTPUTS)
        _emit(on_progress, Stage.CHECK_OUTPUTS, ProgressStatus.STARTED)
        assert_no_output_conflicts(request)
        _emit(on_progress, Stage.CHECK_OUTPUTS, ProgressStatus.FINISHED)

        whisper_input = audio_path
        normalized_path: Path | None = None
        if request.normalize:
            active_stage = Stage.NORMALIZE
            _throw_if_cancelled(cancellation, Stage.NORMALIZE)
            _emit(on_progress, Stage.NORMALIZE, ProgressStatus.STARTED)
            target = normalized_audio_path(request)
            created_paths.append(target)
            normalized_path = normalize_audio(
                ffmpeg=request.ffmpeg,
                audio_path=audio_path,
                output_path=target,
                runner=active_runner,
                cancellation=cancellation,
            )
            whisper_input = normalized_path
            _throw_if_cancelled(cancellation, Stage.NORMALIZE)
            _emit(on_progress, Stage.NORMALIZE, ProgressStatus.FINISHED, str(normalized_path))
        else:
            logger.info("normalize skipped; using source audio for whisper-cli")

        active_stage = Stage.TRANSCRIBE
        _throw_if_cancelled(cancellation, Stage.TRANSCRIBE)
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
            cancellation=cancellation,
        )
        _throw_if_cancelled(cancellation, Stage.TRANSCRIBE)
        _emit(on_progress, Stage.TRANSCRIBE, ProgressStatus.FINISHED)

        active_stage = Stage.VALIDATE_ARTIFACTS
        _throw_if_cancelled(cancellation, Stage.VALIDATE_ARTIFACTS)
        _emit(on_progress, Stage.VALIDATE_ARTIFACTS, ProgressStatus.STARTED)
        artifacts = validate_requested_artifacts(request)
        _emit(on_progress, Stage.VALIDATE_ARTIFACTS, ProgressStatus.FINISHED)
        # Artifacts are committed: later cancel is a no-op and must not delete them.
        created_paths = [
            path
            for path in created_paths
            if path.resolve() not in {item.resolve() for item in artifacts.values()}
        ]

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
    except OperationCancelled as exc:
        stage = Stage(exc.stage) if exc.stage in {item.value for item in Stage} else active_stage
        _emit(on_progress, stage, ProgressStatus.CANCELLED, exc.message)
        # Preserve source audio; drop only this attempt's partial outputs.
        removable = [path for path in created_paths if path.resolve() != audio_path]
        remove_paths(removable)
        if stage.value != exc.stage:
            raise OperationCancelled(
                stage=stage.value,
                message=exc.message,
                cleanup_detail=exc.cleanup_detail,
                cause=exc.cause,
            ) from exc
        raise
    except TranscriptionError as exc:
        _emit(on_progress, exc.stage, ProgressStatus.FAILED, exc.message)
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
