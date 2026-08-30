# B-bot — манифест изоляции от D

Трек: склейка / B-bot.  
Владелец смысла: B-bot Orchestrator.  
Реализующий владелец документа: B Stub Runtime.  
Runtime Storage позже копирует эти пути только в **новые** unit’ы бота.

Связанные спеки: [`b-v0-block-diagram.md`](b-v0-block-diagram.md),
[`b-bot-starter-prompt.md`](b-bot-starter-prompt.md).

Документ — контракт путей, unit и запретов. Не развёртывает unit’ы.

---

## 1. Хост и entrypoint

| Поле | Значение |
|------|----------|
| Host | `root@38.180.94.108` |
| Code checkout | `/root/spread_staging` |
| Python | `/root/venv/bin/python` |
| Entrypoint | `python -m app.bot` (новый) |
| Не entrypoint | `app/screaner_b_o.py` |

---

## 2. Свой контур

| Роль | Значение |
|------|----------|
| Unit | `spread-bbot.service` (новый PID; никогда unit collector) |
| Log | `/var/log/spread/bbot.log` |
| Не log D | `/var/log/spread/runtime.log` |
| Data | `/data/bbot/{journal,state,.tmp}` |
| Backup remote prefix | `backup1tb:spread-bbot` |
| Backup lock | `/run/spread-bbot-backup.lock` |
| Не lock D | `/run/spread-backup.lock` |
| Env prefix | только `BBOT_*` в процессе бота |

`ExecStartPre`: `mkdir` только `/data/bbot` и `/var/log/spread`.  
Не делать `mkdir` `/data/live`, `/data/bars`, `/data/compacted`, `/data/spool`.

Отдельный контур Gear 2 would_send (не переиспользует этот unit и не пишет в `/data/bbot`):

| Роль | Значение |
|------|----------|
| Unit | `spread-bbot-gear2.service` |
| Log | `/var/log/spread/bbot-gear2.log` |
| Data | `/data/bbot-gear2/{journal,state,.tmp}` |
| Backup remote prefix | `backup1tb:spread-bbot-gear2` |
| Backup lock | `/run/spread-bbot-gear2-backup.lock` |
| Coins / sockets | BTC, ETH, SOL, XRP (8 public L1) |

Спека профиля: [`b-bot-gear2-contour.md`](b-bot-gear2-contour.md).

Каталог `/var/log/spread` общий с D — ок; имя файла лога бота должно отличаться
(`bbot.log`, не `runtime.log`).

### Backup unit’ы B (имена контракта)

| Unit | Тип |
|------|-----|
| `spread-bbot-backup-transfer.service` | oneshot |
| `spread-bbot-backup-transfer.timer` | timer |
| `spread-bbot-gear2-backup-transfer.service` | oneshot (optional; prefix `spread-bbot-gear2`) |
| `spread-bbot-gear2-backup-transfer.timer` | timer (optional) |

Не копия D `spread-backup-transfer.service` с leftover
`BACKUP_RCLONE_PATH=spread-compacted` или
`BACKUP_TRANSFER_LOCK_PATH=/run/spread-backup.lock`.

Не импортировать и не exec `app.storage.backup_transfer`.  
Uploader B — под `app/bot/` (будущее). Remote только `backup1tb:spread-bbot`.  
Lock только `/run/spread-bbot-backup.lock`.

---

## 3. Черновые лимиты unit

Ниже collector. У collector сейчас нет `MemoryMax` / `CPUQuota`, только
`LimitNOFILE=65535`.

| Параметр | Значение |
|----------|----------|
| `MemoryMax` | `256M` |
| `CPUQuota` | `15%` |
| `LimitNOFILE` | `4096` |
| `Nice` | `10` |

---

## 4. Systemd path hardening (черновик для Runtime Storage)

Контракт путей; **не** развёрнуто. Тело `ExecStart` здесь не задаётся.

| Директива | Значение |
|-----------|----------|
| `ReadWritePaths` | `/data/bbot` `/var/log/spread` |
| `InaccessiblePaths` | `/data/live` `/data/bars` `/data/compacted` `/data/spool` |

