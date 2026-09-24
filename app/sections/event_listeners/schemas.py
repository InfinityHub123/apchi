"""The curated Event Listener schemas.

Verified against the Trino 483 documentation for the HTTP and Kafka event listeners. The
selector is `event-listener.name`; every other property is prefixed by the listener type.
Every schema drifts with Trino versions, and the supported Trino range is what bounds the
maintenance on them.

Two judgement calls worth knowing about:

The HTTP listener documents `http-event-listener.*` as an escape hatch passing arbitrary
configuration to its HTTP client. That is deliberately **not** modelled as a prefix,
because allowing any name under `http-event-listener.` would accept a typo of
`connect-ingest-uri` -- the exact thing curation exists to catch. An Operator who needs a
client-tuning property gets a schema update rather than a silent pass.

Kafka's documentation lists all four of broker endpoints, both topics and the client id as
mandatory, so they are required here. It is plausible that a topic stops being required
when its `publish-*-event` is false; that is not what the documentation says, and the
ephemeral coordinator is what would catch it either way.
"""

from app.sections.properties import Branch, PropertySchema

HTTP = PropertySchema(
    required=frozenset({"http-event-listener.connect-ingest-uri"}),
    optional=frozenset(
        {
            "http-event-listener.log-created",
            "http-event-listener.log-completed",
            "http-event-listener.connect-http-headers",
            "http-event-listener.connect-retry-count",
            "http-event-listener.connect-retry-delay",
            "http-event-listener.connect-backoff-base",
            "http-event-listener.connect-max-delay",
        }
    ),
    branches=(
        Branch(
            selector="http-event-listener.connect-http-method",
            values={},
            allowed=frozenset({"POST", "PUT"}),
        ),
    ),
)

KAFKA = PropertySchema(
    required=frozenset(
        {
            "kafka-event-listener.broker-endpoints",
            "kafka-event-listener.created-event.topic",
            "kafka-event-listener.completed-event.topic",
            "kafka-event-listener.client-id",
        }
    ),
    optional=frozenset(
        {
            "kafka-event-listener.anonymization.enabled",
            "kafka-event-listener.max-request-size",
            "kafka-event-listener.batch-size",
            "kafka-event-listener.publish-created-event",
            "kafka-event-listener.publish-completed-event",
            "kafka-event-listener.excluded-fields",
            "kafka-event-listener.request-timeout",
            "kafka-event-listener.terminate-on-initialization-failure",
            "kafka-event-listener.env-var-prefix",
            "kafka-event-listener.config.resources",
        }
    ),
)

#: The listener types Apchi curates. Anything else passes through unvalidated, the same
#: rule Catalogs applies to connectors.
CURATED_SCHEMAS: dict[str, PropertySchema] = {"http": HTTP, "kafka": KAFKA}
