import websocket
import json
import time
from datetime import datetime

def on_message(ws, message):
    data = json.loads(message)
    if "data" in data and "ts" in data["data"][0]:
        ts_exchange = int(data["data"][0]['ts'])
        ts_local = int(time.time() * 1000)

        latency = ts_local - ts_exchange
        print(f"Market data latency: {latency} ms")

def on_open(ws):
    sub_msg = {
        "op": "subscribe",
        "args": [
            {
            "channel": "books5",
            "instId": "BTC-USDT-SWAP"
            }
        ]
    }
    ws.send(json.dumps(sub_msg))

url = "wss://ws.okx.com:8443/ws/v5/public"
ws = websocket.WebSocketApp(url, on_message=on_message, on_open=on_open)
ws.run_forever()
