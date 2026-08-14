# Lean production soak — 2026-08-05

> **Host migration (2026-08-10):** current production collector host is `root@38.180.94.108`. The IP below is the historical host for this report.

Вердикт: **GO WITH CONDITIONS** для старта накопления lean+bars завтра утром.

---

## 1. Блок конвейера

```text
OKX books5 + Bybit orderbook.1 (+ OKX business candle5m)
  → app/screaner_b_o.py (SPREAD_LEAN_SCHEMA=1, SPREAD_COLLECT_BARS=1)
  → ParquetPublisher / spool (schema_mode=lean | bar_5m)
  → /data/experiments/lean_soak/{live,bars}   # soak
  → завтра cutover: /data/live + /data/bars
```

| Контекст | Значение |
|----------|----------|
| Код | локальный репо → rsync `/root/spread_staging` |
| Исполнение | VPS `root@38.244.198.42` |
| Runtime log soak | `/var/log/spread/lean_soak_runtime.log` |
| Первая материализация | `/data/experiments/lean_soak/live`, `.../bars/bar_5m` |
| Durable (soak) | тот же локальный `/data` (primary); backup remote не трогали |
| Dual ping | pid 351542 — не убивали; сосуществовал |

---

## 2. Что изменили

| Область | Изменение |
|---------|-----------|
| Schema Contract | Заморожены lean ticks + `bar_5m` в `docs/storage-contract.md` |
| Compactor | Complete + checksum + отсутствие local artifact → `offloaded` (INFO), не `compaction_alert` |
| Collector | Env: `SPREAD_ROW_*`, `SPREAD_PERSIST_EVERY`, `SPREAD_UNIVERSE` |
| systemd | `spread-collector.service`: `SPREAD_LEAN_SCHEMA=1`, `SPREAD_COLLECT_BARS=1`, `SPREAD_BARS_ROOT=/data/bars` |
| Docs | `prod-unit-snippets.md`, `local-lean-collector.md`, gap-doc phase D |

---

## 3. Ops blockers (закрыты достаточно для GO WITH CONDITIONS)

| Блокер | Было | Стало |
|--------|------|-------|
| FNF / compaction_alert storm | 68k+ ERROR | **0** alert; **266** `compaction_artifact_offloaded` INFO |
| Диск `/` | 72% (8G free), archive 13G | **28% (21G free)**, archive **370M**; удалено **392112** archive files |
| Unit без lean | флаги отсутствовали | unit на диске обновлён (collector **не** стартовали на prod) |

Remote backup `backup1tb:spread-compacted` **не** чистили.

---

## 4. Soak evidence

| Параметр | Значение |
|----------|----------|
| Старт | 2026-08-05 11:13:06 UTC |
| Стоп | SIGTERM ~11:15:22 UTC (~2.3 min; bars+ticks уже proven) |
| Universe slice | `SPREAD_ROW_END=5` (0G, 1INCH, 2Z, A, AAOI) |
| schema_mode | lean / bar_5m |
| Tick files / rows | **115** / **2230** |
| Bar files / rows | **5** / **5** (по 1 закрытой свече на монету) |
| Failures / rejected / spool | **0** / **0** / **0** |
| Shutdown | flush ticks+bars; exit без `shutdown incomplete` / ERROR |
| Lean columns | exact `LEAN_TICK_BODY_COLS`; int timestamps; no `spread_*` |
| Bars | `bar_end - bar_start = 300000`; `ref_exchange=okx`; volume ≥ 0 |
| Model smoke | `spread_long/short` из L1 на всех 2230 rows — OK |
| Continuity (0G) | n=357, median gap ~301 ms, max gap ~2.7 s |

Команда soak (повтор):

```bash
mkdir -p /data/experiments/lean_soak/{live,bars,spool}
cd /root/spread_staging
SPREAD_LEAN_SCHEMA=1 SPREAD_COLLECT_BARS=1 \
SPREAD_PARQUET_ROOT=/data/experiments/lean_soak/live \
SPREAD_BARS_ROOT=/data/experiments/lean_soak/bars \
SPREAD_SPOOL_ROOT=/data/experiments/lean_soak/spool \
SPREAD_RUNTIME_LOG=/var/log/spread/lean_soak_runtime.log \
SPREAD_FAILED_BATCHES_LOG=/var/log/spread/lean_soak_failed.log \
SPREAD_ROW_END=5 SPREAD_PERSIST_EVERY=100 SPREAD_BAR_PERSIST_EVERY=5 \
/root/venv/bin/python app/screaner_b_o.py
```

---

## 5. Review Critic (независимо)

Серьёзные residual risks (не блокеры старта, но условия):

1. Soak ≠ full-universe / multi-hour — только 5 монет и ~2 мин.
2. `offloaded` доверяет complete+checksum; durable copy после retention — на remote (уже подтверждён для canary отдельно). Не проверяли remote в этом soak.
3. Unit уже с lean=1: случайный `systemctl start` до cutover checklist — риск. Завтра — осознанный старт.
4. Primary path локальный `/data`, не mount; backup — вторичный контур.

---

## 6. GO / NO-GO

**GO WITH CONDITIONS** — можно включать накопление завтра утром на `/data/live` + `/data/bars`.

Условия:

1. Новый день `event_date` (не мешать v1 canary day partitions без нужды).
2. Следить за диском (цель: не опускаться ниже ~5G free).
3. Compactor/backup timers активны.
4. Не смешивать v1 и lean в одной day-partition.

**NO-GO** только если перед стартом диск снова <~5G free или collector падает при enable.

---

## 7. Operator checklist — завтра утро

```bash
# 0) Диск и timers
df -h /
systemctl list-timers 'spread-*' --no-pager

# 1) Код / unit уже на staging; при необходимости повторный rsync с ноутбука
cp /root/spread_staging/deploy/systemd/spread-collector.service /etc/systemd/system/
systemctl daemon-reload

# 2) Корни
mkdir -p /data/live /data/bars /data/spool /data/compacted /var/log/spread

# 3) Старт lean collector
systemctl enable --now spread-collector.service
systemctl status spread-collector.service --no-pager

# 4) Проверка флагов
grep -E 'runtime_paths|schema_mode=lean|collect_bars=true' /var/log/spread/runtime.log | tail -5

# 5) Через ~5–10 мин: файлы
find /data/live -name '*.parquet' | head
find /data/bars/bar_5m -name '*.parquet' | head

# Rollback
# systemctl edit → SPREAD_LEAN_SCHEMA=0 SPREAD_COLLECT_BARS=0; daemon-reload; restart
```

Ручной full-universe (без systemd), если нужен контроль:

```bash
cd /root/spread_staging
SPREAD_LEAN_SCHEMA=1 SPREAD_COLLECT_BARS=1 \
SPREAD_PARQUET_ROOT=/data/live SPREAD_BARS_ROOT=/data/bars \
SPREAD_SPOOL_ROOT=/data/spool \
SPREAD_RUNTIME_LOG=/var/log/spread/runtime.log \
SPREAD_FAILED_BATCHES_LOG=/var/log/spread/failed_batches.log \
/root/venv/bin/python app/screaner_b_o.py
```
