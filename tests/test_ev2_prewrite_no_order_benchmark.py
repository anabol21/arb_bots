"""The durability timing probe must remain bounded and network-incapable."""

from __future__ import annotations

import asyncio
import unittest

from validation.ev2_prewrite_no_order_benchmark import run_benchmark


class PrewriteNoOrderBenchmarkTests(unittest.TestCase):
    def test_small_no_order_probe_reaches_durable_memory_boundary(self) -> None:
        report = asyncio.run(run_benchmark(samples=5, history=5))
        self.assertEqual(report["orders_sent"], 0)
        self.assertFalse(report["network_capable_sockets"])
        self.assertFalse(report["live_latency_gate_eligible"])
        self.assertEqual(report["fresh_wal_exact_engine_fence"]["fence"]["n"], 5)
        self.assertEqual(report["growing_wal_no_order_audit"]["all"]["n"], 5)
        self.assertGreater(report["growing_wal_no_order_audit"]["final_wal_bytes"], 0)

    def test_probe_rejects_unbounded_sample_counts(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(run_benchmark(samples=501, history=5))
        with self.assertRaises(ValueError):
            asyncio.run(run_benchmark(samples=5, history=1001))


if __name__ == "__main__":
    unittest.main()
