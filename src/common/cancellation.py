"""Thread-safe cooperative cancellation contract.

Cancellation is a distinct terminal outcome — not success and not a generic
failure. Controllers are idempotent: the first ``cancel()`` wins; later calls
are no-ops that do not invent a second terminal event.

Workers may register interrupt callbacks so blocking I/O (socket close, etc.)
can be aborted promptly instead of waiting only on poll boundaries.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)

InterruptCallback = Callable[[], None]


@dataclass
class OperationCancelled(Exception):
    """Typed cancelled terminal outcome for long-running backend work."""

    stage: str
    message: str = "Operation cancelled"
    cleanup_detail: str | None = None
    cause: BaseException | None = field(default=None, repr=False)

    def __str__(self) -> str:  # pragma: no cover - trivial
        detail = f" ({self.cleanup_detail})" if self.cleanup_detail else ""
        return f"[cancelled:{self.stage}] {self.message}{detail}"


class CancellationToken(Protocol):
    """Read-only view used by workers to poll / raise cooperative cancellation."""

    def is_cancelled(self) -> bool:
        """Return True once the owning controller has requested cancellation."""

    def throw_if_cancelled(self, stage: str) -> None:
        """Raise :class:`OperationCancelled` when cancellation has been requested."""

    def register_interrupt(self, callback: InterruptCallback) -> Callable[[], None]:
        """Register a best-effort interrupt invoked when cancel is requested.

        Returns an unregister function. If cancellation already happened, the
        callback is invoked immediately.
        """


class CancellationController:
    """Owner of a single cancellable operation.

    ``cancel()`` is thread-safe and idempotent. It returns True only for the
    first successful request so callers can emit exactly one terminal event.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._interrupts: list[InterruptCallback] = []
        self._token = _CancellationToken(self)

    @property
    def token(self) -> CancellationToken:
        return self._token

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> bool:
        """Request cancellation. Returns True iff this call was the first."""
        with self._lock:
            if self._event.is_set():
                return False
            self._event.set()
            callbacks = list(self._interrupts)
            self._interrupts.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:  # pragma: no cover - defensive interrupt boundary
                logger.warning("Cancellation interrupt callback failed", exc_info=True)
        return True

    def _register_interrupt(self, callback: InterruptCallback) -> Callable[[], None]:
        with self._lock:
            if self._event.is_set():
                already = True
            else:
                self._interrupts.append(callback)
                already = False

        if already:
            try:
                callback()
            except Exception:  # pragma: no cover - defensive interrupt boundary
                logger.warning("Cancellation interrupt callback failed", exc_info=True)

        def unregister() -> None:
            with self._lock:
                try:
                    self._interrupts.remove(callback)
                except ValueError:
                    pass

        return unregister


class _CancellationToken:
    """Concrete token bound to a controller."""

    def __init__(self, controller: CancellationController) -> None:
        self._controller = controller

    def is_cancelled(self) -> bool:
        return self._controller.is_cancelled()

    def throw_if_cancelled(self, stage: str) -> None:
        if self._controller.is_cancelled():
            raise OperationCancelled(stage=stage)

    def register_interrupt(self, callback: InterruptCallback) -> Callable[[], None]:
        return self._controller._register_interrupt(callback)
