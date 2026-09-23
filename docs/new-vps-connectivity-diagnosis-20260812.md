# Диагностика доступности NEW VPS — 2026-08-12

## Scope и границы

- **Трек / блок:** Track 1, доступность VPS по SSH для read-only retrieval.
- **Проверенный endpoint:** `root@38.180.94.108:22`.
- Проверка не меняла сеть, SSH/firewall, credentials, units, процессы, данные
  или VPS controls. Track B остаётся закрыт.

## Канонический endpoint

`docs/vps-runbook.md` и `docs/program-roadmap.md` однозначно называют
`root@38.180.94.108` current production VPS после миграции `2026-08-10`.
Предыдущий `38.244.198.42` отмечен как historical/retired. В локальном
`~/.ssh/config` есть только общие keepalive-настройки, без alias, alternate
port или jump host; никакой документированной альтернативы endpoint не найдено.

## Наблюдения текущей read-only проверки

| Проверка | Результат | Вывод о стадии отказа |
|---|---|---|
| Name resolution | Неприменимо: используется literal IPv4 | DNS не участвует |
| `route -n get 38.180.94.108` | Маршрут через local gateway `192.168.1.1`, интерфейс `en0` | Локальный маршрут существует |
| ICMP, 2 probes | 2/2 replies, 0% loss; RTT `316.818–350.778 ms` | IP достижим, но RTT высокий |
| TCP/22, один 5-s probe | Succeeded | Порт 22 не был закрыт/отклонён в момент проверки |
| SSH, BatchMode, 5-s connect timeout, одна попытка, command `true` | Exit `0`; TCP → key exchange → known-host match → public-key authentication → command exit | Ни адрес, ни SSH authentication не блокируют текущую проверку |

## Исторические признаки

- Локальная история ранее фиксирует успешный non-interactive SSH с `OK` и
  hostname `a845945761.local`.
- Есть разрывы **после уже установленной SSH-сессии**:
  `Connection reset by peer` и `Operation timed out`. Это не соответствует
  ошибке DNS, неверному адресу, закрытому TCP/22 или rejected key.
- Read-only retrieval r2 от `2026-08-12T13:23:55Z` не завершился за
  ~42 s; terminal recorded no SSH diagnostic text, поэтому он сам по себе не
  локализует отказ до TCP, в auth или в remote command.
- Предыдущие документы сообщали intermittent SSH transport/session issue,
  при этом наблюдения canary до outage не показывали OOM, disk pressure или
  collector restarts. Это исключает только подтверждённый ресурсный инцидент
  в том окне, но не доказывает состояние VPS в момент timeout.

## Классификация

**`inconclusive`**.

Сейчас endpoint полностью доступен и public-key auth проходит, поэтому
`likely wrong address`, `authentication-only` и постоянный local-route/TCP
failure не поддержаны evidence. Исторические session reset/timeout совместимы
как с временной проблемой remote host/provider path, так и с локальным/межсетевым
путём. Однократный timeout retrieval без stage-specific stderr не позволяет
разделить эти варианты.

## Рекомендуемое не-мутирующее действие

Если повторится, сохранить **один** short, non-interactive diagnostic с
`BatchMode=yes`, `ConnectTimeout=5`, `ConnectionAttempts=1`, `-vv` и
без remote command (либо `true`), вместе с UTC timestamp и результатом
`nc -vz` на `:22`. Это зафиксирует границу `TCP`/`KEX`/`auth`/`session`;
только если TCP handshake снова не состоится, передать provider/ops точное
время, source network и этот результат для read-only provider-status проверки.
