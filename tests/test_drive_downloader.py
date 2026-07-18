#!/usr/bin/env python3
"""Offline tests for Checkpoint 04.1 public Google Drive downloader."""

from __future__ import annotations

import logging
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from src.drive import (
    DownloadError,
    DownloadStage,
    PublicDriveDownloader,
    file_ref_for_logs,
    parse_public_drive_url,
    redact_url_for_logs,
    resolve_download_filename,
    sanitize_filename,
)
from src.drive.http import UrllibHttpClient, cleanup_http_body
from src.drive.types import HttpResponse


class FakeHttpClient:
    """Streams scripted bodies to temp files in chunks (mirrors production)."""

    def __init__(
        self,
        scripted: list[HttpResponse | Exception | dict],
        *,
        chunk_size: int = 4,
    ) -> None:
        self._scripted = list(scripted)
        self.calls: list[tuple[str, str]] = []
        self._chunk_size = chunk_size

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
        del timeout, headers, allow_redirects
        self.calls.append((method, url))
        if not self._scripted:
            raise AssertionError(f"Unexpected HTTP call: {method} {url}")
        item = self._scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict):
            return self._stream_dict(item, temp_dir=temp_dir)
        # Legacy HttpResponse with in-memory body: stream it.
        return self._stream_bytes(
            status_code=item.status_code,
            headers=item.headers,
            url=item.url,
            body=item.peek if item.body_path is None and item.size_bytes == 0 else (
                item.body_path.read_bytes() if item.body_path else item.peek
            ),
            temp_dir=temp_dir,
            fail_after=None,
        )

    def _stream_dict(self, item: dict, *, temp_dir: Path | None) -> HttpResponse:
        return self._stream_bytes(
            status_code=int(item["status_code"]),
            headers=dict(item.get("headers") or {}),
            url=str(item["url"]),
            body=bytes(item.get("body") or b""),
            temp_dir=temp_dir,
            fail_after=item.get("fail_after"),
        )

    def _stream_bytes(
        self,
        *,
        status_code: int,
        headers: dict[str, str],
        url: str,
        body: bytes,
        temp_dir: Path | None,
        fail_after: int | None,
    ) -> HttpResponse:
        directory = Path(temp_dir) if temp_dir is not None else None
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
        path: Path | None = None
        peek = bytearray()
        size = 0
        handle = tempfile.NamedTemporaryFile(
            prefix="fake-drive-",
            suffix=".part",
            delete=False,
            dir=str(directory) if directory else None,
        )
        path = Path(handle.name)
        try:
            with handle:
                offset = 0
                while offset < len(body):
                    if fail_after is not None and size >= fail_after:
                        raise DownloadError(
                            DownloadStage.DOWNLOAD,
                            "Simulated stream transport failure",
                        )
                    end = min(offset + self._chunk_size, len(body))
                    if fail_after is not None:
                        end = min(end, offset + max(0, fail_after - size))
                        if end <= offset:
                            raise DownloadError(
                                DownloadStage.DOWNLOAD,
                                "Simulated stream transport failure",
                            )
                    chunk = body[offset:end]
                    handle.write(chunk)
                    size += len(chunk)
                    offset = end
                    if len(peek) < 8192:
                        need = 8192 - len(peek)
                        peek.extend(chunk[:need])
                    if fail_after is not None and size >= fail_after and offset < len(body):
                        raise DownloadError(
                            DownloadStage.DOWNLOAD,
                            "Simulated stream transport failure",
                        )
            if size == 0:
                path.unlink(missing_ok=True)
                path = None
            return HttpResponse(
                status_code=status_code,
                headers={k.lower(): v for k, v in headers.items()},
                url=url,
                peek=bytes(peek),
                size_bytes=size,
                body_path=path,
            )
        except Exception:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise


