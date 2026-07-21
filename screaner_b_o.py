import asyncio
import websockets
import json
import time
import logging
import csv
import pickle
from pathlib import Path


runtime_logger = logging.getLogger("runtime")
runtime_logger.setLevel(logging.INFO)

if not runtime_logger.handlers:
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%y-%m-%d %H:%M:%S"
    )

    runtime_file_handler = logging.FileHandler("runtime.log")
    runtime_file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    runtime_logger.addHandler(runtime_file_handler)
    runtime_logger.addHandler(console_handler)


UNIVERSE_PATH = "/Users/mishatrubik/Desktop/spread/output/bybit_okx_universe.csv"
ROW_START = 0
ROW_END = 337

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

PERSIST_EVERY_N = 10000
opportunities_buffer = []
saved_records_count = 0


def load_pairs_from_csv(path, row_start=0, row_end=10):
    pairs = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    subset = rows[row_start:row_end]

    for row in subset:
        base_coin = row["base_coin"].strip()
        okx_symbol = row["okx_symbol"].strip()
        bybit_symbol = row["bybit_symbol"].strip()

        pairs.append({
            "base_coin": base_coin,
            "okx_symbol": okx_symbol,
            "bybit_symbol": bybit_symbol,
        })

    return pairs


pairs = load_pairs_from_csv(UNIVERSE_PATH, ROW_START, ROW_END)

quotes = {
    row["base_coin"]: {
        "okx_symbol": row["okx_symbol"],
        "bybit_symbol": row["bybit_symbol"],
        "okx": {
            "bid_price": None,
            "bid_size": None,
            "ask_price": None,
            "ask_size": None,
            "ts_exchange": None,
            "local_recv_ts_ms": None,
            "delivery_latency_ms": None,
        },
        "bybit": {
            "bid_price": None,
            "bid_size": None,
            "ask_price": None,
            "ask_size": None,
            "ts_exchange": None,
            "cts_exchange": None,
            "local_recv_ts_ms": None,
            "delivery_latency_ms": None,
        },
    }
    for row in pairs
}


def persist_opportunities():
    global saved_records_count

    if not opportunities_buffer:
        return

    ts = int(time.time())
    file_path = OUTPUT_DIR / f"spreads_2.pkl"

    with file_path.open("ab") as f:
        pickle.dump(opportunities_buffer, f, protocol=pickle.HIGHEST_PROTOCOL)

    saved_now = len(opportunities_buffer)
    saved_records_count += saved_now
    opportunities_buffer.clear()

    runtime_logger.info(
        f"Persisted spread records: {saved_now} | total_saved={saved_records_count} | file={file_path}"
    )


def write_spread_record(
    base_coin,
    trigger_exchange,
    spread_long,
    spread_short,
    okx_latency_ms,
    bybit_latency_ms,
    calc_local_ts_ms,
    okx_local_recv_ts_ms,
    okx_ts_ms,
    bybit_local_recv_ts_ms,
    bybit_ts_ms,
):
    record = {
        "base_coin": base_coin,
        "trigger": trigger_exchange,
        "spread_long": spread_long,
        "spread_short": spread_short,
        "okx_latency_ms": okx_latency_ms,
        "bybit_latency_ms": bybit_latency_ms,
        "calc_local_ts_ms": calc_local_ts_ms,
        "okx_local_recv_ts_ms": okx_local_recv_ts_ms,
        "okx_ts_ms": okx_ts_ms,
        "bybit_local_recv_ts_ms": bybit_local_recv_ts_ms,
        "bybit_ts_ms": bybit_ts_ms,
    }

    opportunities_buffer.append(record)

    if len(opportunities_buffer) >= PERSIST_EVERY_N:
        persist_opportunities()


def calc_and_store_spread(base_coin, trigger_exchange):
    state = quotes[base_coin]
    okx = state["okx"]
    bybit = state["bybit"]

    if not (
        okx["bid_price"] is not None and okx["ask_price"] is not None and
        bybit["bid_price"] is not None and bybit["ask_price"] is not None and
        okx["ts_exchange"] is not None and bybit["ts_exchange"] is not None and
        okx["local_recv_ts_ms"] is not None and bybit["local_recv_ts_ms"] is not None and
        okx["delivery_latency_ms"] is not None and bybit["delivery_latency_ms"] is not None
    ):
        return

    calc_local_ts_ms = time.time() * 1000

    spread_long = (bybit["bid_price"] - okx["ask_price"]) * 100 / bybit["bid_price"]
    spread_short = (okx["bid_price"] - bybit["ask_price"]) * 100 / okx["bid_price"]

    write_spread_record(
        base_coin=base_coin,
        trigger_exchange=trigger_exchange,
        spread_long=spread_long,
        spread_short=spread_short,
        okx_latency_ms=okx["delivery_latency_ms"],
        bybit_latency_ms=bybit["delivery_latency_ms"],
        calc_local_ts_ms=calc_local_ts_ms,
        okx_local_recv_ts_ms=okx["local_recv_ts_ms"],
        okx_ts_ms=float(okx["ts_exchange"]),
        bybit_local_recv_ts_ms=bybit["local_recv_ts_ms"],
        bybit_ts_ms=float(bybit["ts_exchange"]),
    )


