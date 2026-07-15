#  Crypto Arbitrage Bots — OKX / Bybit / Hyperliquid

A repository dedicated to **arbitrage trading bots** across **OKX**, **Bybit**, and **Hyperliquid**.  
In addition to the core trading modules, the project includes **utility scripts** for data collection, latency testing, and WebSocket connectivity diagnostics.

---

##  Repository Structure

| File | Description |
|------|------------|
| `bybit_ws.py` | Arbitrage bot using **market orders** on the **Bybit / OKX** pair, operating during the **pre-market of perpetual futures**. |
| `config_example.json` | Example config file containing API credentials and exchange connection parameters. |
| `data_collector_okx_bybit.py` | Spread **data collection script** for the **OKX / Bybit** pair. |
| `okx_hyper.py` | Multi-pair **spread collector** for **Hyperliquid / OKX**, potentially extendable to a **scanner module for arbitrage opportunities**. |
| `ping_bybit.py`, `ping_hyper.py`, `ping_okx.py` | **Latency benchmark scripts** to measure market data response time for each exchange. |

---

##  Project Goals

- Develop **low-latency arbitrage strategies**
- Connect to exchanges via **WebSocket and REST API**
- Execute **market-order based arbitrage logic**
- Collect and analyze **orderbook spreads**
- Build infrastructure for **historical data storage & real-time monitoring**

---

##  Stack & Exchanges

- **Exchanges:** Bybit, OKX, Hyperliquid  
- **API Layers:** WebSocket
- **Utilities:** latency benchmarking, spread logging, data streaming diagnostics  

---

##  Roadmap / Planned Features

- Real-time **spread dashboard / GUI monitor**
- **Asynchronous execution engine** for order handling
- Basic **risk management module**
- **Arbitrage scanner** for opportunity detection across multiple markets

---

##  Configuration

Create your own `config.json` based on the example