def _ok_audio(
    body: bytes = b"AUDIODATA",
    *,
    filename: str = "meeting.m4a",
    content_type: str = "audio/mp4",
) -> dict:
    return {
        "status_code": 200,
        "headers": {
            "content-type": content_type,
            "content-disposition": f'attachment; filename="{filename}"',
        },
        "body": body,
        "url": "https://drive.google.com/uc?export=download&id=abc123XYZ_-99",
    }


class UrlParserTests(unittest.TestCase):
    def test_file_d_path(self) -> None:
        parsed = parse_public_drive_url(
            "https://drive.google.com/file/d/abc123XYZ_-99/view?usp=sharing"
        )
        self.assertEqual(parsed.file_id, "abc123XYZ_-99")
        self.assertIn("id=abc123XYZ_-99", parsed.canonical_url)
        self.assertNotIn("usp=sharing", parsed.canonical_url)

    def test_open_id_query(self) -> None:
        parsed = parse_public_drive_url(
            "https://drive.google.com/open?id=abc123XYZ_-99"
        )
        self.assertEqual(parsed.file_id, "abc123XYZ_-99")

    def test_uc_export_url(self) -> None:
        parsed = parse_public_drive_url(
            "https://docs.google.com/uc?id=abc123XYZ_-99&export=download"
        )
        self.assertEqual(parsed.file_id, "abc123XYZ_-99")

    def test_unsupported_host_and_missing_id(self) -> None:
        with self.assertRaises(DownloadError) as ctx:
            parse_public_drive_url("https://example.com/file/d/abc123XYZ_-99/view")
        self.assertEqual(ctx.exception.stage, DownloadStage.PARSE_URL)

        with self.assertRaises(DownloadError) as ctx2:
            parse_public_drive_url("https://drive.google.com/drive/my-drive")
        self.assertEqual(ctx2.exception.stage, DownloadStage.PARSE_URL)

    def test_redact_url_strips_query(self) -> None:
        redacted = redact_url_for_logs(
            "https://drive.google.com/uc?export=download&id=abc&confirm=TOKEN&token=secret"
        )
        self.assertNotIn("confirm=", redacted)
        self.assertNotIn("token=", redacted)
        self.assertNotIn("secret", redacted)


class SanitizeTests(unittest.TestCase):
    def test_rejects_unsafe_suffix(self) -> None:
        with self.assertRaises(DownloadError) as ctx:
            sanitize_filename("payload.exe")
        self.assertEqual(ctx.exception.stage, DownloadStage.VALIDATE)

    def test_keeps_audio_suffix(self) -> None:
        self.assertEqual(sanitize_filename("會議 錄音.m4a"), "會議 錄音.m4a")

    def test_rejects_missing_suffix(self) -> None:
        with self.assertRaises(DownloadError) as ctx:
            sanitize_filename("noext")
        self.assertEqual(ctx.exception.stage, DownloadStage.VALIDATE)

    def test_mime_inference_and_reject_non_media(self) -> None:
        self.assertEqual(
            resolve_download_filename(
                content_disposition=None,
                content_type="audio/mpeg",
            ),
            "drive_audio.mp3",
        )
        with self.assertRaises(DownloadError) as ctx:
            resolve_download_filename(
                content_disposition=None,
                content_type="application/pdf",
            )
        self.assertEqual(ctx.exception.stage, DownloadStage.VALIDATE)
        with self.assertRaises(DownloadError) as ctx2:
            resolve_download_filename(
                content_disposition=None,
                content_type=None,
            )
        self.assertEqual(ctx2.exception.stage, DownloadStage.VALIDATE)


