# Дашборд: эксперимент E2 (shadow N=1 XRP)

**Вывод сверху.** E2 — теневой скринер на **одной паре XRP** (`N=1`, не `ping×N`) рядом с matched ping; цель — фальсифицировать **H1** (fan-out подписок). Статус запуска и PID — в §6; вердикт H1 **не** закрывает `gate #1`.

| Поле | Значение |
|------|----------|
| Трек | (D) сбор / хранение — подзадача задержки; трек (B) не трогать |
| Контрольная точка | [`program-roadmap.md`](program-roadmap.md) §3, `gate #1` — слой хвостов vs ping |
| Статус E2 | `running` — см. блок «Состояние прогона» ниже |
| Окно | `2026-08-10T17:19:55Z` → `19:19:55Z` (2 ч) |
| Код | локальный репозиторий → VPS `/root/spread_staging` (без патча ingest) |
| Выполнение | VPS `root@38.244.198.42`, **отдельный** процесс shadow (не `systemd` prod) |
| Журналы shadow | `/var/log/spread/e2_n1_xrp_runtime.log` |
| Журналы ping | `/var/log/spread/e2_n1_xrp_ping_dual.log` |
| Первая материализация | `/data/experiments/e2_n1_xrp/live` (VPS local disk) |
| Durable | **нет** — артефакт эксперимента; не писать в `/data/live` |
| Вне области | уплотнение, backup, retention, модель, правка WS / parse / spread, restart prod unit без согласования |

Канон: [`latency-root-cause-experiments.md`](latency-root-cause-experiments.md) §H1 / E2 / лестница §5 · [`latency-e0-dashboard.md`](latency-e0-dashboard.md) · [`latency-e0-evidence-20260805.md`](latency-e0-evidence-20260805.md) · [`latency-screener-vs-ping-experiment.md`](latency-screener-vs-ping-experiment.md).

### Состояние прогона

| Поле | Значение |
|------|----------|
| Статус | `running` (smoke OK ~2 мин: `Loaded pairs: 1`, XRP subscribed, ping ~20–30 мс) |
| Start UTC | `2026-08-10T17:19:55Z` |
| Expected end UTC | `2026-08-10T19:19:55Z` |
| Shadow PID | `1255179` → `/var/log/spread/e2_n1_xrp_shadow.pid` |
| Ping PID | `1255182` → `/var/log/spread/e2_n1_xrp_ping.pid` |
| Prod `spread-collector` | **не стартовать** ради E2; unit был и остаётся `inactive` — факт среды, не часть дизайна H1 |
| Universe slice | `SPREAD_ROW_START=329` `SPREAD_ROW_END=330` → одна строка `base_coin=XRP` |
| Smoke facts | `collect_bars=false`; `schema_mode=lean`; `parquet_root=/data/experiments/e2_n1_xrp/live`; heartbeat `buffer_size` растёт (~500/30 с) |

---

## 1. Уже доказано (факт)

Matched XRP, 6 ч, вердикт **B** ([`latency-screener-vs-ping-20260805-result.md`](latency-screener-vs-ping-20260805-result.md)):

| Факт | Числа |
|------|-------|
| Хвост скринера ≫ ping | OKX p99 ~1258 vs ~37 мс; Bybit p99 ~1350 vs ~46 мс |
| Floor близкий | p50 OKX 40 vs 29; Bybit 18 vs 17 |
| Dual-spike при тихом ping | 42 мин dual>1 с |
| E0 / H4 | слабо ALIVE по OR (`buffer_size>0`); классический BP (`queue_depth` / `backpressure_hit`) отсутствует |

**Не доказано до E2:** что хвост — налог от **N book-WS** (H1), а не процессный оверхед / GIL / persist на одном loop при любом N.

Метрика (не менять): `delivery_latency_ms = local_recv_ts_ms − exchange_ts` (нога-триггер). Не путать с `freshness_ms`.

---

## 2. Гипотезы на экране (≤3)

| ID | Слой | Утверждение | Сигнатура | Роль E2 |
|----|------|-------------|-----------|---------|
| **H1** | цикл / fan-out | при `N≈1` хвост скринера ≈ ping; prod-хвост — налог от многих book-listeners | shadow p95/p99 в пределах ~2× ping; dual>1 с редки/отсутствуют | **целевая фальсификация** |
| H_process | процесс скринера | даже при `N=1` путь listener→json→calc→buffer даёт dual>1 с при тихом ping | shadow всё ещё p99 ≳ 1 с / dual как prod | ослабляет чистый H1 → дальше E3/E5 |
| H_measure | измерение | мало строк lean / ping, или slice не XRP | `Loaded pairs != 1`, пустой parquet, ping samples≈0 | критерий провала измерения |

