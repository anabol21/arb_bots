import websocket
import json
import time

def on_message(ws, message):
    data = json.loads(message)

    if "topic" in data and data["topic"].startswith("orderbook.1."):
        ts_exchange = int(data["ts"])          # market-data timestamp
        cts_exchange = int(data.get("cts", ts_exchange))  # matching engine ts, если есть
        ts_local = time.time_ns() // 1_000_000

        age_ts = ts_local - ts_exchange
        age_cts = ts_local - cts_exchange

        print(f"Bybit orderbook age ts: {age_ts} ms, cts: {age_cts} ms")

def on_open(ws):
    sub_msg = {
        "op": "subscribe",
        "args": ["orderbook.1.XRPUSDT"]  # или BTCUSDT / ETHUSDT
    }
    ws.send(json.dumps(sub_msg))

url = "wss://stream.bybit.com/v5/public/linear"
ws = websocket.WebSocketApp(url, on_message=on_message, on_open=on_open)
ws.run_forever()