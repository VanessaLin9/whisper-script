"""Thin Application Workflow: public Drive link → workspace → local transcribe."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

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
    WorkflowError,
    WorkflowProgressCallback,
    WorkflowProgressEvent,
    WorkflowStage,
)


class DriveDownloader(Protocol):
    def download(self, drive_url: str) -> DownloadResult:
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


def _outputs_with_required_txt(
    requested: frozenset[ArtifactKind] | None,
) -> frozenset[ArtifactKind] | None:
    """Ensure raw transcript TXT is always retained for Phase 1 workflow results."""
    if requested is None:
        return None
    if not requested:
        raise ValueError("outputs must contain at least one artifact kind")
    return frozenset(requested) | {ArtifactKind.TXT}


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
    ) -> DriveTranscribeResult:
        meeting_time = request.meeting_time or datetime.now().astimezone()
        temp_download: Path | None = None

        _emit(on_progress, WorkflowStage.DOWNLOAD, ProgressStatus.STARTED)
        try:
            download = self._downloader.download(request.drive_url)
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
        _emit(
            on_progress,
            WorkflowStage.WORKSPACE,
            ProgressStatus.FINISHED,
            str(workspace.workspace_dir),
        )

        def _core_progress(event: ProgressEvent) -> None:
            # Core emits FAILED before raising; workflow boundary owns the
            # single terminal FAILED event for CLI/GUI consumers.
            if event.status == ProgressStatus.FAILED:
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
            result = self._transcribe(core_request, on_progress=_core_progress)
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
