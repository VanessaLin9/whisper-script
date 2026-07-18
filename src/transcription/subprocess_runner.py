"""Injectable subprocess boundary for FFmpeg and whisper-cli."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class SubprocessRunner(Protocol):
    def run(self, command: Sequence[str], *, cwd: Path | None = None) -> CommandResult:
        """Run a command and return captured output without raising on non-zero."""


class DefaultSubprocessRunner:
    """Production runner using ``subprocess.run``."""

    def run(self, command: Sequence[str], *, cwd: Path | None = None) -> CommandResult:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
