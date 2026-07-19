"""Thread-safe cooperative cancellation contract.

Cancellation is a distinct terminal outcome — not success and not a generic
failure. Controllers are idempotent: the first ``cancel()`` wins; later calls
are no-ops that do not invent a second terminal event.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Protocol


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


class CancellationController:
    """Owner of a single cancellable operation.

    ``cancel()`` is thread-safe and idempotent. It returns True only for the
    first successful request so callers can emit exactly one terminal event.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._token = _CancellationToken(self._event)

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
            return True


class _CancellationToken:
    """Concrete token bound to a controller event."""

    def __init__(self, event: threading.Event) -> None:
        self._event = event

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def throw_if_cancelled(self, stage: str) -> None:
        if self._event.is_set():
            raise OperationCancelled(stage=stage)
