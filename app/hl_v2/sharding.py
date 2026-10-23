"""Round-robin shard assignment for HL websocket sockets."""

from __future__ import annotations

from typing import Sequence, TypeVar

T = TypeVar("T")


def shard_count_from_env(raw: str | None, *, default: int = 1) -> int:
    """HL_SOCKET_COUNT. Default 1. Raise on invalid."""
    if raw is None or not str(raw).strip():
        return default
    value = int(str(raw).strip())
    if value < 1:
        raise ValueError(f"HL_SOCKET_COUNT must be >= 1, got {value}")
    if value > 9:
        raise ValueError(f"HL_SOCKET_COUNT must be <= 9, got {value}")
    return value


def split_round_robin(items: Sequence[T], shard_count: int) -> list[tuple[T, ...]]:
    """Split items evenly across shards by round-robin index."""
    if shard_count < 1:
        raise ValueError(f"shard_count must be >= 1, got {shard_count}")
    buckets: list[list[T]] = [[] for _ in range(shard_count)]
    for index, item in enumerate(items):
        buckets[index % shard_count].append(item)
    return [tuple(bucket) for bucket in buckets]
