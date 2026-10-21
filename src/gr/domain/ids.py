"""Sortable ids for documents and runs: `<prefix>_<ulid>`."""

import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(timestamp_ms: int | None = None) -> str:
    """26-char ULID: 48-bit millisecond timestamp + 80 random bits, Crockford base32."""
    ms = time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms
    value = (ms << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))


def new_id(prefix: str) -> str:
    return f"{prefix}_{new_ulid()}"
