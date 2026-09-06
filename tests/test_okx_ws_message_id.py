"""Prove overnight OKX 60033 was message ``id`` underscore from ``new_opaque_id``.

No live OKX network. Charset + frame construction is enough.

Canary dual open EDEN intent ``653fc03f-8e2c-4543-9102-0c892d466411``
at 2026-09-05 ~20:52 UTC (wire transcript):

- Outbound: ``{"id":"req_c08a00f2f36c481682e7fa5dc998","op":"order",...}``
- Inbound: ``{"event":"error","msg":"Parameter id error","code":"60033"}``
- Bybit same dual: ``retCode=0``. ``clOrdId`` was already alphanumeric.
"""

from __future__ import annotations

import json
import unittest

from app.bot.private.journal_v1 import new_opaque_id
from app.bot.private.order_sign import LiveCredentials
from app.bot.private.ws_messages import (
    OKX_WS_ID_ERROR_CODE,
    OKX_WS_ID_ERROR_MSG,
    OKX_WS_ID_MAX_LEN,
    OKX_WS_ID_PARAMETER,
    OkxWsIdError,
    assert_okx_ws_message_id,
    build_okx_trade_cancel,
    build_okx_trade_place,
    new_okx_ws_id,
    okx_ws_id_is_legal,
    sanitize_okx_ws_id,
)
from app.bot.private.ws_private import new_trade_req_id
from app.bot.private.ws_trivial_dual_leg import (
    assert_signed_place_frame,
    build_signed_place_text,
    make_frame_plan,
)

# Exact overnight wire (ids only; no secrets).
CANARY_EDEN_INTENT_ID = "653fc03f-8e2c-4543-9102-0c892d466411"
WIRE_OKX_OUTBOUND_ID = "req_c08a00f2f36c481682e7fa5dc998"
WIRE_OKX_CLORD_ID = "o653fc03f8e2c454391020c892d466411"[:32]
WIRE_OKX_OUTBOUND = {
    "id": WIRE_OKX_OUTBOUND_ID,
    "op": "order",
    "args": [
        {
            "instId": "EDEN-USDT-SWAP",
            "instIdCode": 1,
            "tdMode": "cross",
            "side": "buy",
            "sz": "1",
            "clOrdId": WIRE_OKX_CLORD_ID,
            "ordType": "market",
        }
    ],
}
WIRE_OKX_INBOUND = {
    "event": "error",
    "msg": "Parameter id error",
    "code": "60033",
}


def _pre_fix_okx_frame_id(req_id: str | None = None) -> str:
    """Historical Contour B: ``frame['id'] = req_id or new_opaque_id('req')[:32]``."""
    return req_id or new_opaque_id("req")[:32]


def _okx_plan(*, order_attempt_id: str = "op_cafebabe"):
    return make_frame_plan(
        venue="okx_live",
        symbol="EDEN-USDT-SWAP",
        side="buy",
        qty="1",
        inst_id_code=193761,
        order_attempt_id=order_attempt_id,
    )


def _creds() -> LiveCredentials:
    return LiveCredentials(api_key="k", api_secret="s", passphrase="p")


