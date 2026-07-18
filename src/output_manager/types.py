"""Typed contracts for Meeting Workspace / Output Manager."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Mapping

from src.transcription.types import ArtifactKind


class SourceKind(str, Enum):
    """Who owns the raw audio and how it must be retained."""

    LOCAL_REFERENCE = "local_reference"
    MANAGED_DOWNLOAD = "managed_download"
    MANAGED_RECORDING = "managed_recording"


class WorkspaceStage(str, Enum):
    VALIDATE = "validate"
    PLAN = "plan"
    CHECK_CONFLICTS = "check_conflicts"
    CREATE = "create"
    PERSIST_SOURCE = "persist_source"
    WRITE_METADATA = "write_metadata"


@dataclass(frozen=True)
class SourceDescriptor:
    """Input Adapter / workflow description of one audio source.

    Core never owns lifecycle. Output Manager only persists managed sources
    according to ``kind``.
    """

    kind: SourceKind
    path: Path
    original_name: str | None = None


@dataclass(frozen=True)
class WorkspacePlan:
    """Dry-run result: paths and ownership without mutating the filesystem."""

    output_root: Path
    meeting_time: datetime
    safe_stem: str
    workspace_dir: Path
    source: SourceDescriptor
    audio_path_for_core: Path
    transcript_stem: str
    outputs: frozenset[ArtifactKind]
    normalize: bool
    planned_artifacts: Mapping[ArtifactKind, Path]
    normalized_audio_path: Path | None
    metadata_path: Path
    managed_audio_path: Path | None
    conflict_candidates: tuple[Path, ...]


@dataclass(frozen=True)
class MeetingWorkspace:
    """Created workspace ready for transcription."""

    plan: WorkspacePlan
    workspace_dir: Path
    audio_path: Path
    transcript_stem: str
    outputs: frozenset[ArtifactKind]
    artifacts: Mapping[ArtifactKind, Path]
    normalized_audio_path: Path | None
    metadata_path: Path
    source_kind: SourceKind
    source_original_path: Path


@dataclass
class WorkspaceError(Exception):
    stage: WorkspaceStage
    message: str
    cause: BaseException | None = field(default=None, repr=False)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.stage.value}] {self.message}"
