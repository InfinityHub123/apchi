"""The Catalog resource.

A Catalog is the Trino-visible object; a Connector is the plugin it uses to reach
an external system.
"""

import re
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# Trino catalog names are case-insensitive identifiers. Constrain to the form that
# survives a round trip through a .properties filename and SQL without quoting.
CATALOG_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

CatalogName = Annotated[str, StringConstraints(pattern=CATALOG_NAME.pattern)]
ConnectorName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,62}$")]


class CatalogWrite(BaseModel):
    """What an Operator sends."""

    model_config = ConfigDict(extra="forbid")

    name: CatalogName
    connector: ConnectorName
    properties: dict[str, Any] = Field(default_factory=dict)


class CatalogUpdate(BaseModel):
    """A partial update. The name is immutable; renaming is a drop and a create,
    which is a different and more destructive operation."""

    model_config = ConfigDict(extra="forbid")

    connector: ConnectorName | None = None
    properties: dict[str, Any] | None = None


class Catalog(BaseModel):
    """A Catalog as it is stored and returned."""

    name: CatalogName
    connector: ConnectorName
    properties: dict[str, str] = Field(default_factory=dict)
    supported: bool = Field(
        description=(
            "False when the connector has no curated schema. Its properties passed "
            "through unvalidated and are checked only by Trino at Apply."
        )
    )
