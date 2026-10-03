# Контуры D / M / B

Один репозиторий, три линии работ. Это зоны ответственности, не отдельные ветки `git`. Нумерация одна и та же в корневом [`architecture.md`](architecture.md), в [`AGENTS.md`](AGENTS.md) и здесь:

| Контур | Что это | Код |
|--------|---------|-----|
| **D** | публичный сбор и хранение | `app/screaner_b_o.py`, `app/storage/**` |
| **M** | историческая модель | `model.ipynb`, `docs/strategy-gears.md` |
| **B** | склейка: stub `would_send` и B-private | `app/bot/**`, `app/bot/private/**` |

Закрытие гира в M — не готовность живого бота. Хранение — задача контура **D**, не единственная цель репозитория.

Канон топологии, журналов и гейтов: [`architecture.md`](architecture.md). Правила: [`.cursor/rules/`](.cursor/rules/). Индекс `docs/`: [`docs/README.md`](docs/README.md).

---

## D — сбор и хранение

Публичные каналы, валидные тики, резервная копия. Точка входа: `app/screaner_b_o.py`. Профиль `L1` принят как основа блок-схемы склейки. Сводка: [`docs/d-track-ready-for-b.md`](docs/d-track-ready-for-b.md). Контракт хранения: [`docs/storage-contract.md`](docs/storage-contract.md).

Живой writer в `deploy/systemd/`: `spread-collector-next.service` (HOT_ADD, `/data/live`). Шаблон `spread-collector.service` не включать поверх next.

Закодированный путь D — гибрид: publisher → локальный hive, spool при сбое, compactor, rclone. Окончательный выбор дизайна хранения внутри D не закрыт. Конечная копия тиков — `backup1tb:spread-compacted`; это не сам `/data/live`.

Всегда различать:

1. локальная разработка
2. runtime на VPS (`/root/spread_staging`)
3. подтверждённая копия rclone

Успех локально ≠ успех на сервере ≠ запись в резерв.

Заморожено без явного разрешения: приём котировок, разбор бирж и расчёт спреда внутри сборщика. Приватные ключи и send в сборщик не класть.

---

## M — модель и историческая симуляция

Только симулятор на истории. Живой контур, приватные каналы и правка публичного сбора «под модель» в этот трек не входят.

С номером гира растут охват, затем строгость статистики, затем адаптивность размера, затем риск подгонки. Подробности: [`docs/strategy-gears.md`](docs/strategy-gears.md).

| Гир | Что растёт |
|-----|------------|
| `0.8` | фундамент в истории `git` |
| `1.0` | одна монета, фиксированный контур — **закрыт** |
| `1.5` | охват: скринер более волатильных монет — **закрыт** |
| `2` | охват: несколько монет, та же модель 1.0 — **закрыт (контур; 2.2 вне scope)** |
| `2.2` | наблюдение / упрощённый прогон, замороженные ручки — **закрыт** (не 2.5, не 3, не живой контур) |
| `2.5` | адаптивность: политика размера — **заблокирован** до явного unlock |
| `3` | данные и риск подгонки: поиск параметров по эпизодам аномалий |

Прыжок к 2.5 или 3 запрещён. Поиск в пространстве параметров — только гир 3.

Гир **2.2 закрыт как наблюдение**: потребитель [`research/gear22_backtest/`](research/gear22_backtest/) (`policy.decide`, прогон `replay.py` с часами 1 Гц и `K=1`, `SLOT_MODE=global`, ручки в `params_frozen.py`); таблица признаков `gear22_bt_features_v1`. Заполнение — `spread_last` той секунды, не `Trade_Lat`. `combined_mark` — метка наблюдения, не прибыль. Ручки: `theta_open=0.50`, `p50_open=0.60`, `min_profit_pp=0.20`, `min_theta_close=0.05` (включён; `0` ≠ `None`), `min_spread_open=None`, `fee_round_trip_pp=0.30`, `SLOT_MODE=global`.

Опорные артефакты:

- `model.ipynb` — код модели и симулятора
- `gear1.svg` — схема закрытого контура гира 1
- [`docs/strategy-gears.md`](docs/strategy-gears.md) — лестница гиров
- [`docs/data-format-model.md`](docs/data-format-model.md) — **запрос** данных со стороны модели (не контракт сборщика)

Спреды и амплитуду модель считает по тикам `L1`. Пятиминутные бары объёма нужны с **гира 1.5**.

---

## B — склейка и исполнение

Спека блок-схемы: [`docs/b-v0-block-diagram.md`](docs/b-v0-block-diagram.md). Stub и журналы `would_send` живут в `app/bot/**` (не `private/`). Приватный контур — `app/bot/private/**`.

**Контур B разблокирован.** Путь живой отправки по умолчанию — очередь → `ws.send` (`app/bot/private/ws_trivial_dual_leg.py`). Полный W6 (recover → approve → lease → preflight) на этом пути выключен, пока явно не заданы `BBOT_PRIVATE_SEND_PATH=w6` и `BBOT_PRIVATE_W6=1`.

Живая отправка только за гейтами среды и fail-closed. Имена — в [`.cursor/rules/70-b-private.mdc`](.cursor/rules/70-b-private.mdc) и в [`architecture.md`](architecture.md) §6.C / §7:

- `BBOT_BROKER=private_live`
- `VENUE=live`
- `LIVE_ORDERS=1`

Нет любого из флагов — send нет. Журнал stub всегда `would_send=true`, `send=false`. GREEN would_send не равен разрешению на live. Кап риска живых заявок ≈ 100 USD на биржу. Секреты не в git, не в docs, не в логах.

Прод **would_send** (stub, hot-add expand-only, без live orders): [`docs/would-send-prod-status.md`](docs/would-send-prod-status.md). Юнит `spread-bbot-would-send-prod`, данные `/data/bbot-would-send-prod`, профиль `gear22_would_send`.

Статус адаптера B-private (снимок опытов, не «бот готов»): [`docs/b-private-status.md`](docs/b-private-status.md).

### Не останавливать без явной просьбы

Шаблоны есть в `deploy/systemd/`:

- `spread-collector-next.service` — живой writer D
- `spread-bbot-would-send-prod.service` — прод would_send (stub, без live orders)
- `spread-bbot-gear22-live-canary.service` — live canary контура B

Не включать `spread-collector.service` поверх next. Не вешать `BindsTo=` сборщика на юниты бота. Файл юнита в git сам по себе процесс не стартует.

---

## Что не смешивать

| Ось | Разделение |
|-----|------------|
| Каналы | публичный сборщик D ≠ приватный бот B |
| Проверка | симуляция на истории (M) ≠ живая торговля |
| Схема | запрос модели (`docs/data-format-model.md`) ≠ контракт хранения D |
| Журнал | stub `would_send` / `send=false` ≠ фактический `ws.send` |
