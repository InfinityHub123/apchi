"""Rendering Catalogs to the durable form the Secret holds.

Trino maintains the coordinator's store directory itself, as a side effect of the
DDL Apchi issues. This renders the same catalogs into .properties files for the
Secret, which is the copy that survives the pod.
"""

from typing import Any

from app.sections import SectionName
from app.sections.catalogs import SECTION
from app.sections.catalogs.certificates import expand, wire


def effective(stored: dict[str, Any]) -> dict[str, str]:
    """The properties Trino should receive, from what the Operator wrote.

    One function, used by both the DDL and the Secret. The two copies of a Catalog have to
    say the same thing -- a catalog that works now and comes back different at the next pod
    start is the silent, weeks-later failure §7.5 is built around avoiding -- and the surest
    way to keep them identical is for neither to compute this itself.
    """
    properties: dict[str, str] = dict(stored.get("properties", {}))
    if certificate := stored.get("certificate"):
        properties = wire(stored["connector"], properties, certificate)
    return expand(properties)


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
        f"{name}.properties": render_properties(stored["connector"], effective(stored))
        for name, stored in sorted(catalogs.items())
    }


class Unreadable(Exception):
    """A file this module cannot read. Translated by the Section, so the generator keeps
    knowing nothing about the pipeline."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


SUFFIX = ".properties"


def name_of(path: str, directory: str) -> str | None:
    """The catalog a path in the store directory belongs to."""
    prefix = f"{directory.rstrip('/')}/"
    if not path.startswith(prefix) or not path.endswith(SUFFIX):
        return None
    return path[len(prefix) : -len(SUFFIX)] or None


def parse_properties(path: str, content: str) -> tuple[str, dict[str, str], list[str]]:
    """The inverse of `render_properties`: the connector and the rest of the properties.

    Returns the connector, the properties, and descriptions of anything a round trip would
    lose -- comments, which Apchi cannot write back.
    """
    connector: str | None = None
    properties: dict[str, str] = {}
    lost: list[str] = []
    for number, raw in enumerate(content.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            lost.append(f"a comment on line {number}")
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise Unreadable(path, f"line {number} is not key=value: {line!r}")
        key, value = key.strip(), value.strip()
        if key == "connector.name":
            connector = value
            continue
        properties[key] = value
    if connector is None:
        raise Unreadable(path, "there is no connector.name, so nothing says what this catalog is")
    return connector, properties, lost
