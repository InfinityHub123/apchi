"""Referring to a Client Certificate from a Catalog.

Two ways, because Trino has no general property for this and the connectors do not agree.
PostgreSQL wants JDBC query parameters inside `connection-url`; MySQL wants a keystore;
others differ again.

**A token**, `${cert:name}` or `${key:name}`, works anywhere a path would go. The Operator
writes the property their connector documents and Apchi substitutes the path, so nobody
types `/etc/trino/certs` and Apchi does not have to know what the surrounding property
means.

**A `certificate` field**, for the connectors Apchi knows how to wire. Less to get right,
and refused for a connector whose convention Apchi does not know rather than guessed at --
with the token named as the way to do it by hand.
"""

import re
from urllib.parse import parse_qsl, urlsplit

from app.sections.client_certificates.generator import certificate_path, key_path

#: `${cert:finance}` and `${key:finance}`. Deliberately not Trino's own `${ENV:...}` shape:
#: Apchi expands these before Trino ever sees the file.
TOKEN = re.compile(r"\$\{(cert|key):([A-Za-z0-9][A-Za-z0-9._-]*)\}")

#: The connectors whose way of naming a client certificate Apchi knows. PostgreSQL carries
#: it as JDBC parameters on the connection URL. MySQL and SQL Server want a keystore, which
#: is a file Apchi does not build, so they are deliberately absent.
WIRED = ("postgresql",)

#: What Apchi adds for a wired connector, and what the Operator must have decided already.
_SSL_MODE = "sslmode"


def referenced(properties: dict[str, str], certificate: str | None) -> set[str]:
    """Every certificate a Catalog names, however it names it."""
    names = {
        name for _, name in (m.groups() for m in TOKEN.finditer(" ".join(properties.values())))
    }
    return names | ({certificate} if certificate else set())


def expand(properties: dict[str, str]) -> dict[str, str]:
    """Replace every token with the path the file will be at."""

    def path(match: re.Match[str]) -> str:
        kind, name = match.groups()
        return certificate_path(name) if kind == "cert" else key_path(name)

    return {key: TOKEN.sub(path, value) for key, value in properties.items()}


def wire(connector: str, properties: dict[str, str], certificate: str) -> dict[str, str]:
    """Put the certificate into the properties the way this connector expects.

    Only for a connector in `WIRED`. The caller refuses the rest, because a certificate
    silently not wired is a Catalog that connects without one.
    """
    if connector != "postgresql":
        raise ValueError(f"Apchi does not know how {connector!r} names a client certificate")
    url = properties["connection-url"]
    joiner = "&" if "?" in url else "?"
    return {
        **properties,
        "connection-url": (
            f"{url}{joiner}sslcert={certificate_path(certificate)}&sslkey={key_path(certificate)}"
        ),
    }


def ssl_is_configured(connector: str, properties: dict[str, str]) -> bool:
    """Whether the Operator has already said how TLS should behave.

    Apchi adds the certificate and the key, and stops there. `sslmode` decides whether the
    connection is encrypted at all and whether the server is verified -- a security posture,
    and not one Apchi should pick on an Operator's behalf.
    """
    if connector != "postgresql":
        return True
    url = properties.get("connection-url", "")
    query = urlsplit(url.removeprefix("jdbc:")).query
    return any(key == _SSL_MODE for key, _ in parse_qsl(query))
