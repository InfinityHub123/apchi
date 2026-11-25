"""Trino's own server configuration: what an Admin sets, and the files Apchi renders from it.

Apchi renders every file under `/etc/trino`, and the chart renders none (ADR-0010, §14). The
Sections cover catalogs, rules, mapping, resource groups and event listeners. This is the rest:
`config.properties`, `jvm.config`, `node.properties`, `log.properties`,
`catalog-store.properties` and `access-control.properties` for the coordinator, and the
workers' `config.properties`, `jvm.config`, `node.properties`, `log.properties` and the
access control that lets Apchi's identity drain them.

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

import json
import re
from typing import ClassVar, Final, Literal

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

#: The workers' access-control rules. Theirs alone: the coordinator's are the Operator's
#: Permissions, which workers never read -- authorization happens on the coordinator.
WORKER_RULES_PATH: Final = f"{CONFIG_DIR}/worker-rules.json"

Role = Literal["coordinator", "worker"]

#: Every file this module renders for each role, by the path Trino reads it at.
ROLE_PATHS: Final[dict[Role, tuple[str, ...]]] = {
    "coordinator": (
        CONFIG_PROPERTIES,
        JVM_CONFIG,
        NODE_PROPERTIES,
        LOG_PROPERTIES,
        CATALOG_STORE_PROPERTIES,
        ACCESS_CONTROL_PATH,
    ),
    "worker": (
        CONFIG_PROPERTIES,
        JVM_CONFIG,
        NODE_PROPERTIES,
        LOG_PROPERTIES,
        ACCESS_CONTROL_PATH,
        WORKER_RULES_PATH,
    ),
}

#: Every path this module renders on either role. A Raw File at any of these is refused, and
#: nothing but Apchi's Secrets may be mounted under the directory (§16).
RENDERED_PATHS: Final = tuple(dict.fromkeys(ROLE_PATHS["coordinator"] + ROLE_PATHS["worker"]))

#: The pod variables sizing reaches Trino through. Fixed names: the chart sets them and these
#: files refer to them, and a renamed one is a coordinator that refuses to start with
#: "Environment variable is not set" -- measured.
QUERY_MAX_MEMORY_ENV: Final = "TRINO_QUERY_MAX_MEMORY"
QUERY_MAX_MEMORY_PER_NODE_ENV: Final = "TRINO_QUERY_MAX_MEMORY_PER_NODE"
HEAP_HEADROOM_PER_NODE_ENV: Final = "TRINO_MEMORY_HEAP_HEADROOM_PER_NODE"

#: What owns each property an Admin may not set, as the message refusing it names it. Per role
#: and file, because a key means something only in the file Trino reads it from.
_SIZING = "sizing, which the chart sets beside the memory limit (trino.{role}.memory)"
_NODE_WIRING = {
    "node.environment": "trino.environment",
    "node.data-dir": "the data volume the chart mounts",
    "node.id": "the pod: the image sets it from the hostname",
}
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
        "query.max-memory": _SIZING.format(role="coordinator"),
        "query.max-memory-per-node": _SIZING.format(role="coordinator"),
        "memory.heap-headroom-per-node": _SIZING.format(role="coordinator"),
    },
    "node.properties": _NODE_WIRING,
    "log.properties": {},
}
WORKER_WIRING: Final[dict[str, dict[str, str]]] = {
    "config.properties": {
        "coordinator": "the role this file is rendered for",
        "http-server.http.port": "trino.httpPort",
        "discovery.uri": "the coordinator Service (trino.name, trino.service.port)",
        "catalog.management": "Apchi: catalogs are applied by DDL (§7.1)",
        "catalog.store": "Apchi: catalogs are applied by DDL (§7.1)",
        "query.max-memory": "the coordinator's sizing (trino.coordinator.memory)",
        "query.max-memory-per-node": _SIZING.format(role="worker"),
        "memory.heap-headroom-per-node": _SIZING.format(role="worker"),
        # Has to fit the termination grace period Kubernetes gives the pod, which is the
        # chart's: a drain longer than that is a worker killed mid-task.
        "shutdown.grace-period": "trino.worker.gracefulShutdown.gracePeriodSeconds",
    },
    "node.properties": _NODE_WIRING,
    "log.properties": {},
}
WIRING_BY_ROLE: Final[dict[Role, dict[str, dict[str, str]]]] = {
    "coordinator": WIRING,
    "worker": WORKER_WIRING,
}

#: An Admin's value of these is the default unless they set it. Only properties whose default
#: in Trino would be wrong for the chart's shape: a coordinator that also schedules work
#: competes with the queries it is coordinating.
DEFAULTS: Final[dict[str, dict[str, str]]] = {
    "config.properties": {"node-scheduler.include-coordinator": "false"},
    "node.properties": {},
    "log.properties": {},
}
_NO_DEFAULTS: Final[dict[str, dict[str, str]]] = {
    "config.properties": {},
    "node.properties": {},
    "log.properties": {},
}
DEFAULTS_BY_ROLE: Final[dict[Role, dict[str, dict[str, str]]]] = {
    "coordinator": DEFAULTS,
    "worker": _NO_DEFAULTS,
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


class _RoleFiles(BaseModel):
    """The Admin's properties for one role, one map per file Trino reads."""

    model_config = ConfigDict(
        extra="forbid", validate_by_name=True, validate_by_alias=True, serialize_by_alias=True
    )

    role: ClassVar[Role]

    config_properties: dict[str, str] = Field(
        default_factory=dict,
        alias="config.properties",
        description=(
            "Properties for this role's config.properties. Wiring and sizing are refused: "
            "they belong to the chart."
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
    def _refuse_what_trino_would_misread_or_the_chart_owns(self) -> "_RoleFiles":
        problems: list[str] = []
        for file, properties in self.by_file().items():
            if len(properties) > MAX_PROPERTIES_PER_FILE:
                problems.append(f"{file}: more than {MAX_PROPERTIES_PER_FILE} properties")
            for key, value in properties.items():
                for problem in (_check_key(key), _check_value(key, value)):
                    if problem:
                        problems.append(f"{file}: {problem}")
                if (owner := WIRING_BY_ROLE[self.role][file].get(key)) is not None:
                    problems.append(
                        f"{file}: {key} is wiring and cannot be set here; it comes from {owner}"
                    )
        if problems:
            raise ValueError("; ".join(problems))
        return self


class CoordinatorFiles(_RoleFiles):
    """The Admin's properties for the coordinator."""

    role: ClassVar[Role] = "coordinator"


class WorkerFiles(_RoleFiles):
    """The Admin's properties for every worker: one set, because the workers are one
    Deployment and Trino expects them alike."""

    role: ClassVar[Role] = "worker"


class ServerConfiguration(BaseModel):
    """The Admin's half of Trino's own files."""

    model_config = ConfigDict(extra="forbid")

    coordinator: CoordinatorFiles = Field(default_factory=CoordinatorFiles)
    worker: WorkerFiles = Field(default_factory=WorkerFiles)

    def of(self, role: Role) -> _RoleFiles:
        return self.coordinator if role == "coordinator" else self.worker


def _properties(lines: list[tuple[str, str]]) -> str:
    return "".join(f"{key}={value}\n" for key, value in lines)


def _with_admin(
    file: str,
    wiring: list[tuple[str, str]],
    admin: dict[str, str],
    defaults: dict[str, dict[str, str]] = DEFAULTS,
) -> list[tuple[str, str]]:
    """Wiring first, then the defaults the Admin did not override, then the Admin's keys --
    sorted, so a rendered file diffs stably in Review whatever order they were written in."""
    merged = {**defaults[file], **admin}
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


def _jvm(settings: Settings) -> str:
    jvm = list(_JVM_REQUIRED)
    if settings.trino_java_truststore_path is not None:
        jvm.append(f"-Djavax.net.ssl.trustStore={settings.trino_java_truststore_path}")
    return "".join(f"{flag}\n" for flag in jvm)


def _node_wiring(settings: Settings) -> list[tuple[str, str]]:
    return [
        ("node.environment", settings.trino_environment),
        ("node.data-dir", settings.trino_data_dir),
    ]


def worker_config_wiring(settings: Settings) -> list[tuple[str, str]]:
    """A worker's config.properties lines that the deployment decides."""
    return [
        ("coordinator", "false"),
        ("http-server.http.port", str(settings.trino_http_port)),
        ("discovery.uri", settings.trino_discovery_uri),
        ("catalog.management", "dynamic"),
        ("query.max-memory-per-node", f"${{ENV:{QUERY_MAX_MEMORY_PER_NODE_ENV}}}"),
        ("memory.heap-headroom-per-node", f"${{ENV:{HEAP_HEADROOM_PER_NODE_ENV}}}"),
        ("shutdown.grace-period", settings.trino_worker_shutdown_grace_period),
    ]


def worker_rules(settings: Settings) -> str:
    """The workers' own access control: Apchi's identity may drain one, and nothing else.

    Needed at all because a worker with no access control configured still refuses the drain
    request -- Trino's default denies writing system information (measured on 483: 403). Nothing
    else is granted, and nothing else is needed: authorization happens on the coordinator, and a
    worker with only this rule ran queries as before (measured). Anyone else is refused (403).
    """
    user = f"^{re.escape(settings.trino_user)}$"
    return json.dumps({"system_information": [{"user": user, "allow": ["write"]}]}) + "\n"


def render_worker(admin: ServerConfiguration, settings: Settings) -> dict[str, str]:
    """Every file a worker reads from `/etc/trino`."""
    files = admin.worker.by_file()
    return {
        CONFIG_PROPERTIES: _properties(
            _with_admin(
                "config.properties",
                worker_config_wiring(settings),
                files["config.properties"],
                _NO_DEFAULTS,
            )
        ),
        NODE_PROPERTIES: _properties(
            _with_admin(
                "node.properties", _node_wiring(settings), files["node.properties"], _NO_DEFAULTS
            )
        ),
        LOG_PROPERTIES: _properties(
            _with_admin("log.properties", [], files["log.properties"], _NO_DEFAULTS)
        ),
        JVM_CONFIG: _jvm(settings),
        ACCESS_CONTROL_PATH: _properties(
            [("access-control.name", "file"), ("security.config-file", WORKER_RULES_PATH)]
        ),
        WORKER_RULES_PATH: worker_rules(settings),
    }


def render(role: Role, admin: ServerConfiguration, settings: Settings) -> dict[str, str]:
    return (
        render_coordinator(admin, settings)
        if role == "coordinator"
        else render_worker(admin, settings)
    )


def secret_name(role: Role, settings: Settings) -> str:
    if role == "coordinator":
        return settings.server_configuration_secret_name
    return settings.worker_server_configuration_secret_name


def role_secret(role: Role, admin: ServerConfiguration, settings: Settings) -> dict[str, str]:
    """One role's server-configuration Secret, exactly as Apchi writes it."""
    return {secret_key(path): content for path, content in render(role, admin, settings).items()}


def render_coordinator(admin: ServerConfiguration, settings: Settings) -> dict[str, str]:
    """Every file the coordinator reads from `/etc/trino` that no Section renders."""
    files = admin.coordinator.by_file()
    return {
        CONFIG_PROPERTIES: _properties(
            _with_admin("config.properties", config_wiring(settings), files["config.properties"])
        ),
        NODE_PROPERTIES: _properties(
            _with_admin("node.properties", _node_wiring(settings), files["node.properties"])
        ),
        LOG_PROPERTIES: _properties(_with_admin("log.properties", [], files["log.properties"])),
        JVM_CONFIG: _jvm(settings),
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
    """The Secret key a file is stored under: its name, which is unique within a role."""
    return path.rsplit("/", 1)[-1]


def coordinator_secret(admin: ServerConfiguration, settings: Settings) -> dict[str, str]:
    """The coordinator's server-configuration Secret, exactly as Apchi writes it."""
    return role_secret("coordinator", admin, settings)


def probe_properties(
    admin: ServerConfiguration, role: Role = "coordinator"
) -> dict[str, dict[str, str]]:
    """What the validation probe is given of one role's Admin properties, by file.

    The Admin's keys, without wiring or sizing: the probe has wiring of its own, and has no
    sizing variables -- a reference to one would stop it with "Environment variable is not set",
    and its small heap would refuse any real memory setting. Those are proved on the Cluster.
    """
    return {file: dict(properties) for file, properties in admin.of(role).by_file().items()}
