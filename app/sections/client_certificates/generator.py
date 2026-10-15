"""Rendering Client Certificates to the files Trino reads.

Two files per certificate, in one directory, in one Secret. ADR-0005 is the whole design:
the directory is mounted once and never changes, so adding a certificate adds a file to a
mount that is already there -- no pod spec change, no Rollout, no queries destroyed because
somebody uploaded a certificate.
"""

import re
from collections.abc import Mapping
from typing import Any

from app.sections.base import Resources

#: Where the Admin mounts the Secret, on the coordinator and on every worker. Workers open
#: their own connections to data sources, so a certificate only the coordinator can read is
#: a catalog that works for metadata and fails for data.
MOUNT_DIR = "/etc/trino/certs"

CERTIFICATE_SUFFIX = ".crt"
KEY_SUFFIX = ".key"

#: The same constraint the API applies to a name an Operator chooses, as a pattern, so a
#: filename on the Cluster that could not have come from Apchi is reported rather than
#: staged under a name the API would later refuse.
CERTIFICATE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")


def certificate_path(name: str) -> str:
    return f"{MOUNT_DIR}/{name}{CERTIFICATE_SUFFIX}"


def key_path(name: str) -> str:
    return f"{MOUNT_DIR}/{name}{KEY_SUFFIX}"


def render(desired: Resources) -> dict[str, str]:
    """Every staged certificate, as a pair of files.

    The private key is PKCS#8 PEM whatever was uploaded, normalised when the archive was
    read, so a consumer of these files has one format to handle rather than three.
    """
    files: dict[str, str] = {}
    for name in sorted(desired):
        stored = desired[name]
        files[certificate_path(name)] = stored["certificate"]
        files[key_path(name)] = stored["private_key"]
    return files


def rendered_size(desired: Resources) -> int:
    """How much of the Secret the staged certificates would take."""
    return sum(len(content.encode()) for content in render(desired).values())


def name_of(path: str) -> tuple[str, str] | None:
    """The certificate a path belongs to, and which half of the pair it is."""
    if not path.startswith(f"{MOUNT_DIR}/"):
        return None
    filename = path[len(MOUNT_DIR) + 1 :]
    for suffix, half in ((CERTIFICATE_SUFFIX, "certificate"), (KEY_SUFFIX, "private_key")):
        if filename.endswith(suffix):
            return filename[: -len(suffix)], half
    return None


def parse(files: Mapping[str, str]) -> tuple[Resources, list[tuple[str, str, Any]]]:
    """The inverse of `render`: a directory of PEM files back into staged certificates.

    Returns the resources and unaccounted entries as (path, description, content).

    A certificate is a **pair**, and a half of one is not a certificate Apchi can hold: a
    `.crt` with no `.key` cannot be presented to a data source, and a `.key` with no `.crt`
    is key material with nothing to identify it. Both are reported rather than kept, and the
    lone key's content is deliberately left out of the report -- unaccounted entries are
    carried around and eventually shown to somebody, and private key material does not
    belong in any of that (§13.2).

    Nothing here is validated against the certificate: that the PEM parses and that the pair
    matches is the Section's `check`, which already exists and is where a bad pair is caught.
    """
    halves: dict[str, dict[str, str]] = {}
    unaccounted: list[tuple[str, str, Any]] = []

    for path in sorted(files):
        found = name_of(path)
        if found is None:
            unaccounted.append(
                (path, "a file in the certificate directory Apchi does not own", None)
            )
            continue
        name, half = found
        if not CERTIFICATE_NAME.fullmatch(name):
            unaccounted.append((path, f"{name!r} is not a name Apchi can hold", None))
            continue
        halves.setdefault(name, {})[half] = files[path]

    resources: Resources = {}
    for name in sorted(halves):
        pair = halves[name]
        if "certificate" not in pair:
            unaccounted.append(
                (key_path(name), f"{name!r} has a private key and no certificate", None)
            )
            continue
        if "private_key" not in pair:
            unaccounted.append(
                (certificate_path(name), f"{name!r} has a certificate and no private key", None)
            )
            continue
        resources[name] = {"certificate": pair["certificate"], "private_key": pair["private_key"]}
    return resources, unaccounted
