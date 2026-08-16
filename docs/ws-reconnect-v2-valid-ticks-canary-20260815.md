# Valid-ticks canary — start 2026-08-15 (pre-B)

Трек: сбор. Блок: WS lifecycle + fail-closed запись тиков.
**Не Track B.** Инвариант: дыра лучше, чем stale-cross тик.

| Поле | Значение |
|------|----------|
| Хост | `root@38.180.94.108` |
| Код | локальный репо → `/root/spread_staging` |
| Entrypoint | `app/screaner_b_o.py` |
| PID | **530649** (`NRestarts=0`) |
| Старт | **2026-08-15 17:30:12 UTC** |
| `runtime_paths` | 17:30:13 UTC: `reconnect_mode=v2`, `connect_per_sec=3.0`, `subscribe_batch_size=0` |
| Флаги (drop-in) | `SPREAD_WS_RECONNECT_V2=1`, `SPREAD_WS_CONNECT_PER_SEC=3`, `SPREAD_TICK_SKEW_MAX_MS=2000`, `SPREAD_TICK_AGE_MAX_MS=2000` |
| Профиль | lean+bars, N=337, `collect_bybit_bars=false` |
| Лог | `/var/log/spread/runtime.log` |
| Окно | ≥24 ч после subscribe-complete; **закрыт** 2026-08-16 17:32:36 UTC, см. [результат](ws-reconnect-v2-valid-ticks-canary-20260815-result.md) |

SHA256 на VPS совпал с локальным:

- `app/screaner_b_o.py` `c06d677aa036dddc378e7651d1e9d3c7fa02fbc8a8489a513d8c3aa475f73a82`
- `app/utils/tick_validity.py` `cf2071adbd6ccf60577970cb95a23c7e2d645581c11143f5b47b7b59af7f6d97`
- `app/utils/ws_reconnect.py` `6682d9fd2909a7f8eafebb406c71cf095133d042a4f47b1993c61bc48d3927bd`

Предыдущий v2 canary (PID 445090) остановлен для деплоя dual-fresh + connect scheduler.
Gap между stop (~17:30:00 UTC) и start — деплой, не reconnect.

Compact/backup не трогали: `spread-compactor.timer` и `spread-backup-transfer.timer` остаются enabled.

## Что уже видно на старте

- `Starting subscriptions via connect scheduler | connect_per_sec=3.0 | coins=337 | books_first=true`
- Книги одной монеты рядом (OKX `books5` + Bybit `orderbook.1` в одну секунду).
- Heartbeat 17:30:43 UTC (ещё не весь флот): `ws_subscribe_ok=168`, `ticks_accepted=3147`, `ticks_suppressed_stale=441`, `ticks_suppressed_generation=102`, `ws_reconnect_unplanned=0`, `ws_wave_*=0`.
- Suppress на старте ожидаем: одна нога уже dual-ready, вторая ещё в очереди или без L1 текущего generation.

## Gates через ≥24 ч

- `spread-collector` active, `NRestarts=0`, `reconnect_mode=v2`
- `ws_reconnect_unplanned=0`, `ws_protocol_errors=0`, `ws_unrecovered_active=0`
- `ticks_suppressed_stale` или `ticks_suppressed_generation` > 0 если была волна disconnect (fail-closed сработал)
- `validation/check_tick_coverage.py` на окне: incomplete windows совпадают с `ws_disconnect`; sample parquet `skew_violations=0` при 2000 ms
- bars: дыры допустимы; нет ложных closed-bar

`ws_wave_*_60s` пишется в heartbeat, но **не** gate и **не** требование Track B
(решение 2026-08-16: пачка planned 1006 — артефакт сети/Bybit).

Вердикт: `canary hold / fail`. Не `B READY`.

```bash
python3 validation/check_tick_coverage.py \
  --log /var/log/spread/runtime.log \
  --log /var/log/spread/runtime.log.1 \
  --since 2026-08-15T17:30:12Z \
  --parquet-root /data/live \
  --skew-max-ms 2000
```
