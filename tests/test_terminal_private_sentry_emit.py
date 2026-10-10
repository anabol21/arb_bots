"""Offline tests for terminal_private Sentry emit after place_live."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Optional
from unittest.mock import MagicMock, patch


def _bind_helpers() -> type:
    """Extract helper methods from runtime.py without importing heavy deps."""
    src = (Path(__file__).resolve().parents[1] / "app" / "bot" / "runtime.py").read_text()
    # Pull the two helper methods as source between their defs and _synthetic_live_place.
    m = re.search(
        r"\n    def _emit_terminal_private_trade_sentry\(.*?\n    def _synthetic_live_place\(",
        src,
        re.S,
    )
    if not m:
        raise RuntimeError("helpers not found in runtime.py")
    body = m.group(0)
    body = body[: body.rfind("\n    def _synthetic_live_place(")]
    ns: dict[str, Any] = {
        "Any": Any,
        "Mapping": Mapping,
        "Optional": Optional,
        "json": __import__("json"),
    }
    # Provide a host class and exec indented methods into it.
    class_src = "class _Host:\n" + body
    exec(class_src, ns, ns)
    return ns["_Host"]


class TestTerminalPrivateSentryEmit(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.Host = _bind_helpers()

    def _runtime(self, data_root: Path) -> Any:
        rt = self.Host()
        rt._terminal_private_execution = True
        rt._canary29_completed_cycles = 2
        rt.notional = 10.0
        rt.data_root = data_root
        rt.log = MagicMock()
        return rt

    @patch("app.bot.sentry_setup.capture_trade_event")
    @patch("app.bot.private.send_legs._attempt_ids", return_value=("b-id", "o-id", "d-id"))
    def test_emits_open_filled(self, _ids: MagicMock, mock_capture: MagicMock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rt = self._runtime(Path(tmp))
            result = SimpleNamespace(
                completed=True,
                keep_pending=False,
                send_attempted=True,
                abort=None,
                status="filled",
                okx_fill_px="0.11",
                bybit_fill_px="0.12",
                okx_filled_qty="10",
                bybit_filled_qty="100",
                coin_qty="100",
                base_coin="RVN",
                side="long",
                intent_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                fill_ts_ms=1_700_000_100,
                latency_ms=70,
            )
            kwargs = {
                "spread_side": "open_long",
                "base_coin": "RVN",
                "signal_ts_ms": 1_700_000_030,
                "intent_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "extra": {
                    "trade_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "theta_1m": 0.55,
                },
            }
            rt._emit_terminal_private_trade_sentry(result, kwargs)
            mock_capture.assert_called_once()
            kw = mock_capture.call_args.kwargs
            self.assertEqual(kw["event"], "open")
            self.assertEqual(kw["coin"], "RVN")
            self.assertEqual(kw["side"], "long")
            self.assertEqual(kw["level"], "error")
            extras = kw["extras"]
            self.assertEqual(extras["spread_side"], "open_long")
            self.assertEqual(extras["completed_cycles"], 2)
            self.assertEqual(extras["notional_usdt"], 10.0)
            self.assertEqual(extras["theta_1m"], 0.55)
            self.assertFalse(extras["reduce_only"])
            self.assertEqual(extras["okx_order_id"], "o-id")
            self.assertEqual(extras["fill_status"], "filled")

    @patch("app.bot.sentry_setup.capture_trade_event")
    @patch("app.bot.private.send_legs._attempt_ids", return_value=("b-id", "o-id", "d-id"))
    def test_emits_asymmetric_error(self, _ids: MagicMock, mock_capture: MagicMock) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rt = self._runtime(Path(tmp))
            result = SimpleNamespace(
                completed=False,
                keep_pending=True,
                send_attempted=True,
                abort="asymmetric_fill",
                status=None,
                okx_fill_px="0.11",
                bybit_fill_px=None,
                okx_filled_qty="10",
                bybit_filled_qty=None,
                coin_qty=None,
                base_coin="CAP",
                side="short",
                intent_id="bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee",
                fill_ts_ms=None,
                latency_ms=None,
            )
            kwargs = {
                "spread_side": "open_short",
                "base_coin": "CAP",
                "signal_ts_ms": 1,
                "intent_id": "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee",
                "extra": {"trade_id": "trade-1"},
            }
            rt._emit_terminal_private_trade_sentry(result, kwargs)
            self.assertEqual(mock_capture.call_args.kwargs["event"], "asymmetric_fill")
            self.assertEqual(mock_capture.call_args.kwargs["level"], "error")
            self.assertTrue(mock_capture.call_args.kwargs["extras"]["keep_pending"])

    @patch("app.bot.sentry_setup.capture_trade_event")
    @patch("app.bot.private.send_legs._attempt_ids", return_value=("b-id", "o-id", "d-id"))
    def test_emit_never_raises(self, _ids: MagicMock, mock_capture: MagicMock) -> None:
        mock_capture.side_effect = RuntimeError("boom")
        with tempfile.TemporaryDirectory() as tmp:
            rt = self._runtime(Path(tmp))
            result = SimpleNamespace(
                completed=True,
                keep_pending=False,
                send_attempted=True,
                abort=None,
                status="filled",
                okx_fill_px=None,
                bybit_fill_px=None,
                okx_filled_qty=None,
                bybit_filled_qty=None,
                coin_qty=None,
                base_coin="ICX",
                side="long",
                intent_id="id-1",
                fill_ts_ms=2,
                latency_ms=1,
            )
            kwargs = {
                "spread_side": "close",
                "close_of": "open_long",
                "base_coin": "ICX",
                "signal_ts_ms": 1,
                "intent_id": "id-1",
                "extra": {"trade_id": "id-1"},
            }
            rt._emit_terminal_private_trade_sentry(result, kwargs)
            self.assertEqual(mock_capture.call_args.kwargs["event"], "close")
            self.assertTrue(mock_capture.call_args.kwargs["extras"]["reduce_only"])


if __name__ == "__main__":
    unittest.main()
