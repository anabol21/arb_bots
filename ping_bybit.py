import websocket
import json
import time
from datetime import datetime

def on_message(ws, message):
    data = json.loads(message)
    if "topic" in data and "tickers" in data["topic"]:
        ts_exchange = data["ts"]
        ts_local = int(time.time() * 1000)

        latency = ts_local - ts_exchange
        print(f"Market data latency: {latency} ms")

def on_open(ws):
    sub_msg = {
        "op": "subscribe",
        "args":["tickers.BTCUSDT"]
    }
    ws.send(json.dumps(sub_msg))

url = "wss://stream.bybit.com/v5/public/linear"
ws = websocket.WebSocketApp(url, on_message=on_message, on_open=on_open)
ws.run_forever()