Эквивалент: `ProtectSystem` + явный deny тех же D-деревьев.  
Лог: `/var/log/spread/bbot.log` (каталог общий; файл отдельный).

---

## 5. Запрет записи (бот никогда не пишет)

| Цель | Запрет |
|------|--------|
| `/data/live` | write deny |
| `/data/bars` | write deny |
| `/data/compacted` | write deny |
| `/data/spool` | write deny |
| `backup1tb:spread-compacted` | write deny |
| `backup1tb:spread-bars*` | write deny |
| `runtime.log` collector | write deny |
| `failed_batches.log` collector | write deny |
| `backup-transfer.log` D | write deny |

---

## 6. Must never

- Править или импортировать runtime из `app/screaner_b_o.py`.
- `stop` / `restart` / `disable` / edit: `spread-collector`, `spread-compactor*`,
  `spread-backup-transfer*`, `spread-bars*`.
- Клонировать D backup unit с leftover `BACKUP_RCLONE_PATH` /
  `BACKUP_TRANSFER_LOCK_PATH` D.
- Импортировать или exec `app.storage.backup_transfer`.
- Стартовать второй fan-out книг `N=337`.
- Открывать собственные `candle5m` / bar WS в slice 1.
- Читать `/data/bars` в slice 1 (только тики гира 1.0; read-only bars —
  отдельное позднее решение).
- Открывать private WS, загружать API keys, REST-ордера, любой send.

---

## 7. Первый live subscribe set

| Правило | Значение |
|---------|----------|
| Пары | только BTC + ETH |
| Сокеты | 4 book: OKX `books5` + Bybit `orderbook.1` на каждую пару |
| Фильтр | `is_crypto` должен пройти |
| Не стартовать | `L1-crypto` на 249 парах |

Тестовый профиль сигналов (не гир 1.0): `BBOT_MODE=policy`,
`BBOT_PROFILE=signal_test`, `BBOT_COINS=BTC,ETH,LA,DOGE`.
Пороги входа/выхода **0.05%** (все четыре), 8 книжных сокетов.
Застывший вектор `DEFAULT_VARIATION` (0.5%) не меняется.
Проверка журнала: `python3 validation/check_bbot_signals.py --data-root …`.

---

## 8. Импорты

### Разрешено без модификации

- `research/is_crypto.py`
- `app/utils/tick_validity.py`
- `bybit_okx_universe.csv`

### Запрещено

| Модуль | Почему |
|--------|--------|
| `app.storage.paths` | defaults указывают на `/data/live` |
| `app.utils.ws_reconnect` | читает `SPREAD_WS_*` |
| `app.storage.backup_transfer` | контур backup D; B uploader — `app/bot/` |
| lock D `/run/spread-backup.lock` | не переиспользовать |

---

## 9. Slice 1a vs 1b

| Срез | Смысл |
|------|--------|
| 1a | probe-intent: может доказать пути, unit, журнал, изоляцию деревьев |
| 1b | смысл гира 1.0: требует `trade_manager` |

Изоляцию можно валидировать на 1a.  
**Не утверждать** «бот не мешает D», пока Validation не проверит деревья и статус
collector. Черновик unit / hardening ≠ доказательство невмешательства.

---

## 10. Validation snapshot (только команды)

До и после enable бота — команды, не результаты:

```bash
systemctl is-active spread-collector.service
systemctl show spread-collector -p MainPID,NRestarts,ActiveEnterTimestamp
find /data/live /data/bars /data/compacted /data/spool -name '*bbot*'
```

После enable:

```bash
systemctl is-active spread-bbot.service
systemctl show spread-bbot -p MainPID
# MainPID бота ≠ MainPID collector
# journal под /data/bbot
# rclone только prefix backup1tb:spread-bbot
# backup unit’ы: spread-bbot-backup-transfer.{service,timer}
# не D lock /run/spread-backup.lock
```

---

## 11. Статус документа

Этот документ — контракт для unit’ов Runtime Storage.  
Он **не** развёртывает их.  
Утверждение «бот не мешает D» — только после Validation.