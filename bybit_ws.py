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
try:
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    with open(config_path, 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    bybit_cfg = (cfg.get('exchanges') or {}).get('bybit') or {}
    bybit_api_key = bybit_cfg.get('apiKey')
    bybit_api_secret = bybit_cfg.get('secret')
    okx_cfg = (cfg.get('exchanges') or {}).get('okx') or {}
    okx_api_key = okx_cfg.get('apiKey')
    okx_api_secret = okx_cfg.get('secret')
    okx_passphrase = okx_cfg.get('passphrase')
    if not bybit_api_key or not bybit_api_secret:
            raise RuntimeError('BYBIT credentials not set (config.json)')
    if not okx_api_key or not okx_api_secret:
            raise RuntimeError('OKX credentials not set (config.json)')
except Exception as e:
    print(e)
    pass

def _okx_sign_ws(timestamp: str, secret: str) -> str:
    prehash = f"{timestamp}GET/users/self/verify"
    digest = hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()

# Файловый обработчик
file_handler = logging.FileHandler('spread.log')
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%y-%m-%d %H:%M:%S')
file_handler.setFormatter(formatter)

# Консольный обработчик (чтобы видеть в терминале тоже)
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

# Добавляем оба обработчика
logger.addHandler(file_handler)
logger.addHandler(console_handler)

DRY_RUN = False

okx_ask_price = None
okx_ask_size = None # количество контрактов (10 XPL)
okx_bid_price = None
okx_bid_size = None # количество контрактов (10 XPL)
bybit_bid_price = None
bybit_bid_size = None # количество XPL
bybit_ask_price = None
bybit_ask_size = None # количество XPL


long_open_threshold = 0.5
long_close_threshold = 0
short_open_threshold = 0.5
short_close_threshold = 0

okx_long_position = False
okx_short_position = False
bybit_long_position = False
bybit_short_position = False

position_size = 10 #XPL
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

async def bybit_private_listener(bybit_cmd_queue):
    """Приватные данные Bybit (ордеры)"""
    global bybit_bid_price, bybit_ask_price, bybit_bid_size, bybit_ask_size, bybit_long_position, bybit_short_position, spread_long, spread_short
    while True:
        try:
            async with websockets.connect("wss://stream.bybit.com/v5/trade") as ws:
                expires = int(time.time() * 1000) + 10000
                signature = hmac.new(
                    bybit_api_secret.encode(), 
                    f"GET/realtime{expires}".encode(), 
                    hashlib.sha256
                ).hexdigest()
                
                auth_msg = {
                    "op": "auth",
                    "args": [bybit_api_key, expires, signature]
                }
                
                await ws.send(json.dumps(auth_msg))
                logger.info("Bybit: auth sent")

                async def sender():
                    while True:
                        order_msg = await bybit_cmd_queue.get()
                        if DRY_RUN:
                            logger.info("Bybit DRY_RUN send: %s", order_msg)
                        else:
                            await ws.send(json.dumps(order_msg))
                            logger.info("Bybit order sent: %s", order_msg)
                        await asyncio.sleep(0)

                asyncio.create_task(sender())

                async for message in ws:
                    logger.debug("Bybit msg: %s", message)
        except Exception as e:
            logger.error(f"Bybit private error: {e}")
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

async def okx_private_listener(okx_cmd_queue):
    """Приватные данные OKX (ордеры)"""
    global okx_bid_price, okx_ask_price, okx_bid_size, okx_ask_size, okx_long_position, okx_short_position, spread_long, spread_short
    while True:
        try:
            async with websockets.connect("wss://ws.okx.com:8443/ws/v5/private") as ws:
                timestamp = str(time.time())
                sign = _okx_sign_ws(timestamp, okx_api_secret)
                
                auth_msg = {
                    "op": "login",
                    "args": [
                        {
                            "apiKey": okx_api_key,
                            "passphrase": okx_passphrase,
                            "timestamp": timestamp,
                            "sign": sign,
                        }
                    ]
                }
                await ws.send(json.dumps(auth_msg))
                logger.info("OKX: auth sent")

                async def sender():
                    while True:
                        order_msg = await okx_cmd_queue.get()
                        if DRY_RUN:
                            logger.info("OKX DRY_RUN send: %s", order_msg)
                        else:
                            await ws.send(json.dumps(order_msg))
                            logger.info("OKX order sent: %s", order_msg)
                        await asyncio.sleep(0)

                asyncio.create_task(sender())

                async for message in ws:
                    logger.debug("OKX msg: %s", message)
        except Exception as e:
            logger.error(f"OKX private error: {e}")
            await asyncio.sleep(10)


async def trade_manager(okx_cmd_queue, bybit_cmd_queue):
    global bybit_long_position, okx_short_position, bybit_short_position, okx_long_position, long_open_threshold, long_close_threshold, short_open_threshold, short_close_threshold, position_size, spread_long, spread_short, bybit_bid_price, okx_ask_price, okx_bid_price, bybit_ask_price
    spread_long_ma = [0,0,0,0,0]
    spread_short_ma = [0,0,0,0,0]
    k = 0.7

    await asyncio.sleep(3)
    while True:
        if bybit_bid_price and okx_ask_price and okx_bid_price and bybit_ask_price:
                spread_long = (bybit_bid_price - okx_ask_price) * 100 / bybit_bid_price
                spread_short = (okx_bid_price - bybit_ask_price) * 100 / okx_bid_price
                spread_long_ma.append(spread_long)
                spread_short_ma.append(spread_short)
                long_ma = (spread_long_ma[-1] + spread_long_ma[-2] + spread_long_ma[-3] + spread_long_ma[-4] + spread_long_ma[-5])/5
                short_ma = (spread_short_ma[-1] + spread_short_ma[-2] + spread_short_ma[-3] + spread_short_ma[-4] + spread_short_ma[-5])/5
                logger.info(f"Spread long: {spread_long}   long ma:{long_ma}     Spread short: {spread_short}   short ma = {short_ma}")

                #okx short    bybit long     open
                if spread_short > short_open_threshold and short_ma > k * short_open_threshold and not bybit_long_position and not okx_short_position and bybit_ask_size > position_size and okx_bid_size > position_size / 10:
                    cl_id = f"okx{int(time.time_ns())}"[:32]  

                    order_args = {
                        "instId": "XPL-USDT-SWAP",
                        "tdMode": 'cross',
                        "side": "sell",
                        "ordType": "market",
                        "sz": str(position_size / 10),
                        "clOrdId": cl_id,
                        "posSide": "short"
                    }
                    request_id = str(int(time.time() * 1000))  
                    okx_order = {"id": request_id, "op": "order", "args": [order_args]}

                    args = {
                        "symbol": "XPLUSDT",
                        "side": "Buy",
                        "orderType": "Market",
                        "qty": str(position_size),
                        "category": "linear",
                        "timeInForce": "PostOnly",
                        "positionIdx": 0
                    }
                    Timestamp = str(int(time.time())*1000)
                    bybit_order = {
                        "op": "order.create",
                        "header":{
                            "X-BAPI-TIMESTAMP": Timestamp
                        },
                        "args": [args]
                    }
                    await bybit_cmd_queue.put(bybit_order)
                    await okx_cmd_queue.put(okx_order)
                    bybit_long_position = True
                    okx_short_position = True
                    logger.info("TradeManager: opened okx short and bybit long positions")
                #okx short    bybit long     close
                elif spread_long > short_close_threshold and long_ma > k * short_close_threshold and bybit_long_position and okx_short_position and bybit_bid_size > position_size and okx_ask_size > position_size / 10:
                    cl_id = f"okx{int(time.time_ns())}"[:32]  

                    order_args = {
                        "instId": "XPL-USDT-SWAP",
                        "tdMode": 'cross',
                        "side": "buy",
                        "ordType": "market",
                        "sz": str(position_size / 10),
                        "clOrdId": cl_id,
                        "posSide": "short"
                    }
                    request_id = str(int(time.time() * 1000))  
                    okx_order = {"id": request_id, "op": "order", "args": [order_args]}

                    args = {
                        "symbol": "XPLUSDT",
                        "side": "Sell",
                        "orderType": "Market",
                        "qty": str(position_size),
                        "category": "linear",
                        "timeInForce": "PostOnly",
                        "positionIdx": 0
                    }
                    Timestamp = str(int(time.time())*1000)
                    bybit_order = {
                        "op": "order.create",
                        "header":{
                            "X-BAPI-TIMESTAMP": Timestamp
                        },
                        "args": [args]
                    }
                    await bybit_cmd_queue.put(bybit_order)
                    await okx_cmd_queue.put(okx_order)
                    bybit_long_position = False
                    okx_short_position = False
                    logger.info("TradeManager: close okx short and bybit long positions")
                    
                    #|okx buy     bybit short     open|

                elif spread_long > long_open_threshold and long_ma > k * long_open_threshold and not bybit_short_position and not okx_long_position and bybit_bid_size > position_size and okx_ask_size > position_size / 10:
                    cl_id = f"okx{int(time.time_ns())}"[:32]  

                    order_args = {
                        "instId": "XPL-USDT-SWAP",
                        "tdMode": 'cross',
                        "side": "buy",
                        "ordType": "market",
                        "sz": str(position_size / 10),
                        "clOrdId": cl_id,
                        "posSide": "long"
                    }
                    request_id = str(int(time.time() * 1000))  
                    okx_order = {"id": request_id, "op": "order", "args": [order_args]}

                    args = {
                        "symbol": "XPLUSDT",
                        "side": "Sell",
                        "orderType": "Market",
                        "qty": str(position_size),
                        "category": "linear",
                        "timeInForce": "PostOnly",
                        "positionIdx": 0
                    }
                    Timestamp = str(int(time.time())*1000)
                    bybit_order = {
                        "op": "order.create",
                        "header":{
                            "X-BAPI-TIMESTAMP": Timestamp
                        },
                        "args": [args]
                    }
                    await bybit_cmd_queue.put(bybit_order)
                    await okx_cmd_queue.put(okx_order)
                    bybit_short_position = True
                    okx_long_position = True
                    logger.info("TradeManager: opened okx long and bybit short positions")
                #okx long    bybit short     close
                elif spread_short > long_close_threshold and short_ma > k * long_close_threshold and bybit_short_position and okx_long_position and bybit_ask_size > position_size and okx_bid_size > position_size / 10:
                    cl_id = f"okx{int(time.time_ns())}"[:32]  

                    order_args = {
                        "instId": "XPL-USDT-SWAP",
                        "tdMode": 'cross',
                        "side": "sell",
                        "ordType": "market",
                        "sz": str(position_size / 10),
                        "clOrdId": cl_id,
                        "posSide": "long"
                    }
                    request_id = str(int(time.time() * 1000))  
                    okx_order = {"id": request_id, "op": "order", "args": [order_args]}

                    args = {
                        "symbol": "XPLUSDT",
                        "side": "Buy",
                        "orderType": "Market",
                        "qty": str(position_size),
                        "category": "linear",
                        "timeInForce": "PostOnly",
                        "positionIdx": 0
                    }
                    Timestamp = str(int(time.time())*1000)
                    bybit_order = {
                        "op": "order.create",
                        "header":{
                            "X-BAPI-TIMESTAMP": Timestamp
                        },
                        "args": [args]
                    }
                    await bybit_cmd_queue.put(bybit_order)
                    await okx_cmd_queue.put(okx_order)
                    bybit_short_position = False
                    okx_long_position = False
                    logger.info("TradeManager: close okx long and bybit short positions")
                await asyncio.sleep(0.1)
        else:
                await asyncio.sleep(1)
                continue
    
async def main():
    symbol = 'W-USDT'
    bybit_cmd_queue = asyncio.Queue()
    okx_cmd_queue = asyncio.Queue()
    await asyncio.gather(
        okx_listener(symbol),
        #okx_private_listener(okx_cmd_queue),
        #bybit_private_listener(bybit_cmd_queue),
        bybit_listener(symbol),
        #trade_manager(okx_cmd_queue, bybit_cmd_queue)
    )

if __name__ == "__main__":
    try:
        import uvloop 
        uvloop.install()
    except Exception:
        pass
    asyncio.run(main())