"""Anomaly-onset proxy detector (Track M, gear 2.2 research).

Historical simulation only. Detects the transition of a coin from the default
"quiet" regime into an anomalous regime using time-weighted metrics on the
directional executable L1 spread. No live execution, no order routing.

Design contract (agreed with user):

- Every tick is kept; nothing is downsampled/smoothed away. Each tick carries a
  ``dwell`` weight = time until the next tick (clamped at holes). All statistics
  (floor quantiles, metric quantiles) are weighted by ``dwell``, never by count.
- Quiet is the DEFAULT regime and is not detected. An episode starts when the
  three metrics simultaneously exceed their thresholds.
- Thresholds are high time-weighted quantiles (default 0.99) of each metric over
  the trailing ``H_floor`` quiet reference; the quantile level is universal
  across coins while the numeric threshold self-calibrates per coin.

Modules:

- ``twstats``  : time-weighted quantile / median / MAD helpers.
- ``io_lean``  : self-contained lean L1 reader (flat ``spread_*.parquet`` + hive),
                 directional spread derivation, dwell weights, hole splitting.
- ``detector`` : floor band, z+/a+, occupancy O_W, integral area I_{W,1},
                 quantile thresholds, onset detection, episode catalog.
- ``synth``    : synthetic quiet+anomaly day generator for smoke tests.
- ``viz``      : plotly figures.
"""

from __future__ import annotations

__all__ = ["twstats", "io_lean", "detector", "synth", "viz"]
