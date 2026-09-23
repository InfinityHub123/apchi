"""The Catalogs Section: the operations the pipeline and the API share."""

from typing import Any

from app.api.errors import NameAlreadyTaken, NotFound, UnprocessablePayload
from app.pipeline.candidate import Candidate
from app.sections import SectionName
from app.sections.catalogs.connectors import PropertyProblem, is_curated, validate_properties
from app.sections.catalogs.model import Catalog, CatalogUpdate, CatalogWrite

SECTION: SectionName = "catalogs"


def _as_catalog(name: str, stored: dict[str, Any]) -> Catalog:
    return Catalog(
        name=name,
        connector=stored["connector"],
        properties=stored.get("properties", {}),
        supported=is_curated(stored["connector"]),
    )


def list_catalogs(candidate: Candidate) -> list[Catalog]:
    stored = candidate.resources(SECTION)
    return [_as_catalog(name, stored[name]) for name in sorted(stored)]


def get_catalog(candidate: Candidate, name: str) -> Catalog:
    stored = candidate.resources(SECTION)
    if name not in stored:
        raise NotFound(f"No Catalog named {name!r} in the Configuration Candidate.")
    return _as_catalog(name, stored[name])


def _validated(connector: str, properties: dict[str, Any]) -> dict[str, str]:
    try:
        return validate_properties(connector, properties)
    except PropertyProblem as exc:
        raise UnprocessablePayload(
            f"The properties are not valid for the {connector!r} connector.",
            details=list(exc.problems),
        ) from exc


def create_catalog(candidate: Candidate, write: CatalogWrite) -> Catalog:
    stored = candidate.resources(SECTION)
    if write.name in stored:
        raise NameAlreadyTaken(f"A Catalog named {write.name!r} already exists.")
    properties = _validated(write.connector, write.properties)
    stored[write.name] = {"connector": write.connector, "properties": properties}
    return _as_catalog(write.name, stored[write.name])


def update_catalog(candidate: Candidate, name: str, update: CatalogUpdate) -> Catalog:
    stored = candidate.resources(SECTION)
    if name not in stored:
        raise NotFound(f"No Catalog named {name!r} in the Configuration Candidate.")
    current = stored[name]
    connector = update.connector or current["connector"]
    raw = current["properties"] if update.properties is None else update.properties
    stored[name] = {"connector": connector, "properties": _validated(connector, raw)}
    return _as_catalog(name, stored[name])


def delete_catalog(candidate: Candidate, name: str) -> None:
    stored = candidate.resources(SECTION)
    if name not in stored:
        raise NotFound(f"No Catalog named {name!r} in the Configuration Candidate.")
    del stored[name]
