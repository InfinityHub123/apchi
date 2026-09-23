"""Catalogs: a Trino-visible data source, as Operators configure it.

The Section's name lives here rather than in any one module, because both the apply
strategy and the Section itself need it and neither should have to import the other.
"""

from app.sections import SectionName

SECTION: SectionName = "catalogs"
