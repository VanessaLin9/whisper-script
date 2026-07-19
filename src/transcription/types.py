"""Typed contracts for the local transcription core."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping


class Stage(str, Enum):
    """Ordered stages reported through the progress callback."""

    VALIDATE_INPUT = "validate_input"
    CHECK_OUTPUTS = "check_outputs"
    NORMALIZE = "normalize"
    TRANSCRIBE = "transcribe"
    VALIDATE_ARTIFACTS = "validate_artifacts"
    CLEANUP = "cleanup"


class ProgressStatus(str, Enum):
    STARTED = "started"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ArtifactKind(str, Enum):
    TXT = "txt"
    SRT = "srt"
    VTT = "vtt"
    JSON = "json"


@dataclass(frozen=True)
class ProgressEvent:
    stage: Stage
    status: ProgressStatus
    detail: str | None = None


ProgressCallback = Callable[[ProgressEvent], None]


@dataclass(frozen=True)
class TranscribeRequest:
    """Inputs for one local single-file transcription.

    The core only reads ``audio_path``; it never moves or deletes the source file.
    ``partial`` status is intentionally not used here — batch orchestration owns
    partial-success aggregation. This API is all-or-nothing: success returns a
    result, failure raises :class:`TranscriptionError`.
    """

    audio_path: Path
    language: str
    model: str
    model_path: Path
    whisper_cli: Path
    threads: int
    output_dir: Path
    stem: str
    outputs: frozenset[ArtifactKind]
    normalize: bool = True
    keep_normalized: bool = True
    ffmpeg: Path = Path("ffmpeg")
    # Optional whisper --output-file basename (single filename component).
    # When None, artifacts use the default ``{stem}_transcription.*`` layout.
    # Legacy shell workflows may pass the stem itself (e.g. meeting_<ts>,
    # segment_NNN) so outputs stay ``<basename>.txt/.srt`` without renaming.
    artifact_basename: str | None = None


@dataclass(frozen=True)
class TranscribeResult:
    """Successful single-file transcription result."""

    raw_audio_path: Path
    normalized_audio_path: Path | None
    artifacts: Mapping[ArtifactKind, Path]
    model: str
    language: str
    started_at: datetime
    finished_at: datetime
    output_dir: Path
    stem: str


@dataclass
class TranscriptionError(Exception):
    """Typed failure for a single transcription attempt."""

    stage: Stage
    message: str
    exit_code: int | None = None
    cause: BaseException | None = field(default=None, repr=False)
    # Bounded subprocess stderr/stdout tail for interactive diagnostics.
    diagnostic: str | None = field(default=None, repr=False)

    def __str__(self) -> str:  # pragma: no cover - trivial
        suffix = f" (exit={self.exit_code})" if self.exit_code is not None else ""
        return f"[{self.stage.value}] {self.message}{suffix}"
