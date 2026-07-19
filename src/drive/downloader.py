"""Public Google Drive file downloader (independent of Whisper / workspace)."""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
from email.message import EmailMessage
from pathlib import Path

from src.common import CancellationToken, OperationCancelled

from .http import (
    HttpClient,
    UrllibHttpClient,
    cleanup_http_body,
    is_absolute_http_url,
    resolve_redirect_url,
)
from .types import DownloadError, DownloadResult, DownloadStage, HttpResponse
from .url import parse_public_drive_url, redact_url_for_logs, uc_download_url

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_CONFIRMATIONS = 2
# Bound HTML parse reads so confirmation / permission checks cannot load
# an arbitrarily large error page into RAM.
MAX_HTML_PARSE_BYTES = 64 * 1024

_SAFE_AUDIO_SUFFIXES = frozenset(
    {
        ".m4a",
        ".mp3",
        ".wav",
        ".aac",
        ".flac",
        ".ogg",
        ".opus",
        ".webm",
        ".mp4",
        ".m4v",
        ".mov",
    }
)
_MIME_TO_SUFFIX = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/m4a": ".m4a",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/webm": ".webm",
    "audio/aac": ".aac",
    "audio/x-aac": ".aac",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}
_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_CONFIRM = re.compile(
    r"confirm=([0-9A-Za-z_-]+)",
    re.IGNORECASE,
)
_CONFIRM_FORM = re.compile(
    r'name="confirm"\s+value="([0-9A-Za-z_-]+)"',
    re.IGNORECASE,
)
_HTML_HINTS = (
    b"<!doctype html",
    b"<html",
    b"accounts.google.com",
    b"sign in",
    b"could not find",
    b"not found",
    b"access denied",
    b"permission",
    b"virus scan warning",
)


def file_ref_for_logs(file_id: str) -> str:
    """Stable non-reversible token for logs (never the raw Drive file id)."""
    digest = hashlib.sha256(file_id.encode("utf-8")).hexdigest()
    return digest[:12]


def sanitize_filename(name: str, *, fallback: str = "drive_audio") -> str:
    cleaned = Path(name or "").name.strip()
    cleaned = _UNSAFE_NAME.sub("-", cleaned)
    cleaned = cleaned.strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = fallback
    stem = Path(cleaned).stem or fallback
    suffix = Path(cleaned).suffix.lower()
    if not suffix:
        raise DownloadError(
            DownloadStage.VALIDATE,
            "Download filename is missing a supported media suffix",
        )
    if suffix not in _SAFE_AUDIO_SUFFIXES:
        raise DownloadError(
            DownloadStage.VALIDATE,
            f"Unsupported or unsafe download filename suffix: {suffix}",
        )
    cleaned = f"{stem}{suffix}"
    if len(cleaned) > 120:
        cleaned = f"{stem[:80]}{suffix}"
    return cleaned


def filename_from_content_disposition(header: str | None) -> str | None:
    if not header:
        return None
    message = EmailMessage()
    message["content-disposition"] = header
    return message.get_filename()


def suffix_from_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    mime = content_type.split(";", 1)[0].strip().lower()
    return _MIME_TO_SUFFIX.get(mime)


def resolve_download_filename(
    *,
    content_disposition: str | None,
    content_type: str | None,
) -> str:
    """Require a safe media suffix from Content-Disposition or allow-listed MIME."""
    disposition_name = filename_from_content_disposition(content_disposition)
    if disposition_name:
        return sanitize_filename(disposition_name)

    suffix = suffix_from_content_type(content_type)
    if suffix is None:
        raise DownloadError(
            DownloadStage.VALIDATE,
            "Missing Content-Disposition filename and unsupported or missing media Content-Type",
        )
    return sanitize_filename(f"drive_audio{suffix}")


def _looks_like_html(peek: bytes, content_type: str | None) -> bool:
    ctype = (content_type or "").lower()
    if "text/html" in ctype or "application/xhtml" in ctype:
        return True
    sample = peek[:2048].lstrip().lower()
    return any(hint in sample for hint in _HTML_HINTS)


def _is_permission_page(sample: bytes) -> bool:
    lowered = sample[:8192].lower()
    markers = (
        b"you need access",
        b"request access",
        b"sign in to continue",
        b"accounts.google.com",
        b"permission denied",
    )
    return any(marker in lowered for marker in markers)


