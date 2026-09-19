"""Page-cache advise after gear22 would_send metrics.jsonl appends."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.bot.floor_watcher import FloorJournalWriter
from app.bot.fsadvise import advise_dontneed
from app.bot.theta_screener import ThetaJournalWriter, theta_from_inputs
from app.bot.tw_p50_watcher import TwP50JournalWriter


class AdviseDontneedHelperTests(unittest.TestCase):
    def test_missing_posix_fadvise_is_noop(self) -> None:
        with patch("app.bot.fsadvise.os.posix_fadvise", None):
            advise_dontneed(3)
            advise_dontneed("/tmp/missing-metrics.jsonl")

        with (
            patch("app.bot.fsadvise.os.POSIX_FADV_DONTNEED", None),
            patch("app.bot.fsadvise.os.posix_fadvise") as mock_advise,
        ):
            advise_dontneed(3)
            mock_advise.assert_not_called()

    def test_posix_fadvise_error_is_noop(self) -> None:
        with patch(
            "app.bot.fsadvise.os.posix_fadvise",
            side_effect=OSError("fadvise failed"),
        ) as mock_advise:
            advise_dontneed(3)
            mock_advise.assert_called_once()

    def test_advises_fd_whole_file(self) -> None:
        mock_advise = MagicMock()
        dontneed = getattr(os, "POSIX_FADV_DONTNEED", 4)
        with (
            patch("app.bot.fsadvise.os.posix_fadvise", mock_advise),
            patch("app.bot.fsadvise.os.POSIX_FADV_DONTNEED", dontneed),
        ):
            advise_dontneed(9)
        mock_advise.assert_called_once_with(9, 0, 0, dontneed)


class MetricsJsonlAdviseTests(unittest.TestCase):
    def _assert_append_advises(
        self, writer, row: dict, mock_target: str
    ) -> None:
        mock_advise = MagicMock()
        dontneed = getattr(os, "POSIX_FADV_DONTNEED", 4)
        with (
            patch(mock_target, mock_advise),
            patch("app.bot.fsadvise.os.POSIX_FADV_DONTNEED", dontneed),
        ):
            paths = writer.append_rows([row])
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].is_file())
        self.assertGreater(paths[0].stat().st_size, 0)
        mock_advise.assert_called()
        args, _kwargs = mock_advise.call_args
        self.assertEqual(args[1], 0)
        self.assertEqual(args[2], 0)
        self.assertEqual(args[3], dontneed)
        self.assertIsInstance(args[0], int)

    def test_theta_append_calls_posix_fadvise(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        snap = theta_from_inputs(
            base_coin="BTC",
            side="long",
            ts_ms=1_725_000_000_000,
            p50_1m=0.2,
            p50_5m=0.15,
            floor=0.05,
            computed_at_ms=1_725_000_000_050,
        )
        self._assert_append_advises(
            ThetaJournalWriter(tmp),
            snap.as_row(),
            "app.bot.fsadvise.os.posix_fadvise",
        )

    def test_tw_p50_append_calls_posix_fadvise(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        row = {
            "schema_version": "bbot.tw_p50.v1",
            "base_coin": "ETH",
            "side": "long",
            "ts_ms": 1_725_000_000_000,
            "p50_1m": 0.1,
            "p50_5m": 0.2,
            "n_1m": 1,
            "n_5m": 1,
            "coverage_1m": 1.0,
            "coverage_5m": 1.0,
            "computed_at_ms": 1_725_000_000_010,
        }
        self._assert_append_advises(
            TwP50JournalWriter(tmp),
            row,
            "app.bot.fsadvise.os.posix_fadvise",
        )

    def test_floor_append_calls_posix_fadvise(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        row = {
            "schema_version": "bbot.floor.v1",
            "base_coin": "SOL",
            "side": "long",
            "event_date": "2024-09-01",
            "bar_end_ms": 1_725_000_000_000,
            "close": 0.1,
        }
        self._assert_append_advises(
            FloorJournalWriter(tmp),
            row,
            "app.bot.fsadvise.os.posix_fadvise",
        )


if __name__ == "__main__":
    unittest.main()
