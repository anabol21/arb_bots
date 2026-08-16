# Лестница latency-профилей (pre-B)

Трек: **(D) latency**. Не Track B. Цель: найти первый неизменяемый профиль,
который проходит [контракт приёмки](latency-production-acceptance-contract.md),
и зафиксировать его. Это не каузальная матрица A/B/C.

Entrypoint: `app/screaner_b_o.py` на `root@38.180.94.108`.
Ingest / parse / spread / compaction / backup **не** меняются в этом шаге.

## Порядок

Останавливаемся на первом pass.

| Шаг | Профиль | Книги | Бары | Запись | Фон compact/backup |
|-----|---------|-------|------|--------|--------------------|
| L1 | текущий рабочий | 337×2 | 337 candle5m | тики → backup, бары VPS-local | включены |
| L2 | вектор 1 | 10×2, обязательно XRP | 337 candle5m | тики 10 → backup, бары все local | включены |
| L3 | диагностика | 337×2 | off | discard | только drain хвоста |
| L4 | нижняя граница | 10×2 + XRP | off | discard | как L3 |

L2 нельзя выразить текущими флагами (`ROW_START/END` открывает books и bars
на один список). Без узкого unlock подписок L2 не запускать.
`ROW_END=10` на `bybit_okx_universe.csv` выкинет XRP (строка 331).

L3/L4 нельзя сделать большим `PERSIST_EVERY`: нужен явный discard, и только
когда дойдём до L3.

## Окна

- Warmup ≥ 10 мин после subscribe-ready, не входит в счёт.
- **Reject:** первое steady 60 мин, если уже провален p99 / S/P / dual /
  unplanned / backpressure.
- **Accept:** только второе независимое 60-мин окно, не продолжение того же
  запуска.
- Planned Bybit 1006 / `wave_60s` — наблюдение, не fail, если unplanned=0,
  unrecovered=0, тики fail-closed.

S: pooled trigger-leg `okx_latency_ms` / `bybit_latency_ms` по XRP.
P: matched XRP ping той же биржи. Не считать p99 от минутных p99.

## Anti-scope

Не открывать B / private WS. Не писать split books/bars до fail L1.
Не трогать `compactor.py`, `backup_transfer.py`, retention.
Не гонять standalone A/B/C probe на том же хосте во время окна.
