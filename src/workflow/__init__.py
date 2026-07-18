"""Phase 1 application workflow (Checkpoint 04.2).

Thin orchestration only: Drive downloader → Output Manager → Transcription Core.
No CLI display strings and no GUI/OAuth/post-processing.
"""

from .drive_transcribe import DriveTranscribeWorkflow
from .types import (
    DriveTranscribeRequest,
    DriveTranscribeResult,
    WorkflowError,
    WorkflowProgressEvent,
    WorkflowStage,
)

__all__ = [
    "DriveTranscribeRequest",
    "DriveTranscribeResult",
    "DriveTranscribeWorkflow",
    "WorkflowError",
    "WorkflowProgressEvent",
    "WorkflowStage",
]
