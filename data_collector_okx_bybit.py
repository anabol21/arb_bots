import asyncio
import websockets
import json
import time
import hmac
import logging
import hashlib
import uuid
import base64
import os 

logger = logging.getLogger('spread_monitor')
logger.setLevel(logging.INFO)

file_handler = logging.FileHandler('spread.log')
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%y-%m-%d %H:%M:%S')
file_handler.setFormatter(formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

# Добавляем оба обработчика
logger.addHandler(file_handler)
logger.addHandler(console_handler)

okx_ask_price = None
okx_ask_size = None 
okx_bid_price = None
okx_bid_size = None 
bybit_bid_price = None
bybit_bid_size = None
bybit_ask_price = None
bybit_ask_size = None 

spread_long = 0 #okx buy  bybit sell
spread_short = 0 #okx sell  bybit buy

async def bybit_listener(symbol):
    global bybit_bid_price, bybit_ask_price, bybit_bid_size, bybit_ask_size, spread_long, spread_short
    while True:
        try:
            async with websockets.connect("wss://stream.bybit.com/v5/public/linear") as ws:
                await ws.send(json.dumps({
                            "op": "subscribe",
                            "args": [f"tickers.{symbol.replace('-', '')}"]
                        }))
                async for message in ws:
                    data = json.loads(message)
                    if 'data' in data and 'bid1Price' in data['data']:
                        bybit_bid_price = float(data['data']['bid1Price'])
                        bybit_bid_size = float(data['data']['bid1Size'])
                    if 'data' in data and 'ask1Price' in data['data']:
                        bybit_ask_price = float(data['data']['ask1Price'])
                        bybit_ask_size = float(data['data']['ask1Size'])
                    if okx_bid_price and okx_bid_size and okx_ask_price and okx_ask_size:
                        spread_long = (bybit_bid_price - okx_ask_price) * 100 / bybit_bid_price
                        spread_short = (okx_bid_price - bybit_ask_price) * 100 / okx_bid_price
                        logger.info(f"Bybit update: Spread_long:{spread_long}, Spread short: {spread_short}, TS:{time.time()}, Bybit bid:{bybit_bid_price}, Bybit ask:{bybit_ask_price}")
        except Exception as e:
            logger.error(e)
            await asyncio.sleep(10)
            
async def okx_listener(symbol):
    global okx_bid_price, okx_bid_size, okx_ask_price, okx_ask_size, spread_long, spread_short
    while True:
        try:
            async with websockets.connect("wss://ws.okx.com:8443/ws/v5/public") as ws:
                await ws.send(json.dumps({
                            "op": "subscribe",
                            "args": [{"channel": "tickers", "instId": f"{symbol}-SWAP"}]
                        }))
                async for message in ws:
                    data = json.loads(message)
                    if 'data' in data and 'bidPx' in data['data'][0]:
                        okx_bid_price = float(data['data'][0].get("bidPx"))
                        okx_bid_size = float(data['data'][0].get("bidSz"))
                    if 'data' in data and 'askPx' in data['data'][0]:
                        okx_ask_price = float(data['data'][0].get("askPx"))
                        okx_ask_size = float(data['data'][0].get("askSz"))
                    if bybit_bid_price and bybit_bid_size and bybit_ask_price and bybit_ask_size and okx_bid_price and okx_bid_size and okx_ask_price and okx_ask_size:
                        spread_long = (bybit_bid_price - okx_ask_price) * 100 / bybit_bid_price
                        spread_short = (okx_bid_price - bybit_ask_price) * 100 / okx_bid_price
                        logger.info(f"OKX update: Spread_long:{spread_long}, Spread short: {spread_short}, TS:{time.time()}, OKX bid:{okx_bid_price}, OKX ask:{okx_ask_price}")
        except Exception as e:
            logger.error(e)
            await asyncio.sleep(10)
    
async def main():
    symbol = 'XPL-USDT'
    bybit_cmd_queue = asyncio.Queue()
    okx_cmd_queue = asyncio.Queue()
    await asyncio.gather(
        okx_listener(symbol),
        bybit_listener(symbol)
    )

if __name__ == "__main__":
    try:
        import uvloop 
        uvloop.install()
    except Exception:
        pass
    asyncio.run(main())