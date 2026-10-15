"""Resource Groups: how the Cluster's capacity is divided.

Groups are keyed by their dotted path -- `global`, `global.etl` -- because order between
them means nothing and an Operator wants Review to name the group that changed. Trino
rejects a group name containing a dot ("Invalid resource group name. 'a.b' contains a '.'"),
so a path is unambiguous.

The selectors cannot be keyed that way: first match wins, so their order *is* the
configuration. They live under one reserved key beside the groups, reserved by a character
Apchi forbids in a group name.
"""

from app.sections import SectionName

SECTION: SectionName = "resource_groups"

#: Where the ordered selector list lives among the groups. A group may not contain `#`, so
#: nothing an Operator can name collides with it.
SELECTORS = "#selectors"

#: And where the file's own settings live, for the same reason. There is one: the CPU quota
#: period, which is a property of the file rather than of any group in it.
SETTINGS = "#settings"

#: What separates one level of the hierarchy from the next, in a path and nowhere else.
SEPARATOR = "."
