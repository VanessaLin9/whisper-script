"""Phase 1 application workflow (Checkpoints 04.2–04.3).

Thin orchestration: Drive downloader → Output Manager → Transcription Core.
CLI entry: ``python3 -m src.workflow``.
"""

from .drive_transcribe import DriveTranscribeWorkflow
from .types import (
    DriveTranscribeRequest,
    DriveTranscribeResult,
    WorkflowCancelled,
    WorkflowError,
    WorkflowProgressEvent,
    WorkflowStage,
)

__all__ = [
    "DriveTranscribeRequest",
    "DriveTranscribeResult",
    "DriveTranscribeWorkflow",
    "WorkflowCancelled",
    "WorkflowError",
    "WorkflowProgressEvent",
    "WorkflowStage",
]