class UrllibClientTests(unittest.TestCase):
    def test_does_not_auto_follow_redirects(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:  # noqa: A003
                return

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", "/final")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.end_headers()
                self.wfile.write(b"FINAL")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            client = UrllibHttpClient(chunk_size=2, peek_size=8)
            with tempfile.TemporaryDirectory() as tmp:
                response = client.request(
                    "GET",
                    f"http://127.0.0.1:{port}/start",
                    timeout=2.0,
                    allow_redirects=False,
                    temp_dir=Path(tmp),
                )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers.get("location"), "/final")
                cleanup_http_body(response)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class DownloaderTests(unittest.TestCase):
    def test_success_writes_temp_and_uses_content_disposition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeHttpClient([_ok_audio()])
            downloader = PublicDriveDownloader(client, temp_dir=Path(tmp))
            result = downloader.download(
                "https://drive.google.com/file/d/abc123XYZ_-99/view"
            )
            self.assertEqual(result.filename, "meeting.m4a")
            self.assertEqual(result.size_bytes, 9)
            self.assertTrue(result.temp_path.is_file())
            self.assertEqual(result.temp_path.read_bytes(), b"AUDIODATA")
            self.assertTrue(str(result.temp_path).startswith(tmp))

    def test_unsupported_url_never_calls_http(self) -> None:
        client = FakeHttpClient([])
        downloader = PublicDriveDownloader(client)
        with self.assertRaises(DownloadError) as ctx:
            downloader.download("https://example.com/x")
        self.assertEqual(ctx.exception.stage, DownloadStage.PARSE_URL)
        self.assertEqual(client.calls, [])

    def test_permission_html_page(self) -> None:
        client = FakeHttpClient(
            [
                {
                    "status_code": 200,
                    "headers": {"content-type": "text/html; charset=utf-8"},
                    "body": b"<html>You need access. Request access via accounts.google.com</html>",
                    "url": "https://drive.google.com/uc?export=download&id=abc123XYZ_-99",
                }
            ]
        )
        downloader = PublicDriveDownloader(client)
        with self.assertRaises(DownloadError) as ctx:
            downloader.download("https://drive.google.com/file/d/abc123XYZ_-99/view")
        self.assertEqual(ctx.exception.stage, DownloadStage.DOWNLOAD)
        self.assertIn("not publicly accessible", ctx.exception.message)

    def test_html_without_confirm_is_validation_error(self) -> None:
        client = FakeHttpClient(
            [
                {
                    "status_code": 200,
                    "headers": {"content-type": "text/html"},
                    "body": b"<html><body>Could not find the file</body></html>",
                    "url": "https://drive.google.com/uc?id=abc123XYZ_-99",
                }
            ]
        )
        downloader = PublicDriveDownloader(client)
        with self.assertRaises(DownloadError) as ctx:
            downloader.download("https://drive.google.com/file/d/abc123XYZ_-99/view")
        self.assertEqual(ctx.exception.stage, DownloadStage.VALIDATE)

    def test_confirmation_with_max_retries_one_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeHttpClient(
                [
                    {
                        "status_code": 200,
                        "headers": {"content-type": "text/html"},
                        "body": b'<form><input name="confirm" value="tOkEn123"/></form>',
                        "url": "https://drive.google.com/uc?export=download&id=abc123XYZ_-99",
                    },
                    {
                        "status_code": 200,
                        "headers": {
                            "content-type": "audio/mpeg",
                            "content-disposition": 'attachment; filename="ok.mp3"',
                        },
                        "body": b"MP3DATA",
                        "url": "https://drive.google.com/uc?export=download&id=abc123XYZ_-99&confirm=tOkEn123",
                    },
                ]
            )
            downloader = PublicDriveDownloader(
                client, temp_dir=Path(tmp), max_retries=1
            )
            result = downloader.download(
                "https://drive.google.com/file/d/abc123XYZ_-99/view"
            )
            self.assertEqual(result.filename, "ok.mp3")
            self.assertEqual(result.temp_path.read_bytes(), b"MP3DATA")
            self.assertEqual(len(client.calls), 2)
            self.assertIn("confirm=tOkEn123", client.calls[1][1])

    def test_confirmation_limit_exhausted(self) -> None:
        confirm_html = {
            "status_code": 200,
            "headers": {"content-type": "text/html"},
            "body": b'<form><input name="confirm" value="tOkEn123"/></form>',
            "url": "https://drive.google.com/uc?export=download&id=abc123XYZ_-99",
        }
        client = FakeHttpClient([confirm_html, confirm_html, confirm_html])
        downloader = PublicDriveDownloader(client, max_confirmations=1, max_retries=3)
        with self.assertRaises(DownloadError) as ctx:
            downloader.download("https://drive.google.com/file/d/abc123XYZ_-99/view")
        self.assertEqual(ctx.exception.stage, DownloadStage.VALIDATE)
        self.assertIn("confirmation limit", ctx.exception.message.lower())
        self.assertIsInstance(ctx.exception, DownloadError)
        self.assertEqual(len(client.calls), 2)

    def test_redirect_budget_exceeded(self) -> None:
        responses = [
            {
                "status_code": 302,
                "headers": {"location": f"https://drive.google.com/next{i}"},
                "body": b"",
                "url": f"https://drive.google.com/start{i}",
            }
            for i in range(6)
        ]
        client = FakeHttpClient(responses)
        downloader = PublicDriveDownloader(client, max_redirects=3)
        with self.assertRaises(DownloadError) as ctx:
            downloader.download("https://drive.google.com/file/d/abc123XYZ_-99/view")
        self.assertEqual(ctx.exception.stage, DownloadStage.DOWNLOAD)
        self.assertIn("redirect limit", ctx.exception.message)

    def test_404_and_5xx(self) -> None:
        for status, needle in ((404, "not found"), (503, "server error")):
            client = FakeHttpClient(
                [
                    {
                        "status_code": status,
                        "headers": {"content-type": "text/plain"},
                        "body": b"nope",
                        "url": "https://drive.google.com/uc?id=abc123XYZ_-99",
                    }
                ]
            )
            downloader = PublicDriveDownloader(client, max_retries=1)
            with self.assertRaises(DownloadError) as ctx:
                downloader.download(
                    "https://drive.google.com/file/d/abc123XYZ_-99/view"
                )
            self.assertEqual(ctx.exception.stage, DownloadStage.DOWNLOAD)
            self.assertEqual(ctx.exception.status_code, status)
            self.assertIn(needle, ctx.exception.message.lower())

    def test_empty_body(self) -> None:
        client = FakeHttpClient(
            [
                {
                    "status_code": 200,
                    "headers": {"content-type": "audio/wav"},
                    "body": b"",
                    "url": "https://drive.google.com/uc?id=abc123XYZ_-99",
                }
            ]
        )
        downloader = PublicDriveDownloader(client)
        with self.assertRaises(DownloadError) as ctx:
            downloader.download("https://drive.google.com/file/d/abc123XYZ_-99/view")
        self.assertEqual(ctx.exception.stage, DownloadStage.VALIDATE)

    def test_timeout_retries_then_fails(self) -> None:
        client = FakeHttpClient(
            [
                DownloadError(DownloadStage.DOWNLOAD, "Timed out requesting x"),
                DownloadError(DownloadStage.DOWNLOAD, "Timed out requesting x"),
            ]
        )
        downloader = PublicDriveDownloader(client, max_retries=2)
        with self.assertRaises(DownloadError) as ctx:
            downloader.download("https://drive.google.com/file/d/abc123XYZ_-99/view")
        self.assertEqual(ctx.exception.stage, DownloadStage.DOWNLOAD)
        self.assertEqual(len(client.calls), 2)

    def test_4xx_does_not_retry(self) -> None:
        client = FakeHttpClient(
            [
                {
                    "status_code": 403,
                    "headers": {"content-type": "text/plain"},
                    "body": b"denied",
                    "url": "https://drive.google.com/uc?id=abc123XYZ_-99",
                }
            ]
        )
        downloader = PublicDriveDownloader(client, max_retries=5)
        with self.assertRaises(DownloadError) as ctx:
            downloader.download("https://drive.google.com/file/d/abc123XYZ_-99/view")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(len(client.calls), 1)

    def test_stream_failure_cleans_partial_and_is_typed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeHttpClient(
                [
                    {
                        "status_code": 200,
                        "headers": {
                            "content-type": "audio/mpeg",
                            "content-disposition": 'attachment; filename="x.mp3"',
                        },
                        "body": b"0123456789abcdef",
                        "url": "https://drive.google.com/uc?id=abc123XYZ_-99",
                        "fail_after": 4,
                    }
                ],
                chunk_size=2,
            )
            downloader = PublicDriveDownloader(
                client, temp_dir=Path(tmp), max_retries=1
            )
            with self.assertRaises(DownloadError) as ctx:
                downloader.download(
                    "https://drive.google.com/file/d/abc123XYZ_-99/view"
                )
            self.assertEqual(ctx.exception.stage, DownloadStage.DOWNLOAD)
            leftovers = list(Path(tmp).glob("fake-drive-*")) + list(
                Path(tmp).glob("whisper-drive-*")
            )
            self.assertEqual(leftovers, [])

    def test_rejects_non_media_without_disposition_filename(self) -> None:
        client = FakeHttpClient(
            [
                {
                    "status_code": 200,
                    "headers": {"content-type": "application/pdf"},
                    "body": b"%PDF-1.4",
                    "url": "https://drive.google.com/uc?id=abc123XYZ_-99",
                }
            ]
        )
        downloader = PublicDriveDownloader(client)
        with self.assertRaises(DownloadError) as ctx:
            downloader.download("https://drive.google.com/file/d/abc123XYZ_-99/view")
        self.assertEqual(ctx.exception.stage, DownloadStage.VALIDATE)

    def test_infers_suffix_from_audio_mime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeHttpClient(
                [
                    {
                        "status_code": 200,
                        "headers": {"content-type": "audio/wav"},
                        "body": b"RIFFdata",
                        "url": "https://drive.google.com/uc?id=abc123XYZ_-99",
                    }
                ]
            )
            downloader = PublicDriveDownloader(client, temp_dir=Path(tmp))
            result = downloader.download(
                "https://drive.google.com/file/d/abc123XYZ_-99/view"
            )
            self.assertEqual(result.filename, "drive_audio.wav")
            self.assertEqual(result.temp_path.read_bytes(), b"RIFFdata")

    def test_logs_do_not_include_file_id_or_sensitive_query(self) -> None:
        file_id = "abc123XYZ_-99"
        ref = file_ref_for_logs(file_id)
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeHttpClient(
                [
                    {
                        "status_code": 200,
                        "headers": {"content-type": "text/html"},
                        "body": b'<form><input name="confirm" value="SECRETTOK"/></form>',
                        "url": f"https://drive.google.com/uc?export=download&id={file_id}&confirm=SECRETTOK",
                    },
                    _ok_audio(b"MP3DATA", filename="ok.mp3", content_type="audio/mpeg"),
                ]
            )
            downloader = PublicDriveDownloader(
                client, temp_dir=Path(tmp), max_retries=1
            )
            with self.assertLogs("src.drive.downloader", level="INFO") as captured:
                downloader.download(
                    f"https://drive.google.com/file/d/{file_id}/view"
                )
            joined = "\n".join(captured.output)
            self.assertNotIn(file_id, joined)
            self.assertNotIn("SECRETTOK", joined)
            self.assertNotIn("confirm=", joined)
            self.assertIn(ref, joined)

    def test_invalid_constructor_limits(self) -> None:
        client = FakeHttpClient([])
        with self.assertRaises(ValueError):
            PublicDriveDownloader(client, max_retries=0)
        with self.assertRaises(ValueError):
            PublicDriveDownloader(client, max_redirects=-1)
        with self.assertRaises(ValueError):
            PublicDriveDownloader(client, max_confirmations=-1)
        with self.assertRaises(ValueError):
            PublicDriveDownloader(client, timeout_seconds=0)


if __name__ == "__main__":
    unittest.main()
