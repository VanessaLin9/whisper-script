"""Injectable subprocess boundary for FFmpeg and whisper-cli."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol, TextIO

from src.common import CancellationToken, OperationCancelled

# Bound retained diagnostics so long-running whisper/ffmpeg jobs cannot grow
# unbounded in-process buffers. Interactive streaming still forwards live lines.
DEFAULT_DIAGNOSTIC_TAIL_CHARS = 16_384
DEFAULT_TERMINATE_WAIT_SECONDS = 2.0

logger = logging.getLogger(__name__)

ProcessFactory = Callable[..., subprocess.Popen]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class SubprocessRunner(Protocol):
    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        cancellation: CancellationToken | None = None,
        cancel_stage: str = "transcribe",
    ) -> CommandResult:
        """Run a command and return captured output without raising on non-zero."""


def bounded_tail(text: str, *, max_chars: int = DEFAULT_DIAGNOSTIC_TAIL_CHARS) -> str:
    """Return the trailing portion of ``text`` capped at ``max_chars``."""
    if max_chars < 1:
        raise ValueError("max_chars must be >= 1")
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _escalate_terminate(
    proc: subprocess.Popen[str],
    *,
    wait_seconds: float,
    use_process_group: bool,
) -> int:
    """terminate → bounded wait → kill → reap. Returns final returncode."""
    if proc.poll() is not None:
        return proc.wait()

    def _signal_group_or_proc(sig: signal.Signals) -> None:
        try:
            if use_process_group and sys.platform != "win32":
                os.killpg(proc.pid, sig)
            elif sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        except OSError:
            logger.warning(
                "subprocess signal failed pid=%s sig=%s",
                proc.pid,
                sig.name,
                exc_info=True,
            )

    _signal_group_or_proc(signal.SIGTERM)
    logger.info("subprocess terminate requested pid=%s", proc.pid)
    try:
        returncode = proc.wait(timeout=wait_seconds)
        logger.info("subprocess terminated pid=%s returncode=%s", proc.pid, returncode)
        return returncode
    except subprocess.TimeoutExpired:
        logger.warning(
            "subprocess terminate timed out pid=%s; escalating to kill",
            proc.pid,
        )

    _signal_group_or_proc(signal.SIGKILL)
    returncode = proc.wait()
    logger.info("subprocess killed pid=%s returncode=%s", proc.pid, returncode)
    return returncode


def _run_cancellable_popen(
    command: Sequence[str],
    *,
    cwd: Path | None,
    cancellation: CancellationToken | None,
    cancel_stage: str,
    stream: bool,
    sink: TextIO,
    max_tail_chars: int,
    terminate_wait_seconds: float,
    process_factory: ProcessFactory,
) -> CommandResult:
    if cancellation is not None:
        cancellation.throw_if_cancelled(cancel_stage)

    popen_kwargs: dict[str, object] = {
        "args": list(command),
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT if stream else subprocess.PIPE,
        "text": True,
        "bufsize": 1 if stream else -1,
    }
    use_process_group = sys.platform != "win32"
    if use_process_group:
        popen_kwargs["start_new_session"] = True

    proc = process_factory(**popen_kwargs)
    assert proc.stdout is not None
    stderr_pipe = proc.stderr
    tail = _BoundedCharTail(max_tail_chars)
    captured_stdout = ""
    unregister = None
    cancelled = False

    def _interrupt() -> None:
        nonlocal cancelled
        cancelled = True
        _escalate_terminate(
            proc,
            wait_seconds=terminate_wait_seconds,
            use_process_group=use_process_group,
        )

    if cancellation is not None:
        unregister = cancellation.register_interrupt(_interrupt)

    try:
        if stream:
            try:
                _drain_stream_to_sink(
                    proc.stdout,
                    sink=sink,
                    tail=tail,
                    cancellation=cancellation,
                    cancel_stage=cancel_stage,
                )
            except OperationCancelled:
                cancelled = True
                if proc.poll() is None:
                    _escalate_terminate(
                        proc,
                        wait_seconds=terminate_wait_seconds,
                        use_process_group=use_process_group,
                    )
                raise
            returncode = proc.wait()
            stderr_text = tail.text()
            stdout_text = ""
        else:
            # Non-streaming: communicate can block; interrupt closes pipes via kill.
            try:
                if cancellation is not None:
                    cancellation.throw_if_cancelled(cancel_stage)
                out, err = proc.communicate()
            except Exception:
                if cancellation is not None and cancellation.is_cancelled():
                    if proc.poll() is None:
                        _escalate_terminate(
                            proc,
                            wait_seconds=terminate_wait_seconds,
                            use_process_group=use_process_group,
                        )
                    raise OperationCancelled(stage=cancel_stage)
                raise
            returncode = proc.returncode if proc.returncode is not None else 0
            captured_stdout = out or ""
            stderr_text = bounded_tail(err or "", max_chars=max_tail_chars)

        if cancellation is not None:
            cancellation.throw_if_cancelled(cancel_stage)
        if cancelled or (cancellation is not None and cancellation.is_cancelled()):
            raise OperationCancelled(
                stage=cancel_stage,
                cleanup_detail=bounded_tail(stderr_text) or None,
            )
        return CommandResult(
            returncode=returncode,
            stdout="" if stream else captured_stdout,
            stderr=stderr_text,
        )
    finally:
        if unregister is not None:
            unregister()
        if proc.poll() is None:
            _escalate_terminate(
                proc,
                wait_seconds=terminate_wait_seconds,
                use_process_group=use_process_group,
            )
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except OSError:
            pass
        if stderr_pipe is not None:
            try:
                stderr_pipe.close()
            except OSError:
                pass


class DefaultSubprocessRunner:
    """Capture stdout/stderr (automation). Supports optional cancellation via Popen."""

    def __init__(
        self,
        *,
        terminate_wait_seconds: float = DEFAULT_TERMINATE_WAIT_SECONDS,
        process_factory: ProcessFactory | None = None,
        max_tail_chars: int = DEFAULT_DIAGNOSTIC_TAIL_CHARS,
    ) -> None:
        self.terminate_wait_seconds = terminate_wait_seconds
        self._process_factory = process_factory or subprocess.Popen
        self.max_tail_chars = max_tail_chars

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        cancellation: CancellationToken | None = None,
        cancel_stage: str = "transcribe",
    ) -> CommandResult:
        if cancellation is None:
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
        return _run_cancellable_popen(
            command,
            cwd=cwd,
            cancellation=cancellation,
            cancel_stage=cancel_stage,
            stream=False,
            sink=sys.stderr,
            max_tail_chars=self.max_tail_chars,
            terminate_wait_seconds=self.terminate_wait_seconds,
            process_factory=self._process_factory,
        )


class StreamingSubprocessRunner:
    """Forward child output live to a sink; retain only a bounded diagnostic tail."""

    def __init__(
        self,
        *,
        max_tail_chars: int = DEFAULT_DIAGNOSTIC_TAIL_CHARS,
        sink: TextIO | None = None,
        terminate_wait_seconds: float = DEFAULT_TERMINATE_WAIT_SECONDS,
        process_factory: ProcessFactory | None = None,
    ) -> None:
        if max_tail_chars < 1:
            raise ValueError("max_tail_chars must be >= 1")
        self.max_tail_chars = max_tail_chars
        self.sink = sink if sink is not None else sys.stderr
        self.terminate_wait_seconds = terminate_wait_seconds
        self._process_factory = process_factory or subprocess.Popen

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        cancellation: CancellationToken | None = None,
        cancel_stage: str = "transcribe",
    ) -> CommandResult:
        return _run_cancellable_popen(
            command,
            cwd=cwd,
            cancellation=cancellation,
            cancel_stage=cancel_stage,
            stream=True,
            sink=self.sink,
            max_tail_chars=self.max_tail_chars,
            terminate_wait_seconds=self.terminate_wait_seconds,
            process_factory=self._process_factory,
        )


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
    cancellation: CancellationToken | None = None,
    cancel_stage: str = "transcribe",
) -> None:
    while True:
        if cancellation is not None:
            cancellation.throw_if_cancelled(cancel_stage)
        try:
            chunk = stream.read(1024)
        except Exception as exc:
            if cancellation is not None and cancellation.is_cancelled():
                raise OperationCancelled(stage=cancel_stage, cause=exc) from exc
            raise
        if chunk == "":
            break
        sink.write(chunk)
        sink.flush()
        tail.append(chunk)
