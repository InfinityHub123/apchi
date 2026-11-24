"""Rendering Client Certificates to the files Trino reads.

Two files per certificate, in one directory, in one Secret. ADR-0005 is the whole design:
the directory is mounted once and never changes, so adding a certificate adds a file to a
mount that is already there -- no pod spec change, no Rollout, no queries destroyed because
somebody uploaded a certificate.
"""

from app.sections.base import Resources

#: Where the Admin mounts the Secret, on the coordinator and on every worker. Workers open
#: their own connections to data sources, so a certificate only the coordinator can read is
#: a catalog that works for metadata and fails for data.
MOUNT_DIR = "/etc/trino/certs"

CERTIFICATE_SUFFIX = ".crt"
KEY_SUFFIX = ".key"


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
