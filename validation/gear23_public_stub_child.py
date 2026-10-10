#!/usr/bin/env python3
"""Run the public runtime with validation-only order entry disabled."""

from __future__ import annotations

import asyncio

from app.bot.runtime import BotRuntime


def main() -> int:
    runtime = BotRuntime()
    runtime.log.info("validation_no_orders | callbacks=probe,policy")

    def refuse_order_entry(**_kwargs) -> None:
        return None

    # Preserve real books, gates, observers, hot-add and WS lifecycle. Only stop
    # the optional signal/probe callbacks from touching even the stub broker.
    runtime._probe_maybe_open = refuse_order_entry
    runtime._policy_maybe_act = refuse_order_entry
    asyncio.run(runtime.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
