"""Thin Application Workflow: public Drive link → workspace → local transcribe."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, NoReturn, Protocol

from src.common import CancellationToken, OperationCancelled
from src.drive import DownloadError, DownloadResult, PublicDriveDownloader
from src.output_manager import (
    SourceDescriptor,
    SourceKind,
    WorkspaceError,
    create_workspace,
    plan_workspace,
)
from src.transcription import (
    ArtifactKind,
    ProgressEvent,
    ProgressStatus,
    Stage,
    TranscribeRequest,
    TranscribeResult,
    TranscriptionError,
    transcribe,
)

from .types import (
    DriveTranscribeRequest,
    DriveTranscribeResult,
    WorkflowCancelled,
    WorkflowError,
    WorkflowProgressCallback,
    WorkflowProgressEvent,
    WorkflowStage,
)


class DriveDownloader(Protocol):
    def download(
        self,
        drive_url: str,
        *,
        cancellation: CancellationToken | None = None,
    ) -> DownloadResult:
        """Download a public Drive file into a controlled temporary path."""


TranscribeFn = Callable[..., TranscribeResult]


_CORE_STAGE_MAP: dict[Stage, WorkflowStage] = {
    Stage.VALIDATE_INPUT: WorkflowStage.TRANSCRIBE,
    Stage.CHECK_OUTPUTS: WorkflowStage.OUTPUT,
    Stage.NORMALIZE: WorkflowStage.NORMALIZE,
    Stage.TRANSCRIBE: WorkflowStage.TRANSCRIBE,
    Stage.VALIDATE_ARTIFACTS: WorkflowStage.OUTPUT,
    Stage.CLEANUP: WorkflowStage.OUTPUT,
}


def _emit(
    callback: WorkflowProgressCallback | None,
    stage: WorkflowStage,
    status: ProgressStatus,
    detail: str | None = None,
) -> None:
    if callback is None:
        return
    callback(WorkflowProgressEvent(stage=stage, status=status, detail=detail))


def _cleanup_path(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _map_core_stage(stage: Stage) -> WorkflowStage:
    return _CORE_STAGE_MAP.get(stage, WorkflowStage.TRANSCRIBE)


def _map_cancel_stage(stage_name: str) -> WorkflowStage:
    try:
        return WorkflowStage(stage_name)
    except ValueError:
        pass
    try:
        return _map_core_stage(Stage(stage_name))
    except ValueError:
        return WorkflowStage.TRANSCRIBE


def _outputs_with_required_txt(
    requested: frozenset[ArtifactKind] | None,
) -> frozenset[ArtifactKind] | None:
    """Ensure raw transcript TXT is always retained for Phase 1 workflow results."""
    if requested is None:
        return None
    if not requested:
        raise ValueError("outputs must contain at least one artifact kind")
    return frozenset(requested) | {ArtifactKind.TXT}


def _throw_if_cancelled(
    cancellation: CancellationToken | None,
    stage: WorkflowStage,
) -> None:
    if cancellation is not None:
        cancellation.throw_if_cancelled(stage.value)


def _raise_cancelled(
    *,
    on_progress: WorkflowProgressCallback | None,
    stage: WorkflowStage,
    message: str,
    workspace_dir: Path | None = None,
    raw_audio_path: Path | None = None,
    retained_paths: tuple[Path, ...] = (),
    cleanup_detail: str | None = None,
    cause: BaseException | None = None,
) -> NoReturn:
    _emit(on_progress, stage, ProgressStatus.CANCELLED, message)
    raise WorkflowCancelled(
        stage=stage,
        message=message,
        workspace_dir=workspace_dir,
        raw_audio_path=raw_audio_path,
        retained_paths=retained_paths,
        cleanup_detail=cleanup_detail,
        cause=cause,
    )


class DriveTranscribeWorkflow:
    """Orchestrate downloader → Output Manager → Transcription Core."""

    def __init__(
        self,
        *,
        downloader: DriveDownloader | None = None,
        transcribe_fn: TranscribeFn | None = None,
    ) -> None:
        self._downloader: DriveDownloader = downloader or PublicDriveDownloader()
        self._transcribe: TranscribeFn = transcribe_fn or transcribe

    def run(
        self,
        request: DriveTranscribeRequest,
        *,
        on_progress: WorkflowProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> DriveTranscribeResult:
        meeting_time = request.meeting_time or datetime.now().astimezone()
        temp_download: Path | None = None
        workspace_dir: Path | None = None
        raw_audio_path: Path | None = None
        metadata_path: Path | None = None

        _emit(on_progress, WorkflowStage.DOWNLOAD, ProgressStatus.STARTED)
        try:
            _throw_if_cancelled(cancellation, WorkflowStage.DOWNLOAD)
            if cancellation is None:
                download = self._downloader.download(request.drive_url)
            else:
                download = self._downloader.download(
                    request.drive_url,
                    cancellation=cancellation,
                )
        except OperationCancelled as exc:
            _cleanup_path(temp_download)
            _raise_cancelled(
                on_progress=on_progress,
                stage=WorkflowStage.DOWNLOAD,
                message=exc.message,
                cleanup_detail=exc.cleanup_detail,
                cause=exc,
            )
        except DownloadError as exc:
            _emit(
                on_progress,
                WorkflowStage.DOWNLOAD,
                ProgressStatus.FAILED,
                exc.message,
            )
            raise WorkflowError(
                WorkflowStage.DOWNLOAD,
                exc.message,
                cause=exc,
            ) from exc
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            _emit(
                on_progress,
                WorkflowStage.DOWNLOAD,
                ProgressStatus.FAILED,
                message,
            )
            raise WorkflowError(
                WorkflowStage.DOWNLOAD,
                message,
                cause=exc,
            ) from exc
        temp_download = download.temp_path
        _emit(
            on_progress,
            WorkflowStage.DOWNLOAD,
            ProgressStatus.FINISHED,
            download.filename,
        )

        _emit(on_progress, WorkflowStage.WORKSPACE, ProgressStatus.STARTED)
        try:
            _throw_if_cancelled(cancellation, WorkflowStage.WORKSPACE)
            source = SourceDescriptor(
                kind=SourceKind.MANAGED_DOWNLOAD,
                path=download.temp_path,
                original_name=download.filename,
            )
            plan = plan_workspace(
                request.output_root,
                source,
                meeting_time,
                outputs=_outputs_with_required_txt(request.outputs),
                normalize=request.normalize,
            )
            workspace = create_workspace(plan)
        except OperationCancelled as exc:
            _cleanup_path(temp_download)
            temp_download = None
            _raise_cancelled(
                on_progress=on_progress,
                stage=WorkflowStage.WORKSPACE,
                message=exc.message,
                cleanup_detail=exc.cleanup_detail,
                cause=exc,
            )
        except WorkspaceError as exc:
            _cleanup_path(temp_download)
            temp_download = None
            _emit(
                on_progress,
                WorkflowStage.WORKSPACE,
                ProgressStatus.FAILED,
                exc.message,
            )
            raise WorkflowError(
                WorkflowStage.WORKSPACE,
                exc.message,
                cause=exc,
            ) from exc
        except Exception as exc:
            _cleanup_path(temp_download)
            temp_download = None
            message = str(exc) or exc.__class__.__name__
            _emit(
                on_progress,
                WorkflowStage.WORKSPACE,
                ProgressStatus.FAILED,
                message,
            )
            raise WorkflowError(
                WorkflowStage.WORKSPACE,
                message,
                cause=exc,
            ) from exc

        # Managed copy is authoritative; drop the download temp.
        _cleanup_path(temp_download)
        temp_download = None
        workspace_dir = workspace.workspace_dir
        raw_audio_path = workspace.audio_path
        metadata_path = workspace.metadata_path
        _emit(
            on_progress,
            WorkflowStage.WORKSPACE,
            ProgressStatus.FINISHED,
            str(workspace.workspace_dir),
        )

        def _core_progress(event: ProgressEvent) -> None:
            # Core emits FAILED/CANCELLED before raising; workflow boundary owns
            # the single terminal event for CLI/GUI consumers.
            if event.status in {ProgressStatus.FAILED, ProgressStatus.CANCELLED}:
                return
            mapped = _map_core_stage(event.stage)
            _emit(on_progress, mapped, event.status, event.detail)

        core_request = TranscribeRequest(
            audio_path=workspace.audio_path,
            language=request.language,
            model=request.model,
            model_path=request.model_path,
            whisper_cli=request.whisper_cli,
            threads=request.threads,
            output_dir=workspace.workspace_dir,
            stem=workspace.transcript_stem,
            outputs=workspace.outputs,
            normalize=request.normalize,
            keep_normalized=request.keep_normalized,
            ffmpeg=request.ffmpeg,
        )
        try:
            if cancellation is None:
                result = self._transcribe(core_request, on_progress=_core_progress)
            else:
                result = self._transcribe(
                    core_request,
                    on_progress=_core_progress,
                    cancellation=cancellation,
                )
        except OperationCancelled as exc:
            retained: list[Path] = []
            if raw_audio_path is not None and raw_audio_path.exists():
                retained.append(raw_audio_path)
            if metadata_path is not None and metadata_path.exists():
                retained.append(metadata_path)
            _raise_cancelled(
                on_progress=on_progress,
                stage=_map_cancel_stage(exc.stage),
                message=exc.message,
                workspace_dir=workspace_dir,
                raw_audio_path=raw_audio_path,
                retained_paths=tuple(retained),
                cleanup_detail=exc.cleanup_detail,
                cause=exc,
            )
        except TranscriptionError as exc:
            mapped = _map_core_stage(exc.stage)
            _emit(on_progress, mapped, ProgressStatus.FAILED, exc.message)
            raise WorkflowError(mapped, exc.message, cause=exc) from exc
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            _emit(
                on_progress,
                WorkflowStage.TRANSCRIBE,
                ProgressStatus.FAILED,
                message,
            )
            raise WorkflowError(
                WorkflowStage.TRANSCRIBE,
                message,
                cause=exc,
            ) from exc

        raw_transcript = result.artifacts.get(ArtifactKind.TXT)
        if raw_transcript is None:
            _emit(
                on_progress,
                WorkflowStage.OUTPUT,
                ProgressStatus.FAILED,
                "Successful transcription missing required TXT artifact",
            )
            raise WorkflowError(
                WorkflowStage.OUTPUT,
                "Successful transcription missing required TXT artifact",
            )

        return DriveTranscribeResult(
            workspace_dir=workspace.workspace_dir,
            raw_audio_path=workspace.audio_path,
            raw_transcript_path=raw_transcript,
            artifacts=dict(result.artifacts),
            normalized_audio_path=result.normalized_audio_path,
            download_filename=download.filename,
            file_id=download.file_id,
            meeting_time=meeting_time,
            language=result.language,
            model=result.model,
            stem=result.stem,
        )