class OkxWsIdHelperTests(unittest.TestCase):
    def test_assert_rejects_underscore_with_60033_semantics(self) -> None:
        with self.assertRaises(OkxWsIdError) as cm:
            assert_okx_ws_message_id(WIRE_OKX_OUTBOUND_ID)
        exc = cm.exception
        self.assertEqual(exc.code, OKX_WS_ID_ERROR_CODE)
        self.assertEqual(exc.msg, OKX_WS_ID_ERROR_MSG)
        self.assertEqual(exc.parameter, OKX_WS_ID_PARAMETER)
        self.assertEqual(exc.code, WIRE_OKX_INBOUND["code"])
        self.assertEqual(exc.msg, WIRE_OKX_INBOUND["msg"])
        self.assertIn("_", WIRE_OKX_OUTBOUND_ID)
        self.assertFalse(okx_ws_id_is_legal(WIRE_OKX_OUTBOUND_ID))

    def test_assert_rejects_empty_hyphen_and_overlong(self) -> None:
        for bad in ("", "req-id", "a" * 33, None, 123):
            with self.assertRaises(OkxWsIdError):
                assert_okx_ws_message_id(bad)  # type: ignore[arg-type]

    def test_sanitize_strips_underscore_and_truncates(self) -> None:
        cleaned = sanitize_okx_ws_id(WIRE_OKX_OUTBOUND_ID)
        self.assertEqual(cleaned, "reqc08a00f2f36c481682e7fa5dc998")
        self.assertTrue(cleaned.isalnum())
        self.assertLessEqual(len(cleaned), OKX_WS_ID_MAX_LEN)
        assert_okx_ws_message_id(cleaned)
        self.assertEqual(sanitize_okx_ws_id("abc"), "abc")
        self.assertEqual(len(sanitize_okx_ws_id("A" * 40)), 32)

    def test_sanitize_fail_closed_when_nothing_alnum(self) -> None:
        with self.assertRaises(OkxWsIdError):
            sanitize_okx_ws_id("___")

    def test_new_okx_ws_id_is_legal(self) -> None:
        for _ in range(20):
            rid = new_okx_ws_id()
            assert_okx_ws_message_id(rid)
            self.assertNotIn("_", rid)
            self.assertLessEqual(len(rid), OKX_WS_ID_MAX_LEN)


class OvernightWireDiagnosisTests(unittest.TestCase):
    def test_new_opaque_id_req_always_contains_underscore(self) -> None:
        """Journal helper is unchanged and is the illegal-id source."""
        for _ in range(20):
            generated = new_opaque_id("req")
            self.assertTrue(generated.startswith("req_"))
            self.assertIn("_", generated)
            truncated = generated[:32]
            self.assertIn("_", truncated)
            self.assertFalse(okx_ws_id_is_legal(truncated))
            with self.assertRaises(OkxWsIdError) as cm:
                assert_okx_ws_message_id(truncated)
            self.assertEqual(cm.exception.code, "60033")
            self.assertEqual(cm.exception.parameter, "id")

    def test_pre_fix_frame_id_matches_wire_failure_signature(self) -> None:
        """Building the overnight way yields an ``id`` OKX rejects as 60033."""
        pre_fix = _pre_fix_okx_frame_id(WIRE_OKX_OUTBOUND_ID)
        self.assertEqual(pre_fix, WIRE_OKX_OUTBOUND["id"])
        self.assertIn("_", pre_fix)
        self.assertFalse(okx_ws_id_is_legal(pre_fix))
        with self.assertRaises(OkxWsIdError) as cm:
            assert_okx_ws_message_id(pre_fix)
        self.assertEqual(cm.exception.code, WIRE_OKX_INBOUND["code"])
        self.assertEqual(cm.exception.msg, WIRE_OKX_INBOUND["msg"])
        self.assertEqual(cm.exception.parameter, "id")

        outbound = json.dumps(WIRE_OKX_OUTBOUND, separators=(",", ":"))
        inbound = json.dumps(WIRE_OKX_INBOUND, separators=(",", ":"))
        self.assertIn(f'"id":"{WIRE_OKX_OUTBOUND_ID}"', outbound)
        self.assertIn('"code":"60033"', inbound)
        self.assertIn("Parameter id error", inbound)

        generated_frame_id = _pre_fix_okx_frame_id(None)
        self.assertTrue(generated_frame_id.startswith("req_"))
        with self.assertRaises(OkxWsIdError) as cm2:
            assert_okx_ws_message_id(generated_frame_id)
        self.assertEqual(cm2.exception.code, "60033")

    def test_clord_id_on_wire_was_already_legal(self) -> None:
        cl = WIRE_OKX_OUTBOUND["args"][0]["clOrdId"]
        self.assertTrue(str(cl).isalnum())
        self.assertNotIn("_", str(cl))
        self.assertLessEqual(len(str(cl)), 32)


