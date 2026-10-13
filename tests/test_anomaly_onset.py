"""Fast, self-contained checks for the gear-2.2 anomaly-onset detector."""

from __future__ import annotations

import numpy as np

from research.anomaly_onset import detector as D
from research.anomaly_onset import io_lean, synth
from research.anomaly_onset import twstats as tw


def test_weighted_quantile_matches_numpy_when_equal_weights():
    v = np.arange(1, 11, dtype=float)
    w = np.ones_like(v)
    assert abs(tw.weighted_quantile(v, w, 0.5) - np.quantile(v, 0.5)) < 1e-9
    # heavy weight on the last value pulls the 0.9 quantile up to it
    w2 = np.array([1] * 9 + [100], dtype=float)
    assert tw.weighted_quantile(v, w2, 0.9) >= 9.5


def test_rolling_tw_quantiles_warmup_and_ordering():
    ts = np.arange(0, 3_600_000, 1000, dtype=np.int64)  # 1h @ 1Hz
    vals = np.sin(ts / 1e6) + 5.0
    w = np.full_like(ts, 1000.0, dtype=float)
    q = D.rolling_tw_quantiles(
        ts, vals, w, H_ms=20 * 60_000, refresh_ms=60_000,
        levels=[0.25, 0.5, 0.75], min_cover_ms=5 * 60_000,
    )
    finite = np.isfinite(q[:, 0])
    assert finite.any() and not finite[0]                      # warm-up: first tick not warm
    ok = np.isfinite(q).all(axis=1)
    assert np.all(q[ok, 0] <= q[ok, 1] + 1e-9)                 # Q25 <= Q50
    assert np.all(q[ok, 1] <= q[ok, 2] + 1e-9)                 # Q50 <= Q75


def test_io_roundtrip_flat(tmp_path):
    coins = [synth.CoinSpec(base_coin="AAA", tick_hz=1.0)]
    root = synth.generate_day(tmp_path / "d", coins, start="2026-08-21T00:00:00Z", hours=1.0, seed=1)
    assert io_lean.detect_layout(root) == "flat"
    df = io_lean.read_lean_ticks(root, "2026-08-21T00:00:00Z", "2026-08-21T01:00:00Z",
                                 coins=["AAA"], workers=2, progress=False)
    df = io_lean.add_dwell_weights(df)
    assert len(df) > 100
    assert {"spread_long", "spread_short", "dwell_ms", "segment"} <= set(df.columns)
    # dwell is a duration: >=0 everywhere (0 only for same-ms coincident ticks), mostly >0
    assert df["dwell_ms"].ge(0).all()
    assert df["dwell_ms"].gt(0).mean() > 0.9


def test_detector_fires_on_injected_anomaly():
    coins = [synth.CoinSpec(
        base_coin="AAA", tick_hz=2.0, quiet_spread_pct=0.02, quiet_noise_pct=0.01,
        anomalies=[synth.Anomaly(start_s=90 * 60, dur_s=20 * 60, bump_pct=0.3)],  # 01:30, 20 min
    )]
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = synth.generate_day(tmp, coins, start="2026-08-21T00:00:00Z", hours=2.0, seed=3)
        df = io_lean.read_lean_ticks(root, "2026-08-21T00:00:00Z", "2026-08-21T02:00:00Z",
                                     coins=["AAA"], workers=2, progress=False)
        df = io_lean.add_dwell_weights(df)
    floor = D.FloorParams(H_floor_ms=45 * 60_000, refresh_ms=60_000, min_cover_ms=15 * 60_000)
    metric = D.MetricParams(W_ms=15 * 60_000)
    fr = D.analyze(df[df.base_coin == "AAA"], "long", floor=floor, metric=metric)
    ep = D.episodes_from_fire(fr)
    assert not ep.empty
    # the anomaly window carries far more integral area than the quiet baseline
    onset = ep["onset_ts"].to_numpy()
    assert ((onset >= io_lean.parse_ts_ms("2026-08-21T01:25:00Z"))
            & (onset <= io_lean.parse_ts_ms("2026-08-21T01:40:00Z"))).any()
    assert float(np.nanmax(fr["I_norm"])) > 0.1
