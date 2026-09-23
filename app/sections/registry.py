"""The registered Sections.

Adding a Section is adding it here. The pipeline reads this and nothing else, so it
never names a Section, and `SECTIONS` is derived from the registry rather than written
beside it -- the two cannot drift.
"""

from app.sections import SectionName
from app.sections.base import Section
from app.sections.catalogs.section import CatalogsSection

REGISTERED: tuple[Section, ...] = (CatalogsSection(),)

#: The names of the registered Sections, in registration order. Every other name in
#: SectionName is vocabulary the model knows and nobody can edit yet.
#:
#: Nothing reads `requires_rollout` yet. The flag is one of the four things a Section
#: provides and Catalogs declares it false; the minimal restart set is what will use it,
#: and inventing the helper for it here before anything calls it would be a hook nobody
#: calls.
SECTIONS: tuple[SectionName, ...] = tuple(section.name for section in REGISTERED)
