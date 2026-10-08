"""Admin values: platform configuration that is applied but never recorded in a Snapshot.

Snapshots are the history of Operator-managed configuration. Admin values have a different
lifecycle -- they belong to the platform, they survive a rollback deliberately, and what
actually ran on a Cluster was the Snapshot merged with the Admin values current at the time.
That is invariant 9, and this is its first implementation. See section 14.

Whether permissions are *enforced* is one of these, and it belongs here for the reason the
preserved mapping patterns do: it is a migration, not a preference. Staging grants on a
Cluster that is already serving users takes nothing away, and the day the catch-all is
removed every identity without a grant loses everything it had. That is a platform decision
with an irreversible-feeling morning after it, so it is an Admin's to make and an Admin's to
time -- an Operator should not be able to close a Cluster by editing configuration, and
Apchi should not do it to them on an upgrade.

The document is typed rather than free-form. Section 14's wider escape hatch -- arbitrary
properties written into arbitrary files -- is still open; what is settled is modelled here and
nothing else.

The preserved values are the shape Adoption needs (§15). A Cluster being onboarded has
configuration Apchi's models cannot express -- access-control rules of kinds it does not
model, certificate mapping patterns beyond the single one it holds -- and preserving them as
Admin values is what makes adoption non-destructive by construction: nobody loses access on
the day their Cluster is onboarded, and an Admin removes them deliberately when the migration
is finished.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.sections.certificate_mapping.model import CertificateMappingWrite

#: Enough to migrate a Cluster that accumulated conventions over years, few enough that the
#: file stays something a person can read. Nobody has a legitimate hundredth pattern.
MAX_PRESERVED_MAPPINGS = 20


class AdminValues(BaseModel):
    """Everything Admins configure outside the Candidate. One document, so an Apply reads
    the Admin side of the Cluster in one go and holds it frozen for its whole run."""

    model_config = ConfigDict(extra="forbid")

    enforce_permissions: bool = Field(
        default=False,
        description=(
            "Whether a Trino Identity may reach only what it has been granted. False -- the "
            "default -- leaves the generated rules ending in a catch-all that allows "
            "everything to everyone, which is what a Cluster did before Apchi was installed."
        ),
    )
    preserved_access_control: dict[str, list[dict[str, Any]]] = Field(
        default_factory=dict,
        description=(
            "Access-control rules a Cluster already ran before it was onboarded, kept "
            "working beneath the Operator's grants while the grants catch up. Keyed by the "
            "block they belong to -- the blocks Apchi generates, and the ones it does not "
            "model at all. An Admin removes them when the migration is done."
        ),
    )
    preserved_certificate_mappings: list[CertificateMappingWrite] = Field(
        default_factory=list,
        max_length=MAX_PRESERVED_MAPPINGS,
        description=(
            "Mapping patterns a Cluster already ran before it was onboarded, kept working "
            "while its clients migrate to the single Operator pattern. Evaluated in the "
            "order given, beneath the Operator's pattern."
        ),
    )
