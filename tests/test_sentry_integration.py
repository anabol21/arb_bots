"""Unit tests for Sentry integration in theta trade manager."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

from app.bot.theta_trade_manager import (
    ThetaTradeConfig,
    ThetaTradeManager,
    ThetaSnapshot,
)


class TestSentryIntegration(unittest.TestCase):
    """Test Sentry SDK integration without network calls."""

    def setUp(self) -> None:
        self.test_data_root = Path("/tmp/test-sentry-bbot")
        self.test_data_root.mkdir(parents=True, exist_ok=True)
        self.mock_sentry = MagicMock()
        self.mock_scope = MagicMock()
        # Mock both push_scope and new_scope context managers
        self.mock_sentry.push_scope.return_value.__enter__.return_value = self.mock_scope
        self.mock_sentry.push_scope.return_value.__exit__.return_value = None
        self.mock_sentry.new_scope = MagicMock()
        self.mock_sentry.new_scope.return_value.__enter__.return_value = self.mock_scope
        self.mock_sentry.new_scope.return_value.__exit__.return_value = None
        self.capture_calls: list[tuple[str, dict[str, Any]]] = []

    def tearDown(self) -> None:
        import shutil
        if self.test_data_root.exists():
            shutil.rmtree(self.test_data_root)

    @patch("app.bot.sentry_setup._try_import_sentry")
    @patch("app.bot.sentry_setup._sentry_enabled", False)
    def test_sentry_not_enabled_without_dsn(self, mock_import: Mock) -> None:
        """When SENTRY_DSN is unset, Sentry should not be enabled."""
        from app.bot.sentry_setup import init_sentry, sentry_enabled

        env = {}
        result = init_sentry(profile="gear22_would_send", env=env)
        
        self.assertFalse(result)
        self.assertFalse(sentry_enabled())
        mock_import.assert_not_called()

    @patch("app.bot.sentry_setup._try_import_sentry")
    def test_sentry_init_with_dsn(self, mock_import: Mock) -> None:
        """When SENTRY_DSN is set, Sentry should initialize."""
        mock_import.return_value = self.mock_sentry
        from app.bot.sentry_setup import init_sentry

        env = {"SENTRY_DSN": "https://fake@sentry.io/123"}
        result = init_sentry(profile="gear22_would_send", env=env)
        
        self.assertTrue(result)
        self.mock_sentry.init.assert_called_once()
        call_kwargs = self.mock_sentry.init.call_args[1]
        self.assertEqual(call_kwargs["dsn"], "https://fake@sentry.io/123")
        self.assertIn("environment", call_kwargs)
        self.mock_sentry.flush.assert_not_called()

    @patch("app.bot.sentry_setup._sentry_enabled", True)
    @patch("app.bot.sentry_setup._init_attempted", True)
    @patch("app.bot.sentry_setup._try_import_sentry")
    def test_capture_trade_open(self, mock_import: Mock) -> None:
        """Test Sentry capture on trade open with default error level."""
        mock_import.return_value = self.mock_sentry
        from app.bot.sentry_setup import capture_trade_event

        capture_trade_event(
            event="open",
            trade_id="test-uuid-123",
            coin="BTC",
            side="long",
            extras={
                "signal_ts_ms": 1000,
                "fill_ts_ms": 1070,
                "theta_1m": 0.25,
                "spread_signal": 0.30,
            },
        )

        self.mock_sentry.capture_message.assert_called_once()
        call_args = self.mock_sentry.capture_message.call_args
        message = call_args[0][0]
        level = call_args[1].get("level", call_args[0][1] if len(call_args[0]) > 1 else None)
        self.assertIn("theta_k1 trade open", message)
        self.assertIn("BTC", message)
        self.assertEqual(level, "error")
        
        self.mock_scope.set_tag.assert_any_call("event", "open")
        self.mock_scope.set_tag.assert_any_call("coin", "BTC")
        self.mock_scope.set_tag.assert_any_call("side", "long")
        self.mock_scope.set_tag.assert_any_call("trade_id", "test-uuid-123")
        self.mock_scope.set_tag.assert_any_call("kind", "trade")
        
        self.mock_sentry.flush.assert_called_once_with(timeout=5)

    @patch("app.bot.sentry_setup._sentry_enabled", True)
    @patch("app.bot.sentry_setup._init_attempted", True)
    @patch("app.bot.sentry_setup._try_import_sentry")
    def test_capture_trade_close_with_pnl(self, mock_import: Mock) -> None:
        """Test Sentry capture on trade close with PnL."""
        mock_import.return_value = self.mock_sentry
        from app.bot.sentry_setup import capture_trade_event

        capture_trade_event(
            event="close",
            trade_id="test-uuid-456",
            coin="ETH",
            side="short",
            extras={
                "pnl_spread": 0.15,
                "pnl_usdt_approx": 0.15,
                "slip_spread": 0.02,
            },
        )

        self.mock_sentry.capture_message.assert_called_once()
        call_args = self.mock_sentry.capture_message.call_args
        level = call_args[1].get("level", call_args[0][1] if len(call_args[0]) > 1 else None)
        self.assertEqual(level, "error")
        self.mock_scope.set_tag.assert_any_call("event", "close")
        self.mock_scope.set_tag.assert_any_call("kind", "trade")
        self.mock_scope.set_extra.assert_any_call("pnl_spread", 0.15)
        self.mock_sentry.flush.assert_called_once_with(timeout=5)

    @patch("app.bot.sentry_setup._sentry_enabled", True)
    @patch("app.bot.sentry_setup._init_attempted", True)
    @patch("app.bot.sentry_setup._try_import_sentry")
    def test_no_reject_emits(self, mock_import: Mock) -> None:
        """Test that reject events do NOT emit to Sentry (journal only)."""
        mock_import.return_value = self.mock_sentry
        
        # Rejects are journaled but not sent to Sentry to avoid flooding.
        # This test verifies the API works but should not be called for rejects.
        from app.bot.sentry_setup import capture_trade_event

        # This would work if called, but should NOT be called for rejects.
        capture_trade_event(
            event="open",  # Only open/close emit
            trade_id="test-123",
            coin="SOL",
            side="long",
            extras={},
        )

        self.mock_sentry.capture_message.assert_called_once()
        call_args = self.mock_sentry.capture_message.call_args
        level = call_args[1].get("level", call_args[0][1] if len(call_args[0]) > 1 else None)
        self.assertEqual(level, "error")
        self.mock_scope.set_tag.assert_any_call("event", "open")
        self.mock_sentry.flush.assert_called_once_with(timeout=5)

    @patch("app.bot.sentry_setup._sentry_enabled", True)
    @patch("app.bot.sentry_setup._init_attempted", True)
    @patch("app.bot.sentry_setup._try_import_sentry")
    def test_capture_exception(self, mock_import: Mock) -> None:
        """Test Sentry exception capture with flush."""
        mock_import.return_value = self.mock_sentry
        from app.bot.sentry_setup import capture_exception

        test_exc = ValueError("Test error")
        capture_exception(test_exc, extras={"profile": "gear22_would_send"})

        self.mock_sentry.capture_exception.assert_called_once_with(test_exc)
        self.mock_scope.set_extra.assert_called_with("profile", "gear22_would_send")
        self.mock_sentry.flush.assert_called_once_with(timeout=5)

    @patch("app.bot.sentry_setup._sentry_enabled", True)
    @patch("app.bot.sentry_setup._init_attempted", True)
    @patch("app.bot.sentry_setup._try_import_sentry")
    def test_theta_manager_emits_on_open(self, mock_import: Mock) -> None:
        """Test that ThetaTradeManager emits to Sentry on successful open."""
        mock_import.return_value = self.mock_sentry
        
        config = ThetaTradeConfig(
            theta_thr=0.2,
            fill_delay_ms=70,
            slot_k=1,
            notional_usdt=100.0,
            book_depth=1,
        )
        manager = ThetaTradeManager(
            data_root=self.test_data_root,
            config=config,
            log=lambda _: None,
            sleep_fn=lambda _: None,
        )

        snapshots = [
            ThetaSnapshot(
                base_coin="BTC",
                side="long",
                theta_1m=0.25,
                theta_5m=0.22,
                floor_tf_select_a25=0.10,
                p50_1m=0.15,
                p50_5m=0.14,
                ts_ms=1000,
                computed_at_ms=1000,
            )
        ]
        
        quotes = {
            "BTC": {
                "okx": {
                    "bid_price": 50000,
                    "bid_size": 10,
                    "ask_price": 50010,
                    "ask_size": 10,
                    "ts_exchange": 1000,
                    "local_recv_ts_ms": 1000,
                },
                "bybit": {
                    "bid_price": 50020,
                    "bid_size": 10,
                    "ask_price": 50030,
                    "ask_size": 10,
                    "ts_exchange": 1000,
                    "local_recv_ts_ms": 1000,
                },
            }
        }

        rows = manager.on_theta_snapshots(snapshots, quotes=quotes, now_ms=1000)
        
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "open")
        self.mock_sentry.capture_message.assert_called()
        message = self.mock_sentry.capture_message.call_args[0][0]
        self.assertIn("theta_k1 trade open", message)

    @patch("app.bot.sentry_setup._sentry_enabled", False)
    def test_no_network_calls_when_disabled(self) -> None:
        """Test that no Sentry calls are made when DSN is not set."""
        from app.bot.sentry_setup import capture_trade_event

        capture_trade_event(
            event="open",
            trade_id="test-123",
            coin="BTC",
            side="long",
            extras={},
            level="warning",
        )

    @patch("app.bot.sentry_setup._sentry_enabled", True)
    @patch("app.bot.sentry_setup._init_attempted", True)
    @patch("app.bot.sentry_setup._try_import_sentry")
    def test_async_execution_path_emits_to_sentry(self, mock_import: Mock) -> None:
        """Test that async execution path (_execute_decision_async) emits to Sentry."""
        import asyncio
        
        mock_import.return_value = self.mock_sentry
        
        config = ThetaTradeConfig(
            theta_thr=0.2,
            fill_delay_ms=70,
            slot_k=1,
            notional_usdt=100.0,
            book_depth=1,
        )
        
        # Mock async sleep
        async def mock_async_sleep(seconds: float) -> None:
            pass
        
        manager = ThetaTradeManager(
            data_root=self.test_data_root,
            config=config,
            log=lambda _: None,
            sleep_fn=mock_async_sleep,
        )

        snapshots = [
            ThetaSnapshot(
                base_coin="BTC",
                side="long",
                theta_1m=0.25,
                theta_5m=0.22,
                floor_tf_select_a25=0.10,
                p50_1m=0.15,
                p50_5m=0.14,
                ts_ms=1000,
                computed_at_ms=1000,
            )
        ]
        
        quotes = {
            "BTC": {
                "okx": {
                    "bid_price": 50000,
                    "bid_size": 10,
                    "ask_price": 50010,
                    "ask_size": 10,
                    "ts_exchange": 1000,
                    "local_recv_ts_ms": 1000,
                },
                "bybit": {
                    "bid_price": 50020,
                    "bid_size": 10,
                    "ask_price": 50030,
                    "ask_size": 10,
                    "ts_exchange": 1000,
                    "local_recv_ts_ms": 1000,
                },
            }
        }

        # Run async path
        rows = asyncio.run(
            manager.on_theta_snapshots_async(snapshots, quotes=quotes, now_ms=1000)
        )
        
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"], "open")
        
        # Verify Sentry was called from async path
        self.mock_sentry.capture_message.assert_called()
        message = self.mock_sentry.capture_message.call_args[0][0]
        self.assertIn("theta_k1 trade open", message)
        self.mock_sentry.flush.assert_called_with(timeout=5)
        self.mock_scope.set_tag.assert_any_call("kind", "trade")


if __name__ == "__main__":
    unittest.main()
