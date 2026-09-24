"""The Event Listener resource.

An Event Listener is what Trino sends query events to; its type is the plugin that
implements the sending. Trino's own configuration file has no name for a listener, so the
name here is Apchi's: it is what an Operator refers to, and what Review diffs against.
"""

import re
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

#: Apchi's own identifier for the listener. Constrained to what survives a
#: .properties filename, because that is what it will become when Trino supports more
#: than one listener file.
LISTENER_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

ListenerName = Annotated[str, StringConstraints(pattern=LISTENER_NAME.pattern)]
#: The value of `event-listener.name`: which plugin Trino should load.
ListenerType = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,62}$")]


class EventListenerWrite(BaseModel):
    """What an Operator sends."""

    model_config = ConfigDict(extra="forbid")

    name: ListenerName
    type: ListenerType
    properties: dict[str, Any] = Field(default_factory=dict)


class EventListenerUpdate(BaseModel):
    """A partial update. The name is immutable; renaming is a remove and an add."""

    model_config = ConfigDict(extra="forbid")

    type: ListenerType | None = None
    properties: dict[str, Any] | None = None


class EventListener(BaseModel):
    """An Event Listener as it is stored and returned."""

    name: ListenerName
    type: ListenerType
    properties: dict[str, str] = Field(default_factory=dict)
    supported: bool = Field(
        description=(
            "False when the listener type has no curated schema. Its properties passed "
            "through unvalidated and are checked only by Trino at Apply."
        )
    )
