#!/usr/bin/env python3
"""Unit tests for the shared cancellation contract."""

from __future__ import annotations

import threading
import unittest

from src.common import CancellationController, OperationCancelled


class CancellationContractTests(unittest.TestCase):
    def test_token_starts_uncancelled(self) -> None:
        controller = CancellationController()
        self.assertFalse(controller.is_cancelled())
        self.assertFalse(controller.token.is_cancelled())
        controller.token.throw_if_cancelled("download")

    def test_cancel_is_idempotent_and_thread_safe(self) -> None:
        controller = CancellationController()
        winners = []

        def worker() -> None:
            winners.append(controller.cancel())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1)
        self.assertEqual(sum(1 for won in winners if won), 1)
        self.assertEqual(sum(1 for won in winners if not won), 7)
        self.assertTrue(controller.is_cancelled())
        with self.assertRaises(OperationCancelled) as ctx:
            controller.token.throw_if_cancelled("download")
        self.assertEqual(ctx.exception.stage, "download")

    def test_register_interrupt_runs_on_cancel_and_unregister(self) -> None:
        controller = CancellationController()
        hits: list[str] = []
        unregister = controller.token.register_interrupt(lambda: hits.append("a"))
        unregister()
        self.assertTrue(controller.cancel())
        self.assertEqual(hits, [])

        controller2 = CancellationController()
        hits2: list[str] = []
        controller2.token.register_interrupt(lambda: hits2.append("b"))
        self.assertTrue(controller2.cancel())
        self.assertEqual(hits2, ["b"])
        # Already cancelled: register invokes immediately.
        controller2.token.register_interrupt(lambda: hits2.append("c"))
        self.assertEqual(hits2, ["b", "c"])


if __name__ == "__main__":
    unittest.main()