"""Certificate Mapping: how a caller authenticating inward becomes a Trino Identity.

No private key is involved -- the caller holds that. Apchi configures the derivation, and
exposes exactly one pattern, which is what removes the need for a mapping entry per identity.
"""

from app.sections import SectionName

SECTION: SectionName = "certificate_mapping"

#: The Section holds at most one resource, under this key. A singleton by design rather than
#: a collection that happens to have one member.
RESOURCE = "pattern"
