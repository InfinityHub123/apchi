"""The curated connector schemas, and what Apchi does with a connector it has no schema
for.

Apchi curates the connectors Operators actually use so a typo like `connection-uri` for
`connection-url` fails at request time rather than at Apply. Connectors outside that set
pass through and are marked unsupported in the API, validated only by the ephemeral Trino
at Apply.
"""

from typing import Any

from app.sections.catalogs.schemas import CURATED_SCHEMAS
from app.sections.properties import PropertyProblem, validate

__all__ = ["PropertyProblem", "is_curated", "validate_properties"]


def is_curated(connector: str) -> bool:
    return connector in CURATED_SCHEMAS


def validate_properties(connector: str, properties: dict[str, Any]) -> dict[str, str]:
    return validate(CURATED_SCHEMAS.get(connector), properties, connector, "connector")
