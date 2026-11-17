# Changelog

What changed between releases, for whoever automates against Apchi's API.

While Apchi is `0.x` a breaking change may land in any minor release, and each one is named
under `## Breaking` below. The contract firms up at 1.0. There is no `/api/v2` planned and no
deprecation window; §19 of `apchi_implementation.md` argues why.

`## Breaking` is the only part of an entry that has to be here. `openapi.json` is the contract
itself and the release notes carry the commit log, so neither is worth repeating.

Two breaks the committed contract cannot show you, and which belong here when they happen:

- Pydantic models reject unknown fields, so a client sending one gets a `422` that no schema
  diff announced.
- A response *value* can move with no schema change. A Client Certificate's
  `valid`/`expiring`/`expired` is recomputed on every read against
  `certificate_expiring_within_days`, so a client branching on it reads a different answer once
  an Admin changes that setting (§13.2).

## Unreleased

`GET /api/v1/cluster` gains `permissions_enforced`. Whether grants are enforced was readable
only through `GET /api/v1/admin/permissions/enforcement`, which answers `403` to an Operator --
so the people reading a screen of grants had no way to discover that the rules end in a
catch-all allowing everything to everyone, and that is the **default**. The switch stays
Admin-only; only the reading moved, onto the route that is already the one to poll. One more
MongoDB read and still nothing outbound.

`GET /api/v1/audit` takes four more filters: `outcome`, `resource`, `action` and `apply_id`.
Additive, so nothing breaks. `outcome=refused` is the one worth knowing about — refusals were
always recorded, and asking for them meant paging a category and filtering client-side, which
got worse the longer a deployment had existed. `outcome` and `action` are closed sets, so a
value outside the vocabulary is a `422` rather than an empty page that reads as "nothing
happened". Each new filter has an index, because with no retention by default this collection
outlives everything else in the product.

`GET /api/v1/cluster/drift` is new, and answers whether the Cluster still holds what Apchi
committed to it. Per Section: in sync, diverged, unknown, or not comparable — the last being
Catalogs, which reach Trino as statements rather than as a mounted file, so no file diff can
speak for them. The overall `diverged` verdict is three-valued for the same reason `verified`
is: null means no Section is known to have drifted but at least one could not be read, and an
all-clear nobody verified is not an all-clear. Until now the only trace of drift was a Section
named in `cost.rollout_sections` with nothing in the diff to account for it, which covered the
three rollout-required Sections and required somebody to notice an absence.

`GET /api/v1/cluster` and `GET /api/v1/cluster/probe` are new, and answer what §21 asks to
be visible at all times: the committed Snapshot, what is known about the Cluster's health,
and whether Maintenance Mode is engaged. The first touches nothing outside MongoDB, so it is
the one to poll; the second asks the coordinator and takes `?workers=true` for a count that
costs a SQL query. Maintenance Mode is readable by an Operator there —
`PUT /admin/maintenance-mode` stays Admin-only, and so does reading it at that path.

`GET /api/v1/auth/sign-in` is new, and answers without a credential in every mode: it
reports which of the three authentication modes is configured, and nothing else. Nothing
could tell `password` from `sso` before it, because `/auth/me` refuses in both and only the
prose of the refusal differed — so a client had to branch on an error message, which made
that message a contract nobody meant to publish.

`GET /api/v1/review` and the Section Revert preview now report `cost.rollout_sections`: which
Sections the Rollout is for, rather than only whether there is one. Additive, so nothing
breaks. A client that held its own table of which Sections restart Trino can drop it, and a
Section named there with no entry in the diff is a Rollout that no staged change accounts for
— what Apchi would write is not what the Cluster has mounted.

## 0.1.1 — 2026-11-02

The first published release, and the baseline this file measures from. The six Sections
through the full lifecycle — stage, review, validate, apply, verify, commit — on both
deployment shapes, with Adoption, three authentication modes, the audit history, and outbound
TLS to all four destinations. Earlier history is the commit log.

Published as `ghcr.io/infinityhub123/apchi:0.1.1` and
`oci://ghcr.io/infinityhub123/charts/apchi` at version and appVersion `0.1.1`.