Полный каталог — [`latency-root-cause-experiments.md`](latency-root-cause-experiments.md).

---

## 3. Дизайн E2

### Что делаем

1. Запустить **shadow** `app/screaner_b_o.py` с `ROW` на одну пару XRP, `SPREAD_COLLECT_BARS=0`, persist on, **отдельные** `SPREAD_PARQUET_ROOT` / spool / runtime log.
2. Параллельно — matched **ping** XRP (`validation/ping_okx_bybit_2h.py`, OKX `XRP-USDT-SWAP` + Bybit `XRPUSDT`) на 2 ч.
3. После окна: квантили trigger-latency shadow vs ping; dual-spike минуты shadow при тихом ping.

### Universe slice (факт кода)

```text
pairs = rows[SPREAD_ROW_START:SPREAD_ROW_END]   # полуинтервал
# bybit_okx_universe.csv: индекс 329 = XRP → START=329 END=330 → N=1
```

### Env shadow (не prod)

| Переменная | Значение | Зачем |
|------------|----------|-------|
| `SPREAD_ROW_START` / `SPREAD_ROW_END` | `329` / `330` | N=1 XRP |
| `SPREAD_COLLECT_BARS` | `0` | bars off (канон E2) |
| `SPREAD_LEAN_SCHEMA` | `1` | те же stamps, что prod lean |
| `SPREAD_PARQUET_ROOT` | `/data/experiments/e2_n1_xrp/live` | не `/data/live` |
| `SPREAD_SPOOL_ROOT` | `/data/experiments/e2_n1_xrp/spool` | изоляция spool |
| `SPREAD_RUNTIME_LOG` | `/var/log/spread/e2_n1_xrp_runtime.log` | изоляция логов |
| `SPREAD_FAILED_BATCHES_LOG` | `/var/log/spread/e2_n1_xrp_failed_batches.log` | изоляция |
| `SPREAD_PERSIST_EVERY` | `5000` | persist **on**; порог ниже prod `100000`, иначе за 1–2 ч при N=1 буфер может не сброситься до shutdown — только observability flush, не смена формулы |

`SPREAD_BARS_ROOT` не задавать / bars off — candle listeners не стартуют при `COLLECT_BARS=0`.

### Кто

