"""Rendering Catalogs to the durable form the Secret holds.

Trino maintains the coordinator's store directory itself, as a side effect of the
DDL Apchi issues. This renders the same catalogs into .properties files for the
Secret, which is the copy that survives the pod.
"""

from typing import Any

from app.sections import SectionName
from app.sections.catalogs.section import SECTION


def render_properties(connector: str, properties: dict[str, str]) -> str:
    lines = [f"connector.name={connector}"]
    lines.extend(f"{key}={value}" for key, value in sorted(properties.items()))
    return "\n".join(lines) + "\n"


def render_secret(sections: dict[SectionName, dict[str, Any]]) -> dict[str, str]:
    """The Secret's full contents: one .properties key per Catalog.

    The whole Secret is rendered rather than patched key by key, so a Catalog
    removed from the Candidate disappears from the durable copy too -- otherwise a
    dropped catalog would return at the next pod restart.
    """
    catalogs = sections.get(SECTION, {})
    return {
        f"{name}.properties": render_properties(stored["connector"], stored.get("properties", {}))
        for name, stored in sorted(catalogs.items())
    }
