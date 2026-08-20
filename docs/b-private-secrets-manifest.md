# B-private — манифест секретов / путей / venue

Дата: 2026-08-19. Владелец: B Private Runtime.  
Этап: **1 — без send**. Значения ключей в этот документ не входят.

## Venue и флаги

| Переменная | Default | Смысл |
|------------|---------|--------|
| `VENUE` | `testnet` | `testnet` = Bybit testnet + OKX demo. `live` только после явной фразы и гейта. |
| `LIVE_ORDERS` | `0` | Send разрешён **только** при `VENUE=live` **и** `LIVE_ORDERS=1`. Иначе send запрещён. |

Harness этапа 1 (`python -m app.bot.private`) всегда `VENUE=testnet` и отказывается работать, если `LIVE_ORDERS` truthy или `VENUE=live`.

## Файлы секретов (вне git, mode 600)

| Файл | Когда читается | Назначение |
|------|----------------|------------|
| `/etc/spread/bbot-private-testnet.env` | `VENUE=testnet` (предпочтительно) | Demo/testnet ключи |
| `/etc/spread/bbot-private.env` | `VENUE=testnet` (алиас) | То же; не класть сюда live |
| `/etc/spread/bbot-private-live.env` | только `VENUE=live` | Live-ключи (~100 USD/биржа) |

Override: `BBOT_PRIVATE_ENV_FILE=/path/to/file`. При `VENUE=testnet` путь **нельзя** содержать подстроку `live` (case-insensitive) — процесс testnet не открывает live env.

Не коммитить `.env`. Не печатать secret/passphrase в чат, лог, журнал.

## Имена переменных в env-файле (значения — только на диске)

**Testnet / demo** (`bbot-private-testnet.env` или `bbot-private.env`):

| Имя | Биржа |
|-----|--------|
| `BYBIT_TESTNET_API_KEY` | Bybit testnet |
| `BYBIT_TESTNET_API_SECRET` | Bybit testnet |
| `OKX_DEMO_API_KEY` | OKX demo |
| `OKX_DEMO_API_SECRET` | OKX demo |
| `OKX_DEMO_PASSPHRASE` | OKX demo |

**Live** (только `bbot-private-live.env`, не на этапе 1):

| Имя | Биржа |
|-----|--------|
| `BYBIT_LIVE_API_KEY` | Bybit mainnet |
| `BYBIT_LIVE_API_SECRET` | Bybit mainnet |
| `OKX_LIVE_API_KEY` | OKX live |
| `OKX_LIVE_API_SECRET` | OKX live |
| `OKX_LIVE_PASSPHRASE` | OKX live |

В логах: `key_present=true|false`, опционально masked prefix (`abcd…`), никогда полное значение.

## Endpoints (этап 1, read-only)

| Venue | Биржа | REST base | Auth / флаги | Account read |
|-------|--------|-----------|--------------|--------------|
| testnet | Bybit | `https://api-testnet.bybit.com` | HMAC v5 headers | `GET /v5/account/wallet-balance?accountType=UNIFIED` |
| testnet | OKX demo | `https://www.okx.com` | HMAC + `x-simulated-trading: 1` | `GET /api/v5/account/balance` |
| live | Bybit | `https://api.bybit.com` | — | **не этап 1** |
| live | OKX | `https://www.okx.com` без simulated | — | **не этап 1** |

Этап 1 **не** вызывает place/cancel/amend и **не** открывает private WS / market subscriptions.

Один опциональный символ для будущих шагов (не обязателен для account read): `BTC-USDT-SWAP` / `BTCUSDT` — harness этапа 1 его не подписывает.

## Пути процесса

| Роль | Путь |
|------|------|
| Данные / журнал probe | `/data/bbot/private/` |
| Лог | `/var/log/spread/bbot-private.log` |
| Запрещено | `/data/live`, `/data/bars`, `/data/compacted`, spool D, `runtime.log`, stub `/var/log/spread/bbot.log` как primary |

Локальный fallback только если `/data/bbot/private` недоступен для записи: `<repo>/output/bbot/private/` (dev). На VPS целевой путь — `/data/bbot/private/`.

## Изоляция от D / stub

- Код только `app/bot/private/**`.
- Не править `app/screaner_b_o.py`, `stub_broker.py`, deploy/systemd collector.
- Не писать в деревья D и не stop/restart `spread-collector` / compactor / backup D.

## Критерий этапа 1

- Манифест зафиксирован (этот файл + код `app/bot/private`).
- При наличии testnet/demo env: auth + account read на Bybit testnet и OKX demo; journal probe без секретов; `orders_sent=0`.
- Без env: локальный selftest конфигурации проходит; network auth — `unavailable`, не fail закрытия гейта.
