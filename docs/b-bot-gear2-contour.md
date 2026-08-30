# Контур Gear 2 would_send (Track 3)

Трек **склейка / B-bot**. Не D, не frozen close гира 2, не alpha и не PnL.

Этот live-data эксперимент собирает dual-leg журнал `would_send=true`, `send=false`
на четырёх монетах с Arm A и глобальным `K=1`. Он **не** является штампом
[`gear-2-close-20260825.md`](gear-2-close-20260825.md) (all-crypto, пороги 0.5)
и не доказательство готовности к live send.

## Профиль (зафиксированный контракт)

| Поле | Значение | Не путать с |
|------|----------|-------------|
| Монеты | BTC, ETH, SOL, XRP | all-crypto close гира 2 |
| Пороги | все четыре `0.02` (strict `>`) | frozen 0.5; notebook ±0.1; первый soak 0.1 (без сделок) |
| `open_frac` / `close_frac` | 0.7 / 0.7 | |
| `avg_window_sec` | 10 | live gear1 = 2s |
| `K` | 1 глобальный слот | per-coin K |
| Arm | A (`regime_topn` не читается) | Arm B Top-N |
| `Trade_Lat` | 100 ms | |
| Volume / freshness | выкл. | |
| Latency caps | OKX 54 ms, Bybit 35 ms | gear1 40/25 |
| Broker | stub dual-leg | private send |

Канон elif-цепочки: [`research/gear2_backtest.py`](../research/gear2_backtest.py).
Streaming-порт: [`app/policy/gear2_market_manager.py`](../app/policy/gear2_market_manager.py).
Константы: `GEAR2_WOULD_SEND_*` в [`app/policy/trade_manager.py`](../app/policy/trade_manager.py).

## Изоляция

| | |
|--|--|
| Unit | `spread-bbot-gear2.service` (не `spread-bbot.service`) |
| Data | `/data/bbot-gear2` |
| Log | `/var/log/spread/bbot-gear2.log` |
| Запрет записи | `/data/live`, `/data/bars`, `/data/compacted`, `/data/spool` |
| Collector | не stop / не restart |

Сериализация решений: один `asyncio.Lock` на все монеты; `ordering_key` = `MarketState.seq`.
Restart читает `held_coin` из `position.json` / `pending.json` и не открывает второй слот.

## GREEN (24h soak)

GREEN только если одновременно:

1. Процесс непрерывно прошёл 24 часа (нет crash loop).
2. Collector остался `active`; его `NRestarts` не вырос из-за бота.
3. `K=1`: нет перекрывающихся позиций/pending.
4. Каждый intent имеет две terminal-ноги; все `send=false`.
5. Config в журнале: `0.02/0.02/0.02/0.02`, MA 10s, четыре монеты.
6. Нет файлов бота в деревьях D.
7. Журнал читается локальным Plotly-разбором.

Малое число или отсутствие сделок — наблюдение, не автоматический FAIL механики.

Проверка: `python3 validation/check_bbot_gear2.py --data-root /data/bbot-gear2`.

## После GREEN: только private testnet/demo

`StubBroker` не превращается в sender. Монтирование: `BBOT_BROKER=private_testnet`,
`VENUE=testnet`, `LIVE_ORDERS=0`. Лестница: read-only auth → одна нога → dual-leg/abort.
Live send требует отдельной явной фразы пользователя, Review Critic и Host Ops.

Лестница после GREEN (ещё не исполняется этим unit’ом; soak держит `BBOT_BROKER=stub`):

1. `BBOT_BROKER=private_testnet`, `VENUE=testnet`, `LIVE_ORDERS=0`, отдельный secret file, журнал `/data/bbot/private/`.
2. Read-only auth / balance.
3. Одна минимальная нога place/ack/cancel-or-fill.
4. Controlled dual-leg / abort.
5. B Private Validator + D-isolation review.

`make_broker()` уже отказывает `VENUE=live` и `LIVE_ORDERS=1`. Непрерывный private systemd на этом этапе не запускается.

## Soak start (VPS)

| | |
|--|--|
| Host | `root@38.180.94.108` |
| Start (UTC) | 2026-08-28 15:44:28 (порог 0.1; сделок не было) |
| Restart (UTC) | 2026-08-29 11:10:01 — порог `0.02`; часы GREEN с этого рестарта |
| Collector baseline | PID 902378, `NRestarts=0`, active since 2026-08-20 13:08:32 UTC |
| Unit | `spread-bbot-gear2.service` (disabled, started manually) |
| Backup timer | installed, **not** enabled |

Canary (2026-08-28 ~16:05 UTC, ~20 min): unit active, same PID 1596629, `NRestarts=0`;
collector PID 902378 / `NRestarts=0` unchanged; 8 WS subscribed (BTC/ETH/SOL/XRP);
accepted ≈ 244k, `sup_stale=0`, disconnects=0; RSS ≈ 28 MiB, FD=15, CPU ≈ 13%;
D-tree bbot names=0. `raw` signals observed, zero terminal intents yet (not FAIL).

GREEN 24h soak is running. Do not stop collector. After 2026-08-29 15:44:28 UTC:

```bash
systemctl stop spread-bbot-gear2.service   # only the would_send unit
python3 validation/check_bbot_gear2.py --data-root /data/bbot-gear2
```
