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


if __name__ == "__main__":
    unittest.main()
