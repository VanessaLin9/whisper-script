"""平台相關層的離線單元測試。"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import src.platform_support as platform_support


class PlatformSupportTests(unittest.TestCase):
    def test_macos_defaults_keep_home_meeting_records_and_repo_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(platform_support, "is_windows", return_value=False):
            repo = Path(tmp) / "repo"
            self.assertEqual(platform_support.default_meeting_root(), Path.home() / "MeetingRecords")
            self.assertEqual(platform_support.config_dir(repo), repo / ".local")
            self.assertEqual(platform_support.data_dir(repo), repo / ".local")

    def test_windows_defaults_and_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(platform_support, "is_windows", return_value=True), patch.dict(
            os.environ,
            {"APPDATA": str(Path(tmp) / "roaming"), "LOCALAPPDATA": str(Path(tmp) / "local")},
            clear=False,
        ):
            repo = Path(tmp) / "repo"
            self.assertEqual(platform_support.config_dir(repo), Path(tmp) / "roaming" / "WhisperScript")
            self.assertEqual(platform_support.data_dir(repo), Path(tmp) / "local" / "WhisperScript")
            with patch.dict(os.environ, {"WHISPER_SCRIPT_CONFIG_DIR": "%USERPROFILE%/cfg", "WHISPER_SCRIPT_DATA_DIR": str(Path(tmp) / "data")}):
                self.assertEqual(platform_support.data_dir(repo), Path(tmp) / "data")
                self.assertEqual(platform_support.config_dir(repo), Path.home() / "cfg")

    def test_executable_order_and_windows_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(platform_support, "is_windows", return_value=True):
            root = Path(tmp) / "whisper"
            release = root / "build" / "bin" / "Release" / "whisper-cli.exe"
            release.parent.mkdir(parents=True)
            release.write_bytes(b"fake")
            with patch.object(platform_support.shutil, "which", return_value=None):
                self.assertEqual(platform_support.find_executable("whisper-cli", whisper_root=root), release.resolve())
            explicit = Path(tmp) / "explicit.exe"
            explicit.write_bytes(b"fake")
            with patch.object(platform_support.shutil, "which", return_value=str(explicit)):
                self.assertEqual(platform_support.find_executable("whisper-cli", explicit=explicit, whisper_root=root), explicit.resolve())

    def test_executable_error_names_environment_variable(self) -> None:
        with patch.object(platform_support.shutil, "which", return_value=None):
            with self.assertRaisesRegex(FileNotFoundError, "FFPROBE"):
                platform_support.find_executable("ffprobe")

    def test_lock_release_and_busy_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".desktop.lock"
            with platform_support.file_lock(path):
                with self.assertRaisesRegex(ValueError, "另一個視窗"):
                    with platform_support.file_lock(path):
                        pass
            with platform_support.file_lock(path):
                pass

    def test_windows_lock_uses_msvcrt_and_releases(self) -> None:
        calls = []
        fake_msvcrt = types.SimpleNamespace(
            LK_NBLCK=1,
            LK_UNLCK=2,
            locking=lambda fd, mode, size: calls.append((fd, mode, size)),
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(platform_support, "is_windows", return_value=True), patch.dict(
            sys.modules, {"msvcrt": fake_msvcrt}
        ):
            with platform_support.file_lock(Path(tmp) / ".desktop.lock"):
                self.assertEqual(calls[-1][1], fake_msvcrt.LK_NBLCK)
            self.assertEqual(calls[-1][1], fake_msvcrt.LK_UNLCK)

    def test_windows_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(platform_support, "is_windows", return_value=True), patch.object(platform_support.subprocess, "run") as run:
            platform_support.copy_to_clipboard("中文")
            self.assertEqual(run.call_args.args[0], ["clip"])
            self.assertEqual(run.call_args.kwargs["input"], "中文".encode("utf-16le"))
            target = Path(tmp) / "a.txt"
            target.write_text("x", encoding="utf-8")
            platform_support.show_in_folder(target)
            self.assertEqual(run.call_args.args[0][0], "explorer")
            self.assertIn("/select,", run.call_args.args[0][1])


if __name__ == "__main__":
    unittest.main()
