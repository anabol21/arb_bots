# Индекс `docs/`

Короткий указатель. Если файл спорит с корневым [`architecture.md`](../architecture.md), верен корень. Правила агентов: [`../.cursor/rules/`](../.cursor/rules/).

## Канон

Текущий смысл контуров, гиров и контрактов. Не датированные отчёты.

| Файл | Зачем |
|------|--------|
| [`../architecture.md`](../architecture.md) | Топология D/M/B, журналы, гейты live send, глоссарий |
| [`../README.md`](../README.md) | Вход: три контура и что не смешивать |
| [`../AGENTS.md`](../AGENTS.md) | Политика агентов, live safety, что не останавливать |
| [`strategy-gears.md`](strategy-gears.md) | Лестница гиров M (симулятор, не бот) |
| [`storage-contract.md`](storage-contract.md) | Контракт хранения D |
| [`b-v0-block-diagram.md`](b-v0-block-diagram.md) | Черновик блок-схемы склейки |
| [`data-format-model.md`](data-format-model.md) | Запрос полей со стороны модели, не спецификация сборщика |

[`architecture.md`](architecture.md) в этой папке — однострочный указатель на корень, не второй канон.

## Runbooks и текущий ops

Как устроено сейчас и куда смотреть на VPS. Это не разрешение слать заявки и не разрешение останавливать юниты.

| Файл | Зачем |
|------|--------|
| [`would-send-prod-status.md`](would-send-prod-status.md) | Прод would_send: `spread-bbot-would-send-prod`, stub, без live orders |
| [`b-private-status.md`](b-private-status.md) | Статус адаптера B-private для оркестраторов (не «бот готов») |
| [`b-private-trivial-dual-leg.md`](b-private-trivial-dual-leg.md) | Контур B: очередь → `ws.send` |
| [`b-private-warm-single-loop.md`](b-private-warm-single-loop.md) | Тёплые private+trade сокеты |
| [`gear22-live-canary.md`](gear22-live-canary.md) | Live canary гира 2.2 |
| [`d-track-ready-for-b.md`](d-track-ready-for-b.md) | Сводка: D готов как основа склейки |
| [`vps-runbook.md`](vps-runbook.md) | Операции на VPS |
| [`vps-validation-checklist.md`](vps-validation-checklist.md) | Чеклист проверки |
| [`compaction-backup-runbook.md`](compaction-backup-runbook.md) | Уплотнение и выгрузка D |
| [`hot-add-new-coins.md`](hot-add-new-coins.md) | HOT_ADD |
| [`prod-unit-snippets.md`](prod-unit-snippets.md) | Фрагменты юнитов |
| [`local-lean-collector.md`](local-lean-collector.md) | Локальный lean, не prod |
| [`NOW.md`](NOW.md) | Утренний снимок, ожидающий заполнения головой |

Живая отправка только при `VENUE=live` и `LIVE_ORDERS=1` (и `BBOT_BROKER=private_live`). Без явной просьбы не останавливать `spread-collector-next`, `spread-bbot-would-send-prod`, `spread-bbot-gear22-live-canary`.

## Датированные отчёты

Имя с датой (`*-20260803*`, `*-20260810*`, `*-20260905*` и похожие) — снимок окна, не текущая топология. Туда же относятся приёмки soak/canary, forensics отпуска и окна задержки. Читать как доказательство того дня.

Отдельно помечены баннером **HISTORY** (текст сохранён, сверху предупреждение):

- [`program-roadmap.md`](program-roadmap.md) — журнал GD; §5–§6 устарели как «текущая работа»
- [`b-bot-starter-prompt.md`](b-bot-starter-prompt.md) — промпт stub-чата, не prod would_send
- [`b-private-secrets-manifest.md`](b-private-secrets-manifest.md) — этап 1 без send; значений ключей нет и не добавлять
- [`hl-v2-canary.md`](hl-v2-canary.md) — рамка «только `main_hl`» до stitch 2026-10-01
