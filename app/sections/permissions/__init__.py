"""Permissions: who may reach what, and the system-owned rules nobody may edit.

The whole system access-control file is Apchi's. Today it holds only what Apchi generates
for itself -- the block restricting catalog DDL to Apchi's identity -- and Operator-managed
grants come later. What is already true is that the file is *this Section's*, delivered the
way every Section's file is delivered rather than as a special case in the pipeline.

Trino re-reads the file on its own timer, so this Section needs no Rollout (section 7.2).
"""

from app.sections import SectionName

SECTION: SectionName = "permissions"
