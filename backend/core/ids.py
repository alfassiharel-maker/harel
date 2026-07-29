"""UUIDv7 generation (RFC 9562).

Postgres 16 has no built-in `uuidv7()` — it arrives in 18 — so ids are generated
here and passed to the database explicitly. See ADR-009 for why v7 rather than v4:
time-ordered ids keep B-tree inserts at the right edge of the index instead of
scattering them, and they need no sequence to coordinate once athlete-scoped
tables are sharded by `user_id`.

Generating ids in the application also means an object graph can be built and
linked before anything is written, which the registration flow depends on: it must
know the new user's id in order to establish the row-level-security context that
allows the insert.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

__all__ = ["timestamp_ms_of", "uuid7", "uuid7_at"]

_lock = threading.Lock()
# Guards monotonicity within a single millisecond. Without this, two ids minted in
# the same millisecond order only by their random bits, which loses the
# time-ordering property that is the entire reason for choosing v7.
_last_ms = -1
_last_counter = 0

# 12 bits of `rand_a` are used as an intra-millisecond counter (the RFC's
# "replace leftmost random bits with increased clock precision" method).
_COUNTER_BITS = 12
_COUNTER_MAX = (1 << _COUNTER_BITS) - 1


def uuid7() -> uuid.UUID:
    """A time-ordered UUIDv7 for the current instant.

    Monotonically increasing within a process, including within a single
    millisecond. Two processes can still interleave, which is fine — the
    guarantee we need is index locality, not a global total order.
    """
    return uuid7_at(time.time_ns() // 1_000_000)


def uuid7_at(timestamp_ms: int) -> uuid.UUID:
    """Build a UUIDv7 for an explicit Unix millisecond timestamp.

    Separated out so tests can pin time without patching the clock.
    """
    if timestamp_ms < 0:
        raise ValueError("timestamp_ms must not be negative")

    global _last_ms, _last_counter
    with _lock:
        if timestamp_ms > _last_ms:
            _last_ms = timestamp_ms
            # Start each millisecond partway up the counter space so a burst has
            # room to increment without overflowing, while still leaving the low
            # bits random.
            _last_counter = int.from_bytes(os.urandom(2), "big") & 0x03FF
        else:
            # Clock did not advance (or went backwards). Keep issuing ids after
            # the previous one rather than emitting a duplicate or an earlier id.
            timestamp_ms = _last_ms
            _last_counter += 1
            if _last_counter > _COUNTER_MAX:
                # Counter space for this millisecond is exhausted. Borrow from the
                # next millisecond instead of blocking or wrapping.
                _last_ms += 1
                timestamp_ms = _last_ms
                _last_counter = 0
        counter = _last_counter

    #  0                   1                   2                   3
    #  0-------------------------------------47 48-51 52--------63
    #  |            unix_ts_ms (48 bits)      | ver |  rand_a    |
    #  64-65 66------------------------------------------------127
    #  | var |                 rand_b (62 bits)                 |
    time_bytes = (timestamp_ms & 0xFFFF_FFFF_FFFF).to_bytes(6, "big")

    # Version 7 in the high nibble of byte 6, then the 12-bit counter.
    ver_and_counter = (0x7 << 12) | counter
    ver_bytes = ver_and_counter.to_bytes(2, "big")

    rand_b = bytearray(os.urandom(8))
    # RFC 4122 variant: binary 10 in the two most significant bits of byte 8.
    rand_b[0] = (rand_b[0] & 0x3F) | 0x80

    return uuid.UUID(bytes=bytes(time_bytes + ver_bytes + bytes(rand_b)))


def timestamp_ms_of(value: uuid.UUID) -> int:
    """Extract the embedded millisecond timestamp from a UUIDv7.

    Useful for debugging and for asserting ordering in tests. Raises for any other
    UUID version, because reading the first six bytes of a v4 as a timestamp
    produces a plausible-looking number that means nothing.
    """
    if value.version != 7:
        raise ValueError(f"not a UUIDv7 (version {value.version})")
    return int.from_bytes(value.bytes[:6], "big")
