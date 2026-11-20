"""The curated connector schemas.

Declarative rather than a Pydantic model per connector, because four of the seven
branch: a selector property decides which further properties are required.
Expressing that as nested models would obscure the rule it encodes.

Verified against the Trino 483 connector documentation. Every schema drifts with
Trino versions; the supported Trino range is what bounds the maintenance.
"""

from app.sections.properties import Branch
from app.sections.properties import PropertySchema as ConnectorSchema

# --- shared families, modelled once ------------------------------------------

# The JDBC connectors repeat this table verbatim on each of their doc pages.
JDBC_CREDENTIALS = frozenset(
    {
        "connection-user",
        "connection-password",
        "credential-provider.type",
        "connection-credential-file",
        "keystore-file-path",
        "keystore-type",
        "keystore-password",
        "keystore-user-credential-name",
        "keystore-password-credential-name",
    }
)
#: The two JDBC credentials that are actually credentials. `connection-user` is a name,
#: `credential-provider.type` is a strategy, and the `*-credential-name` pair names a secret
#: stored somewhere else rather than holding one.
JDBC_SECRETS = frozenset({"connection-password", "keystore-password"})

JDBC_TUNING = frozenset(
    {
        "case-insensitive-name-matching",
        "case-insensitive-name-matching.cache-ttl",
        "metadata.cache-ttl",
        "metadata.cache-missing",
        "metadata.cache-maximum-size",
        "write.batch-size",
        "dynamic-filtering.enabled",
        "dynamic-filtering.wait-timeout",
        "join-pushdown.enabled",
        "join-pushdown.strategy",
        "domain-compaction-threshold",
        "unsupported-type-handling",
    }
)

# Genuinely shared between Hive and Iceberg, per Trino's own metastores page.
METASTORE = frozenset(
    {
        "hive.metastore",
        "hive.metastore.uri",
        "hive.metastore.username",
        "hive.metastore.authentication.type",
        "hive.metastore.thrift.client.connect-timeout",
        "hive.metastore.thrift.client.read-timeout",
        "hive.metastore.thrift.client.ssl.enabled",
        "hive.metastore.glue.region",
        "hive.metastore.glue.pin-client-to-current-region",
        "hive.metastore.glue.max-connections",
        "hive.metastore.glue.catalogid",
        "hive.metastore.glue.endpoint-url",
        "hive.metastore-cache-ttl",
    }
)

#: Only the secret half of the S3 pair. An access key id is an identifier that travels in
#: every signed request -- marking it would teach a form to hide something that is not
#: hidden anywhere else, and a mask that covers identifiers is a mask nobody reads.
FILE_SYSTEM_SECRETS = frozenset({"s3.aws-secret-key"})

# The object-storage toggles and their credentials, shared by Hive and Iceberg.
FILE_SYSTEM = frozenset(
    {
        "fs.s3.enabled",
        "fs.native-azure.enabled",
        "fs.native-gcs.enabled",
        "fs.hadoop.enabled",
        "s3.aws-access-key",
        "s3.aws-secret-key",
        "s3.region",
        "s3.endpoint",
        "s3.path-style-access",
        "s3.iam-role",
        "s3.auth-type",
    }
)


def _tls(prefix: str) -> frozenset[str]:
    """The TLS block repeats per connector with a different prefix."""
    return frozenset(
        {
            f"{prefix}.enabled",
            f"{prefix}.keystore-path",
            f"{prefix}.keystore-password",
            f"{prefix}.truststore-path",
            f"{prefix}.truststore-password",
        }
    )


def _tls_secrets(prefix: str) -> frozenset[str]:
    """The two passwords in that block. Paths are not secrets."""
    return frozenset({f"{prefix}.keystore-password", f"{prefix}.truststore-password"})


# --- the curated seven --------------------------------------------------------

