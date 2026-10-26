"""The Catalog resource.

A Catalog is the Trino-visible object; a Connector is the plugin it uses to reach
an external system.
"""

import re
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.sections.client_certificates.model import CertificateName

# Trino catalog names are case-insensitive identifiers. Constrain to the form that
# survives a round trip through a .properties filename and SQL without quoting.
CATALOG_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

CatalogName = Annotated[str, StringConstraints(pattern=CATALOG_NAME.pattern)]
ConnectorName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,62}$")]


class CatalogWrite(BaseModel):
    """What an Operator sends."""

    #: A real connector with a real-shaped JDBC URL. The route offers more, including
    #: one that presents a Client Certificate.
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "name": "finance_pg",
                    "connector": "postgresql",
                    "properties": {
                        "connection-url": (
                            "jdbc:postgresql://pg.finance.svc.cluster.local:5432/ledger"
                            "?sslmode=verify-full"
                        ),
                        "connection-user": "trino_reader",
                        "connection-password": "the-password-goes-here",
                    },
                }
            ]
        },
    )

    name: CatalogName
    connector: ConnectorName
    properties: dict[str, Any] = Field(default_factory=dict)
    certificate: CertificateName | None = Field(
        default=None,
        description=(
            "A Client Certificate this Catalog presents. Apchi puts it into the properties "
            "the way this connector expects, so no path is ever typed. Only for connectors "
            "Apchi knows how to wire; elsewhere use ${cert:name} and ${key:name} in the "
            "property the connector documents."
        ),
    )


class CatalogUpdate(BaseModel):
    """A partial update, by named fields (§19).

    Omitted means unchanged; present means replaces; `null` clears the fields that can be
    cleared. The name is immutable -- renaming is a drop and a create, which is a different
    and more destructive operation.
    """

    model_config = ConfigDict(extra="forbid")

    connector: ConnectorName | None = Field(
        default=None,
        description="Omit to keep the current connector. It cannot be cleared.",
    )
    properties: dict[str, Any] | None = Field(
        default=None,
        description=(
            "**Replaces** the property map rather than merging into it: a PATCH naming one "
            "property discards the others. Omit to keep the current ones; they cannot be "
            "cleared."
        ),
    )
    certificate: CertificateName | None = Field(
        default=None,
        description=(
            "Omit to keep the current Client Certificate. Send null to stop using one -- "
            "the certificate itself is unaffected and stays available to other Catalogs."
        ),
    )


class Catalog(BaseModel):
    """A Catalog as it is stored and returned."""

    #: Its own rather than `CatalogWrite`'s -- this is not a subclass, but the response adds
    #: `supported`, and an example without it would not show the one field here that an
    #: Operator cannot set and has to read.
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "finance_pg",
                    "connector": "postgresql",
                    "properties": {
                        "connection-url": (
                            "jdbc:postgresql://pg.finance.svc.cluster.local:5432/ledger"
                            "?sslmode=verify-full"
                        ),
                        "connection-user": "trino_reader",
                        "connection-password": "the-password-goes-here",
                    },
                    "certificate": None,
                    "supported": True,
                }
            ]
        }
    )

    name: CatalogName
    connector: ConnectorName
    properties: dict[str, str] = Field(default_factory=dict)
    certificate: CertificateName | None = None
    supported: bool = Field(
        description=(
            "False when the connector has no curated schema. Its properties passed "
            "through unvalidated and are checked only by Trino at Apply."
        )
    )
