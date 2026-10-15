"""§20's audit history: who changed what, and when.

Separate from Snapshots on purpose. A Snapshot answers *what complete configuration was
committed*; the audit answers *who changed what*. Neither derives the other, and the audit is
the more load-bearing of the two for one specific reason: the Configuration Candidate is
shared (ADR-0002), so the audit is the only record of whose work an Apply is shipping. It is
also the only record of an Admin change at all, because §14 keeps Admin values out of
Snapshots deliberately.
"""
