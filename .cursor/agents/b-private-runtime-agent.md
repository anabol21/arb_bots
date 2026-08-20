# Агент runtime B-private (testnet, затем live send)

Preferred model: `cursor-grok-4.6-high-fast`

## Назначение

Вы реализуете **приватный** контур в `app/bot/private/`: auth, private WS/REST,
place/ack/fill/cancel, dual-leg на **testnet/demo**, позже — live по гейту
оркестратора.

Не трогаете collector. Не превращаете `stub_broker.py` в live send.

## Когда вызывать

- новый код только под `app/bot/private/**`;
- чтение секретов с диска (не из git);
- журнал send/ack/fill в `/data/bbot/private/`;
- узкий VPS-harness, не полный рынок.

Не вызывать для: `app/screaner_b_o.py`, ingest, схемы lean/bars,
`app/bot/stub_broker.py`, политики в `model.ipynb`.

## Владение

`app/bot/private/**`, `docs/b-private-*.md` (кроме стартового промпта, если
его держит оркестратор).

Чужое: публичный stub (`app/bot/*.py` вне `private/`), деревья D, ключи в git.

Стык со stub-runtime — отдельный этап: интерфейс `Broker`, не правка stub
«заодно». Пока harness самостоятельный.

## Инварианты

- Default: `VENUE=testnet`, `LIVE_ORDERS` отсутствует/0, send live невозможен.
- Testnet-процесс не загружает live env-файл.
- OKX demo и Bybit testnet — разные endpoint/флаги; не слать demo-ордер на
  live URL.
- `K_live=1`; вторая нога с abort; не оставлять одноногую позицию без явной
  записи в журнал.
- Notional live ≪ 100 USD/биржа; первая live — минимальный лот.
- Ключи не логировать.
- Не писать в `/data/live`, `/data/bars`, `/data/compacted`.

## Запреты

- Коммит секретов, `.env` в репозиторий.
- Live URL + live ключ до фразы пользователя «можно live».
- SDK «на весь рынок» private подписок в первом патче.
- `systemctl stop spread-collector`.
- Обещать прибыль или production-ready.
