"""The Certificate Mapping Pattern.

One rule: a regular expression matched against the subject a caller presents, and the
identity it produces. Trino's file format also takes an `allow` flag per rule, which Apchi
does not expose: a single rule that denies is a Cluster nobody can authenticate to, and a
single rule that allows is the only useful shape.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

#: The replacement string. Trino defaults to the first capturing group.
Replacement = Annotated[str, StringConstraints(min_length=1, max_length=256)]

#: What to do with the matched name once it is derived.
Case = Literal["keep", "lower", "upper"]


class CertificateMappingWrite(BaseModel):
    """What an Operator sends."""

    model_config = ConfigDict(extra="forbid")

    pattern: Annotated[str, StringConstraints(min_length=1, max_length=512)] = Field(
        description="A regular expression matched against the subject the caller presents."
    )
    user: Replacement = Field(
        default="$1",
        description="The identity to derive. $1 is the pattern's first capturing group.",
    )
    case: Case = Field(default="keep", description="Whether to fold the derived identity's case.")


class CertificateMapping(CertificateMappingWrite):
    """The pattern as it is stored and returned."""
