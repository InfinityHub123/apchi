"""Runtime settings. Everything here is overridable without a rebuild."""

from enum import StrEnum
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    NP = "np"
    TEST = "test"
    PREP = "prep"
    PROD = "prod"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APCHI_", env_file=".env", extra="ignore")

    environment: Environment = Environment.NP

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
