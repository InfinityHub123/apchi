"""Adoption's own surface: the discovery an Admin reads, completes and adopts.

Admin-authority, per §14. Adoption decides what a Cluster's configuration *is*, it runs
once, and it writes Admin values -- none of which is an Operator's to do.

A resource rather than an action, deliberately. Apchi cannot recover everything: a Catalog
created by DDL after the pod started has its properties only in the coordinator's writable
store, and Apchi will not invent a connection it could not read (#88). So the discovery is a
document to read, correct and complete, and nothing reaches the Cluster until the seed and
the cutover that follow it (#101, #91).
"""

import logging
from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import DiscoveryStoreDep, KubernetesDep, SettingsDep, TrinoDep
from app.api.errors import Conflict, NotFound
from app.pipeline.discoveries import StoredDiscovery
from app.pipeline.discovery import DiscoveryFailed, Problem, discover
from app.sections import SectionName
from app.sections.catalogs import SECTION as CATALOGS
from app.sections.catalogs.model import CatalogWrite
from app.sections.catalogs.section import validated as validated_catalog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/adoption", tags=["adoption"])


class DiscoveryView(BaseModel):
    """A discovery as an Admin reads it."""

    model_config = ConfigDict(extra="forbid")

    read_at: str
    complete: bool = Field(
        description="Whether everything Apchi could not read has been supplied. "
        "Seeding and adopting both require it."
    )
    outstanding: list[Problem] = Field(
        description="Problems no amendment has answered yet, each naming where it is."
    )
    changed_since_last_read: list[str] = Field(
        description="What the Cluster changed between the previous reading and this one."
    )
    sections: list[dict[str, Any]] = Field(
        description="Per Section: what was read, where Apchi looked, and what it amended."
    )
    admin_values: dict[str, Any] = Field(
        description="Configuration that belongs to the Admin rather than the Candidate: "
        "rules Apchi's models cannot express, kept working beneath the Operator's own."
    )


def _view(stored: StoredDiscovery) -> DiscoveryView:
    return DiscoveryView(
        read_at=stored.read_at.isoformat(),
        complete=stored.complete,
        outstanding=stored.outstanding,
        changed_since_last_read=stored.changed_since_last_read,
        sections=[
            {
                "section": section.section,
                "readable": section.readable,
                "looked_at": section.looked_at,
                "guessed_because": section.guessed_because,
                "resources": stored.resolved(section.section),
                "amended": sorted(stored.amendments.get(section.section, {})),
                "problems": [problem.model_dump(mode="json") for problem in section.problems],
            }
            for section in stored.read.sections
        ],
        admin_values=stored.read.admin_values,
    )


@router.post(
    "/discoveries",
    response_model=DiscoveryView,
    status_code=status.HTTP_201_CREATED,
    summary="Read the Cluster's configuration, changing nothing",
)
async def create_discovery(
    store: DiscoveryStoreDep,
    kubernetes: KubernetesDep,
    trino: TrinoDep,
    settings: SettingsDep,
) -> DiscoveryView:
    """Reads and records. Touches no part of the Cluster and not the Candidate.

    Re-reading keeps whatever has been amended and reports what the Cluster changed, because
    an Admin re-reads precisely when they are unsure -- the worst moment to take their work
    away.
    """
    try:
        read = await discover(kubernetes, settings, trino)
    except DiscoveryFailed as failed:
        raise Conflict(str(failed)) from failed
    return _view(await store.record(read))


@router.get("/discovery", response_model=DiscoveryView, summary="The current discovery")
async def get_discovery(store: DiscoveryStoreDep) -> DiscoveryView:
    stored = await store.load()
    if stored is None:
        raise NotFound("No discovery has been read. POST /admin/adoption/discoveries first.")
    return _view(stored)


@router.delete(
    "/discovery",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Discard the discovery and everything supplied for it",
)
async def discard_discovery(store: DiscoveryStoreDep) -> None:
    """Discards Apchi's reading and the amendments with it. Nothing on the Cluster is
    touched -- a discovery never reached it."""
    await store.discard()


@router.put(
    "/discovery/catalogs/{name}",
    response_model=DiscoveryView,
    summary="Supply the properties of a Catalog Apchi could not read",
)
async def amend_catalog(name: str, write: CatalogWrite, store: DiscoveryStoreDep) -> DiscoveryView:
    """Judged exactly as `POST /catalogs` judges a staged Catalog, through the same code.

    The name in the path wins: an amendment answers a question Apchi asked about a named
    Catalog, so a body naming a different one is a mistake worth refusing rather than
    quietly resolving.
    """
    if write.name != name:
        raise Conflict(
            f"The body names {write.name!r} and the path names {name!r}. An amendment "
            "answers Apchi's question about one Catalog."
        )
    stored = await store.load()
    if stored is None:
        raise NotFound("No discovery has been read. POST /admin/adoption/discoveries first.")
    return _view(await store.amend(CATALOGS, name, validated_catalog(write)))


@router.delete(
    "/discovery/{section}/{resource}",
    response_model=DiscoveryView,
    summary="Withdraw something supplied for a discovery",
)
async def withdraw_amendment(
    section: SectionName, resource: str, store: DiscoveryStoreDep
) -> DiscoveryView:
    try:
        return _view(await store.withdraw(section, resource))
    except LookupError as exc:
        raise NotFound(
            "No discovery has been read. POST /admin/adoption/discoveries first."
        ) from exc
    except KeyError as exc:
        raise NotFound(f"Nothing was supplied for {resource!r} in {section!r}.") from exc