class FixedPathTests(unittest.TestCase):
    def test_build_okx_place_sanitizes_production_shaped_id(self) -> None:
        plan = _okx_plan()
        msg = build_okx_trade_place(plan, req_id=WIRE_OKX_OUTBOUND_ID)
        data = json.loads(msg.text)
        assert_okx_ws_message_id(data["id"])
        self.assertEqual(data["id"], sanitize_okx_ws_id(WIRE_OKX_OUTBOUND_ID))
        self.assertNotEqual(data["id"], WIRE_OKX_OUTBOUND_ID)
        self.assertNotIn("_", data["id"])

    def test_build_okx_cancel_sanitizes_production_shaped_id(self) -> None:
        plan = _okx_plan()
        msg = build_okx_trade_cancel(plan, req_id=WIRE_OKX_OUTBOUND_ID)
        data = json.loads(msg.text)
        assert_okx_ws_message_id(data["id"])
        self.assertEqual(data["op"], "cancel-order")

    def test_build_signed_place_text_default_okx_id_is_legal_and_correlates(self) -> None:
        text, req, _ = build_signed_place_text(
            venue="okx",
            symbol="EDEN-USDT-SWAP",
            side="buy",
            qty="1",
            credentials=None,
            inst_id_code=193761,
        )
        data = json.loads(text)
        self.assertEqual(data["id"], req)
        assert_okx_ws_message_id(req)
        self.assertTrue(req.isalnum())
        self.assertLessEqual(len(req), 32)
        assert_signed_place_frame("okx_live", text)

    def test_build_signed_place_text_returns_sanitized_id_when_caller_passes_req_(
        self,
    ) -> None:
        text, req, _ = build_signed_place_text(
            venue="okx",
            symbol="EDEN-USDT-SWAP",
            side="buy",
            qty="1",
            credentials=None,
            inst_id_code=193761,
            req_id=WIRE_OKX_OUTBOUND_ID,
        )
        data = json.loads(text)
        self.assertEqual(data["id"], req)
        self.assertEqual(req, sanitize_okx_ws_id(WIRE_OKX_OUTBOUND_ID))
        assert_okx_ws_message_id(req)


class RegressionTests(unittest.TestCase):
    def test_clord_id_strips_underscore_independently_of_message_id(self) -> None:
        plan = _okx_plan(order_attempt_id="op_deadbeef")
        msg = build_okx_trade_place(plan, req_id=new_okx_ws_id())
        data = json.loads(msg.text)
        cl = data["args"][0]["clOrdId"]
        self.assertEqual(cl, "opdeadbeef")
        self.assertTrue(cl.isalnum())
        assert_okx_ws_message_id(data["id"])

    def test_bybit_place_still_accepts_journal_style_req_id(self) -> None:
        bybit_req = new_opaque_id("req")[:32]
        self.assertIn("_", bybit_req)
        text, req, _ = build_signed_place_text(
            venue="bybit",
            symbol="EDENUSDT",
            side="sell",
            qty="1",
            credentials=_creds(),
            req_id=bybit_req,
        )
        data = json.loads(text)
        self.assertEqual(data["reqId"], bybit_req)
        self.assertEqual(req, bybit_req)
        self.assertIn("_", data["reqId"])
        assert_signed_place_frame("bybit_live", text)

    def test_journal_opaque_ids_still_use_prefix_underscore(self) -> None:
        run = new_opaque_id("run")
        op = new_opaque_id("op")
        self.assertTrue(run.startswith("run_"))
        self.assertTrue(op.startswith("op_"))
        self.assertIn("_", run)
        self.assertIn("_", op)

    def test_w4_okx_trade_req_id_stays_alphanumeric(self) -> None:
        okx_id = new_trade_req_id(exchange="okx")
        assert_okx_ws_message_id(okx_id)
        self.assertTrue(okx_id.startswith("w4"))
        bybit_id = new_trade_req_id(exchange="bybit")
        self.assertTrue(bybit_id.startswith("w4_"))

    def test_assert_signed_place_frame_rejects_underscore_okx_id(self) -> None:
        illegal = json.dumps(
            {
                "id": WIRE_OKX_OUTBOUND_ID,
                "op": "order",
                "args": [{"instIdCode": 1, "instId": "EDEN-USDT-SWAP"}],
            }
        )
        from app.bot.private.ws_trivial_dual_leg import TrivialSendError

        with self.assertRaises(TrivialSendError):
            assert_signed_place_frame("okx_live", illegal)


if __name__ == "__main__":
    unittest.main()
