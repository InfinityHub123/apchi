"""Trino's own server configuration: what an Admin sets, and the files Apchi renders from it.

Apchi renders every file under `/etc/trino`, and the chart renders none (ADR-0010, §14). The
Sections cover catalogs, rules, mapping, resource groups and event listeners. This is the rest:
`config.properties`, `jvm.config`, `node.properties`, `log.properties`,
`catalog-store.properties` and `access-control.properties` -- the coordinator's in this stage,
the workers' in the next.

Three owners meet in these files, and a line always has exactly one of them:

* **Wiring** names something the chart creates -- a port, a path, a Service. It arrives as a
  setting, Apchi renders it, and no Admin input may set it, because an Apply could otherwise
  point Trino at a port the Service does not expose.
* **Sizing** has to fit the memory limit the chart sets, so it is the chart's too, and reaches
  Trino on the pod itself: the heap through `JDK_JAVA_OPTIONS`, query memory through variables
  these files refer to by `${ENV:...}`. Measured on Trino 483: `jvm.config` is read after
  `JDK_JAVA_OPTIONS` and its last `-Xmx` wins, so `jvm.config` carries no heap flag at all.
* **Everything else** is the Admin's: a map of properties per file, with no schema. A model of
  Trino's properties would be a second, worse copy that is wrong after every upgrade, so Trino
  judges them -- on the validation probe before an Apply, and on the Cluster after.

Apchi does check the Admin's input where nothing else would: a line break that would add a
hidden line, a key a properties parser would read as two, and a wiring key. Each of those is a
failure no rollback would ever see, or one that crosses into the chart's territory.
"""

import re
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import Settings

#: Where the Sections' files are, as their generators define them. Spelled out rather than
#: imported: the generators import the Section base, which imports the Admin values this
#: module is part of. A tier 1 test holds the two spellings equal.
USER_MAPPING_PATH: Final = "/etc/trino/user-mapping/user-mapping.json"
RULES_PATH: Final = "/etc/trino/access-control/rules.json"
ACCESS_CONTROL_PATH: Final = "/etc/trino/access-control.properties"
REFRESH_PERIOD: Final = "security.refresh-period"

CONFIG_DIR: Final = "/etc/trino"
CONFIG_PROPERTIES: Final = f"{CONFIG_DIR}/config.properties"
JVM_CONFIG: Final = f"{CONFIG_DIR}/jvm.config"
NODE_PROPERTIES: Final = f"{CONFIG_DIR}/node.properties"
LOG_PROPERTIES: Final = f"{CONFIG_DIR}/log.properties"
CATALOG_STORE_PROPERTIES: Final = f"{CONFIG_DIR}/catalog-store.properties"

#: Every file this module renders, by the path Trino reads it at. A Raw File at any of these is
#: refused, and nothing but Apchi's Secrets may be mounted under the directory (§16).
RENDERED_PATHS: Final = (
    CONFIG_PROPERTIES,
    JVM_CONFIG,
    NODE_PROPERTIES,
    LOG_PROPERTIES,
    CATALOG_STORE_PROPERTIES,
    ACCESS_CONTROL_PATH,
)

#: The pod variables sizing reaches Trino through. Fixed names: the chart sets them and these
#: files refer to them, and a renamed one is a coordinator that refuses to start with
#: "Environment variable is not set" -- measured.
QUERY_MAX_MEMORY_ENV: Final = "TRINO_QUERY_MAX_MEMORY"
QUERY_MAX_MEMORY_PER_NODE_ENV: Final = "TRINO_QUERY_MAX_MEMORY_PER_NODE"
HEAP_HEADROOM_PER_NODE_ENV: Final = "TRINO_MEMORY_HEAP_HEADROOM_PER_NODE"

#: What owns each property an Admin may not set, as the message refusing it names it. Per file,
#: because a key means something only in the file Trino reads it from.
_SIZING = "sizing, which the chart sets beside the memory limit (trino.coordinator.memory)"
WIRING: Final[dict[str, dict[str, str]]] = {
    "config.properties": {
        "coordinator": "the role this file is rendered for",
        "http-server.http.port": "trino.httpPort",
        "discovery.uri": "the coordinator Service (trino.name, trino.service.port)",
        "catalog.management": "Apchi: catalogs are applied by DDL (§7.1)",
        "catalog.store": "Apchi: catalogs are applied by DDL (§7.1)",
        "http-server.authentication.insecure.user-mapping.file": "trino.authentication.type",
        "http-server.authentication.certificate.user-mapping.file": "trino.authentication.type",
        "http-server.process-forwarded": "expose.trino",
        "http-server.https.enabled": "trino.https",
        "http-server.https.port": "trino.https.port",
        "http-server.https.keystore.path": "trino.https",
        "query.max-memory": _SIZING,
        "query.max-memory-per-node": _SIZING,
        "memory.heap-headroom-per-node": _SIZING,
    },
    "node.properties": {
        "node.environment": "trino.environment",
        "node.data-dir": "the data volume the chart mounts",
        "node.id": "the pod: the image sets it from the hostname",
    },
    "log.properties": {},
}

