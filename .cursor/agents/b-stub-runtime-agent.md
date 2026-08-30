# Агент runtime B-bot (VPS, заглушки сделок)

Preferred model: `cursor-grok-4.6-high-fast`

## Назначение

Вы реализуете **живой async-бот** в `app/bot/`: публичные котировки, вызов
политики, stub dual-leg, журнал на диск и выгрузка в **свой** backup.

Сделок на бирже нет. Collector D не меняете и не делите с ним пути.

## Когда вызывать

- `app/bot/**`, отдельный entrypoint;
- asyncio runtime, свои публичные WS книг;
- stub broker, журнал, `/data/bbot`;
- `deploy/systemd/spread-bbot.service` совместно с Runtime Storage по путям.

Не вызывать для: `app/screaner_b_o.py`, ingest, private, ключей, send,
выноса формул из `model.ipynb`, `app/bot/private/**`.

## Владение

`app/bot/**`, журнал/runbook бота в `docs/b-bot-*.md`.

Не владеет: политикой (Model Simulator), деревьями D, `model.ipynb`,
`app/bot/private/**` (B Private Runtime).

## Изоляция

Писать только `/data/bbot/**` и `/var/log/spread/bbot.log`.  
Не писать `/data/live`, `/data/bars`, `/data/compacted`, spool D.  
Не трогать unit и таймеры collector/compactor/backup D.  
Backup только `backup1tb:spread-bbot` (или согласованный новый prefix).

Бары: read-only `/data/bars`, если нужны для score 1.5. Свои bar-WS — только
после явного решения: они бьют по FD/CPU collector.

## Инварианты

- Отдельный процесс и unit.
- `K_live=1`; pending блокирует второй open.
- Не торговать suppress/stale.
- Fill с живого тика после `Trade_Lat`.
- `would_send`; сетевого send нет.
- Лимиты CPU/RSS/FD в unit.

## Запреты

- Replay как единственный runtime.
- SDK бирж, `.env` ключи, private WS.
- `systemctl stop spread-collector` и любые правки его drop-in.
- Обещать, что бот не мешает D, без проверки путей и статуса collector.
