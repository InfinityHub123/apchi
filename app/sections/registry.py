"""The registered Sections.

Adding a Section is adding it here. The pipeline reads this and nothing else, so it
never names a Section, and `SECTIONS` is derived from the registry rather than written
beside it -- the two cannot drift.
"""

from app.sections import SectionName
from app.sections.base import Section
from app.sections.catalogs.section import CatalogsSection
from app.sections.certificate_mapping.section import CertificateMappingSection
from app.sections.client_certificates.section import ClientCertificatesSection
from app.sections.event_listeners.section import EventListenersSection
from app.sections.permissions.section import PermissionsSection
from app.sections.resource_groups.section import ResourceGroupsSection

REGISTERED: tuple[Section, ...] = (
    CatalogsSection(),
    ClientCertificatesSection(),
    CertificateMappingSection(),
    EventListenersSection(),
    PermissionsSection(),
    ResourceGroupsSection(),
)

#: The names of the registered Sections, in registration order. Every other name in
#: SectionName is vocabulary the model knows and nobody can edit yet.
SECTIONS: tuple[SectionName, ...] = tuple(section.name for section in REGISTERED)
