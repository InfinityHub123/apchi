"""Admin values: platform configuration that is applied but never recorded in a Snapshot.

Snapshots are the history of Operator-managed configuration. Admin values have a different
lifecycle -- they belong to the platform, they survive a rollback deliberately, and what
actually ran on a Cluster was the Snapshot merged with the Admin values current at the time.
That is invariant 9, and this is its first implementation. See section 14.

The document is typed rather than free-form. Section 14's wider escape hatch -- arbitrary
properties written into arbitrary files -- is still open; what is settled is this one value,
so this one value is modelled.
"""

from pydantic import BaseModel, ConfigDict, Field

from app.sections.certificate_mapping.model import CertificateMappingWrite

#: Enough to migrate a Cluster that accumulated conventions over years, few enough that the
#: file stays something a person can read. Nobody has a legitimate hundredth pattern.
MAX_PRESERVED_MAPPINGS = 20


class AdminValues(BaseModel):
    """Everything Admins configure outside the Candidate. One document, so an Apply reads
    the Admin side of the Cluster in one go and holds it frozen for its whole run."""

    model_config = ConfigDict(extra="forbid")

    preserved_certificate_mappings: list[CertificateMappingWrite] = Field(
        default_factory=list,
        max_length=MAX_PRESERVED_MAPPINGS,
        description=(
            "Mapping patterns a Cluster already ran before it was onboarded, kept working "
            "while its clients migrate to the single Operator pattern. Evaluated in the "
            "order given, beneath the Operator's pattern."
        ),
    )
