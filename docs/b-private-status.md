# B-private — статус готовности (для оркестраторов)

Дата фиксации: 2026-08-20. Трек: склейка / исполнение.  
Владелец чата: оркестратор B-private. Код: `app/bot/private/**`.

**Вердикт.** Приватный адаптер Bybit + OKX на VPS **доказан** как узкий испытательный
контур (не непрерывный боевой бот). Подветка закрыта по цели адаптера (P9). Стык со
заглушкой (`Broker`) — **отдельное решение**; код отправки заявок в `stub_broker.py` не вшит.

---

## 1. Что готово / что нет

| Готово | Не готово / не обещано |
|--------|-------------------------|
| Аутентификация и чтение счёта на **живых** конечных точках | Прибыль, «бот готов», гиры 2 / 2.5 / 3 |
| `place` → `ack` → `fill` или `cancel` (WS trade) | Private внутри `app/screaner_b_o.py` |
| Dual-leg: сначала по очереди (W6), затем параллельно (W7) | Непрерывный процесс 24/7 рядом со stub |
| Журнал без секретов: `/data/bbot/private/` | Снятие капа ≈ 100 USD на биржу |
| Изоляция от деревьев D; collector не рестартили | Host Ops-агент (отложен до процесса с живыми заявками) |
| По умолчанию отправка выключена; нужны флаги + явный CLI | Закрытый пакет **testnet/demo** auth (ключи demo не авторизовались; живые заявки — по явной фразе) |

Unlock: [`b-private-unlock.md`](b-private-unlock.md). Контракт журнала:
[`b-private-journal-contract.md`](b-private-journal-contract.md). Лестница этапов:
[`b-private-roadmap.md`](b-private-roadmap.md).

---

## 2. Лестница опытов (кратко)

Все опыты с отправкой заявок — VPS `root@38.180.94.108`, код `/root/spread_staging`,
кап риска ≪ 100 USD на биржу, `K_live=1`. Collector `active`, MainPID стабилен
на окнах опытов.

| Опыт | Суть | Итог |
|------|------|------|
| W3 | Private WS только чтение, обе площадки | `orders_sent=0` |
| W4 | Post-only: выставление + отмена | Bybit, затем OKX |
| W5 | Рыночное открытие + reduce-only закрытие | По одной площадке |
| W6 | Dual-leg market **по очереди** (Bybit buy → OKX sell), n=20 | `n_completed=20`, `orders_sent=80`, `flat_after=true` |
| W7 | Dual-leg market **параллельно** (барьер перед WS), n=1 | `status=ok`, `orders_sent=4`, `flat_after=true` |

Профиль dual-leg (W6/W7): `TRUMPUSDT` / `TRUMP-USDT-SWAP`, номинал ≈ 6–8 USD
на ногу (не BTC min lot — лоты на биржах не совпадают).

CLI (только явный флаг; транспорт CLI по умолчанию не открывает отправку):

- W6: `--ws-w6-dual-leg --w6-n=N --w6-approve-one-shot` + `BBOT_PRIVATE_W6=1`
- W7: `--ws-w7-parallel-dual-leg --w7-n=N --w7-approve-one-shot` + `BBOT_PRIVATE_W7=1`
- Общее: `VENUE=live`, `LIVE_ORDERS=1`, env `/etc/spread/bbot-private-live.env` (режим 600, не git)

---

## 3. Задержка и стык с моделью (`Trade_Lat`)

В симуляторе гира 1.0 константа `Trade_Lat` = 100 мс на обе площадки
([`gear-2-private-params.md`](gear-2-private-params.md), [`strategy-gears.md`](strategy-gears.md)).

**Private sockets = process-lifetime (как public L1).** С 2026-09-02 warm
supervisor (`app/bot/private/ws_warm_session.py`) поднимает private+trade WS
OKX/Bybit при старте private-live / `python -m app.bot` (`VENUE=live` +
`LIVE_ORDERS=1`) **до** signal loop и держит сессию на жизнь процесса:
heartbeat/ping, auto-reconnect с bounded backoff в фоне (в т.ч. на idle —
не lazy на следующем send). Live send **переиспользует** тот же `run_id` /
журнал: нет нового `event_seq=1` + auth + subscribe + REST reseed на каждый
сигнал на здоровой сессии. CLI: `--ws-warm-session` (без send); dual-leg send
подхватывает process warm session автоматически.

**Default live-manager send = trivial dual-leg (2026-09-04).** Staging A/B
showed Contour A (full W6 recover→approve→lease→prepare on signal→send)
~2.3–2.6 s with ~340 ms leg skew. Contour B (queue→ws.send) ~0.7–1.1 ms
signal→send. The live manager default is now Contour B:
`app/bot/private/ws_trivial_dual_leg.py` + `live_broker.default_live_send_pair`.
Strategy filters (coin, size, open/close, already-in-position / held_coin)
stay in `place`. Recover / operator_approval / lease / prepare_approved+
journal fsync / preflight are **off** the hot path. Frames still use W6
`build_bybit_trade_place` / `build_okx_trade_place` (reqId+HMAC+orderLinkId,
OKX instIdCode; OKX WS `id` alphanumeric ≤32 — no underscore). Opt back to W6: `BBOT_PRIVATE_SEND_PATH=w6` **and**
`BBOT_PRIVATE_W6=1`. `BBOT_PRIVATE_W6=1` alone does not switch the manager.
See [`b-private-trivial-dual-leg.md`](b-private-trivial-dual-leg.md).
After both `ws.send`s, Contour B requires both venue trade ACKs before
local `open_*` / close-clear; one-leg reject or ACK timeout stays flat
and flatten-closes the accepted open leg (overnight EDEN `60033` mode).
This is a code default only — no VPS/live deploy in this change.

