"""Injectable subprocess boundary for FFmpeg and whisper-cli."""

from __future__ import annotations

import subprocess
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol, Sequence, TextIO

# Bound retained diagnostics so long-running whisper/ffmpeg jobs cannot grow
# unbounded in-process buffers. Interactive streaming still forwards live lines.
DEFAULT_DIAGNOSTIC_TAIL_CHARS = 16_384


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class SubprocessRunner(Protocol):
    def run(self, command: Sequence[str], *, cwd: Path | None = None) -> CommandResult:
        """Run a command and return captured output without raising on non-zero."""


def bounded_tail(text: str, *, max_chars: int = DEFAULT_DIAGNOSTIC_TAIL_CHARS) -> str:
    """Return the trailing portion of ``text`` capped at ``max_chars``."""
    if max_chars < 1:
        raise ValueError("max_chars must be >= 1")
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


class DefaultSubprocessRunner:
    """Capture stdout/stderr fully (automation / offline tests).

    Prefer :class:`StreamingSubprocessRunner` for long interactive jobs so child
    progress is visible and retained diagnostics stay bounded.
    """

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


class StreamingSubprocessRunner:
    """Forward child output live to a sink; retain only a bounded diagnostic tail.

    Child stdout and stderr are merged and written to ``sink`` (default:
    ``sys.stderr``) so interactive wrappers stay observable without polluting a
    machine-readable stdout contract on the parent CLI. Returned
    ``CommandResult.stderr`` holds the bounded tail only; ``stdout`` is empty.
    """

    def __init__(
        self,
        *,
        max_tail_chars: int = DEFAULT_DIAGNOSTIC_TAIL_CHARS,
        sink: TextIO | None = None,
    ) -> None:
        if max_tail_chars < 1:
            raise ValueError("max_tail_chars must be >= 1")
        self.max_tail_chars = max_tail_chars
        self.sink = sink if sink is not None else sys.stderr

    def run(self, command: Sequence[str], *, cwd: Path | None = None) -> CommandResult:
        try:
            proc = subprocess.Popen(
                list(command),
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception:
            raise

        assert proc.stdout is not None
        tail = _BoundedCharTail(self.max_tail_chars)
        try:
            _drain_stream_to_sink(proc.stdout, sink=self.sink, tail=tail)
            returncode = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()

        return CommandResult(returncode=returncode, stdout="", stderr=tail.text())


class _BoundedCharTail:
    """Rolling character buffer with a hard upper bound."""

    def __init__(self, max_chars: int) -> None:
        self._max_chars = max_chars
        self._chunks: deque[str] = deque()
        self._size = 0

    def append(self, chunk: str) -> None:
        if not chunk:
            return
        self._chunks.append(chunk)
        self._size += len(chunk)
        while self._size > self._max_chars and self._chunks:
            oldest = self._chunks.popleft()
            overflow = self._size - self._max_chars
            if overflow >= len(oldest):
                self._size -= len(oldest)
                continue
            trimmed = oldest[overflow:]
            self._chunks.appendleft(trimmed)
            self._size -= overflow
            break

    def text(self) -> str:
        return "".join(self._chunks)


def _drain_stream_to_sink(
    stream: IO[str],
    *,
    sink: TextIO,
    tail: _BoundedCharTail,
) -> None:
    while True:
        chunk = stream.read(1024)
        if chunk == "":
            break
        sink.write(chunk)
        sink.flush()
        tail.append(chunk)
