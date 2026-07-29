"""Entity tags for optimistic concurrency.

`docs/03 §1` requires `ETag` + `If-Match` on mutable resources. The reason is
specific: a lost update on an athlete's threshold values silently corrupts every
derived metric downstream, and the athlete has no way to notice.

The tag is a hash of the resource's own serialised representation rather than a
row version or a timestamp. That choice is deliberate:

* `updated_at` has clock granularity and depends on the trigger firing.
* Postgres `xmin` is a real row version, but freezing can rewrite it, after which
  two unrelated rows can share a tag.
* A content hash is what RFC 9110 §8.8.3 actually describes a strong validator
  as being, needs no extra column, and cannot go stale.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel

__all__ = ["etag_matches", "etag_of"]


def etag_of(representation: BaseModel) -> str:
    """A strong entity tag for a DTO, in the quoted form the header requires."""
    # mode="json" so the bytes hashed are the bytes served: a datetime that
    # serialises one way in Python and another way on the wire would otherwise
    # produce a tag that never matches what the client was given.
    canonical = representation.model_dump_json(by_alias=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f'"{digest[:32]}"'


def etag_matches(header_value: str, current: str) -> bool:
    """Compare an `If-Match` header against the resource's current tag.

    `*` matches any existing resource, per RFC 9110 §13.1.1. A list of tags
    matches if any member does — clients legitimately send several after a
    redirect or a retry.

    Weak comparison (`W/` prefix) is not accepted for `If-Match`: the whole point
    here is byte-exact agreement about what is being overwritten.
    """
    header_value = header_value.strip()
    if header_value == "*":
        return True
    return any(candidate.strip() == current for candidate in header_value.split(","))