**Warm + parallel place thread safety (2026-09-04).** Production symptom:
reduce-only close `request_sent` → immediate `post_dispatch_ambiguity` /
`error_code=unknown` (not the 5s ack timeout) when warm gen≥2 + W6/W7
parallel place reused sockets. Root cause: `WebsocketsClientSocket` used a
per-socket asyncio loop with `run_until_complete` from whichever thread called
send/recv (keepalive vs place workers), and keepalive drained/heartbeated only
**private** while idle **trade** could look `connected`. Fix: dedicated owner
loop thread + `run_coroutine_threadsafe` under a per-socket lock; warm
`place_io_section()` pauses keepalive I/O/reconnect for the place+ack window;
trade channel gets heartbeat + silence + non-noise stash so ACK frames are not
stolen. Cold (non-warm) W6 path unchanged. Fail-closed: `TimeoutError` →
ambiguous timeout; venue rejects stay rejects.

**`l1_at_send` / journal fill stamps ≠ venue fill latency.** Метки public
journal `l1_at_send` и stub `Trade_Lat_ms=100` не измеряют время матча на
бирже. Сравнивать place→fill нужно по private journal `request_sent` →
`terminal_update` / venue fill observation на уже тёплой сессии.

Замер W7 n=1 (журнал, монотонные метки):

| Участок | ≈ мс | Комментарий |
|---------|------|-------------|
| Подготовка → общая отправка | ~550 | Локально: подготовка + запись журнала **до** барьера |
| **Отправка → обе ноги исполнены** | **~70** | После одновременного WS; узкое место OKX |
| Метка journal `send` → ACK (Bybit) | ~450 | **Артефакт:** метка до барьера; не физика биржи |

Для параллельного dual-leg в модели ориентир — **ожидание медленной ноги после
одновременной отправки** (~70 мс в этом замере), не сумма ног и не ~450 мс.
Чувствительность для M по-прежнему уместна: 80 / 100 / 150; опорное значение гира 1.0
не менять без отдельного решения в чате модели. Один замер ≠ статистика
(для p50/p90 нужен пакет n, напр. W7 n=20).

`request_ack_rtt` в журнале — **круговая задержка (RTT)**, не односторонний путь до биржи.

---

## 4. Готовность к интеграции с B-bot (реальные сделки)

| Вопрос | Ответ |
|--------|--------|
| Можно ли уже слать из stub? | **Нет.** Отправка заявок живёт только в `app/bot/private/**`. |
| Что стыковать? | Отдельный интерфейс `Broker`: stub остаётся заглушкой, private — реализация. |
| Политика / гиры | По-прежнему Model Simulator; private не несёт стратегию. |
| Риск на VPS | Кап ≈ 100 USD/биржа; однократный испытательный контур; не второй N=337 private. |
| Host Ops | Агент не создан; отложен до **постоянного** процесса с живыми заявками рядом с D. Validator снимает snapshot collector/load при живых заявках. |

B-bot чат: [`b-bot-starter-prompt.md`](b-bot-starter-prompt.md).  
Схема склейки: [`b-v0-block-diagram.md`](b-v0-block-diagram.md).

---

## 5. Индекс документов

| Документ | Роль |
|----------|------|
| [`b-private-status.md`](b-private-status.md) | Этот статус для чужих оркестраторов |
| [`b-private-trivial-dual-leg.md`](b-private-trivial-dual-leg.md) | Default live send = Contour B; how to flip W6 |
| [`b-private-roadmap.md`](b-private-roadmap.md) | Цель ветки и гейты P0–P9 |
| [`b-private-unlock.md`](b-private-unlock.md) | Письменный unlock 2026-08-18 |
| [`b-private-starter-prompt.md`](b-private-starter-prompt.md) | Старт чата B-private |
| [`b-private-journal-contract.md`](b-private-journal-contract.md) | Контракт журнала v1 |
| [`b-private-secrets-manifest.md`](b-private-secrets-manifest.md) | Имена ключей/путей без значений |
| [`gear-2-private-params.md`](gear-2-private-params.md) | Параметры для честности M |
| [`program-roadmap.md`](program-roadmap.md) | Статус задачи в программной карте |

Агенты: `.cursor/agents/b-private-orchestrator.md`, `b-private-runtime-agent.md`,
`b-private-validator-agent.md`. Правило: `.cursor/rules/70-b-private.mdc`.

Проверка журнала: `validation/check_bbot_private_journal.py`.

---

## 6. Запрещено следующим чатам «заодно»

- Вшивать отправку заявок в `stub_broker.py` без отдельного гейта GD.
- Private API в collector / запись в `/data/live`, `/data/bars`, `/data/compacted`.
- Объявлять готовность к промышленной эксплуатации или снимать кап 100 USD.
- Менять `Trade_Lat` в гире 1.0 без чата модели и явного контракта.
- Остановка/перезапуск `spread-collector` ради опытов с приватным API.
