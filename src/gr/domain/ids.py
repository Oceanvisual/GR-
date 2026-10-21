"""Sortable ids for documents and runs: `<prefix>_<ulid>`."""

import os
import threading
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RANDOM_BITS = 80
_lock = threading.Lock()
_last: tuple[int, int] = (-1, 0)  # (milliseconds, random part) of the previous id


def _encode(value: int) -> str:
    return "".join(_CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))


def new_ulid(timestamp_ms: int | None = None) -> str:
    """26-char ULID: 48-bit millisecond timestamp + 80 random bits, Crockford base32.

    Monotonic within a process: ids made in the same millisecond increment the random part,
    so sorting by id preserves creation order (ULID spec, monotonic variant).
    """
    global _last
    ms = time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms
    with _lock:
        last_ms, last_random = _last
        if ms == last_ms and last_random + 1 < 1 << _RANDOM_BITS:
            random_part = last_random + 1
        else:
            random_part = int.from_bytes(os.urandom(10), "big") >> 1  # headroom to increment
        _last = (ms, random_part)
    return _encode((ms << _RANDOM_BITS) | random_part)


def new_id(prefix: str) -> str:
    return f"{prefix}_{new_ulid()}"
