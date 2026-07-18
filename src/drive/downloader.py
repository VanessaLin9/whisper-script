"""Public Google Drive file downloader (independent of Whisper / workspace)."""

from __future__ import annotations

import logging
import re
import tempfile
from email.message import EmailMessage
from pathlib import Path

from .http import HttpClient, UrllibHttpClient, is_absolute_http_url, resolve_redirect_url
from .types import DownloadError, DownloadResult, DownloadStage, HttpResponse
from .url import parse_public_drive_url, redact_url_for_logs, uc_download_url

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_CONFIRMATIONS = 2

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


def sanitize_filename(name: str, *, fallback: str = "drive_audio") -> str:
    cleaned = Path(name or "").name.strip()
    cleaned = _UNSAFE_NAME.sub("-", cleaned)
    cleaned = cleaned.strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = fallback
    stem = Path(cleaned).stem or fallback
    suffix = Path(cleaned).suffix.lower()
    if suffix and suffix not in _SAFE_AUDIO_SUFFIXES:
        raise DownloadError(
            DownloadStage.VALIDATE,
            f"Unsupported or unsafe download filename suffix: {suffix}",
        )
    if not suffix:
        suffix = ".bin"
        cleaned = f"{stem}{suffix}"
    if len(cleaned) > 120:
        cleaned = f"{stem[:80]}{suffix}"
    return cleaned


def filename_from_content_disposition(header: str | None) -> str | None:
    if not header:
        return None
    message = EmailMessage()
    message["content-disposition"] = header
    filename = message.get_filename()
    return filename


def _looks_like_html(body: bytes, content_type: str | None) -> bool:
    ctype = (content_type or "").lower()
    if "text/html" in ctype or "application/xhtml" in ctype:
        return True
    sample = body[:2048].lstrip().lower()
    return any(hint in sample for hint in _HTML_HINTS)


def _is_permission_page(body: bytes) -> bool:
    sample = body[:8192].lower()
    markers = (
        b"you need access",
        b"request access",
        b"sign in to continue",
        b"accounts.google.com",
        b"permission denied",
    )
    return any(marker in sample for marker in markers)


def extract_confirm_token(body: bytes) -> str | None:
    text = body.decode("utf-8", errors="ignore")
    match = _CONFIRM_FORM.search(text) or _CONFIRM.search(text)
    return match.group(1) if match else None


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
        self._http = http or UrllibHttpClient()
        self._timeout = timeout_seconds
        self._max_redirects = max_redirects
        self._max_retries = max_retries
        self._max_confirmations = max_confirmations
        self._temp_dir = temp_dir

    def download(self, drive_url: str) -> DownloadResult:
        parsed = parse_public_drive_url(drive_url)
        url = parsed.canonical_url
        confirmations = 0
        attempt = 0
        last_error: DownloadError | None = None

        while attempt < self._max_retries:
            attempt += 1
            try:
                response = self._get_with_redirect_budget(url)
                self._assert_http_ok(response)
                content_type = response.headers.get("content-type")

                if _looks_like_html(response.body, content_type):
                    if _is_permission_page(response.body):
                        raise DownloadError(
                            DownloadStage.DOWNLOAD,
                            "Drive file is not publicly accessible (permission page)",
                            status_code=response.status_code,
                        )
                    token = extract_confirm_token(response.body)
                    if token and confirmations < self._max_confirmations:
                        confirmations += 1
                        url = uc_download_url(parsed.file_id, confirm=token)
                        logger.info(
                            "Drive download confirmation required for file_id=%s (attempt %s)",
                            parsed.file_id,
                            confirmations,
                        )
                        continue
                    raise DownloadError(
                        DownloadStage.VALIDATE,
                        "Drive returned an HTML page instead of file content",
                        status_code=response.status_code,
                    )

                if not response.body:
                    raise DownloadError(
                        DownloadStage.VALIDATE,
                        "Downloaded Drive content is empty",
                        status_code=response.status_code,
                    )

                filename = filename_from_content_disposition(
                    response.headers.get("content-disposition")
                )
                safe_name = sanitize_filename(
                    filename or f"drive_{parsed.file_id}.bin"
                )
                temp_path = self._write_temp(safe_name, response.body)
                return DownloadResult(
                    file_id=parsed.file_id,
                    temp_path=temp_path,
                    filename=safe_name,
                    content_type=content_type,
                    size_bytes=len(response.body),
                )
            except DownloadError as exc:
                last_error = exc
                # Retry only transient transport / 5xx failures.
                retryable = (
                    exc.stage == DownloadStage.DOWNLOAD
                    and (
                        exc.status_code is None
                        or exc.status_code >= 500
                    )
                    and "redirect limit" not in exc.message.lower()
                    and "not publicly accessible" not in exc.message.lower()
                )
                if not retryable or attempt >= self._max_retries:
                    raise
                logger.warning(
                    "Drive download retry %s/%s for file_id=%s stage=%s",
                    attempt,
                    self._max_retries,
                    parsed.file_id,
                    exc.stage.value,
                )

        assert last_error is not None
        raise last_error

    def _get_with_redirect_budget(self, url: str) -> HttpResponse:
        current = url
        for _ in range(self._max_redirects + 1):
            logger.info("Drive HTTP GET %s", redact_url_for_logs(current))
            response = self._http.request(
                "GET",
                current,
                timeout=self._timeout,
                headers={"User-Agent": "whisper-script-drive-downloader/1.0"},
                allow_redirects=False,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                nxt = resolve_redirect_url(current, location or "")
                if not is_absolute_http_url(nxt):
                    raise DownloadError(
                        DownloadStage.DOWNLOAD,
                        "Redirect target is not an absolute http(s) URL",
                        status_code=response.status_code,
                    )
                current = nxt
                continue
            return response
        raise DownloadError(
            DownloadStage.DOWNLOAD,
            f"Exceeded redirect limit ({self._max_redirects})",
        )

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

    def _write_temp(self, filename: str, body: bytes) -> Path:
        path: Path | None = None
        try:
            directory = Path(self._temp_dir) if self._temp_dir else None
            if directory is not None:
                directory.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                prefix="whisper-drive-",
                suffix=Path(filename).suffix,
                delete=False,
                dir=str(directory) if directory else None,
            )
            path = Path(handle.name)
            with handle:
                handle.write(body)
            return path
        except OSError as exc:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise DownloadError(
                DownloadStage.TEMP_STORE,
                "Failed to write downloaded Drive file to temporary storage",
                cause=exc,
            ) from exc
