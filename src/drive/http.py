"""Minimal HTTP client abstraction for offline-testable downloads."""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request
from typing import Protocol
from urllib.parse import urljoin, urlparse

from .types import DownloadError, DownloadStage, HttpResponse
from .url import redact_url_for_logs


class HttpClient(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        timeout: float,
        headers: dict[str, str] | None = None,
        allow_redirects: bool = False,
    ) -> HttpResponse:
        """Perform one HTTP request. Redirects are never followed when False."""


class UrllibHttpClient:
    """stdlib HTTP client used in production; tests inject fakes instead."""

    def request(
        self,
        method: str,
        url: str,
        *,
        timeout: float,
        headers: dict[str, str] | None = None,
        allow_redirects: bool = False,
    ) -> HttpResponse:
        del allow_redirects  # urllib handler below never auto-follows.
        request = urllib.request.Request(url, method=method.upper(), headers=headers or {})
        opener = urllib.request.build_opener(urllib.request.HTTPHandler())
        try:
            with opener.open(request, timeout=timeout) as response:
                body = response.read()
                raw_headers = {k.lower(): v for k, v in response.headers.items()}
                return HttpResponse(
                    status_code=getattr(response, "status", 200),
                    headers=raw_headers,
                    body=body,
                    url=response.geturl(),
                )
        except urllib.error.HTTPError as exc:
            body = exc.read() if hasattr(exc, "read") else b""
            raw_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
            return HttpResponse(
                status_code=exc.code,
                headers=raw_headers,
                body=body,
                url=exc.geturl() if hasattr(exc, "geturl") else url,
            )
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


def resolve_redirect_url(current_url: str, location: str) -> str:
    if not location:
        raise DownloadError(DownloadStage.DOWNLOAD, "Redirect response missing Location")
    return urljoin(current_url, location)


def is_absolute_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
