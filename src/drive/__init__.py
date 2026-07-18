"""Public Google Drive input adapter / downloader (Checkpoint 04.1).

Independent of Transcription Core and Output Manager. Network I/O is injected
via ``HttpClient`` so tests stay offline.
"""

from .downloader import PublicDriveDownloader, sanitize_filename
from .types import DownloadError, DownloadResult, DownloadStage, ParsedDriveUrl
from .url import parse_public_drive_url, redact_url_for_logs

__all__ = [
    "DownloadError",
    "DownloadResult",
    "DownloadStage",
    "ParsedDriveUrl",
    "PublicDriveDownloader",
    "parse_public_drive_url",
    "redact_url_for_logs",
    "sanitize_filename",
]
