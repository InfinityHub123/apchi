"""The registered Sections.

Adding a Section is adding it here. The pipeline reads this and nothing else, so it
never names a Section, and `SECTIONS` is derived from the registry rather than written
beside it -- the two cannot drift.
"""

from app.sections import SectionName
from app.sections.base import Section
from app.sections.catalogs.section import CatalogsSection
from app.sections.event_listeners.section import EventListenersSection

REGISTERED: tuple[Section, ...] = (CatalogsSection(), EventListenersSection())

#: The names of the registered Sections, in registration order. Every other name in
#: SectionName is vocabulary the model knows and nobody can edit yet.
#:
#: Nothing reads `requires_rollout` yet, though the two registered Sections now disagree
#: about it: Catalogs are applied by DDL, Event Listeners only by restarting. The minimal
#: restart set is what will use it.
SECTIONS: tuple[SectionName, ...] = tuple(section.name for section in REGISTERED)
