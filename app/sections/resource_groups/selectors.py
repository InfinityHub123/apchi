"""Which group a query lands in, as Trino would decide it.

Verification needs this to say "expected X, reached Y". Predicting it means evaluating the
selector list the way Trino does -- first match wins, every field given must match -- which
is a second implementation of somebody else's matcher, and a wrong prediction fails
Verification on a healthy Cluster and rolls back a good Apply.

So the rule here is to answer only when the answer is certain. A selector that turns on
something Apchi cannot know about its own query -- which groups its user belongs to, who it
was before impersonation -- makes every selector after it unreachable to reasoning, and the
prediction stops rather than guesses.

Two details verified against a running coordinator, because getting either wrong would
produce exactly the false failure this module exists to avoid:

- The regexes are **full matches**. A selector for user `apch` does not match `apchi`.
- A Cluster with no resource group manager configured still reports a group: Trino's legacy
  manager files every query under `global`.
"""

import re
from collections.abc import Sequence
from typing import Any

from app.sections.resource_groups.model import Selector

#: Fields of a selector that describe something about the caller Apchi does not know about
#: itself. Any of them makes the outcome unpredictable rather than false.
_UNKNOWABLE = ("user_group", "authenticated_user", "original_user")


class Unpredictable(Exception):
    """Apchi cannot say where this query would land, and will not guess."""


def _matches(pattern: str, value: str) -> bool:
    """Java's `Matcher.matches()`, which is a full match. An expression Python cannot
    compile is not a mismatch -- it is a question Apchi cannot answer."""
    try:
        return re.fullmatch(pattern, value) is not None
    except re.error as exc:
        raise Unpredictable(f"selector pattern {pattern!r} is not one Apchi can evaluate") from exc


def group_for(
    selectors: Sequence[dict[str, Any]],
    *,
    user: str,
    source: str,
    query: str,
    query_type: str = "SELECT",
    client_tags: Sequence[str] = (),
) -> str | None:
    """The group the first matching selector names, or None when none matches.

    Raises `Unpredictable` rather than answering when a selector Trino would have considered
    turns on something Apchi cannot evaluate: skipping such a selector could hand back a
    group Trino would never have chosen.
    """
    for raw in selectors:
        selector = Selector.model_validate(raw)
        for field in _UNKNOWABLE:
            if getattr(selector, field) is not None:
                raise Unpredictable(
                    f"a selector matches on {field}, which Apchi cannot know about its own query"
                )
        if selector.user is not None and not _matches(selector.user, user):
            continue
        if selector.source is not None and not _matches(selector.source, source):
            continue
        if selector.query_text is not None and not _matches(selector.query_text, query):
            continue
        if selector.query_type is not None and selector.query_type != query_type:
            continue
        if selector.client_tags is not None and not set(selector.client_tags) <= set(client_tags):
            continue
        return selector.group
    return None
