"""Extension points for future quiet-regime / integral metric traces.

Do **not** invent fake floors or thresholds here. Gear 2.2 observation tool
only; plug real candidates later by returning ``MetricTrace`` objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

import pandas as pd


@dataclass(frozen=True)
class MetricTrace:
    """One optional overlay for the HTML figure.

    ``panel`` selects a subplot id understood by ``plot.write_coin_html``:
    ``edge`` | ``mid`` | ``tick_count`` | ``gap_fraction`` | ``mean_std`` | ``range_iqr``.
    """

    name: str
    panel: str
    x: Sequence[Any]
    y: Sequence[float]
    mode: str = "lines"
    line: Mapping[str, Any] = field(default_factory=dict)
    meta: Mapping[str, Any] = field(default_factory=dict)


def collect_extension_traces(
    ticks: pd.DataFrame,
    buckets: pd.DataFrame,
    *,
    coin: str,
) -> list[MetricTrace]:
    """Hook for slow statistical floor / quiet-regime threshold candidates.

    Default implementation returns an empty list (no synthetic metrics).
    Later: append ``MetricTrace`` rows (e.g. rolling quiet floor on ``edge``).
    """
    _ = (ticks, buckets, coin)
    return []


def extension_help_html() -> str:
    """HTML blurb embedded in each page describing how to add a metric."""
    return (
        "<section class='ext-help'>"
        "<h2>Extension point — future integral metrics</h2>"
        "<p>Quiet-regime statistical floor / threshold candidates are <strong>not</strong> "
        "plotted yet. Implement them in "
        "<code>research.gear22_quiet_regime_viz.metrics_ext.collect_extension_traces</code> "
        "by returning <code>MetricTrace</code> objects (panel ids: "
        "<code>edge</code>, <code>mid</code>, <code>tick_count</code>, "
        "<code>gap_fraction</code>, <code>mean_std</code>, <code>range_iqr</code>). "
        "Do not invent placeholder series.</p>"
        "</section>"
    )
