"""Focused startup preparation checks; no exchange sockets or order frames."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.bot.runtime import BotRuntime


class _WarmSession:
    run_id = "startup-test"
    _handshake_count = 1

    def __init__(self, ready: bool = True) -> None:
        self.ready = ready

    def is_ready(self) -> bool:
        return self.ready


def _runtime(session: _WarmSession) -> BotRuntime:
    runtime = object.__new__(BotRuntime)
    runtime.profile = "synthetic_roll"
    runtime._synthetic_live_send_enabled = True
    runtime._private_warm = session
    runtime._synthetic_sender = None
    runtime._synthetic_sender_session = None
    runtime.log = Mock()
    return runtime


class SyntheticStartupPrewarmTests(unittest.TestCase):
    def test_prepares_once_with_empty_ready_queues_and_ordered_cleanup(self) -> None:
        session = _WarmSession()
        runtime = _runtime(session)
        send_callbacks = []
        cleanup_order = []

        with patch(
            "app.bot.private.ws_trivial_dual_leg.warm_trade_send_fn",
            side_effect=lambda _session: lambda item: send_callbacks.append(item),
        ) as make_send_fn:
            self.assertTrue(runtime._prepare_synthetic_live_sender())
            sender = runtime._synthetic_sender
            self.assertIsNotNone(sender)
            self.assertTrue(sender.is_ready())
            self.assertEqual(sender.queue_depths(), {"bybit": 0, "okx": 0})
            self.assertTrue(runtime._prepare_synthetic_live_sender())
            self.assertIs(runtime._synthetic_sender, sender)
            make_send_fn.assert_called_once_with(session)
            self.assertEqual(send_callbacks, [])

            real_close = sender.close

            def close_sender() -> None:
                cleanup_order.append("sender.close")
                real_close()

            sender.close = close_sender
            with patch(
                "app.bot.private.ws_warm_session.clear_process_warm_session",
                side_effect=lambda *, stop: cleanup_order.append(f"session.stop:{stop}"),
            ):
                runtime._stop_synthetic_private_send()

        self.assertEqual(cleanup_order, ["sender.close", "session.stop:True"])
        self.assertIsNone(runtime._private_warm)
        self.assertIsNone(runtime._synthetic_sender)

    def test_refuses_preparation_when_warm_session_is_not_ready(self) -> None:
        runtime = _runtime(_WarmSession(ready=False))
        with self.assertRaisesRegex(RuntimeError, "session is not ready"):
            runtime._prepare_synthetic_live_sender()
        self.assertIsNone(runtime._synthetic_sender)


if __name__ == "__main__":
    unittest.main()
