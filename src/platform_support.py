"""Meeting Desk 的跨平台系統邊界。

此模組只使用 Python 標準庫，讓服務層不需要直接依賴作業系統專用 API。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterator


_EXECUTABLE_ENV = {"whisper-cli": "WHISPER_CLI", "ffmpeg": "FFMPEG", "ffprobe": "FFPROBE"}


def _expand_path(value: str | Path) -> Path:
    text = str(value)
    userprofile = os.getenv("USERPROFILE", str(Path.home()))
    text = text.replace("%USERPROFILE%", userprofile).replace("%userprofile%", userprofile)
    return Path(text).expanduser()


def is_windows() -> bool:
    return os.name == "nt" or sys.platform.startswith("win")


def is_macos() -> bool:
    return sys.platform == "darwin"


def default_meeting_root() -> Path:
    """回傳不含環境覆寫的預設會議根目錄。"""
    return Path.home() / "MeetingRecords"


def config_dir(repo: Path) -> Path:
    """回傳設定目錄；macOS/Linux 維持 repo 內 `.local`。"""
    configured = os.getenv("WHISPER_SCRIPT_CONFIG_DIR")
    if configured:
        return _expand_path(configured)
    if is_windows():
        return Path(os.getenv("APPDATA") or (Path.home() / "AppData" / "Roaming")) / "WhisperScript"
    return repo / ".local"


def data_dir(repo: Path) -> Path:
    """回傳資料目錄；macOS/Linux 維持 repo 內 `.local`。"""
    configured = os.getenv("WHISPER_SCRIPT_DATA_DIR")
    if configured:
        return _expand_path(configured)
    if is_windows():
        return Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "WhisperScript"
    return repo / ".local"


def _unlock(handle) -> None:
    if is_windows():
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """以非阻塞獨佔鎖保護檔案，並在離開 context 時釋放。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            if is_windows():
                import msvcrt
                handle.seek(0)
                if handle.tell() == 0 and path.stat().st_size == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            raise ValueError("這場會議正在另一個視窗處理，請稍後再試。") from exc
        try:
            yield
        finally:
            _unlock(handle)


def _candidate_is_file(path: Path) -> bool:
    return path.is_file()


def whisper_cli_candidates(whisper_root: str | Path) -> list[Path]:
    """列出 whisper.cpp 的既有建置輸出位置。"""
    root = _expand_path(whisper_root)
    candidates = [root / "build" / "bin" / "whisper-cli"]
    if is_windows():
        candidates.extend([
            root / "build" / "bin" / "whisper-cli.exe",
            root / "build" / "bin" / "Release" / "whisper-cli.exe",
        ])
    return candidates


def find_executable(
    name: str,
    *,
    explicit: str | Path | None = None,
    settings: dict | None = None,
    whisper_root: str | Path | None = None,
) -> Path:
    """依設定、PATH、既有預設位置順序尋找可執行檔。"""
    if name not in _EXECUTABLE_ENV:
        raise ValueError(f"不支援的執行檔：{name}")
    env_name = _EXECUTABLE_ENV[name]
    configured = explicit or (settings or {}).get(name.replace("-", "_")) or os.getenv(env_name)
    if configured:
        candidate = _expand_path(configured)
        if _candidate_is_file(candidate):
            return candidate.resolve()
    found = shutil.which(name)
    if found:
        return Path(found).resolve()

    candidates: list[Path] = []
    if name == "whisper-cli" and whisper_root:
        candidates.extend(whisper_cli_candidates(whisper_root))
    for candidate in candidates:
        if _candidate_is_file(candidate):
            return candidate.resolve()
    raise FileNotFoundError(
        f"找不到 {name}。請設定環境變數 {env_name}，或將 {name} 加入 PATH。"
    )


def copy_to_clipboard(text: str) -> None:
    """複製文字；Windows `clip` 以 UTF-16LE 接收輸入。"""
    if is_windows():
        subprocess.run(["clip"], input=text.encode("utf-16le"), check=True)
    else:
        subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)


def show_in_folder(path: Path) -> None:
    """在檔案管理器中顯示檔案或開啟資料夾。"""
    target = Path(path).expanduser().resolve()
    if is_windows():
        command = ["explorer", f"/select,{target}"] if target.is_file() else ["explorer", str(target)]
    elif is_macos():
        command = ["open", "-R", str(target)] if target.is_file() else ["open", str(target)]
    else:
        command = ["xdg-open", str(target)]
    subprocess.run(command, check=True)
