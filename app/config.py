"""Runtime settings. Everything here is overridable without a rebuild."""

from enum import StrEnum
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    NP = "np"
    TEST = "test"
    PREP = "prep"
    PROD = "prod"


class AuthMode(StrEnum):
    """How a caller proves who they are (§19, #128).

    `NONE` is the default because it is what Apchi does today -- every endpoint open -- and
    anything else would break every existing install on upgrade. Tokens are not a mode: they
    are a credential an authenticated user mints, so they need one of the others.
    """

    NONE = "none"
    PASSWORD = "password"
    SSO = "sso"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APCHI_", env_file=".env", extra="ignore")

    environment: Environment = Environment.NP

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

    #: How long an `Idempotency-Key` is remembered. The window a retry plausibly spans: long
    #: enough for a client to come back after a network failure, short enough that a key
    #: reused weeks later is a new request rather than a replay of a forgotten one.
    idempotency_hours: float = 24.0

    mongo_uri: str = "mongodb://localhost:27017"
    mongo_database: str = "apchi"

    #: The namespace Apchi reads and writes: the Cluster's own.
    kubernetes_namespace: str = "default"

    # The one Trino Cluster this Apchi manages.
    trino_host: str = "trino"
    trino_port: int = 8080
    #: The identity Apchi issues DDL as. It is the only identity granted `owner`
    #: on catalogs, which is what restricts catalog DDL to Apchi.
    trino_user: str = "apchi"

    #: Holds the durable copy of every catalog. Apchi patches it; an initContainer
    #: seeds the coordinator's store directory from it at pod start.
    catalog_secret_name: str = "trino-catalog-seed"
    #: The one thing Apchi keeps outside MongoDB: the highest Snapshot number ever
    #: committed here. Not for recovery -- operating that database is the platform's job
    #: (§18) -- but because an empty `snapshots` collection is indistinguishable from a new
    #: install, and Adoption answers those two differently (§15).
    #: A ConfigMap rather than a Secret -- it holds a number, not configuration.
    high_water_config_map: str = "apchi-high-water"
    #: Holds the generated system access-control file. Mounted as a whole volume so the
    #: kubelet keeps it current; never with subPath (§16).
    access_control_secret_name: str = "trino-access-control"
    #: Holds the generated Event Listener configuration. Apchi mounts it on the
    #: coordinator when a listener is configured and unmounts it when none is: Trino
    #: refuses to start if the file it is told to read is missing, so the presence of the
    #: mount is the only way to express "no listener".
    event_listener_secret_name: str = "trino-event-listener"
    #: The one volume Apchi owns on the coordinator's pod template. Everything else there
    #: belongs to the Admin.
    event_listener_volume_name: str = "apchi-event-listener"
    #: Holds the generated user-mapping file. Always present once Apchi has applied: the
    #: authenticator reading it refuses to start when it is missing, so "no pattern" is a
    #: file that changes nothing rather than no file.
    certificate_mapping_secret_name: str = "trino-user-mapping"
    certificate_mapping_volume_name: str = "apchi-user-mapping"

    #: Holds every Client Certificate, two files each. Mounted once at a fixed directory on
    #: the coordinator and on the workers, so adding one adds a file to a mount that already
    #: exists: no pod spec change and no restart (ADR-0005).
    client_certificate_secret_name: str = "trino-client-certificates"
    #: How close to expiry a certificate has to be before Apchi calls it expiring. Renewal
    #: is manual for now, so this is the warning an Operator acts on.
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

    #: Where a failed Auto Rollback is reported -- a Mattermost or Slack incoming
    #: webhook. Unset means log only: an alert that cannot be delivered must never
    #: stop Maintenance Mode engaging.
    alert_webhook_url: str | None = None

    # None means "derive from environment"; an explicit value always wins, so the
    # level can be raised during a production incident without a rebuild.
    log_level: str | None = None
    log_json: bool | None = None

    @property
    def admin_groups(self) -> frozenset[str]:
        return frozenset(g.strip() for g in self.oidc_admin_groups.split(",") if g.strip())

    @property
    def oidc_scope_list(self) -> list[str]:
        return self.oidc_scopes.split()

    @property
    def effective_log_level(self) -> str:
        if self.log_level is not None:
            return self.log_level.upper()
        return "DEBUG" if self.environment in (Environment.NP, Environment.TEST) else "INFO"

    @property
    def effective_log_json(self) -> bool:
        # JSON in-cluster, plain console locally. Same switch as the level.
        if self.log_json is not None:
            return self.log_json
        return self.environment in (Environment.PREP, Environment.PROD)


@lru_cache
def get_settings() -> Settings:
    return Settings()
