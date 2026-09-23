# Предпроизводственный dashboard latency-gate

> **Вердикт: `NOT FINAL / production verdict не обоснован`.** Raw-артефакты
> r2 получены с NEW VPS, проверены SHA-256 и пересчитаны. `A` и `B` valid и
> quiet; `C` получила 6/94 unplanned reconnect (OKX/Bybit) при лимите `≤1`,
> поэтому `C=measurement_failed` и A/B/C matrix не применима. Кроме того,
> отсутствуют controlled background off/on и два production-like окна с
> bars/persistence. Track (B) остаётся закрытым.

## 1. Блок конвейера и граница вывода

- **Трек / блок:** `(D)`; standalone shadow WebSocket fan-out → `asyncio`
  loop → discard (`B`) либо in-memory parse/calc (`C`).
- **Production entrypoint:** `app/screaner_b_o.py` не менялся и данным
  dashboard не валидируется.
- **Плановая среда r2:** NEW VPS `root@38.180.94.108`, experiment root
  `/data/experiments/wsfanout_abc_20260812r2/`; локальные artifacts должны
  быть отдельной копией, а не `/data/live` или mounted durable target.
- **Persistence/bars:** выключены в каждом arm r2. Следовательно, даже
  успешный r2 является standalone no-persistence evidence, а не
  production-profile evidence с bars/publisher/spool.

## 2. Статус r2: что доказано и что не доказано

| Проверка r2 | Требуемый первоисточник | Статус | Причина |
|---|---|---|---|
| Завершение supervisor и exit status arm/ping | `supervisor.status`, `arm_*/arm_manifest.json` | `verified` | `A→B→C`, все probe/ping exit `0`, supervisor finished |
| Фактические start/end/warmup | manifests + `runtime.jsonl` | `verified` | около 3600 s wall, 600 s warmup, около 3000 s steady в каждой arm |
| `B/C = 600/600` connections/subscription sends | start/runtime counters | `B passed`; `C failed validity` | B `600/600`; C active max 600, но 700 sends после reconnects |
| XRP delivery raw по обеим ногам | `xrp_delivery_{okx,bybit}.csv` | `verified` | exact pooled raw S p50/p95/p99/max посчитаны |
| Raw loop lag | `loop_lag.csv` | `verified` | exact pooled raw quantiles и threshold counts посчитаны |
| Matched ping overlap и validity | `ping_xrp.log` | `A/B passed`; `C invalid` | A/B matched sample counts; C Bybit ping имеет один error |
| Reconnect/drop/error/resource budgets | runtime counters + manifests | `A/B passed`; `C failed` | C: OKX 6, Bybit 94 unplanned reconnect; resource abort 0 во всех arms |

**Валидность рук:** `A = valid`, `B = valid`, `C = measurement_failed`.
Именно failure `C`, а не доступность artifacts, запрещает использовать эту
серию в decision matrix.

## 3. Известная исходная картина — не замена r2

| Контекст | S OKX p99 / P p99 | S Bybit p99 / P p99 | Dual `>500` / `>1000` | Доказательная граница |
|---|---:|---:|---:|---|
| Production `N≈337`, bars/persistence включены, 26 мин | 612 / 47 мс (13.01×) | 676 / 39 мс (17.33×) | 25 / 10 из 26 | Наблюдаем production tail, но это не controlled shadow point |
| Shadow `N=50`, bars off, отдельный persist mode, 26 мин | 50 / 47 мс (1.06×) | 111 / 39 мс (2.85×) | 0 / 0 из 26 | Тихая standalone point, не production equivalence |
| Shadow `N=100`, NEW VPS, 25 мин | 56 / 48 мс (1.17×) | 41 / 35 мс (1.17×) | 0 / 0 из 26 | Тихая standalone point |
| A/B/C r1, 50 мин на руку | Exact pooled S отсутствует | Exact pooled S отсутствует | 0 / 0 из 50 во всех arms | `inconclusive / measurement-limited`; C имела 229 reconnect/error |