#: An Admin's value of these is the default unless they set it. Only properties whose default
#: in Trino would be wrong for the chart's shape: a coordinator that also schedules work
#: competes with the queries it is coordinating.
DEFAULTS: Final[dict[str, dict[str, str]]] = {
    "config.properties": {"node-scheduler.include-coordinator": "false"},
    "node.properties": {},
    "log.properties": {},
}

#: Flags Trino or the chart's shape require, and nothing else. Every JVM *tuning* flag is the
#: chart's and travels in `JDK_JAVA_OPTIONS`, so none is pinned here, where it would override
#: the chart's choice: the JVM reads this file after the variable and the last flag wins.
_JVM_REQUIRED: Final = ("-Djdk.attach.allowAttachSelf=true",)

#: Enough for a configuration accumulated over years, few enough that a file stays readable.
MAX_PROPERTIES_PER_FILE: Final = 500
MAX_VALUE_BYTES: Final = 16 * 1024

#: A key a Java properties parser reads as one key. Whitespace, `=` and `:` end a key, a
#: leading `#` or `!` makes the line a comment, and a backslash is an escape -- each would make
#: the file Trino reads differ from the map the Admin wrote.
_KEY = re.compile(r"[^\s=:#!\\][^\s=:\\]*")


def _check_key(key: str) -> str | None:
    if not key:
        return "a property name cannot be empty"
    if not _KEY.fullmatch(key):
        return (
            f"{key!r} is not a property name Trino would read as written: it may not contain "
            "whitespace, '=', ':' or '\\', nor start with '#' or '!'"
        )
    return None


def _check_value(key: str, value: str) -> str | None:
    if "\n" in value or "\r" in value:
        # The one that matters most: a value with a line break in it adds a line of its own to
        # the file, and that line can be wiring -- `2h\ncoordinator=false`.
        return f"the value of {key!r} contains a line break, which would add a hidden line"
    trailing = len(value) - len(value.rstrip("\\"))
    if trailing % 2:
        # An odd run of trailing backslashes continues the line onto the next one, which joins
        # the next property into this value.
        return f"the value of {key!r} ends in a backslash, which joins the next line onto it"
    if len(value.encode()) > MAX_VALUE_BYTES:
        return f"the value of {key!r} is longer than {MAX_VALUE_BYTES} bytes"
    return None


class CoordinatorFiles(BaseModel):
    """The Admin's properties for the coordinator, one map per file Trino reads."""

    model_config = ConfigDict(
        extra="forbid", validate_by_name=True, validate_by_alias=True, serialize_by_alias=True
    )

    config_properties: dict[str, str] = Field(
        default_factory=dict,
        alias="config.properties",
        description=(
            "Properties for the coordinator's config.properties. Wiring and sizing are "
            "refused: they belong to the chart."
        ),
    )
    node_properties: dict[str, str] = Field(
        default_factory=dict, alias="node.properties", description="Extra node.properties."
    )
    log_properties: dict[str, str] = Field(
        default_factory=dict,
        alias="log.properties",
        description='Log levels, by logger name: {"io.trino.plugin.postgresql": "DEBUG"}.',
    )

    def by_file(self) -> dict[str, dict[str, str]]:
        return {
            "config.properties": self.config_properties,
            "node.properties": self.node_properties,
            "log.properties": self.log_properties,
        }

    @model_validator(mode="after")
    def _refuse_what_trino_would_misread_or_the_chart_owns(self) -> "CoordinatorFiles":
        problems: list[str] = []
        for file, properties in self.by_file().items():
            if len(properties) > MAX_PROPERTIES_PER_FILE:
                problems.append(f"{file}: more than {MAX_PROPERTIES_PER_FILE} properties")
            for key, value in properties.items():
                for problem in (_check_key(key), _check_value(key, value)):
                    if problem:
                        problems.append(f"{file}: {problem}")
                if (owner := WIRING[file].get(key)) is not None:
                    problems.append(
                        f"{file}: {key} is wiring and cannot be set here; it comes from {owner}"
                    )
        if problems:
            raise ValueError("; ".join(problems))
        return self