CURATED_SCHEMAS: dict[str, ConnectorSchema] = {
    "postgresql": ConnectorSchema(
        required=frozenset({"connection-url"}),
        optional=JDBC_CREDENTIALS
        | JDBC_TUNING
        | frozenset({"postgresql.array-mapping", "postgresql.include-system-tables"}),
        secrets=JDBC_SECRETS,
    ),
    "mongodb": ConnectorSchema(
        # The connection URL embeds credentials; there is no separate password.
        required=frozenset({"mongodb.connection-url"}),
        optional=frozenset(
            {
                "mongodb.schema-collection",
                "mongodb.case-insensitive-name-matching",
                "mongodb.min-connections-per-host",
                "mongodb.connections-per-host",
                "mongodb.max-wait-time",
                "mongodb.max-connection-idle-time",
                "mongodb.connection-timeout",
                "mongodb.socket-timeout",
                "mongodb.read-preference",
                "mongodb.write-concern",
                "mongodb.required-replica-set",
            }
        )
        | _tls("mongodb.tls"),
        # The whole connection URL, because the password is inside it. This is the case a
        # name test cannot reach, and the reason secrets are marked rather than guessed.
        secrets=frozenset({"mongodb.connection-url"}) | _tls_secrets("mongodb.tls"),
    ),
    "hive": ConnectorSchema(
        branches=(
            Branch(
                selector="hive.metastore",
                default="thrift",
                allowed=frozenset({"thrift", "glue"}),
                values={
                    "thrift": frozenset({"hive.metastore.uri"}),
                    "glue": frozenset(),
                },
            ),
        ),
        optional=METASTORE
        | FILE_SYSTEM
        | frozenset(
            {
                "hive.recursive-directories",
                "hive.storage-format",
                "hive.compression-codec",
                "hive.target-max-file-size",
                "hive.non-managed-table-writes-enabled",
                "hive.hive-views.enabled",
                "hive.query-partition-filter-required",
                "hive.max-partitions-per-scan",
                "hive.security",
                "hive.timestamp-precision",
                "hive.metastore.glue.aws-access-key",
                "hive.metastore.glue.aws-secret-key",
            }
        ),
        secrets=FILE_SYSTEM_SECRETS | frozenset({"hive.metastore.glue.aws-secret-key"}),
    ),
    "iceberg": ConnectorSchema(
        branches=(
            Branch(
                selector="iceberg.catalog.type",
                default="hive_metastore",
                allowed=frozenset(
                    {"hive_metastore", "glue", "jdbc", "rest", "nessie", "snowflake"}
                ),
                values={
                    "hive_metastore": frozenset({"hive.metastore.uri"}),
                    "glue": frozenset(),
                    "rest": frozenset({"iceberg.rest-catalog.uri"}),
                    "jdbc": frozenset(
                        {
                            "iceberg.jdbc-catalog.driver-class",
                            "iceberg.jdbc-catalog.connection-url",
                            "iceberg.jdbc-catalog.default-warehouse-dir",
                            "iceberg.jdbc-catalog.catalog-name",
                        }
                    ),
                    "nessie": frozenset({"iceberg.nessie-catalog.uri"}),
                    "snowflake": frozenset(
                        {
                            "iceberg.snowflake-catalog.account-uri",
                            "iceberg.snowflake-catalog.user",
                            "iceberg.snowflake-catalog.password",
                            "iceberg.snowflake-catalog.database",
                        }
                    ),
                },
            ),
        ),
        optional=METASTORE
        | FILE_SYSTEM
        | frozenset(
            {
                "iceberg.file-format",
                "iceberg.compression-codec",
                "iceberg.max-partitions-per-writer",
                "iceberg.target-max-file-size",
                "iceberg.unique-table-location",
                "iceberg.dynamic-filtering.wait-timeout",
                "iceberg.table-statistics-enabled",
                "iceberg.register-table-procedure.enabled",
                "iceberg.security",
                "iceberg.jdbc-catalog.connection-user",
                "iceberg.jdbc-catalog.connection-password",
                "iceberg.rest-catalog.security",
                "iceberg.rest-catalog.session",
                "iceberg.rest-catalog.oauth2.token",
                "iceberg.rest-catalog.oauth2.credential",
                "iceberg.nessie-catalog.authentication.token",
            }
        ),
        open_ended=frozenset({"iceberg.rest-catalog.http-headers"}),
        secrets=FILE_SYSTEM_SECRETS
        | frozenset(
            {
                "iceberg.jdbc-catalog.connection-password",
                "iceberg.snowflake-catalog.password",
                "iceberg.rest-catalog.oauth2.token",
                "iceberg.rest-catalog.oauth2.credential",
                "iceberg.nessie-catalog.authentication.token",
                # Open-ended, and the usual contents are an Authorization header. Marked
                # even though nothing can redact its value, because what this list is for
                # is keeping a credential off a shared screen.
                "iceberg.rest-catalog.http-headers",
            }
        ),
    ),
    "redis": ConnectorSchema(
        # redis.table-names appears in the minimal example but is optional: the
        # connector falls back to redis.table-description-dir.
        required=frozenset({"redis.nodes"}),
        optional=frozenset(
            {
                "redis.table-names",
                "redis.default-schema",
                "redis.scan-count",
                "redis.max-keys-per-fetch",
                "redis.key-prefix-schema-table",
                "redis.key-delimiter",
                "redis.table-description-dir",
                "redis.table-description-cache-ttl",
                "redis.hide-internal-columns",
                "redis.database-index",
                "redis.user",
                "redis.password",
            }
        )
        | _tls("redis.tls"),
        secrets=frozenset({"redis.password"}) | _tls_secrets("redis.tls"),
    ),
    "elasticsearch": ConnectorSchema(
        required=frozenset({"elasticsearch.host"}),
        branches=(
            Branch(
                selector="elasticsearch.security",
                allowed=frozenset({"AWS", "PASSWORD"}),
                values={
                    "AWS": frozenset({"elasticsearch.aws.region"}),
                    "PASSWORD": frozenset(
                        {"elasticsearch.auth.user", "elasticsearch.auth.password"}
                    ),
                },
            ),
        ),
        optional=frozenset(
            {
                "elasticsearch.port",
                "elasticsearch.default-schema-name",
                "elasticsearch.scroll-size",
                "elasticsearch.scroll-timeout",
                "elasticsearch.request-timeout",
                "elasticsearch.connect-timeout",
                "elasticsearch.backoff-init-delay",
                "elasticsearch.backoff-max-delay",
                "elasticsearch.max-retry-time",
                "elasticsearch.node-refresh-interval",
                "elasticsearch.tls.verify-hostnames",
                "elasticsearch.aws.access-key",
                "elasticsearch.aws.secret-key",
                "elasticsearch.aws.iam-role",
                "elasticsearch.aws.external-id",
            }
        )
        | _tls("elasticsearch.tls"),
        # The password arrives through the PASSWORD branch, so a secret is not always in
        # `required` or `optional` -- which is why this is one set over the whole schema.
        secrets=frozenset({"elasticsearch.auth.password", "elasticsearch.aws.secret-key"})
        | _tls_secrets("elasticsearch.tls"),
    ),
    "kafka": ConnectorSchema(
        required=frozenset({"kafka.nodes"}),
        branches=(
            Branch(
                selector="kafka.table-description-supplier",
                default="FILE",
                allowed=frozenset({"FILE", "CONFLUENT"}),
                values={
                    "FILE": frozenset({"kafka.table-names"}),
                    "CONFLUENT": frozenset({"kafka.confluent-schema-registry-url"}),
                },
            ),
        ),
        optional=frozenset(
            {
                "kafka.default-schema",
                "kafka.buffer-size",
                "kafka.hide-internal-columns",
                "kafka.internal-column-prefix",
                "kafka.messages-per-split",
                "kafka.table-description-dir",
                "kafka.protobuf-any-support-enabled",
                "kafka.timestamp-upper-bound-force-push-down-enabled",
                "kafka.confluent-schema-registry-client-cache-size",
                "kafka.empty-field-strategy",
                "kafka.security-protocol",
                "kafka.ssl.keystore.location",
                "kafka.ssl.keystore.password",
                "kafka.ssl.keystore.type",
                "kafka.ssl.truststore.location",
                "kafka.ssl.truststore.password",
                "kafka.ssl.truststore.type",
                "kafka.ssl.key.password",
                "kafka.ssl.endpoint-identification-algorithm",
            }
        ),
        # Points at external Kafka client property files whose keyspace is the
        # whole Kafka client property space. Trino never sees their contents as
        # catalog properties, so nothing here can validate or redact them.
        open_ended=frozenset({"kafka.config.resources"}),
        secrets=frozenset(
            {
                "kafka.ssl.keystore.password",
                "kafka.ssl.truststore.password",
                "kafka.ssl.key.password",
            }
        ),
    ),
}
