"""Runtime settings. Everything here is overridable without a rebuild."""

import difflib
import os
import re
from collections.abc import Mapping
from enum import StrEnum
from functools import lru_cache
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: What Kubernetes injects into every pod for every Service in its namespace, unasked: a
#: Service named `apchi` -- which is what the chart names its own -- yields `APCHI_PORT`,
#: `APCHI_SERVICE_HOST`, `APCHI_PORT_8000_TCP_ADDR` and more. Measured on a cluster with the
#: chart installed: eighteen of them, none Apchi's. They share the prefix by accident, so they
#: are not settings anybody mistyped. The chart turns service links off as well; this is for
#: the deployments that do not use it.
_SERVICE_LINK = re.compile(
    r"\A[A-Z0-9_]+_(SERVICE_HOST|SERVICE_PORT(_[A-Z0-9_]+)?|PORT(_\d+_(TCP|UDP|SCTP)(_ADDR|_PORT|_PROTO)?)?)\Z"
)

_PREFIX = "APCHI_"


class AuthMode(StrEnum):
    """How a caller proves who they are (§19, #128).

    `NONE` is the default because it is what Apchi does today -- every endpoint open -- and
    anything else would break every existing install on upgrade. Tokens are not a mode: they
    are a credential an authenticated user mints, so they need one of the others.
    """

    NONE = "none"
    PASSWORD = "password"
    SSO = "sso"


#: Where the CA bundle and the Java truststore are mounted, in every pod that gets them. The
#: bundle at the path RHEL-based images, Trino's among them, read their system trust from; the
#: truststore in a directory of its own, so that a mount over a file the image already has --
#: `/etc/pki/java/cacerts` is a symlink in trinodb/trino -- cannot land somewhere unexpected.
CA_BUNDLE_PATH = "/etc/pki/tls/certs/ca-bundle.crt"
TRUSTSTORE_DIR = "/etc/pki/java/truststore"
TRUSTSTORE_PATH = f"{TRUSTSTORE_DIR}/cacerts"


class MountedFile(BaseModel):
    """One key of a ConfigMap or a Secret, which Apchi mounts into the pods it creates."""

    model_config = ConfigDict(extra="forbid")

    config_map: str | None = None
    secret: str | None = None
    key: str

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if (self.config_map is None) == (self.secret is None):
            raise ValueError("name exactly one of config_map and secret")
        return self