class ServerConfiguration(BaseModel):
    """The Admin's half of Trino's own files. Workers arrive in the next stage."""

    model_config = ConfigDict(extra="forbid")

    coordinator: CoordinatorFiles = Field(default_factory=CoordinatorFiles)


def _properties(lines: list[tuple[str, str]]) -> str:
    return "".join(f"{key}={value}\n" for key, value in lines)


def _with_admin(
    file: str, wiring: list[tuple[str, str]], admin: dict[str, str]
) -> list[tuple[str, str]]:
    """Wiring first, then the defaults the Admin did not override, then the Admin's keys --
    sorted, so a rendered file diffs stably in Review whatever order they were written in."""
    merged = {**DEFAULTS[file], **admin}
    return [*wiring, *sorted(merged.items())]


def config_wiring(settings: Settings) -> list[tuple[str, str]]:
    """The coordinator's config.properties lines that the deployment decides."""
    lines = [
        ("coordinator", "true"),
        ("http-server.http.port", str(settings.trino_http_port)),
        ("discovery.uri", settings.trino_discovery_uri),
        ("catalog.management", "dynamic"),
        ("catalog.store", "file"),
    ]
    # Which authenticator reads the mapping file is the chart's, because the file is mounted
    # for it. Turning the authenticator on -- `http-server.authentication.type` and the
    # truststore it validates client certificates against -- is the Admin's, as it was.
    variant = settings.trino_authentication_type
    lines.append((f"http-server.authentication.{variant}.user-mapping.file", USER_MAPPING_PATH))
    if settings.trino_process_forwarded:
        lines.append(("http-server.process-forwarded", "true"))
    if settings.trino_https_port is not None:
        lines += [
            ("http-server.https.enabled", "true"),
            ("http-server.https.port", str(settings.trino_https_port)),
            ("http-server.https.keystore.path", settings.trino_https_keystore_path),
        ]
    # Sizing, by reference. The values are on the pod, beside the memory limit they have to fit.
    lines += [
        ("query.max-memory", f"${{ENV:{QUERY_MAX_MEMORY_ENV}}}"),
        ("query.max-memory-per-node", f"${{ENV:{QUERY_MAX_MEMORY_PER_NODE_ENV}}}"),
        ("memory.heap-headroom-per-node", f"${{ENV:{HEAP_HEADROOM_PER_NODE_ENV}}}"),
    ]
    return lines


def render_coordinator(admin: ServerConfiguration, settings: Settings) -> dict[str, str]:
    """Every file the coordinator reads from `/etc/trino` that no Section renders."""
    files = admin.coordinator.by_file()
    jvm = list(_JVM_REQUIRED)
    if settings.trino_java_truststore_path is not None:
        jvm.append(f"-Djavax.net.ssl.trustStore={settings.trino_java_truststore_path}")
    return {
        CONFIG_PROPERTIES: _properties(
            _with_admin("config.properties", config_wiring(settings), files["config.properties"])
        ),
        NODE_PROPERTIES: _properties(
            _with_admin(
                "node.properties",
                [
                    ("node.environment", settings.trino_environment),
                    ("node.data-dir", settings.trino_data_dir),
                ],
                files["node.properties"],
            )
        ),
        LOG_PROPERTIES: _properties(_with_admin("log.properties", [], files["log.properties"])),
        JVM_CONFIG: "".join(f"{flag}\n" for flag in jvm),
        # The store's directory is the chart's emptyDir, which the seed initContainer fills.
        CATALOG_STORE_PROPERTIES: f"catalog.config-dir={settings.catalog_store_dir}\n",
        ACCESS_CONTROL_PATH: _properties(
            [
                ("access-control.name", "file"),
                ("security.config-file", RULES_PATH),
                (REFRESH_PERIOD, settings.trino_security_refresh_period),
            ]
        ),
    }


def secret_key(path: str) -> str:
    """The Secret key a file is stored under: its name, which is unique among these six."""
    return path.rsplit("/", 1)[-1]


def coordinator_secret(admin: ServerConfiguration, settings: Settings) -> dict[str, str]:
    """The coordinator's server-configuration Secret, exactly as Apchi writes it."""
    rendered = render_coordinator(admin, settings)
    return {secret_key(path): content for path, content in rendered.items()}


def probe_properties(admin: ServerConfiguration) -> dict[str, dict[str, str]]:
    """What the validation probe is given of the Admin's properties, by file.

    The Admin's keys, without wiring or sizing: the probe has wiring of its own, and has no
    sizing variables -- a reference to one would stop it with "Environment variable is not set",
    and its small heap would refuse any real memory setting. Those are proved on the Cluster.
    """
    return {file: dict(properties) for file, properties in admin.coordinator.by_file().items()}
