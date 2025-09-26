import websocket
import json
import time
from datetime import datetime

def on_message(ws, message):
    data = json.loads(message)
    if "data" in data and "time" in data['data']:
        ts_exchange = data["data"]["time"]
        ts_local = int(time.time() * 1000)

        latency = ts_local - ts_exchange
        print(f"Market data latency: {latency} ms")

def on_open(ws):
    sub_msg = {
        "method": "subscribe",
        "subscription": {
            "type": "bbo",
            "coin": "BTC"
        }
    }
    ws.send(json.dumps(sub_msg))

url = "wss://api.hyperliquid.xyz/ws"
ws = websocket.WebSocketApp(url, on_message=on_message, on_open=on_open)
ws.run_forever()