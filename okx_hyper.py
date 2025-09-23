import asyncio
import websockets
import json
import time
import hmac
import logging
import hashlib
import uuid
import base64
import ccxt.pro as ccxtpro
import os 

logger = logging.getLogger('spread_monitor')
logger.setLevel(logging.INFO)

file_handler = logging.FileHandler('screaner.log')
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%y-%m-%d %H:%M:%S')
file_handler.setFormatter(formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

logger.addHandler(file_handler)
logger.addHandler(console_handler)

spread_data = {}
price_data = {
                'hyper': {},
                'okx': {}
            }
coins = ['HMSTR', 'TRUMP', 'WLFI', 'FARTCOIN', 'IP', 'LINEA', 'MERL', 'DOOD', 'LAUNCHCOIN', 'W']

async def hyper_listener():
    global price_data, spread_data
    while True:
        try:
            async with websockets.connect("wss://api.hyperliquid.xyz/ws") as ws:
                for coin in coins:
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": {
                            "type": "bbo",
                            "coin": coin
                        }
                    }))
                async for message in ws:
                    data = json.loads(message)
                    if 'data' in data and data['channel'] == 'bbo':
                        coin = data['data']['coin']
                        bid_px = float(data['data']['bbo'][0]['px'])
                        bid_sz = float(data['data']['bbo'][0]['sz'])
                        ask_px = float(data['data']['bbo'][1]['px'])
                        ask_sz = float(data['data']['bbo'][1]['sz'])
                        price_data['hyper'][coin] = {
                            "bid_px": bid_px,
                            "bid_sz": bid_sz,
                            "ask_px": ask_px,
                            "ask_sz": ask_sz
                        }
                        if coin in price_data['okx'] and price_data['okx'][coin]:
                            spread_data[coin] = {
                                "spread_long": (price_data['okx'][coin]['bid_px'] - price_data['hyper'][coin]['ask_px'])*100/ price_data['okx'][coin]['bid_px'],
                                "spread_short": (price_data['hyper'][coin]['bid_px'] - price_data['okx'][coin]['ask_px'])*100/ price_data['hyper'][coin]['bid_px']   
                            }
                            logger.info(f"Hyper update:{coin} {spread_data[coin]}, okx bid:{price_data['okx'][coin]['bid_px']}, okx ask:{price_data['okx'][coin]['ask_px']}, hyper bid:{price_data['hyper'][coin]['bid_px']}, hyper ask:{price_data['okx'][coin]['ask_px']}")
        except Exception as e:
            logger.error(e)
            await asyncio.sleep(10)
 
def arguments():
    args = [{}]
    for coin in coins:
        args.append({"channel":"tickers", "instId": coin+"-USDT-SWAP"}) 
    return args

async def okx_listener():
    global price_data, spread_data
    while True:
        try:
            async with websockets.connect("wss://ws.okx.com:8443/ws/v5/public") as ws:
                await ws.send(json.dumps({
                            "op": "subscribe",
                            "args": arguments()
                        }))
                async for message in ws:
                    data = json.loads(message)
                    if 'data' in data and 'arg' in data and data['arg']['channel'] == 'tickers':
                        inst_id = data['arg']['instId']
                        coin = inst_id.replace("-USDT-SWAP", "")  
                        ticker_data = data['data'][0]
                        price_data['okx'][coin] = {
                            "bid_px": float(ticker_data.get("bidPx", 0)),
                            "bid_sz": float(ticker_data.get("bidSz", 0)),
                            "ask_px": float(ticker_data.get("askPx", 0)),
                            "ask_sz": float(ticker_data.get("askSz", 0))
                        }
                        
                        if coin in price_data['hyper'] and price_data['hyper'][coin]:
                            spread_data[coin] = {
                                "spread_long": (price_data['okx'][coin]['bid_px'] - price_data['hyper'][coin]['ask_px'])*100/ price_data['okx'][coin]['bid_px'],
                                "spread_short": (price_data['hyper'][coin]['bid_px'] - price_data['okx'][coin]['ask_px'])*100/ price_data['hyper'][coin]['bid_px']   
                            }
                            logger.info(f"OKX update:{coin} {spread_data[coin]}, okx bid:{price_data['okx'][coin]['bid_px']}, okx ask:{price_data['okx'][coin]['ask_px']}, hyper bid:{price_data['hyper'][coin]['bid_px']}, hyper ask:{price_data['okx'][coin]['ask_px']}")
        except Exception as e:
            logger.error(e)
            await asyncio.sleep(10)

async def main():
    hyper_cmd_queue = asyncio.Queue()
    okx_cmd_queue = asyncio.Queue()
    await asyncio.gather(
        okx_listener(),
        hyper_listener()
    )

if __name__ == "__main__":
    try:
        import uvloop 
        uvloop.install()
    except Exception:
        pass
    asyncio.run(main())