/**
 * How the console introduces each curated connector. Presentation only: what a connector
 * requires, allows and treats as secret comes from `GET /catalogs/connectors`, so this table
 * cannot drift from the rules the write endpoints enforce -- a connector the API adds shows
 * up with its name and no blurb rather than not at all.
 */
export const CONNECTOR_INFO: Record<string, { name: string; mono: string; blurb: string; placeholder: string }> = {
  iceberg: { name: "Iceberg", mono: "Ic", blurb: "Open table format on object storage, through a REST, Glue, Hive, JDBC, Nessie or Snowflake catalog.", placeholder: "lakehouse" },
  hive: { name: "Hive", mono: "Hv", blurb: "Tables registered in a Hive metastore or AWS Glue.", placeholder: "warehouse" },
  postgresql: { name: "PostgreSQL", mono: "Pg", blurb: "A PostgreSQL database, optionally with a client certificate.", placeholder: "finance_pg" },
  mongodb: { name: "MongoDB", mono: "Mg", blurb: "Document collections queried as tables.", placeholder: "orders_mongo" },
  kafka: { name: "Kafka", mono: "Kf", blurb: "Topics read as tables, described by files or a schema registry.", placeholder: "events_kafka" },
  redis: { name: "Redis", mono: "Rd", blurb: "Key/value data exposed as tables.", placeholder: "sessions_redis" },
  elasticsearch: { name: "Elasticsearch", mono: "Es", blurb: "Search indices queried with SQL.", placeholder: "search_es" },
};

export const connectorName = (id: string) => CONNECTOR_INFO[id]?.name ?? id;

/**
 * The connectors Apchi can wire a Client Certificate into by itself. Mirrors `WIRED` in
 * `app/sections/catalogs/certificates.py`; the API refuses the field for any other, so a
 * stale copy here costs a clear refusal, never a catalog that silently connects without one.
 */
export const WIRES_CERTIFICATE = new Set(["postgresql"]);