class Truststore(MountedFile):
    """A Java truststore, and the Secret holding its password if it needs one to load.

    Usually it does not: Java reads the certificates in a truststore without its password,
    which only protects the store's integrity.
    """

    password_secret: str | None = None
    password_key: str | None = None

    @model_validator(mode="after")
    def _password_whole(self) -> Self:
        if (self.password_secret is None) != (self.password_key is None):
            raise ValueError("password_secret and password_key go together")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix=_PREFIX, env_file=".env", extra="ignore")

    #: Deprecated, and read only for the log defaults it used to decide; removed in the next
    #: release. It tied three unrelated behaviours to one tier name, and refused at startup
    #: any name outside four -- so an installer with tiers of their own crashlooped on a
    #: label. Any value is accepted now, and Apchi warns while it is set. See
    #: `effective_log_level`.
    environment: str | None = None

    #: Defaults to `none`, which resolves every caller to an Admin: that is the behaviour
    #: Apchi has today, and a default of anything else would lock existing installs out of
    #: their own Admin surface on upgrade.
    auth_mode: AuthMode = AuthMode.NONE
    #: The one Admin a new install starts with, in `password` mode. A *hash* rather than a
    #: password, so the plaintext is never in values, a Helm release, or `helm get values`.
    #: Applied only when the user collection is empty, so it cannot resurrect a deleted
    #: account or reset a changed password on a restart.
    auth_initial_admin_username: str = "admin"
    auth_initial_admin_password_hash: str | None = None
    #: How long a browser session lasts. Sessions are rows, so logging out or disabling a
    #: user takes effect at once; this is only how long an untouched one survives.
    auth_session_hours: float = 12.0

    #: The identity provider, in `sso` mode. Every endpoint Apchi uses is discovered from
    #: here rather than configured: a provider that rotates its keys or moves an endpoint
    #: keeps working, and a mistyped endpoint is a failure at the worst possible moment.
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    #: Where the provider sends the browser back. Deployment-specific, and a wrong one is a
    #: login loop with no error anywhere, so the chart spells out the consequence.
    oidc_redirect_url: str | None = None
    #: `groups` is not a standard OIDC scope, and whether a provider needs one to emit the
    #: role claim differs: Dex and Keycloak want it, Entra puts roles in the token with no
    #: extra scope at all. Configurable because no single default is right everywhere.
    oidc_scopes: str = "openid profile email groups"
    #: Which claim names the person. `sub` is the only claim OIDC guarantees is stable, but
    #: it is routinely an opaque blob -- Dex returns `Cg0wLTM4NS0yODA4OS0wEgRtb2Nr` -- and
    #: §20's audit records an actor somebody has to be able to read. So: a login name, and a
    #: login whose token lacks that claim is refused rather than recorded under a blob.
    #:
    #: The consequence, stated because it is not recoverable afterwards: this value is the
    #: Apchi username, so a provider that *changes* somebody's login name produces a new
    #: Apchi user. Their tokens (#131) stay with the old name for an Admin to revoke.
    oidc_username_claim: str = "preferred_username"
    #: Which claim carries group membership, and which of its values mean Admin. Comma
    #: separated rather than a JSON list: pydantic-settings parses a list-typed setting as
    #: JSON, and `["a","b"]` inside a Helm value is a quoting trap nobody needs.
    oidc_role_claim: str = "groups"
    oidc_admin_groups: str = ""
    #: How long a browser has to come back from the provider. Short: this covers one
    #: redirect round trip, and the pending login it keeps is what makes a replayed `state`
    #: refusable.
    oidc_login_seconds: float = 300.0

    #: How long §20's audit history is kept. **Unset means forever, and that is the default
    #: on purpose**: an audit trail that silently deletes itself is worse than one that grows,
    #: because the first question an incident asks is what happened before the window you
    #: kept. Setting this creates a TTL index, so choosing to discard history is a decision
    #: somebody made rather than one they inherited.
    audit_retention_days: float | None = None

    #: The most the Configuration Candidate may occupy. It is **one** MongoDB document by
    #: design (ADR-0002, §4), and MongoDB caps a document at 16MB -- so this is a property of
    #: that decision rather than an arbitrary number.
    #:
    #: Below 16MB on purpose. BSON overhead means the serialised size is not the document
    #: size, and a guard that only just fits is a guard that sometimes does not. The headroom
    #: also leaves room for the Snapshot an Apply makes of the same content.
    candidate_max_bytes: int = 15 * 1024 * 1024

    #: Holds every raw file an Admin wrote (§14). One Secret for all of them, mounted one
    #: file at a time: the paths are arbitrary under /etc/trino, so a directory mount would
    #: cover files Apchi and the Admin own between them.
    raw_files_secret_name: str = "trino-admin-raw-files"
    #: The one volume those mounts name. Apchi adds it with the first raw file and removes it
    #: with the last, the same way it owns the Resource Groups volume.
    raw_files_volume_name: str = "apchi-admin-raw-files"

    #: Whether a Catalog the validation probe cannot connect to fails the Apply.
    #:
    #: Off, because the probe's network is not the coordinator's. §6 already warns that a
    #: validation pod must reach the data sources a catalog references "or catalog validation
    #: fails for reasons unrelated to the configuration" -- and a firewall between the probe
    #: and a database would then block every Apply for a configuration that works. So the
    #: default reports and the Operator decides. Turn it on where the two are known to share
    #: a network and an unreachable catalog should stop a release.
    catalog_connectivity_blocks_apply: bool = False

    #: A CA to trust **in addition to** the public store, for every outbound TLS connection
    #: Apchi makes: the Trino coordinator, MongoDB, the OIDC provider and the alert webhook.
    #:
    #: One setting and not one per destination, because a CA bundle is conventionally one
    #: thing and an organisation with an internal PKI has one root for all four. Passed to
    #: every client explicitly rather than left to the environment: Apchi reaches those four
    #: through three libraries, and `requests` reads `REQUESTS_CA_BUNDLE` while httpx reads
    #: `SSL_CERT_FILE`, so one destination could need two variables set. See `app.trust`.
    ca_file: str | None = None

    mongo_uri: str = "mongodb://localhost:27017"
    mongo_database: str = "apchi"
    #: One PEM holding the certificate Apchi authenticates with **and its key**, which is the
    #: shape PyMongo's `tlsCertificateKeyFile` takes -- unlike the Trino client, which wants
    #: the two apart (#188). It turns TLS on by itself: PyMongo builds an SSL context from it
    #: and does not also need `tls=true`, verified against a mongod
    #: running `--tlsMode requireTLS`.
    #:
    #: How to authenticate stays in `mongo_uri` -- `?authMechanism=MONGODB-X509` -- because
    #: that is where PyMongo already reads it, and because an x509 URI carries no secret at
    #: all, unlike the SCRAM password that otherwise sits in `helm get values`.
    mongo_certificate_key_file: str | None = None
    #: The same certificate and key as two files, which is how most issuers write them: one
    #: Secret, two keys. Apchi joins them into one private file of its own as it loads them
    #: (`app.trust.combined`), so nothing between the Secret and the pod has to concatenate
    #: PEMs in a shell -- which a real deployment did, and lost hours to a certificate with no
    #: trailing newline. Both or neither, and not alongside `mongo_certificate_key_file`.
    mongo_certificate_file: str | None = None
    mongo_key_file: str | None = None

    #: The namespace Apchi reads and writes: the Cluster's own.
    kubernetes_namespace: str = "default"

    # The one Trino Cluster this Apchi manages.
    #: The coordinator's address, scheme included. A URL rather than a host and a port
    #: because the Trino client infers the scheme from the port at **exactly 443** and
    #: nowhere else -- so `8443`, the port Trino's own documentation uses for HTTPS, produced
    #: plaintext requests against a TLS listener and said nothing (#188).
    #:
    #: The coordinator's HTTP port, inside the namespace, with or without HTTPS: Apchi sends
    #: only a user name, which that port takes (#295).
    trino_url: str = "http://trino:8080"
    #: The identity Apchi issues DDL as. It is the only identity granted `owner`
    #: on catalogs, which is what restricts catalog DDL to Apchi.
    trino_user: str = "apchi"

    # Wiring: the lines in Trino's own files that name something the deployment creates.
    # Apchi renders every file under /etc/trino, so these arrive from the chart that creates
    # the ports, Services and mounts they name, and no Admin input can change them
    # (ADR-0010, §14). Each default is what the chart renders for a default install.
    #: Holds the coordinator's server configuration files, one key each. The chart mounts
    #: every key with subPath, and never changes that mount: each file needs a restart to
    #: take effect anyway, and the Rollout is what makes new content live.
    server_configuration_secret_name: str = "trino-server-config-coordinator"
    #: The workers' files, the same way: one Secret for the whole worker Deployment.
    worker_server_configuration_secret_name: str = "trino-server-config-worker"
    #: How long a worker waits for the coordinator to notice it is draining, before and after
    #: its tasks finish -- so a drain takes at least twice this (measured: 5s gave 10s). The
    #: chart sets the pod's termination grace period from the same value, and has to: a pod
    #: killed before it has drained ends the tasks it was draining.
    trino_worker_shutdown_grace_period: str = "120s"
    trino_http_port: int = 8080
    #: Where workers announce themselves: the coordinator's Service, never a pod.
    trino_discovery_uri: str = "http://trino:8080"
    #: The HTTPS listener, when the coordinator serves one, and so how callers sign in, which
    #: the chart decides and no Admin input can change. Set: HTTPS authenticates -- by
    #: certificate, or whatever methods an Admin sets instead -- the clients the Route or
    #: Ingress brings, while the HTTP port, which the NetworkPolicy keeps to the namespace,
    #: takes the name a client sends. Apchi uses that port, so it needs no certificate. Unset:
    #: plain HTTP alone, for the quickstart and development.
    trino_https_port: int | None = None
    trino_https_keystore_path: str = "/data/trino-https/keystore.pem"
    #: The CA that client certificates are checked against, for the CERTIFICATE method.
    trino_https_truststore_path: str | None = None
    #: Behind a proxy that terminates TLS, Trino has to believe its X-Forwarded headers, or
    #: every redirect it issues points at plain http.
    trino_process_forwarded: bool = False
    #: `node.environment`. Every node must agree on it, so one value serves both roles.
    trino_environment: str = "production"
    trino_data_dir: str = "/data/trino"
    #: How often Trino re-reads the access-control rules. Required, not tunable away:
    #: without it Trino reads the rules once at startup, and every permission change Apchi
    #: applies is reported applied and never arrives (§13.4).
    trino_security_refresh_period: str = "30s"
    #: The Java truststore every Trino JVM is pointed at, when the chart mounts one.
    trino_java_truststore_path: str | None = None

    #: Holds the durable copy of every catalog. Apchi patches it; an initContainer
    #: seeds the coordinator's store directory from it at pod start.
    catalog_secret_name: str = "trino-catalog-seed"
    #: Holds the generated system access-control file. Mounted as a whole volume so the
    #: kubelet keeps it current; never with subPath (§16).
    access_control_secret_name: str = "trino-access-control"
    #: Where the Event Listeners Section kept its file, before the listener became the Admin's
    #: (#269). Apchi no longer writes it. Read so that the volume Apchi mounted from it can be
    #: taken off the coordinator, and so that the ninth precondition does not refuse that
    #: mount as foreign before it has been.
    event_listener_secret_name: str = "trino-event-listener"
    event_listener_volume_name: str = "apchi-event-listener"
    #: Holds the generated user-mapping file. Always present once Apchi has applied: the
    #: authenticator reading it refuses to start when it is missing, so "no pattern" is a
    #: file that changes nothing rather than no file. The Admin mounts it as a directory, so
    #: there is no volume name here -- Apchi never adds or removes the mount (#93).
    certificate_mapping_secret_name: str = "trino-user-mapping"
    #: Holds `internal-communication.shared-secret`, which both roles read from their
    #: environment. Created at startup when it is missing and never touched once it exists:
    #: replacing it would split the Cluster until every pod restarted.
    shared_secret_name: str = "trino-internal-shared-secret"

    #: Holds every Client Certificate, two files each. Mounted once at a fixed directory on
    #: the coordinator and on the workers, so adding one adds a file to a mount that already
    #: exists: no pod spec change and no restart (ADR-0005).
    client_certificate_secret_name: str = "trino-client-certificates"
    #: How close to expiry a certificate has to be before Apchi calls it expiring. Renewal
    #: is manual for now, so this is the warning an Operator acts on. It covers both a Client
    #: Certificate, where it decides the `expiring` status an Operator filters on, and the
    #: certificates Apchi itself presents, where it decides what the Apply path warns about
    #: in the log (#196) -- one window, because an organisation that renews on a schedule
    #: renews everything on it.
    certificate_expiring_within_days: int = 30
    #: What a Kubernetes Secret may hold. An upload that would exceed it is refused with
    #: that reason rather than as a rejected write from the API server.
    client_certificate_max_bytes: int = 1024 * 1024

    #: Holds both files the Resource Groups Section owns: the rules and the properties file
    #: that tells Trino to read them. One Secret, so they arrive and leave together.
    resource_groups_secret_name: str = "trino-resource-groups"
    resource_groups_volume_name: str = "apchi-resource-groups"

    #: How long a rollout may take before the Apply fails. A coordinator that never comes
    #: back must not hang the pipeline.
    rollout_timeout_seconds: float = 600.0
    #: The same for the workers, which are replaced one at a time and each drains its running
    #: tasks first -- so the bound grows with the worker count and the longest task, and the
    #: coordinator's would be wrong for it.
    worker_rollout_timeout_seconds: float = 1800.0

    #: Where Trino keeps its dynamic catalog store. Nothing read-only may be mounted
    #: here or above it, or every CREATE CATALOG fails.
    catalog_store_dir: str = "/data/trino/catalogs"

    #: Compared against the worker count Trino reports, so the expectation adjusts
    #: when an Admin scales the Cluster.
    worker_deployment_name: str = "trino-worker"
    #: The catalog the Verification smoke query runs against.
    verification_catalog: str = "system"

    #: Validation reads the image from this Deployment's container, so the ephemeral
    #: coordinator is always the version the Cluster runs.
    coordinator_deployment_name: str = "trino-coordinator"
    trino_container_name: str = "trino"
    #: The hard timeout on Validation. A coordinator that never starts serving fails
    #: Validation; it must never hang the pipeline.
    validation_timeout_seconds: float = 300.0
    #: Requests and limits for the Validation pod's one container, in the shape a pod spec
    #: takes them: `{"requests": {"cpu": "1", "memory": "2Gi"}, "limits": {...}}`. Unset
    #: means none, which a permissive cluster accepts and a policy that requires limits on
    #: every container refuses -- and the Validation pod is Apchi's to build, so no chart
    #: can add them afterwards. A quantity may be a bare number, as Helm renders `cpu: 2`.
    validation_resources: dict[str, dict[str, str | int | float]] | None = None
    #: What the Validation pod trusts, mounted the way the chart mounts it on the Cluster's
    #: own Trino pods -- so a Catalog behind an internal CA validates as it will run. The
    #: bundle at `CA_BUNDLE_PATH`, for anything in the image that is not Java; the
    #: truststore at `TRUSTSTORE_PATH`, which the JVM is pointed at, because Java reads only
    #: its own store and the image's is the public one.
    validation_ca_bundle: MountedFile | None = None
    validation_java_truststore: Truststore | None = None

    #: The cluster forbids pinning `fsGroup` and assigns every pod an arbitrary uid, as
    #: OpenShift's restricted-v2 SCC does. Credential files are then readable only if they
    #: are world-readable, because nothing Apchi may set gives a group to the process that
    #: reads them. So under this setting Apchi accepts 0644 credential mounts on the Trino
    #: pods instead of refusing them, and builds its Validation pod with no `fsGroup` and a
    #: writable data directory. The rest of the securityContext such a cluster demands it
    #: adds itself. Off, Apchi keeps the stricter 0640 with a group, which every other
    #: cluster can provide. §13.2.
    restricted_pod_security: bool = False

    #: Where a failed Auto Rollback is reported -- a Mattermost or Slack incoming
    #: webhook. Unset means log only: an alert that cannot be delivered must never
    #: stop Maintenance Mode engaging.
    alert_webhook_url: str | None = None

    # None means "derive from environment"; an explicit value always wins, so the
    # level can be raised during a production incident without a rebuild.
    log_level: str | None = None
    log_json: bool | None = None
    #: Debugging mode: DEBUG unless `log_level` says otherwise, and every line names the
    #: logger and line it came from. It never turns on a dependency's own debug output --
    #: see `configure_logging` for why that is not safe at any level of redaction.
    debug: bool = False

    @property
    def admin_groups(self) -> frozenset[str]:
        return frozenset(g.strip() for g in self.oidc_admin_groups.split(",") if g.strip())

    @property
    def oidc_scope_list(self) -> list[str]:
        return self.oidc_scopes.split()

    @model_validator(mode="after")
    def _refuse_unrecognised_settings(self) -> Self:
        refuse_unrecognised(os.environ)
        return self

    @model_validator(mode="after")
    def _wiring_trino_accepts(self) -> Self:
        # Both are rendered into every Trino file Apchi writes, so a bad value here is a
        # coordinator that will not start after the next Apply -- refused at Apchi's start
        # instead, where the setting is named.
        if not self.trino_security_refresh_period.strip():
            raise ValueError(
                "trino_security_refresh_period is required: without it Trino reads the "
                "access-control rules once at startup and never again (§13.4)"
            )
        if not re.fullmatch(r"[a-z0-9_]+", self.trino_environment):
            raise ValueError(
                f"trino_environment {self.trino_environment!r} is not one Trino accepts: "
                "lowercase letters, digits and underscores only"
            )
        return self

    @model_validator(mode="after")
    def _one_shape_of_mongo_certificate(self) -> Self:
        pair = (self.mongo_certificate_file, self.mongo_key_file)
        if (pair[0] is None) != (pair[1] is None):
            raise ValueError(
                "mongo_certificate_file and mongo_key_file go together: a certificate "
                "without its key cannot be presented, and a key without its certificate names "
                "nobody"
            )
        if pair[0] is not None and self.mongo_certificate_key_file is not None:
            raise ValueError(
                "set either mongo_certificate_key_file, one file holding both, or "
                "mongo_certificate_file and mongo_key_file -- not both shapes at once"
            )
        return self

    @property
    def effective_log_level(self) -> str:
        if self.log_level is not None:
            return self.log_level.upper()
        if self.debug:
            return "DEBUG"
        # What `environment` used to decide, honoured until the setting is removed so that
        # an upgrade does not quietly change a deployment's log level.
        return "DEBUG" if self.environment in ("np", "test") else "INFO"

    @property
    def effective_log_json(self) -> bool:
        if self.log_json is not None:
            return self.log_json
        return self.environment in ("prep", "prod")


