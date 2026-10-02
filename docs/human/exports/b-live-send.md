# Contour — Live send (mermaid)

Lite title: **Contour — Live send** (key `b-live-send`). Two templates; do not share one process.

Legend: solid = sync, dashed = async/event. Pipe = in-process order queue.

```mermaid
flowchart TB
  okxPub[OKX — публичные цены]
  bybitPub[Bybit — публичные цены]
  okxOrd[OKX — приём ордеров]
  bybitOrd[Bybit — приём ордеров]

  wal[Решение и отправка — 2 монеты]
  walQ[Очередь ордеров — 2 монеты]
  walJ[(Журнал ордеров — 2 монеты)]

  g22[Решение и отправка — 30 монет]
  g22Q[Очередь ордеров — 30 монет]
  g22J[(Журнал ордеров — 30 монет)]
  g22W[(Журнал провода)]

  okxPub -.->|котировки async| wal
  bybitPub -.->|котировки async| wal
  wal -.->|постановка async| walQ
  walQ -.->|отправка ордера async| okxOrd
  walQ -.->|отправка ордера async| bybitOrd
  okxOrd -.->|ACK async| wal
  bybitOrd -.->|ACK async| wal
  wal -->|журнал| walJ

  okxPub -.->|котировки async| g22
  bybitPub -.->|котировки async| g22
  g22 -.->|постановка async| g22Q
  g22Q -.->|отправка ордера async| okxOrd
  g22Q -.->|отправка ордера async| bybitOrd
  okxOrd -.->|ACK async| g22
  bybitOrd -.->|ACK async| g22
  g22 -->|журнал| g22J
  g22 -->|провод| g22W
```
