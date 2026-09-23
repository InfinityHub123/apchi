"""Validating an Event Listener's properties, or passing them through."""

from typing import Any

from app.sections.event_listeners.schemas import CURATED_SCHEMAS
from app.sections.properties import PropertyProblem, validate

__all__ = ["PropertyProblem", "is_curated", "validate_properties"]


def is_curated(listener_type: str) -> bool:
    return listener_type in CURATED_SCHEMAS


def validate_properties(listener_type: str, properties: dict[str, Any]) -> dict[str, str]:
    return validate(CURATED_SCHEMAS.get(listener_type), properties, listener_type, "event listener")
