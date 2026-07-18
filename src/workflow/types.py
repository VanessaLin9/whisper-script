"""Typed contracts for the Phase 1 Drive → workspace → transcription workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping

from src.transcription.types import ArtifactKind, ProgressStatus


class WorkflowStage(str, Enum):
    DOWNLOAD = "download"
    WORKSPACE = "workspace"
    NORMALIZE = "normalize"
    TRANSCRIBE = "transcribe"
    OUTPUT = "output"


@dataclass(frozen=True)
class WorkflowProgressEvent:
    stage: WorkflowStage
    status: ProgressStatus
    detail: str | None = None


WorkflowProgressCallback = Callable[[WorkflowProgressEvent], None]


@dataclass(frozen=True)
class DriveTranscribeRequest:
    """Inputs for one public-Drive → meeting-workspace transcription run."""

    drive_url: str
    output_root: Path
    language: str
    model: str
    model_path: Path
    whisper_cli: Path
    threads: int
    meeting_time: datetime | None = None
    outputs: frozenset[ArtifactKind] | None = None
    normalize: bool = True
    keep_normalized: bool = True
    ffmpeg: Path = Path("ffmpeg")


@dataclass(frozen=True)
class DriveTranscribeResult:
    """Successful workflow result with workspace and artifact paths."""

    workspace_dir: Path
    raw_audio_path: Path
    raw_transcript_path: Path | None
    artifacts: Mapping[ArtifactKind, Path]
    normalized_audio_path: Path | None
    download_filename: str
    file_id: str
    meeting_time: datetime
    language: str
    model: str
    stem: str


@dataclass
class WorkflowError(Exception):
    stage: WorkflowStage
    message: str
    cause: BaseException | None = field(default=None, repr=False)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.stage.value}] {self.message}"
