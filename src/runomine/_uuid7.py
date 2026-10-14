"""UUID version 7 (RFC 9562): 48-bit Unix milliseconds, then randomness.

``uuid.uuid7`` only exists from Python 3.14, so we build it by hand.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

__all__ = ["uuid7"]

_lock = threading.Lock()
_last_ms = 0
_last_seq = 0


def uuid7() -> str:
    """Return a new UUID v7 as a string. Monotonic within a process (12-bit counter per ms)."""
    global _last_ms, _last_seq
    rand = int.from_bytes(os.urandom(10), "big")
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms > _last_ms:
            _last_ms = ms
            _last_seq = (rand >> 64) & 0x7FF  # random start, leaves room to count up
        else:
            ms = _last_ms
            _last_seq += 1
            if _last_seq > 0xFFF:  # counter exhausted: borrow the next millisecond
                _last_ms += 1
                ms = _last_ms
                _last_seq = 0
        seq = _last_seq
    rand_b = rand & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76  # version
    value |= seq << 64  # rand_a, used as a counter
    value |= 0b10 << 62  # variant
    value |= rand_b
    return str(uuid.UUID(int=value))
