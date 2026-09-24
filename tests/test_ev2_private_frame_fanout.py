"""Owner-loop private fanout is opt-in and cannot silently drop failures."""

from __future__ import annotations

import asyncio
import unittest

from app.bot.private.ws_warm_loop import LoopOwnedSocket, PrivateWarmLoop, SocketSlot


class _Frames:
    def __init__(self, *items: str) -> None:
        self._items = iter(items)

    def __aiter__(self) -> "_Frames":
        return self

    async def __anext__(self) -> str:
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration from None


class _Runtime:
    exchange = "bybit"

    def __init__(self) -> None:
        self.parsed: list[str] = []

    def note_private_activity(self) -> None:
        pass

    def note_trade_activity(self) -> None:
        pass

    def handle_inbound_text(self, text: str) -> None:
        self.parsed.append(text)


class PrivateFrameFanoutTests(unittest.IsolatedAsyncioTestCase):
    def _slot(self) -> tuple[PrivateWarmLoop, SocketSlot, _Runtime]:
        loop = PrivateWarmLoop()
        socket = LoopOwnedSocket(
            url="wss://invalid.example", exchange="bybit", channel="private", owner=loop
        )
        runtime = _Runtime()
        socket.runtime = runtime
        socket.handshake_done = True
        slot = SocketSlot(socket=socket)
        loop._slots["bybit:private"] = slot
        return loop, slot, runtime

    async def test_observer_receives_after_legacy_parse_with_receive_clock(self) -> None:
        loop, slot, runtime = self._slot()
        seen: list[tuple[str, int]] = []

        async def observe(text: str, mono_ns: int) -> None:
            self.assertEqual(runtime.parsed, [text])
            seen.append((text, mono_ns))

        loop.set_private_frame_observer("bybit", observe)
        await loop._pump(slot, _Frames('{"topic":"order","data":[]}'))
        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0][1], 0)

    async def test_observer_failure_propagates_to_reconnect_owner(self) -> None:
        loop, slot, _ = self._slot()

        def broken(_text: str, _mono_ns: int) -> None:
            raise RuntimeError("ev2_ingest_failed")

        loop.set_private_frame_observer("bybit", broken)
        with self.assertRaisesRegex(RuntimeError, "ev2_ingest_failed"):
            await loop._pump(slot, _Frames('{"topic":"order","data":[]}'))

    async def test_default_has_no_observer_and_wrong_channel_rejected(self) -> None:
        loop, slot, runtime = self._slot()
        await loop._pump(slot, _Frames('{"topic":"order","data":[]}'))
        self.assertEqual(len(runtime.parsed), 1)
        with self.assertRaises(ValueError):
            loop.set_private_frame_observer("okx", lambda _t, _m: None)

    async def test_trade_ack_tap_preserves_queue_and_receive_clock(self) -> None:
        loop = PrivateWarmLoop()
        socket = LoopOwnedSocket(
            url="wss://invalid.example", exchange="bybit", channel="trade", owner=loop
        )
        socket.runtime = _Runtime()
        socket.handshake_done = True
        slot = SocketSlot(socket=socket)
        loop._slots["bybit:trade"] = slot
        seen: list[tuple[str, int]] = []
        loop.set_trade_frame_observer("bybit", lambda text, mono: seen.append((text, mono)))
        ack = '{"op":"order.create","reqId":"test","retCode":0}'
        await loop._pump(slot, _Frames(ack))
        self.assertEqual(socket.recv_text(timeout_sec=0), ack)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], ack)
        self.assertGreater(seen[0][1], 0)

    async def test_trade_tap_failure_propagates_before_ack_queue(self) -> None:
        loop = PrivateWarmLoop()
        socket = LoopOwnedSocket(
            url="wss://invalid.example", exchange="bybit", channel="trade", owner=loop
        )
        socket.runtime = _Runtime()
        socket.handshake_done = True
        slot = SocketSlot(socket=socket)
        loop._slots["bybit:trade"] = slot

        def broken(_text: str, _mono: int) -> None:
            raise RuntimeError("ev2_ack_tap_failed")

        loop.set_trade_frame_observer("bybit", broken)
        with self.assertRaisesRegex(RuntimeError, "ev2_ack_tap_failed"):
            await loop._pump(slot, _Frames('{"op":"order.create","reqId":"test"}'))
        with self.assertRaises(TimeoutError):
            socket.recv_text(timeout_sec=0)
