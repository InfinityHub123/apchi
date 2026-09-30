"""Rules about one Section referring to another.

A Section's own `check` sees only its own resources, deliberately: a Section knows nothing
about the pipeline and nothing about its neighbours. But a Catalog that presents a Client
Certificate is a fact about both, and the relationship has to live somewhere. It lives here,
where the whole Candidate is visible, rather than inside either Section -- which would make
one Section import the other and turn two independent areas into one.

Checked at Validate rather than at request time, so an Operator can write the Catalog before
uploading the certificate it will use. The same reason a Resource Group selector may name a
group staged later (§13.5).
"""

from app.sections import SectionName
from app.sections.base import Resources, ValidationFailure
from app.sections.catalogs import SECTION as CATALOGS
from app.sections.catalogs.certificates import referenced
from app.sections.client_certificates import SECTION as CERTIFICATES


def missing_certificates(sections: dict[SectionName, Resources]) -> list[ValidationFailure]:
    """Every Catalog that names a Client Certificate nobody has staged."""
    staged = set(sections.get(CERTIFICATES, {}))
    failures = []
    for name in sorted(sections.get(CATALOGS, {})):
        stored = sections[CATALOGS][name]
        wanted = referenced(stored.get("properties", {}), stored.get("certificate"))
        for missing in sorted(wanted - staged):
            failures.append(
                ValidationFailure(
                    section=CATALOGS,
                    resource=name,
                    reason=(
                        f"references the client certificate {missing!r}, which is not "
                        "configured. Upload it, or remove the reference."
                    ),
                )
            )
    return failures


def certificates_in_use(sections: dict[SectionName, Resources], certificate: str) -> list[str]:
    """Which Catalogs would break if this certificate went away.

    Asked at request time, because removing a certificate a Catalog still presents is a
    mistake an Operator can be stopped from making rather than told about later.
    """
    return [
        name
        for name in sorted(sections.get(CATALOGS, {}))
        if certificate
        in referenced(
            sections[CATALOGS][name].get("properties", {}),
            sections[CATALOGS][name].get("certificate"),
        )
    ]
