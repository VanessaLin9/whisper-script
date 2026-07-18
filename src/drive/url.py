"""Parse and validate public Google Drive sharing URLs (no network I/O)."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from .types import DownloadError, DownloadStage, ParsedDriveUrl

_FILE_PATH = re.compile(
    r"^/file/d/(?P<id>[a-zA-Z0-9_-]{10,})(?:/|$)",
    re.IGNORECASE,
)
_FILE_ID = re.compile(r"^[a-zA-Z0-9_-]{10,}$")


def _reject(message: str) -> None:
    raise DownloadError(DownloadStage.PARSE_URL, message)


def parse_public_drive_url(raw: str) -> ParsedDriveUrl:
    """Return a parsed public Drive file URL or raise ``DownloadError``."""
    if raw is None or not str(raw).strip():
        _reject("Drive URL is required")

    text = str(raw).strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"}:
        _reject(f"Unsupported URL scheme: {parsed.scheme or '<missing>'}")
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in {"drive.google.com", "docs.google.com"}:
        _reject(f"Unsupported Drive host: {parsed.netloc or '<missing>'}")

    file_id: str | None = None
    path_match = _FILE_PATH.match(parsed.path or "")
    if path_match:
        file_id = path_match.group("id")
    else:
        query = parse_qs(parsed.query, keep_blank_values=False)
        for key in ("id", "file_id"):
            values = query.get(key) or []
            if values and _FILE_ID.fullmatch(values[0]):
                file_id = values[0]
                break

    if not file_id or not _FILE_ID.fullmatch(file_id):
        _reject("Could not extract a valid Google Drive file id from URL")

    canonical = f"https://drive.google.com/uc?export=download&id={file_id}"
    return ParsedDriveUrl(file_id=file_id, canonical_url=canonical)


def uc_download_url(file_id: str, *, confirm: str | None = None) -> str:
    """Build a download URL for ``file_id`` (optional virus-scan confirm token)."""
    if not _FILE_ID.fullmatch(file_id):
        _reject(f"Invalid Drive file id: {file_id!r}")
    base = f"https://drive.google.com/uc?export=download&id={file_id}"
    if confirm:
        return f"{base}&confirm={confirm}"
    return base


def redact_url_for_logs(url: str) -> str:
    """Return a log-safe URL without query string / fragments."""
    parsed = urlparse(url)
    host = parsed.netloc or ""
    path = parsed.path or ""
    return f"{parsed.scheme}://{host}{path}" if parsed.scheme else path or "<redacted>"
