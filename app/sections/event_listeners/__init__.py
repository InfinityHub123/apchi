"""Event Listeners: where Trino sends query events, as Operators configure them.

The Section's name lives here so neither the apply strategy nor the Section itself has to
import the other.
"""

from app.sections import SectionName

SECTION: SectionName = "event_listeners"