Источник первых двух строк — [dose N](latency-dose-n-results.md); третьей —
[host N=100](latency-host-n100-results-20260811.md); четвёртой —
[A/B/C r1](latency-ws-fanout-three-arm-results.md). Production `N≈337`
содержит bars/persistence и co-resident runtime; его нельзя количественно
сопоставлять со standalone rows.

## 4. Каузальный verdict

| Вопрос decision matrix | Допустимый статус |
|---|---|
| Connection/FD dominant | `not assessed`: B valid и quiet, но C invalid; сравнение B↔C запрещено |
| Parse/calc dominant | `not assessed`: B↔C не проходит reconnect quality gate |
| Mixed | `not assessed`: C invalid |
| Heavy tail r2 не воспроизведён | `supported only for valid A/B standalone`: pooled S p99 30–46 ms, S/P 0.938–1.070×, dual 0/50 |
| Background compaction/backup factor | `not tested`: r2 не фиксирует controlled background off/on comparison |

Даже при последующем успешном доступе к r2, один A→B→C repeat с выключенными
bars/persistence не докажет причину production tail, если tail в r2 не
воспроизводится. Он сможет только классифицировать controlled standalone
fan-out/process factor при прохождении validity gates.

## 5. Acceptance contract: что уже есть, чего нет

| Категория контракта | Текущий статус |
|---|---|
| Low-N standalone near-ping evidence | `supported` |
| WS connection/FD fan-out | `indeterminate`: r2 B valid/quiet, но C measurement_failed; controlled comparison отсутствует |
| Per-message parse/calc | `indeterminate`: r2 C measurement_failed, несмотря на raw coverage |
| Buffer/publish path | `indeterminate`: E0 не показал queue/backpressure, но не изолировал path |
| Background compaction/backup / host contention | `not tested` controlled off/on |
| Production profile с bars и persistence | `not accepted`: нет двух независимых 60-min окон для одного неизменяемого профиля |

## 6. Минимальная ранжированная программа до production verdict

1. **P0 — done: неизменяемая local copy r2 и анализ.** Все 31 файлов
   experiment root совпали с VPS по SHA-256; manifests, exit status, raw
   delivery/lag, ping и ресурсы проверены. Отчёт:
   [r2 results](latency-ws-fanout-three-arm-r2-results.md).
2. **P1 — повторить только `C` с тем же immutable contract.** Причина:
   `6` OKX и `94` Bybit unplanned reconnect, следовательно matrix B↔C
   невалидна. При valid repeat фиксировать только controlled standalone
   conclusion, не безопасность production `N=300`.
3. **P2 — controlled background off/on**, только после valid A/B/C: один
   неизменяемый shadow profile, randomised order, exact background manifest,
   raw delivery/lag и all quality gates. Это отделяет compaction/backup или
   иной co-resident contention от WS/process factors.
4. **P3 — acceptance конкретного production-like профиля** с bars,
   persistence и полным списком co-resident processes: два независимых
   60-min окна, pooled raw/histogram S и lag, matched ping и все budgets из
   [контракта](latency-production-acceptance-contract.md). Пока этот шаг не
   пройден, нельзя говорить «ready» ни о каком будущем production profile.

## 7. Условные implications для будущей policy

Никаких production constraints не принимаются сейчас. После P0–P3 policy
может быть сформулирована только для профиля, реально прошедшего contract:

- если valid `B` воспроизводимо хуже `A`, а `C` не хуже `B`, — лимитировать
  connections/subscriptions **per process** с доказанным FD headroom;
- если `A≈B`, а valid `C` хуже `B`, — лимитировать processed streams/message
  load **per process**, не выдавая это за FD limit;
- если оба эффекта присутствуют — зафиксировать оба guardrail;
- если A/B/C quiet, сначала проверять bars/persistence/background, а не
  утверждать, что `N=300` production-safe.

Все варианты остаются спецификацией Track (D); они не открывают Track (B) и
не разрешают production patch.
