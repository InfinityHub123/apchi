"""Client Certificates: what Trino presents when it connects **outward**.

A PostgreSQL-backed catalog behind mTLS, an event-listener endpoint that demands a client
certificate -- Trino is the client, and these are its credentials. The opposite direction
from the Certificate Mapping Pattern, which is about callers authenticating inward *to*
Trino.

Operators never convert formats. They upload a ZIP; Apchi works out which member is the
certificate and which is the key, proves they are a pair, and normalises what it stores.
"""

from app.sections import SectionName

SECTION: SectionName = "client_certificates"