def refuse_unrecognised(environ: Mapping[str, str]) -> None:
    """Stop Apchi starting with an `APCHI_` variable it does not read.

    pydantic-settings maps a variable to a setting by name and passes over any other, and
    `extra="forbid"` does not change that -- it only judges the sources parsed whole, never a
    prefixed variable that matched nothing. So a misspelt or invented name runs Apchi on the
    default it was meant to replace, and says nothing. That is how a real deployment set
    APCHI_MONGO_CERTIFICATE_FILE and APCHI_MONGO_KEY_FILE before either existed, connected to
    MongoDB with no client certificate, and spent a day on a 30-second server-selection
    timeout that never named a setting.
    """
    known = {_PREFIX + name.upper() for name in Settings.model_fields}
    unrecognised = sorted(
        name
        for name in environ
        if name.upper().startswith(_PREFIX)
        and name.upper() not in known
        and not _SERVICE_LINK.match(name.upper())
    )
    if not unrecognised:
        return
    lines = []
    for name in unrecognised:
        close = difflib.get_close_matches(name.upper(), known, n=1)
        lines.append(f"{name} (did you mean {close[0]}?)" if close else name)
    raise ValueError(
        "Apchi does not read "
        + ", ".join(lines)
        + ". A setting Apchi does not read would leave it running on a default instead, "
        "so it refuses to start. Remove the variable or correct its name."
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
