# Контур Gear 2 would_send (Track 3)

Трек **склейка / B-bot**. Не D, не frozen close гира 2, не alpha и не PnL.

Этот live-data эксперимент собирает dual-leg журнал `would_send=true`, `send=false`
на четырёх монетах с Arm A и глобальным `K=1`. Он **не** является штампом
[`gear-2-close-20260825.md`](gear-2-close-20260825.md) (all-crypto, пороги 0.5).

**GREEN закрыт 2026-08-30.** GREEN измерил 24h процесс, `K=1`, stub dual-leg,
`send=false` и изоляцию от D. GREEN **не** измерил fill≠L1, ACK, venue minQty
и live PnL. GREEN ≠ разрешение на send.

Следующий разрешённый шаг B — live send на **этом же** контуре (не testnet,
не полный пул, не гиры 2.5/3). Отдельный live broker; `StubBroker` не
становится sender. Код `make_broker()` по-прежнему отказывает `VENUE=live` /
`LIVE_ORDERS=1`, пока не будет явного патча + Review Critic.

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
| Broker (GREEN soak) | stub dual-leg | live send |

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

## GREEN (критерии и закрытие)

GREEN только если одновременно:

1. Процесс непрерывно прошёл 24 часа (нет crash loop).
2. Collector остался `active`; его `NRestarts` не вырос из-за бота.
3. `K=1`: нет перекрывающихся позиций/pending. Открытый слот на отметке 24h — OK;
   не требовать, чтобы каждый цикл был закрыт.
4. Каждый **записанный** intent имеет две terminal-ноги (`filled` или `aborted`);
   все `send=false`.
5. Config в журнале: `0.02/0.02/0.02/0.02`, MA 10s, четыре монеты.
6. Нет файлов бота в деревьях D.
7. Журнал читается локальным Plotly-разбором.

Малое число или отсутствие сделок по монете — наблюдение, не автоматический FAIL
механики.

Проверка: `python3 validation/check_bbot_gear2.py --data-root /data/bbot-gear2`.

### Stamp GREEN closed

| | |
|--|--|
| Host | `root@38.180.94.108` |
| 0.02 restart (UTC) | 2026-08-29 11:10:01 |
| GREEN closed | ~2026-08-30 14:25 MSK (~11:25 UTC), ≈24.25 h |
| Gear2 unit | PID **1650273**, `NRestarts=0` |
| Collector | PID **902378**, `NRestarts=0`, без изменений |
| Intents | 11 would_send, все `send=false`, `K=1` без overlap |
| ETH | 4 завершённых dual-leg цикла + 1 open short на отметке GREEN (слот busy часами — ожидаемо) |
| BTC | 2 `open_long` abort `okx_qty_below_min` при notional 100 |
| SOL / XRP | 0 intents — наблюдение, не баг |
| D trees | файлов bbot нет |

Первый soak 2026-08-28 15:44:28 UTC (порог 0.1) сделок не дал и **не** является
часами этого GREEN. Часы GREEN — только с рестарта 0.02.

Soak **не** «идёт». Этот документ больше не содержит команду stop после
2026-08-29 15:44:28 UTC (это были брошенные часы 0.1).

## После GREEN: live send на том же контуре (план, не исполнение)

Лестница private testnet/demo как обязательный гейт **отменена** (2026-08-25:
testnet API не держался). Override 2026-08-30: следующий шаг B — **realnet live
send** на том же would_send контуре. Не testnet, не полный пул, не гиры 2.5/3,
не frozen 0.4/0. Этот раздел — locked plan. Код и unit’ы этим документом не
меняются. GREEN ≠ разрешение на send.

1. Не ставить `send=true` на работающем stub. `StubBroker` не sender.
2. Отдельный live broker через `app/bot/broker.py` / `app/bot/private`.
   `make_broker()` сейчас принимает только `stub|private_testnet` и отказывает
   `VENUE=live` / `LIVE_ORDERS=1`. Снять отказ можно только явным патчем +
   Review Critic, не env-хаком. Этот docs-PR отказ **не** снимает.
3. Та же политика: `0.02`×4, MA 10s, Arm A, `K=1`, BTC/ETH/SOL/XRP,
   `Trade_Lat=100`. Unit `spread-bbot-gear2`, данные `/data/bbot-gear2`.
   Не 337-pair WS.
4. Жёсткий notional cap. BTC @ 100 USDT уже дважды упёрся в OKX minQty —
   первый live либо поднимает размер до minQty, либо не шлёт BTC.
5. Журнал обязан различать would_send vs send, ack, fill vs L1, abort.
6. Деплой: никогда не stop/restart collector или compactor. Canary 15–30 мин,
   затем 24h только если PID/`NRestarts` collector не изменились.
7. ETH, держащий слот `K=1` часами (SOL/XRP молчат), — ожидаемо.

Непрерывный private systemd и live send этим документом не запускаются.
Нужны отдельная явная фраза пользователя, Review Critic и Host Ops на гейте
первой live-заявки.