| Роль | Действие |
|------|----------|
| Orchestrator + Validation | запуск shadow+ping, наблюдение, вердикт H1 |
| Review Critic | сила вывода после окна (запрет «gate #1 закрыт») |
| Text Stylist | только этот документ |
| Runtime Storage / Schema | не нужны (env-only, схема lean уже есть) |

### Не делать

- `systemctl stop/start spread-collector` без явного согласования
- писать в `/data/live` / prod spool / prod `runtime.log`
- патчить WS / parse / spread
- compaction / backup / truncate логов / delete backlog
- трек (B), модель, I1 instrumentation в этом прогоне

### Команды VPS (готовые)

```bash
# 0) Контекст (только чтение)
hostname; date -u
systemctl is-active spread-collector || true
df -h / /data | head
free -h | head -2
test -f /root/spread_staging/bybit_okx_universe.csv

# 1) Каталоги shadow
mkdir -p /data/experiments/e2_n1_xrp/live \
         /data/experiments/e2_n1_xrp/spool \
         /var/log/spread

# 2) Shadow N=1 (nohup; не systemd prod)
cd /root/spread_staging
nohup env \
  SPREAD_ROW_START=329 \
  SPREAD_ROW_END=330 \
  SPREAD_COLLECT_BARS=0 \
  SPREAD_LEAN_SCHEMA=1 \
  SPREAD_PERSIST_EVERY=5000 \
  SPREAD_PARQUET_ROOT=/data/experiments/e2_n1_xrp/live \
  SPREAD_SPOOL_ROOT=/data/experiments/e2_n1_xrp/spool \
  SPREAD_RUNTIME_LOG=/var/log/spread/e2_n1_xrp_runtime.log \
  SPREAD_FAILED_BATCHES_LOG=/var/log/spread/e2_n1_xrp_failed_batches.log \
  /root/venv/bin/python app/screaner_b_o.py \
  > /var/log/spread/e2_n1_xrp_shadow.nohup.out 2>&1 &
echo $! | tee /var/log/spread/e2_n1_xrp_shadow.pid

# 3) Ping matched XRP, 2h
nohup /root/venv/bin/python validation/ping_okx_bybit_2h.py \
  --duration-sec 7200 \
  --okx-inst XRP-USDT-SWAP \
  --bybit-symbol XRPUSDT \
  --log-file /var/log/spread/e2_n1_xrp_ping_dual.log \
  > /var/log/spread/e2_n1_xrp_ping.nohup.out 2>&1 &
echo $! | tee /var/log/spread/e2_n1_xrp_ping.pid

# 4) Smoke 2–5 мин
sleep 5
grep -E 'Loaded pairs|runtime_paths|XRP \| (OKX|Bybit) subscribed' \
  /var/log/spread/e2_n1_xrp_runtime.log | head -20
# ожидание: Loaded pairs: 1; подписки XRP books5 + orderbook.1
ps -p "$(cat /var/log/spread/e2_n1_xrp_shadow.pid)" -o pid,etime,cmd
ps -p "$(cat /var/log/spread/e2_n1_xrp_ping.pid)" -o pid,etime,cmd
tail -5 /var/log/spread/e2_n1_xrp_ping_dual.log
ls /data/experiments/e2_n1_xrp/live/base_coin=XRP/event_date=*/ 2>/dev/null | head
```

### Остановить только shadow / ping (prod не трогать)

```bash
# Shadow
kill -TERM "$(cat /var/log/spread/e2_n1_xrp_shadow.pid)"
# дождаться shutdown_flush_done в e2 runtime log (до ~120 с)

# Ping (если ещё жив после duration)
kill -TERM "$(cat /var/log/spread/e2_n1_xrp_ping.pid)" 2>/dev/null || true

# НЕ делать:
# systemctl stop spread-collector
# kill <prod pid из unit>
```

### Анализ после окна (офлайн / read-only)

Тот же каркас, что matched 2026-08-05: [`research/latency_screener_vs_ping.ipynb`](../research/latency_screener_vs_ping.ipynb).

```text
S_*  ← lean /data/experiments/e2_n1_xrp/live/base_coin=XRP/...
       derive: okx_latency_ms = okx_local_recv_ts_ms - okx_ts_ms
               bybit_latency_ms = bybit_local_recv_ts_ms - bybit_ts_ms
       сравнивать trigger-ногу
P_*  ← /var/log/spread/e2_n1_xrp_ping_dual.log  (age_ts_ms / latency_ms)
Окно ← meta start/finished ping ∩ min/max event_local_ts_ms shadow
```

---

## 4. Светофор (критерии H1)

Опора на прежние порядки: prod p99 **S~1.2 с**, ping p99 **P~40 мс** (отношение ~30×). Для E2 порог **тесные** относительно prod, не «чуть лучше 1 с».

| Исход | Порог (после ≥60 мин steady-state; отбросить первые ~5 мин subscribe) | Следствие |
|-------|----------------------------------------------------------------------|-----------|
| **H1 поддержана** | shadow p99 (OKX и Bybit, trigger-leg) **≤ ~2×** ping p99 той же ноги **и** доля минут dual>1 с ≈ 0 (или ≪ matched-prod 42/360) | fan-out N — необходимый фактор хвоста; дальше E4/E6 (dose), не правка ingest |
| **H1 ослаблена** | shadow всё ещё dual>1 с при тихом ping **или** p99 shadow ≳ **500 мс** при ping p99 ≲ 50 мс (≫2×) | не чистый fan-out N → E3 (persist off) / процессный оверхед |
| **Измерение провалено** | `Loaded pairs` ≠ 1; нет parquet XRP; ping samples≈0; окно <45 мин overlap | чинить конфиг/запуск; не вердиктить H1 |
| **Запрет вывода** | короткий smoke (минуты) → «gate #1 закрыт» / «prod можно не шардировать» | E2 только про H1; gate #1 остаётся open |

Числовой якорь «~2×»: при ping p99≈40 мс → shadow p99 ≲ **80–100 мс** на обеих ногах. Если shadow p99~200–400 мс без dual>1 с — **частичная** поддержка H1 (хвост сжат vs prod, но не ≈ping); зафиксировать как «H1 частично», не binary fail.

---

## 5. Риски

- Prod unit `inactive`: E2 всё ещё валиден (shadow vs ping); не смешивать с «налог от соседнего полного коллектора».
- `PERSIST_EVERY=5000` ≠ prod `100000` — влияет на частоту flush (H5), не на число WS; при вердикте H1 отметить.
- Disk: `/data/live` уже ~12G; shadow пишет только в `experiments/e2_n1_xrp`.
- RAM 2 GiB: N=1 + ping умеренно; не запускать E3/E4 параллельно.
- Неверный `ROW_*` → не XRP / N≠1 → брак измерения.

---

## 6. Следующий шаг

1. Дождаться конца 2h окна (или `finished` в ping log).
2. Снять квантили S vs P + dual-минуты → короткий evidence doc (как E0).
3. Review Critic: сила H1; запрет закрытия gate #1.
4. По лестнице: если H1↑ → E4/E6; если H1↓ → E3; **не** правка приёма.