def extract_confirm_token(body: bytes) -> str | None:
    text = body.decode("utf-8", errors="ignore")
    match = _CONFIRM_FORM.search(text) or _CONFIRM.search(text)
    return match.group(1) if match else None


def _read_body_prefix(response: HttpResponse, *, limit: int = MAX_HTML_PARSE_BYTES) -> bytes:
    """Read at most ``limit`` bytes for HTML validation (never full-file load)."""
    if limit <= 0:
        return b""
    if response.body_path is None:
        return response.peek[:limit]
    with response.body_path.open("rb") as handle:
        return handle.read(limit)


class PublicDriveDownloader:
    """Download a public Drive file into a controlled temporary path."""

    def __init__(
        self,
        http: HttpClient | None = None,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_confirmations: int = DEFAULT_MAX_CONFIRMATIONS,
        temp_dir: Path | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if max_redirects < 0:
            raise ValueError("max_redirects must be >= 0")
        if max_retries < 1:
            raise ValueError("max_retries must be >= 1")
        if max_confirmations < 0:
            raise ValueError("max_confirmations must be >= 0")
        self._http = http or UrllibHttpClient()
        self._timeout = timeout_seconds
        self._max_redirects = max_redirects
        self._max_retries = max_retries
        self._max_confirmations = max_confirmations
        self._temp_dir = temp_dir

    def download(
        self,
        drive_url: str,
        *,
        cancellation: CancellationToken | None = None,
    ) -> DownloadResult:
        parsed = parse_public_drive_url(drive_url)
        url = parsed.canonical_url
        confirmations = 0
        retries_used = 0
        file_ref = file_ref_for_logs(parsed.file_id)
        self._throw_if_cancelled(cancellation, DownloadStage.DOWNLOAD)

        while True:
            response: HttpResponse | None = None
            try:
                self._throw_if_cancelled(cancellation, DownloadStage.DOWNLOAD)
                response = self._get_with_redirect_budget(url, cancellation=cancellation)
                self._assert_http_ok(response)
                content_type = response.headers.get("content-type")

                if _looks_like_html(response.peek, content_type):
                    body = _read_body_prefix(response, limit=MAX_HTML_PARSE_BYTES)
                    oversized = response.size_bytes > MAX_HTML_PARSE_BYTES
                    cleanup_http_body(response)
                    response = None
                    if _is_permission_page(body):
                        raise DownloadError(
                            DownloadStage.DOWNLOAD,
                            "Drive file is not publicly accessible (permission page)",
                            status_code=200,
                        )
                    token = extract_confirm_token(body)
                    if token and confirmations < self._max_confirmations:
                        confirmations += 1
                        url = uc_download_url(parsed.file_id, confirm=token)
                        logger.info(
                            "Drive download confirmation required ref=%s (attempt %s)",
                            file_ref,
                            confirmations,
                        )
                        # Confirmation budget is independent of transient retries.
                        self._throw_if_cancelled(cancellation, DownloadStage.DOWNLOAD)
                        continue
                    if token and confirmations >= self._max_confirmations:
                        raise DownloadError(
                            DownloadStage.VALIDATE,
                            f"Exceeded confirmation limit ({self._max_confirmations})",
                        )
                    if oversized and not token:
                        raise DownloadError(
                            DownloadStage.VALIDATE,
                            "Drive returned an oversized HTML page instead of file content",
                            status_code=200,
                        )
                    raise DownloadError(
                        DownloadStage.VALIDATE,
                        "Drive returned an HTML page instead of file content",
                        status_code=200,
                    )

                if response.size_bytes == 0:
                    cleanup_http_body(response)
                    response = None
                    raise DownloadError(
                        DownloadStage.VALIDATE,
                        "Downloaded Drive content is empty",
                        status_code=200,
                    )

                safe_name = resolve_download_filename(
                    content_disposition=response.headers.get("content-disposition"),
                    content_type=content_type,
                )
                # Cancel before commit. Once finalize succeeds the DownloadResult
                # is committed and a later cancel is a no-op (success wins).
                self._throw_if_cancelled(cancellation, DownloadStage.DOWNLOAD)
                temp_path = self._finalize_temp(response, safe_name)
                response = None
                return DownloadResult(
                    file_id=parsed.file_id,
                    temp_path=temp_path,
                    filename=safe_name,
                    content_type=content_type,
                    size_bytes=temp_path.stat().st_size,
                )
            except OperationCancelled as exc:
                cleanup_detail = exc.cleanup_detail
                if response is not None:
                    try:
                        cleanup_http_body(response)
                    except OSError as cleanup_exc:
                        cleanup_detail = cleanup_detail or f"cleanup failed: {cleanup_exc}"
                    response = None
                logger.info(
                    "Drive download cancelled ref=%s stage=%s",
                    file_ref,
                    exc.stage,
                )
                if cleanup_detail and cleanup_detail != exc.cleanup_detail:
                    raise OperationCancelled(
                        stage=exc.stage,
                        message=exc.message,
                        cleanup_detail=cleanup_detail,
                        cause=exc.cause,
                    ) from exc
                raise
            except DownloadError as exc:
                if response is not None:
                    cleanup_http_body(response)
                    response = None
                retryable = (
                    exc.stage == DownloadStage.DOWNLOAD
                    and (
                        exc.status_code is None
                        or exc.status_code >= 500
                    )
                    and "redirect limit" not in exc.message.lower()
                    and "not publicly accessible" not in exc.message.lower()
                )
                if not retryable:
                    raise
                retries_used += 1
                if retries_used >= self._max_retries:
                    raise
                self._throw_if_cancelled(cancellation, DownloadStage.DOWNLOAD)
                logger.warning(
                    "Drive download retry %s/%s ref=%s stage=%s",
                    retries_used,
                    self._max_retries,
                    file_ref,
                    exc.stage.value,
                )

    def _get_with_redirect_budget(
        self,
        url: str,
        *,
        cancellation: CancellationToken | None = None,
    ) -> HttpResponse:
        current = url
        for _ in range(self._max_redirects + 1):
            self._throw_if_cancelled(cancellation, DownloadStage.DOWNLOAD)
            logger.info("Drive HTTP GET %s", redact_url_for_logs(current))
            response = self._http.request(
                "GET",
                current,
                timeout=self._timeout,
                headers={"User-Agent": "whisper-script-drive-downloader/1.0"},
                allow_redirects=False,
                temp_dir=self._temp_dir,
                cancellation=cancellation,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                cleanup_http_body(response)
                location = response.headers.get("location")
                nxt = resolve_redirect_url(current, location or "")
                if not is_absolute_http_url(nxt):
                    raise DownloadError(
                        DownloadStage.DOWNLOAD,
                        "Redirect target is not an absolute http(s) URL",
                        status_code=response.status_code,
                    )
                current = nxt
                self._throw_if_cancelled(cancellation, DownloadStage.DOWNLOAD)
                continue
            return response
        raise DownloadError(
            DownloadStage.DOWNLOAD,
            f"Exceeded redirect limit ({self._max_redirects})",
        )

    @staticmethod
    def _throw_if_cancelled(
        cancellation: CancellationToken | None,
        stage: DownloadStage,
    ) -> None:
        if cancellation is not None:
            cancellation.throw_if_cancelled(stage.value)


    def _assert_http_ok(self, response: HttpResponse) -> None:
        code = response.status_code
        if code == 404:
            raise DownloadError(
                DownloadStage.DOWNLOAD,
                "Drive file not found",
                status_code=code,
            )
        if code in {401, 403}:
            raise DownloadError(
                DownloadStage.DOWNLOAD,
                "Drive file access denied",
                status_code=code,
            )
        if code >= 500:
            raise DownloadError(
                DownloadStage.DOWNLOAD,
                "Drive server error",
                status_code=code,
            )
        if code >= 400:
            raise DownloadError(
                DownloadStage.DOWNLOAD,
                f"Unexpected Drive HTTP status {code}",
                status_code=code,
            )

    def _finalize_temp(self, response: HttpResponse, filename: str) -> Path:
        if response.body_path is None:
            raise DownloadError(
                DownloadStage.TEMP_STORE,
                "Download stream completed without a temporary body file",
            )
        directory = Path(self._temp_dir) if self._temp_dir else response.body_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            prefix="whisper-drive-",
            suffix=Path(filename).suffix,
            delete=False,
            dir=str(directory),
        )
        handle.close()
        dest = Path(handle.name)
        try:
            response.body_path.replace(dest)
            return dest
        except OSError as exc:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            cleanup_http_body(response)
            raise DownloadError(
                DownloadStage.TEMP_STORE,
                "Failed to finalize downloaded Drive file in temporary storage",
                cause=exc,
            ) from exc
