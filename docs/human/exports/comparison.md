# Contour comparison (mermaid)

Not a mixed-topology container view. Boxes are the six confirmed views. Open one Level 2 diagram at a time.

| View | contour-id | status |
|---|---|---|
| Contour — Live prices | d-live | live |
| Contour — Canary prices | d-hotadd-canary | canary |
| Contour — Stub B | b-stub | legacy |
| Contour — Canary B | b-theta-would-send | canary |
| Contour — Live send | b-live-send | canary |
| Contour — Simulator | m-sim | sim |

```mermaid
flowchart TB
  dLive["Contour — Live prices\nсбор цен · журнал тиков\nstatus live"]
  dCan["Contour — Canary prices\nизолированный сбор цен\nstatus canary · PR #53"]
  bStub["Contour — Stub B\nрешение без отправки\nstatus legacy"]
  bCan["Contour — Canary B\nрешение 1 Гц без отправки\nstatus canary · unit not in git"]
  bSend["Contour — Live send\nрешение + отправка ордера\nstatus canary · templates"]
  mSim["Contour — Simulator\nпрогон истории\nstatus sim · no systemd"]
```

Do not overlay Live prices and Canary prices. Human **Canary B** ≠ architecture.md “Contour B” (that name is the live send path).
