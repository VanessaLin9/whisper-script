"""Typed contracts for the public Google Drive downloader."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class DownloadStage(str, Enum):
    PARSE_URL = "parse_url"
    DOWNLOAD = "download"
    VALIDATE = "validate"
    TEMP_STORE = "temp_store"


@dataclass(frozen=True)
class ParsedDriveUrl:
    file_id: str
    canonical_url: str


@dataclass(frozen=True)
class DownloadResult:
    file_id: str
    temp_path: Path
    filename: str
    content_type: str | None
    size_bytes: int


@dataclass
class DownloadError(Exception):
    stage: DownloadStage
    message: str
    status_code: int | None = None
    cause: BaseException | None = field(default=None, repr=False)

    def __str__(self) -> str:  # pragma: no cover - trivial
        suffix = f" (http={self.status_code})" if self.status_code is not None else ""
        return f"[{self.stage.value}] {self.message}{suffix}"


@dataclass(frozen=True)
class HttpResponse:
    """HTTP result with body streamed to ``body_path`` (not fully buffered in RAM).

    ``peek`` holds the first bytes for HTML / content-type checks without
    reloading the whole payload.
    """

    status_code: int
    headers: dict[str, str]
    url: str
    peek: bytes
    size_bytes: int
    body_path: Path | None = None
