# Fixture: Gear 2.2 quiet-regime visualizer

Synthetic sparse L1 ticks for SOL and XRP around the documented gear2 restart
`2026-09-03T08:21:00Z`, including intentional multi-minute holes so gap
highlighting and empty 5m buckets are visible offline / in CI.

Rebuild:

```bash
PYTHONPATH=. python -m research.gear22_quiet_regime_viz.build_fixture
```

`universe_spread_std_august.csv` is a tiny sort fixture (XRP СКО > SOL). It is
not the production August table (`research/data/universe_spread_std_august.csv`).
