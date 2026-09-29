"""What an Operator writes, and what Trino reads.

Trino owns and documents this format, so unlike connectors and event listeners there is no
pass-through: every field is modelled and anything else is refused. The names are Apchi's
snake_case here, like the rest of the API; the generator camelCases them on the way into the
file, which is a mechanical rule rather than a table that can drift.

What is required here is what Trino requires, verified against a real coordinator: a group
without `hardConcurrencyLimit` fails to start ("Missing required property:
hardConcurrencyLimit"), and a group without `maxQueued` starts. Apchi refuses nothing Trino
would have accepted.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

#: One segment of a path. Trino rejects a `.`; Apchi also rejects `#`, which is what keeps
#: the selector list's key out of reach of anything an Operator can name.
GroupName = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[^.#]+$")]

#: A dotted path from a root group down. Never ends or begins with a separator.
GroupPath = Annotated[
    str, StringConstraints(min_length=1, max_length=512, pattern=r"^[^.#]+(\.[^.#]+)*$")
]

#: Trino's four, and no others: an unknown one fails with "No enum constant
#: io.trino.spi.resourcegroups.SchedulingPolicy.ROUND_ROBIN".
SchedulingPolicy = Literal["fair", "weighted", "weighted_fair", "query_priority"]

#: The query types a selector may name. An unknown one is rejected by Trino with "Selector
#: specifies an invalid query type".
QueryType = Literal[
    "SELECT",
    "INSERT",
    "DELETE",
    "UPDATE",
    "MERGE",
    "ANALYZE",
    "EXPLAIN",
    "DESCRIBE",
    "DATA_DEFINITION",
    "ALTER_TABLE_EXECUTE",
]

#: A memory or CPU limit as Trino spells it: `80%`, `10GB`, `30m`, `1h`.
Limit = Annotated[str, StringConstraints(min_length=1, max_length=32)]


class _TrinoModel(BaseModel):
    """Snake_case, like the rest of Apchi's API. The generator camelCases on the way out."""

    model_config = ConfigDict(extra="forbid")


class ResourceGroupWrite(_TrinoModel):
    """A group's own configuration. Its name and place in the hierarchy are its path."""

    hard_concurrency_limit: int = Field(
        ge=0, description="How many queries may run in this group at once."
    )
    max_queued: int | None = Field(
        default=None,
        ge=0,
        description="How many queries may wait. Trino's own default applies when unset.",
    )
    soft_memory_limit: Limit | None = Field(
        default=None,
        description="Memory beyond which new queries queue, absolute (10GB) or a share (80%).",
    )
    soft_concurrency_limit: int | None = Field(
        default=None, ge=0, description="Concurrency beyond which queries are deprioritised."
    )
    soft_cpu_limit: Limit | None = Field(
        default=None, description="CPU time per quota period beyond which queries slow down."
    )
    hard_cpu_limit: Limit | None = Field(
        default=None, description="CPU time per quota period beyond which queries are refused."
    )
    scheduling_policy: SchedulingPolicy | None = Field(
        default=None, description="How this group picks which of its queued queries runs next."
    )
    scheduling_weight: int | None = Field(
        default=None, description="This group's share among its siblings under a weighted policy."
    )
    jmx_export: bool | None = Field(
        default=None, description="Whether this group's statistics are exported over JMX."
    )

    @model_validator(mode="after")
    def _cpu_limits_come_in_pairs(self) -> "ResourceGroupWrite":
        """A soft CPU limit without a hard one stops the coordinator starting: "Must specify
        hard CPU limit in addition to soft limit". Within one resource, so it is caught at
        request time rather than at Validate."""
        if self.soft_cpu_limit is not None and self.hard_cpu_limit is None:
            raise ValueError(
                "soft_cpu_limit needs hard_cpu_limit beside it; Trino refuses to start "
                "with one and not the other."
            )
        return self


class ResourceGroup(ResourceGroupWrite):
    """A group as it is stored and returned, with the path that addresses it."""

    path: GroupPath = Field(description="The dotted path from a root group down to this one.")


class Selector(_TrinoModel):
    """One rule choosing a group for a query. First match wins, so position matters.

    Every field but `group` is optional and every one given must match. An empty selector
    therefore matches everything, which is the usual last rule.
    """

    group: GroupPath = Field(description="The group queries matching this rule run in.")
    user: str | None = Field(default=None, description="Regex matched against the user.")
    original_user: str | None = Field(
        default=None, description="Regex matched against the user before impersonation."
    )
    authenticated_user: str | None = Field(
        default=None, description="Regex matched against the authenticated principal."
    )
    user_group: str | None = Field(
        default=None, description="Regex matched against the user's groups."
    )
    source: str | None = Field(default=None, description="Regex matched against the query source.")
    query_type: QueryType | None = Field(
        default=None, description="Only queries of this kind match."
    )
    query_text: str | None = Field(
        default=None, description="Regex matched against the query text itself."
    )
    client_tags: list[str] | None = Field(
        default=None, description="Every tag listed must be present on the query."
    )


class Selectors(BaseModel):
    """The whole ordered list, read and replaced as one.

    Positional meaning is why there is no endpoint for one selector: editing a rule in place
    would let an Operator change what matches without seeing what now shadows it.
    """

    model_config = ConfigDict(extra="forbid")

    selectors: list[Selector] = Field(
        default_factory=list, max_length=200, description="Evaluated top to bottom."
    )


class ResourceGroupSettings(BaseModel):
    """What the file says outside any group. One thing, so far.

    `cpu_quota_period` is not optional decoration: a real coordinator refuses to start when
    a group sets a CPU limit without it -- "cpuQuotaPeriod must be specified to use CPU
    limits on group: etl" -- so a Section offering CPU limits has to offer this too.
    """

    model_config = ConfigDict(extra="forbid")

    cpu_quota_period: Limit | None = Field(
        default=None,
        description=(
            "How often each group's CPU limits reset, as Trino spells a duration (1h). "
            "Required by Trino before any group may set a CPU limit."
        ),
    )
