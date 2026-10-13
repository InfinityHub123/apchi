"""One pagination model, for the collections that grow without bound (§19).

§19 asks for *"one consistent pagination, filtering and sorting model"* and names snapshots,
audit events, permissions, certificates and catalogs as likely large. Two of those cannot be
paginated usefully: **five of the six Section collections live inside the single Candidate
document** (ADR-0002, §4), which is loaded whole on every read, so slicing it afterwards saves
nothing — and §21 needs every Section at once to show the full diff before an Apply. Their real
limit is the 16MB document ceiling, which §4 now records and `CandidateStore` enforces.

So this covers what actually grows: `snapshots`, `applies`, `validations`, `audit`, `tokens`.
Append-only, and nothing ever removes them.

**Keyset cursors, not offsets.** These collections grow at the *head* — newest first — so an
offset pager silently repeats rows it has already shown while new ones arrive above them. A
cursor names the last row seen, which cannot drift.

**The cursor carries the sort key *and* the id.** Two Applies can start in the same instant,
and a boundary that cannot tell them apart repeats or skips one. Snapshots are ordered by a
unique number where the tiebreak is redundant, and it costs nothing to keep the shape the
same.

What is *not* consistent across collections is the sort key itself: snapshots go by number,
applies and validations by when they started. No single key is meaningful for all of them, so
each documents its own order. What §19 asks for -- and what this provides -- is one envelope,
one set of parameter names, and one cursor semantics.
"""

import base64
import binascii
from typing import Annotated, Any, Final

from fastapi import Query
from pydantic import BaseModel, Field

#: The most a single page may hold, whatever a caller asks for. A limit a client can raise
#: without bound is an unbounded response wearing a parameter.
MAX_LIMIT: Final = 500
DEFAULT_LIMIT: Final = 50


class BadCursor(ValueError):
    """A `before` value that did not come from a previous page."""


class Page[T](BaseModel):
    """One page of a collection, newest first."""

    items: list[T]
    next: str | None = Field(
        default=None,
        description=(
            "Pass as `before` for the following page. Null when this is the last one. A "
            "cursor rather than an offset: these collections grow at the head, and an offset "
            "pager repeats or skips rows while they are being appended to."
        ),
    )


def encode_cursor(*parts: str) -> str:
    """An opaque cursor from the parts of a sort key.

    Opaque on purpose. A client that parses it is a client that breaks when a collection
    changes how it sorts, and the only promise made here is that a `next` can be handed back
    as a `before`.
    """
    return base64.urlsafe_b64encode("\x1f".join(parts).encode()).decode().rstrip("=")


def decode_cursor(cursor: str, expected: int) -> list[str]:
    """The parts back, or `BadCursor`.

    A mangled cursor is a 422 the caller can act on rather than a 500 — and `expected` is
    checked so a cursor from a different collection is refused rather than half-read.
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        parts = base64.urlsafe_b64decode(padded).decode().split("\x1f")
    except (binascii.Error, UnicodeDecodeError, ValueError) as bad:
        raise BadCursor(cursor) from bad
    if len(parts) != expected:
        raise BadCursor(cursor)
    return parts


def after(field: str, value: Any, identifier: Any) -> dict[str, Any]:
    """The keyset predicate: everything strictly below the cursor's position.

    Written as an `$or` rather than a compound comparison because MongoDB has no tuple
    comparison: *older than this instant, or in this instant with a lower id*. That second
    clause is what makes a boundary exact when several rows share a sort key.
    """
    return {"$or": [{field: {"$lt": value}}, {field: value, "_id": {"$lt": identifier}}]}


def bounded(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


#: The two query parameters every paginated collection takes, declared once so they cannot
#: drift apart in name, bounds or wording -- which is most of what §19 means by "one
#: consistent model".
LimitParam = Annotated[
    int,
    Query(ge=1, le=MAX_LIMIT, description=f"How many to return, at most {MAX_LIMIT}."),
]
BeforeParam = Annotated[
    str | None,
    Query(
        description=(
            "A `next` from a previous page. Omit for the newest page. Opaque: it names a "
            "position in this collection's order and should be passed back unmodified."
        )
    ),
]
