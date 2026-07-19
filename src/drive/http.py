"""Minimal HTTP client abstraction for offline-testable downloads."""

from __future__ import annotations

import http.client
import logging
import socket
import ssl
import tempfile
import urllib.error
import urllib.request
import urllib.response
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlparse

from src.common import CancellationToken, OperationCancelled

from .types import DownloadError, DownloadStage, HttpResponse
from .url import redact_url_for_logs

logger = logging.getLogger(__name__)

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
        cancellation: CancellationToken | None = None,
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


class _ConnectionTracker:
    """Track live HTTP(S) connections/responses so cancel can close them."""

    def __init__(self) -> None:
        self._objects: list[object] = []

    def track(self, obj: object) -> None:
        self._objects.append(obj)

    def close_all(self) -> None:
        for obj in list(self._objects):
            sock = getattr(obj, "sock", None)
            if sock is None:
                fp = getattr(obj, "fp", None)
                sock = getattr(fp, "raw", None) if fp is not None else None
                if sock is None and fp is not None:
                    sock = getattr(fp, "socket", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    sock.close()
                except Exception:
                    pass
            try:
                close = getattr(obj, "close", None)
                if callable(close):
                    close()
            except Exception:  # pragma: no cover - best-effort interrupt
                logger.debug("Failed closing tracked HTTP object on cancel", exc_info=True)


def _make_tracked_handlers(
    tracker: _ConnectionTracker,
    *,
    allow_redirects: bool,
) -> list[urllib.request.BaseHandler]:
    class TrackedHTTPConnection(http.client.HTTPConnection):
        def connect(self) -> None:  # noqa: D401 - stdlib override
            super().connect()
            tracker.track(self)

    class TrackedHTTPSConnection(http.client.HTTPSConnection):
        def connect(self) -> None:  # noqa: D401 - stdlib override
            super().connect()
            tracker.track(self)

    class TrackedHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):  # noqa: ANN001
            return self.do_open(TrackedHTTPConnection, req)

    class TrackedHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):  # noqa: ANN001
            return self.do_open(TrackedHTTPSConnection, req)

    handlers: list[urllib.request.BaseHandler] = [
        TrackedHTTPSHandler(),
        TrackedHTTPHandler(),
    ]
    if not allow_redirects:
        handlers.insert(0, _NoRedirectHandler())
    return handlers


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
        cancellation: CancellationToken | None = None,
    ) -> HttpResponse:
        if cancellation is not None:
            cancellation.throw_if_cancelled(DownloadStage.DOWNLOAD.value)
        request = urllib.request.Request(
            url, method=method.upper(), headers=headers or {}
        )
        tracker = _ConnectionTracker()
        handlers = _make_tracked_handlers(tracker, allow_redirects=allow_redirects)
        opener = urllib.request.build_opener(*handlers)
        unregister = None
        if cancellation is not None:
            unregister = cancellation.register_interrupt(tracker.close_all)
        try:
            try:
                response = opener.open(request, timeout=timeout)
            except OperationCancelled:
                raise
            except (
                urllib.error.URLError,
                TimeoutError,
                ssl.SSLError,
                http.client.HTTPException,
                ConnectionError,
                OSError,
            ) as exc:
                if cancellation is not None and cancellation.is_cancelled():
                    raise OperationCancelled(
                        stage=DownloadStage.DOWNLOAD.value,
                        cause=exc,
                    ) from exc
                if isinstance(exc, urllib.error.HTTPError):
                    # 3xx should be retained by _NoRedirectHandler; other HTTPError
                    # bodies are still streamed for status mapping / HTML checks.
                    tracker.track(exc)
                    return self._stream_response(
                        exc,
                        fallback_url=url,
                        temp_dir=temp_dir,
                        cancellation=cancellation,
                        tracker=tracker,
                    )
                if isinstance(exc, urllib.error.URLError):
                    raise DownloadError(
                        DownloadStage.DOWNLOAD,
                        f"Network error requesting {redact_url_for_logs(url)}: {exc.reason}",
                        cause=exc,
                    ) from exc
                if isinstance(exc, TimeoutError):
                    raise DownloadError(
                        DownloadStage.DOWNLOAD,
                        f"Timed out requesting {redact_url_for_logs(url)}",
                        cause=exc,
                    ) from exc
                if isinstance(exc, ssl.SSLError):
                    raise DownloadError(
                        DownloadStage.DOWNLOAD,
                        f"TLS error requesting {redact_url_for_logs(url)}",
                        cause=exc,
                    ) from exc
                raise DownloadError(
                    DownloadStage.DOWNLOAD,
                    f"Failed while opening {redact_url_for_logs(url)}",
                    cause=exc,
                ) from exc

            tracker.track(response)
            if cancellation is not None:
                cancellation.throw_if_cancelled(DownloadStage.DOWNLOAD.value)
            try:
                return self._stream_response(
                    response,
                    fallback_url=url,
                    temp_dir=temp_dir,
                    cancellation=cancellation,
                    tracker=tracker,
                )
            finally:
                try:
                    response.close()
                except Exception:
                    pass
        finally:
            if unregister is not None:
                unregister()

    def _stream_response(
        self,
        response: object,
        *,
        fallback_url: str,
        temp_dir: Path | None,
        cancellation: CancellationToken | None = None,
        tracker: _ConnectionTracker | None = None,
    ) -> HttpResponse:
        del tracker  # tracked for interrupt close; unused in stream body
        status = int(getattr(response, "status", None) or getattr(response, "code", 200))
        raw_headers_obj = getattr(response, "headers", None) or {}
        raw_headers = {str(k).lower(): v for k, v in raw_headers_obj.items()}
        final_url = getattr(response, "geturl", lambda: fallback_url)()
        redacted = redact_url_for_logs(fallback_url)
        path: Path | None = None
        handle = None
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
        except OSError as exc:
            raise DownloadError(
                DownloadStage.TEMP_STORE,
                f"Failed to create temporary file for {redacted}",
                cause=exc,
            ) from exc

        try:
            while True:
                if cancellation is not None:
                    cancellation.throw_if_cancelled(DownloadStage.DOWNLOAD.value)
                try:
                    chunk = response.read(self._chunk_size)  # type: ignore[attr-defined]
                except OperationCancelled:
                    raise
                except (
                    TimeoutError,
                    ssl.SSLError,
                    http.client.HTTPException,
                    ConnectionError,
                    OSError,
                    ValueError,
                ) as exc:
                    if cancellation is not None and cancellation.is_cancelled():
                        raise OperationCancelled(
                            stage=DownloadStage.DOWNLOAD.value,
                            cause=exc,
                        ) from exc
                    raise DownloadError(
                        DownloadStage.DOWNLOAD,
                        f"Failed while reading response from {redacted}",
                        cause=exc,
                    ) from exc
                if not chunk:
                    break
                try:
                    handle.write(chunk)
                except OSError as exc:
                    raise DownloadError(
                        DownloadStage.TEMP_STORE,
                        f"Failed while writing streamed response from {redacted}",
                        cause=exc,
                    ) from exc
                size += len(chunk)
                if len(peek) < self._peek_size:
                    need = self._peek_size - len(peek)
                    peek.extend(chunk[:need])
            try:
                handle.close()
            except OSError as exc:
                raise DownloadError(
                    DownloadStage.TEMP_STORE,
                    f"Failed while closing streamed response from {redacted}",
                    cause=exc,
                ) from exc
            handle = None
        except (DownloadError, OperationCancelled):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

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


def cleanup_http_body(response: HttpResponse) -> str | None:
    """Best-effort removal of a streamed body file.

    Returns ``None`` on success (or when there is no body). Returns a short
    diagnostic string when unlink fails so callers can attach
    ``OperationCancelled.cleanup_detail`` without treating cleanup failure as
    a different terminal outcome.
    """
    if response.body_path is None:
        return None
    try:
        response.body_path.unlink(missing_ok=True)
        return None
    except OSError as exc:
        detail = f"cleanup failed: {exc}"
        logger.warning(
            "Failed to remove streamed Drive body path=%s detail=%s",
            response.body_path,
            detail,
        )
        return detail


def resolve_redirect_url(current_url: str, location: str) -> str:
    if not location:
        raise DownloadError(DownloadStage.DOWNLOAD, "Redirect response missing Location")
    return urljoin(current_url, location)


def is_absolute_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
