# B-private — цель и дорожная карта

Трек: склейка / исполнение. Не stub и не collector.  
Unlock: [`b-private-unlock.md`](b-private-unlock.md). Чат: [`b-private-starter-prompt.md`](b-private-starter-prompt.md).  
**Статус для оркестраторов:** [`b-private-status.md`](b-private-status.md) (2026-08-20).

Это не «боевой бот с гиром 1.0». Это **доказанный приватный адаптер** двух бирж
под капом ≈100 USD на площадку.

---

## 1. Результат ветки (done, 2026-08-20)

На VPS, не мешая D, есть узкий испытательный контур в `app/bot/private/**`, который:

1. читает **разные** env: testnet/demo vs live (режим `0600`, не git);
2. на **живых** Bybit и OKX умеет: auth, account read, `place` → `ack` →
   `fill` **или** `cancel`; dual-leg по очереди (W6) и параллельно (W7);
3. пишет журнал в `/data/bbot/private/` без секретов (контракт
   [`b-private-journal-contract.md`](b-private-journal-contract.md));
4. по умолчанию отправка выключена; живые заявки только при `VENUE=live` **и**
   `LIVE_ORDERS=1` **и** явном CLI-флаге опыта **и** фразе в чате;
5. опыты: min lot / matched TRUMP, номинал ≪ 100 USD, `K_live=1`;
6. `spread-collector` оставался `active`; деревья D без файлов бота;
   ключи не в логах.

**Обход лестницы testnet:** P1 auth на Bybit testnet / OKX demo на VPS
**не** закрыт (отклонение ключей). Живое read-only и отправка живых заявок выполнены
после явных фраз пользователя в чате B-private. Это зафиксировано, не
скрыто.

**Не результат этой ветки:** прибыль; гиры 2/2.5/3; private внутри collector;
вшить отправку заявок в `stub_broker.py`; второй N=337; Host Ops-агент (отложен до
процесса с живыми заявками); снятие капа 100 USD; непрерывный runtime 24/7.

Стык со stub — отдельным решением: интерфейс `Broker`, не правка stub
«заодно».

---

## 2. Гейты

Каждый этап закрывается журналом + изоляцией D. Новый тип отправки заявок — только
после Critic и явной фразы, если это живые заявки.

| Этап | Что делаем | Статус |
|------|------------|--------|
| **P0** манифест | пути, venue, имена ключей | **done** |
| **P1** read-only testnet/demo | auth+balance, `orders_sent=0` | **не закрыт** (auth fail); обойдён живым read-only |
| **P2** журнал контракт | send/ack/fill/cancel/reject/abort | **done** |
| **P3–P5** одна нога / fill / dual-leg testnet | лестница demo | **не на testnet**; аналоги на **live** (W4–W6) |
| **P6** private WS узко | один символ | **done** (W3+) |
| **P7** пакет testnet | «testnet done» | **не формально**; пакет живых заявок по фразам |
| **P8** live | Host Ops + min lot + dual-leg | **лестница отправки done** (W4→W7); Host Ops-агент **отложен** |
| **P9** стоп ветки | адаптер доказан; ACK vs `Trade_Lat` — опция M | **done** по адаптеру; замер для M — см. статус |

Подробные CLI и цифры задержек: [`b-private-status.md`](b-private-status.md).

---

## 3. Правила аккуратности

- Код только `app/bot/private/**`. Не `stub_broker.py`, не `screaner_b_o.py`.
- Testnet-процесс **не** открывает файл с подстрокой `live` в пути.
- Не печатать secret/passphrase. В логе: `key_present`, masked prefix.
- Один символ / узкий профиль. Не private на все крипто.
- Review Critic — до **каждой** первой отправки заявок нового типа.
- Отправка по умолчанию выключена. Нет «заодно живые заявки, раз ключи уже есть».

---

## 4. Промпт продолжения (только если снова откроют чат)

```text
Ты — B-private Orchestrator. Модель: gpt-5.6-terra-medium.
Работай по .cursor/agents/b-private-orchestrator.md и docs/b-private-status.md.

Подветка адаптера закрыта (P9, 2026-08-20). Не объявлять «бот готов».
Не вшивать отправку заявок в stub без отдельного решения GD.
Опции: W7 n=20 для статистики; Host Ops при постоянном процессе с живыми заявками;
стык Broker со stub — отдельный чат; Trade_Lat в M — чат модели.
Код только app/bot/private/**. Collector и деревья D не трогать.
```
