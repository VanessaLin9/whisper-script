#!/usr/bin/env python3
"""Offline tests for Checkpoint 04.1 public Google Drive downloader."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.drive import (
    DownloadError,
    DownloadStage,
    PublicDriveDownloader,
    parse_public_drive_url,
    redact_url_for_logs,
    sanitize_filename,
)
from src.drive.types import HttpResponse


class FakeHttpClient:
    def __init__(self, scripted: list[HttpResponse | Exception]) -> None:
        self._scripted = list(scripted)
        self.calls: list[tuple[str, str]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        timeout: float,
        headers: dict[str, str] | None = None,
        allow_redirects: bool = False,
    ) -> HttpResponse:
        del timeout, headers, allow_redirects
        self.calls.append((method, url))
        if not self._scripted:
            raise AssertionError(f"Unexpected HTTP call: {method} {url}")
        item = self._scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


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


class DownloaderTests(unittest.TestCase):
    def test_success_writes_temp_and_uses_content_disposition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeHttpClient(
                [
                    HttpResponse(
                        200,
                        {
                            "content-type": "audio/mp4",
                            "content-disposition": 'attachment; filename="meeting.m4a"',
                        },
                        b"AUDIODATA",
                        "https://drive.google.com/uc?export=download&id=abc123XYZ_-99",
                    )
                ]
            )
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
                HttpResponse(
                    200,
                    {"content-type": "text/html; charset=utf-8"},
                    b"<html>You need access. Request access via accounts.google.com</html>",
                    "https://drive.google.com/uc?export=download&id=abc123XYZ_-99",
                )
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
                HttpResponse(
                    200,
                    {"content-type": "text/html"},
                    b"<html><body>Could not find the file</body></html>",
                    "https://drive.google.com/uc?id=abc123XYZ_-99",
                )
            ]
        )
        downloader = PublicDriveDownloader(client)
        with self.assertRaises(DownloadError) as ctx:
            downloader.download("https://drive.google.com/file/d/abc123XYZ_-99/view")
        self.assertEqual(ctx.exception.stage, DownloadStage.VALIDATE)

    def test_confirmation_flow_then_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeHttpClient(
                [
                    HttpResponse(
                        200,
                        {"content-type": "text/html"},
                        b'<form><input name="confirm" value="tOkEn123"/></form>',
                        "https://drive.google.com/uc?export=download&id=abc123XYZ_-99",
                    ),
                    HttpResponse(
                        200,
                        {
                            "content-type": "audio/mpeg",
                            "content-disposition": 'attachment; filename="ok.mp3"',
                        },
                        b"MP3DATA",
                        "https://drive.google.com/uc?export=download&id=abc123XYZ_-99&confirm=tOkEn123",
                    ),
                ]
            )
            downloader = PublicDriveDownloader(client, temp_dir=Path(tmp))
            result = downloader.download(
                "https://drive.google.com/file/d/abc123XYZ_-99/view"
            )
            self.assertEqual(result.filename, "ok.mp3")
            self.assertEqual(result.temp_path.read_bytes(), b"MP3DATA")
            self.assertEqual(len(client.calls), 2)
            self.assertIn("confirm=tOkEn123", client.calls[1][1])

    def test_redirect_budget_exceeded(self) -> None:
        responses = [
            HttpResponse(
                302,
                {"location": f"https://drive.google.com/next{i}"},
                b"",
                f"https://drive.google.com/start{i}",
            )
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
                    HttpResponse(
                        status,
                        {"content-type": "text/plain"},
                        b"nope",
                        "https://drive.google.com/uc?id=abc123XYZ_-99",
                    )
                ]
            )
            # max_retries=1: assert status mapping without exercising retry budget
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
                HttpResponse(
                    200,
                    {"content-type": "audio/wav"},
                    b"",
                    "https://drive.google.com/uc?id=abc123XYZ_-99",
                )
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
                HttpResponse(
                    403,
                    {"content-type": "text/plain"},
                    b"denied",
                    "https://drive.google.com/uc?id=abc123XYZ_-99",
                )
            ]
        )
        downloader = PublicDriveDownloader(client, max_retries=5)
        with self.assertRaises(DownloadError) as ctx:
            downloader.download("https://drive.google.com/file/d/abc123XYZ_-99/view")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(len(client.calls), 1)

    def test_logs_do_not_include_sensitive_query(self) -> None:
        # Ensure helper used by downloader strips secrets.
        message = redact_url_for_logs(
            "https://drive.google.com/uc?id=abc&confirm=SECRET&token=COOKIE"
        )
        self.assertNotIn("SECRET", message)
        self.assertNotIn("COOKIE", message)


if __name__ == "__main__":
    unittest.main()
