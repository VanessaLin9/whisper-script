"""Minimal HTTP client abstraction for offline-testable downloads."""

from __future__ import annotations

import ssl
import tempfile
import urllib.error
import urllib.request
import urllib.response
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlparse

from .types import DownloadError, DownloadStage, HttpResponse
from .url import redact_url_for_logs

DEFAULT_CHUNK_SIZE = 64 * 1024
DEFAULT_PEEK_SIZE = 8192


class HttpClient(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        timeout: float,
        headers: dict[str, str] | None = None,
        allow_redirects: bool = False,
        temp_dir: Path | None = None,
    ) -> HttpResponse:
        """Perform one HTTP request.

        When ``allow_redirects`` is False, 3xx responses are returned as-is
        (never auto-followed). Response bodies are streamed to a temp file.
        """


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Return each 3xx response to the caller instead of following Location."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None

    def http_error_301(self, req, fp, code, msg, headers):  # noqa: ANN001
        return self._retain(req, fp, code, headers)

    http_error_302 = http_error_301
    http_error_303 = http_error_301
    http_error_307 = http_error_301
    http_error_308 = http_error_301

    def _retain(self, req, fp, code, headers):  # noqa: ANN001
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        # Fourth ctor arg sets .code; .status is read-only on modern Python.
        return urllib.response.addinfourl(fp, headers, url, code)


class UrllibHttpClient:
    """stdlib HTTP client used in production; tests inject fakes instead."""

    def __init__(
        self,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        peek_size: int = DEFAULT_PEEK_SIZE,
    ) -> None:
        self._chunk_size = chunk_size
        self._peek_size = peek_size

    def request(
        self,
        method: str,
        url: str,
        *,
        timeout: float,
        headers: dict[str, str] | None = None,
        allow_redirects: bool = False,
        temp_dir: Path | None = None,
    ) -> HttpResponse:
        request = urllib.request.Request(
            url, method=method.upper(), headers=headers or {}
        )
        handlers: list[urllib.request.BaseHandler] = [
            urllib.request.HTTPSHandler(),
            urllib.request.HTTPHandler(),
        ]
        if not allow_redirects:
            handlers.insert(0, _NoRedirectHandler())
        opener = urllib.request.build_opener(*handlers)
        try:
            with opener.open(request, timeout=timeout) as response:
                return self._stream_response(response, fallback_url=url, temp_dir=temp_dir)
        except urllib.error.HTTPError as exc:
            # 3xx should be retained by _NoRedirectHandler; other HTTPError
            # bodies are still streamed for status mapping / HTML checks.
            return self._stream_response(exc, fallback_url=url, temp_dir=temp_dir)
        except urllib.error.URLError as exc:
            raise DownloadError(
                DownloadStage.DOWNLOAD,
                f"Network error requesting {redact_url_for_logs(url)}: {exc.reason}",
                cause=exc,
            ) from exc
        except TimeoutError as exc:
            raise DownloadError(
                DownloadStage.DOWNLOAD,
                f"Timed out requesting {redact_url_for_logs(url)}",
                cause=exc,
            ) from exc
        except ssl.SSLError as exc:
            raise DownloadError(
                DownloadStage.DOWNLOAD,
                f"TLS error requesting {redact_url_for_logs(url)}",
                cause=exc,
            ) from exc

    def _stream_response(
        self,
        response: object,
        *,
        fallback_url: str,
        temp_dir: Path | None,
    ) -> HttpResponse:
        status = int(getattr(response, "status", None) or getattr(response, "code", 200))
        raw_headers_obj = getattr(response, "headers", None) or {}
        raw_headers = {str(k).lower(): v for k, v in raw_headers_obj.items()}
        final_url = getattr(response, "geturl", lambda: fallback_url)()
        path: Path | None = None
        peek = bytearray()
        size = 0
        try:
            directory = Path(temp_dir) if temp_dir is not None else None
            if directory is not None:
                directory.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                prefix="whisper-drive-http-",
                suffix=".part",
                delete=False,
                dir=str(directory) if directory else None,
            )
            path = Path(handle.name)
            with handle:
                while True:
                    chunk = response.read(self._chunk_size)  # type: ignore[attr-defined]
                    if not chunk:
                        break
                    handle.write(chunk)
                    size += len(chunk)
                    if len(peek) < self._peek_size:
                        need = self._peek_size - len(peek)
                        peek.extend(chunk[:need])
            if size == 0:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                path = None
            return HttpResponse(
                status_code=status,
                headers=raw_headers,
                url=final_url,
                peek=bytes(peek),
                size_bytes=size,
                body_path=path,
            )
        except (OSError, TimeoutError, ssl.SSLError) as exc:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            stage = (
                DownloadStage.TEMP_STORE
                if isinstance(exc, OSError) and not isinstance(exc, TimeoutError)
                else DownloadStage.DOWNLOAD
            )
            raise DownloadError(
                stage,
                f"Failed while streaming response from {redact_url_for_logs(fallback_url)}",
                cause=exc,
            ) from exc


def cleanup_http_body(response: HttpResponse) -> None:
    """Best-effort removal of a streamed body file."""
    if response.body_path is None:
        return
    try:
        response.body_path.unlink(missing_ok=True)
    except OSError:
        pass


def resolve_redirect_url(current_url: str, location: str) -> str:
    if not location:
        raise DownloadError(DownloadStage.DOWNLOAD, "Redirect response missing Location")
    return urljoin(current_url, location)


def is_absolute_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