async def bybit_listener(base_coin, bybit_symbol):
    ws = None

    while True:
        try:
            async with websockets.connect(
                "wss://stream.bybit.com/v5/public/linear",
                ping_interval=20,
                ping_timeout=20,
                close_timeout=2,
            ) as ws:
                sub_msg = {
                    "op": "subscribe",
                    "args": [f"orderbook.1.{bybit_symbol}"]
                }
                await ws.send(json.dumps(sub_msg))
                runtime_logger.info(f"{base_coin} | Bybit subscribed: orderbook.1.{bybit_symbol}")

                async for message in ws:
                    local_recv_ts_ms = time.time() * 1000
                    data = json.loads(message)

                    if "data" not in data:
                        continue

                    payload = data["data"]
                    if not isinstance(payload, dict):
                        continue

                    bids = payload.get("b", [])
                    asks = payload.get("a", [])

                    if bids and len(bids[0]) >= 2:
                        quotes[base_coin]["bybit"]["bid_price"] = float(bids[0][0])
                        quotes[base_coin]["bybit"]["bid_size"] = float(bids[0][1])

                    if asks and len(asks[0]) >= 2:
                        quotes[base_coin]["bybit"]["ask_price"] = float(asks[0][0])
                        quotes[base_coin]["bybit"]["ask_size"] = float(asks[0][1])

                    exchange_ts_ms = data.get("ts")
                    cts_exchange_ms = data.get("cts")

                    quotes[base_coin]["bybit"]["ts_exchange"] = float(exchange_ts_ms) if exchange_ts_ms is not None else None
                    quotes[base_coin]["bybit"]["cts_exchange"] = float(cts_exchange_ms) if cts_exchange_ms is not None else None
                    quotes[base_coin]["bybit"]["local_recv_ts_ms"] = local_recv_ts_ms

                    if exchange_ts_ms is not None:
                        quotes[base_coin]["bybit"]["delivery_latency_ms"] = local_recv_ts_ms - float(exchange_ts_ms)

                    calc_and_store_spread(base_coin, "bybit")

        except asyncio.CancelledError:
            runtime_logger.info(f"{base_coin} | Bybit listener cancelled")
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            raise

        except Exception as e:
            runtime_logger.error(f"{base_coin} | Bybit error: {e}")
            await asyncio.sleep(10)


async def okx_listener(base_coin, okx_symbol):
    ws = None

    while True:
        try:
            async with websockets.connect(
                "wss://ws.okx.com:8443/ws/v5/public",
                ping_interval=20,
                ping_timeout=20,
                close_timeout=2,
            ) as ws:
                sub_msg = {
                    "op": "subscribe",
                    "args": [{"channel": "books5", "instId": okx_symbol}]
                }
                await ws.send(json.dumps(sub_msg))
                runtime_logger.info(f"{base_coin} | OKX subscribed: books5 {okx_symbol}")

                async for message in ws:
                    local_recv_ts_ms = time.time() * 1000
                    data = json.loads(message)

                    if "data" not in data or not data["data"]:
                        continue

                    payload = data["data"][0]
                    bids = payload.get("bids", [])
                    asks = payload.get("asks", [])

                    if bids and len(bids[0]) >= 2:
                        quotes[base_coin]["okx"]["bid_price"] = float(bids[0][0])
                        quotes[base_coin]["okx"]["bid_size"] = float(bids[0][1])

                    if asks and len(asks[0]) >= 2:
                        quotes[base_coin]["okx"]["ask_price"] = float(asks[0][0])
                        quotes[base_coin]["okx"]["ask_size"] = float(asks[0][1])

                    exchange_ts_ms = payload.get("ts")

                    quotes[base_coin]["okx"]["ts_exchange"] = float(exchange_ts_ms) if exchange_ts_ms is not None else None
                    quotes[base_coin]["okx"]["local_recv_ts_ms"] = local_recv_ts_ms

                    if exchange_ts_ms is not None:
                        quotes[base_coin]["okx"]["delivery_latency_ms"] = local_recv_ts_ms - float(exchange_ts_ms)

                    calc_and_store_spread(base_coin, "okx")

        except asyncio.CancelledError:
            runtime_logger.info(f"{base_coin} | OKX listener cancelled")
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            raise

        except Exception as e:
            runtime_logger.error(f"{base_coin} | OKX error: {e}")
            await asyncio.sleep(10)

async def heartbeat():
    while True:
        runtime_logger.info(
            f"heartbeat | pairs={len(pairs)} | buffer_size={len(opportunities_buffer)} | total_saved={saved_records_count}"
        )
        await asyncio.sleep(30)


async def main():
    runtime_logger.info(f"Loaded pairs: {len(pairs)}")
    for row in pairs:
        runtime_logger.info(
            f"PAIR | base_coin={row['base_coin']} | "
            f"okx_symbol={row['okx_symbol']} | bybit_symbol={row['bybit_symbol']}"
        )

    tasks = [asyncio.create_task(heartbeat(), name="heartbeat")]

    for row in pairs:
        base_coin = row["base_coin"]
        okx_symbol = row["okx_symbol"]
        bybit_symbol = row["bybit_symbol"]

        tasks.append(asyncio.create_task(okx_listener(base_coin, okx_symbol), name=f"okx:{base_coin}"))
        tasks.append(asyncio.create_task(bybit_listener(base_coin, bybit_symbol), name=f"bybit:{base_coin}"))

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        runtime_logger.info("main | cancellation received, shutting down tasks")

        for task in tasks:
            task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)

        await asyncio.sleep(0.25)

        raise
    finally:
        runtime_logger.info("main | flushing opportunities buffer before exit")
        persist_opportunities()


if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()
    except Exception:
        pass

    if not Path(UNIVERSE_PATH).exists():
        raise FileNotFoundError(f"Universe file not found: {UNIVERSE_PATH}")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        runtime_logger.info("KeyboardInterrupt received, process stopped by user")