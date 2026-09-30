"""What an Operator grants, and what Trino reads.

Apchi's vocabulary is an identity, a place, and a set of privileges (§13.4). Trino's is a
list of rules whose fields are regular expressions. The translation is one way and
deliberate: an Operator writes literal names, and the generator anchors them, so granting on
`finance` grants on `finance` and not on `finance_archive`. Patterns may follow if anyone
needs them; a pattern an Operator did not know they were writing is a data leak.

The privileges are Trino's six, verified against a running coordinator: an unknown one fails
startup with "not one of the values accepted for Enum class: [INSERT, DELETE, SELECT,
GRANT_SELECT, UPDATE, OWNERSHIP]".
"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Privilege = Literal["SELECT", "INSERT", "UPDATE", "DELETE", "OWNERSHIP", "GRANT_SELECT"]

#: A Trino Identity, a catalog, a schema or a table, as an Operator writes it: a literal
#: name. `:` is excluded because it separates the parts of the key a grant is addressed by,
#: the way Resource Groups excludes `.` from a group name.
Name = Annotated[str, StringConstraints(min_length=1, max_length=256, pattern=r"^[^:]+$")]

#: What stands in for a level a grant does not name, in its key. Not a wildcard an Operator
#: can write -- it is how "every schema in this catalog" is spelled when addressing the grant.
ANY = "*"


class GrantWrite(BaseModel):
    """A grant as an Operator sends it."""

    model_config = ConfigDict(extra="forbid")

    identity: Name = Field(description="The Trino Identity being granted access.")
    catalog: Name = Field(description="The catalog the grant applies to.")
    schema_: Name | None = Field(
        default=None,
        alias="schema",
        description="The schema, or omitted for every schema in the catalog.",
    )
    table: Name | None = Field(
        default=None,
        description="The table, or omitted for every table in the schema.",
    )
    privileges: list[Privilege] = Field(
        min_length=1, description="What the identity may do. At least one."
    )

    @model_validator(mode="after")
    def _a_table_needs_its_schema(self) -> Self:
        """A table without a schema names two places at once: `orders` in some schema, or
        every schema's `orders`. Neither is what anybody meant."""
        if self.table is not None and self.schema_ is None:
            raise ValueError("table needs the schema it is in; name schema as well, or omit table")
        return self


def key_of(write: GrantWrite) -> str:
    """How a grant is addressed: identity, catalog, schema, table.

    Derived rather than chosen, so the same grant is always the same resource -- which is
    what lets Review name the grant that changed instead of saying the permissions changed.
    """
    return ":".join((write.identity, write.catalog, write.schema_ or ANY, write.table or ANY))


class Grant(GrantWrite):
    """A grant as it is stored and returned, with the key that addresses it."""

    key: str = Field(description="The identifier this grant is addressed by.")


class SystemRule(BaseModel):
    """One rule Apchi owns, shown to Operators and editable by nobody."""

    rule: str = Field(description="What the rule does, in Apchi's words.")
    why: str = Field(description="Why it exists, and what breaks without it.")


class SystemRules(BaseModel):
    """The rules Apchi generates for itself.

    Visible on purpose. An Operator who cannot see them cannot understand why catalog DDL is
    refused to them, and §13.4 asks for the same of the queries block: a system-owned rule an
    Operator can read and reason about, not a silent default.
    """

    rules: list[SystemRule]
