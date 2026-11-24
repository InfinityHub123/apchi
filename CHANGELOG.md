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

### Breaking

- **Apchi refuses to start on an `APCHI_` variable it does not read**, naming it and the
  setting it is closest to. A misspelt or invented setting used to be ignored, leaving Apchi
  on the default it was meant to replace. Kubernetes' service-link variables are exempt.
- **Session cookies are always `Secure`.** They used to be only in `prep` and `prod`. Serve
  Apchi over TLS, or reach it on `http://localhost`, which browsers treat as secure.
- **`environment` is deprecated** and removed in the next release. Any value is accepted now,
  not only `np`/`test`/`prep`/`prod`; while set it still decides the log level and format,
  with a warning. Unset, logging defaults to INFO and console lines: set `APCHI_LOG_LEVEL` and
  `APCHI_LOG_JSON` (chart: `logging.level`, `logging.json`). The chart no longer sets it.

### Changed

- **MongoDB's client certificate and key can be two files**: `APCHI_MONGO_CERTIFICATE_FILE`
  and `APCHI_MONGO_KEY_FILE` (chart: `mongodb.tls.certificateKey`, `.keyKey`), joined by Apchi
  into one private file whatever newline either ends with. One combined file still works.
- The chart sets `enableServiceLinks: false`.
- **`/docs` and `/redoc` work without internet access.** Swagger UI 5.34.1 and Redoc 2.5.4 are
  served from the image instead of a public CDN, and Redoc no longer loads Google Fonts.
- **The chart's CA bundle can come from a ConfigMap** (`pod.caBundle.configMap`), such as
  OpenShift's injected trusted-CA bundle.
- **Apchi runs on OpenShift's `restricted-v2`.** The image runs as any uid: its tree belongs
  to group 0 and its `USER` is the number `10001`. Every pod the chart renders now sets
  `runAsNonRoot`, the `RuntimeDefault` seccomp profile, no privilege escalation and every
  capability dropped. A cluster that forbids pinning `fsGroup` sets `restrictedPodSecurity`
  (`APCHI_RESTRICTED_POD_SECURITY`). The Validation pod then carries no `fsGroup` and the
  same security context, and Apchi accepts `0644` credential mounts on the Trino pods
  instead of refusing them. `validation.resources` (`APCHI_VALIDATION_RESOURCES`) gives the
  Validation pod requests and limits.

### API additions

`AuditEvent` gains `token_name`: the acting token's name as it was when the event was written.
Only `token_id` was recorded, and `GET /tokens` lists only the caller's own, so an Admin reading
the trail could not say which automation of somebody else's acted. `GET /auth/me` reports it too
for a caller authenticated by a token. Events written before this carry null.

`ApplyRecord` gains `kind` (`apply` or `admin_apply`), `started_by` and `started_via`. Both
pipelines walk the same stages and an Admin Apply commits no Snapshot, so an Admin Apply read
exactly like an Operator's Apply that failed to commit; and who started an Apply was only in the
audit trail. Records written before this read as `apply` with a null `started_by`.

`ApplyRecord` also gains `failures`: every reason Validation refused the Apply, structured as a
Validation's own record has them. They were only in `failure_reason`, joined into one sentence,
so saying which Section failed meant parsing prose.

A `reason` sent when releasing Maintenance Mode is now kept as the `released` audit event's
`detail`. It was accepted and dropped, and releasing clears the state, so why a Cluster was
judged safe to edit again was recorded nowhere.

`ApplyRecord` gains `verification`: one entry per check, in the order Verification runs them,
each `passed`, `failed` or `not_reached`. Before this an Apply recorded only that Verification
passed or a sentence saying why it did not, so "which check failed" meant parsing prose.

`not_reached` is the third value because Verification stops at the first failure -- there is no
point asking a broken Cluster the next question, and Auto Rollback is waiting -- so at most one
check is ever `failed` and the rest after it were never asked. The per-Section checks come from
the registry, so the list grows with the Sections; a client should not count on its length. Empty
when Verification never ran, and never overwritten by Auto Rollback's own Verification of what it
restored, which would otherwise read as every check passing on an Apply that failed.

`POST /api/v1/certificates/inspect` is new, and reads an uploaded archive without staging it:
which member was taken as the certificate and which as the key, what would be converted, the
resulting certificate, the certificate it would replace, and which Catalogs present that name
today. Uploading under an existing name replaces it immediately -- that is how renewal works --
so a wrong file silently swapped out a working certificate and the only way to see what was in
an archive was to stage it.

A separate route rather than a flag on the upload. The reverts take `dry_run` in a JSON body,
where an unknown field is a `422`; this endpoint takes multipart form data, where an unknown
form field cannot be refused the same way, so a mistyped flag would have been ignored and the
certificate staged for real. A path with no save in it cannot do that.

It refuses in exactly the cases the upload refuses, because the validating half of
`store_certificate` was split out and is what both call. `Bundle` gains the two member names
and the conversions, which the ZIP reader previously discarded.

`GET /api/v1/review` gains `size_bytes` and `limit_bytes`, and `ClientCertificate` gains
`expiring_within_days`. Both are numbers an interface was already rendering something about
without being able to name it: how close the Candidate is to the ceiling it gets refused at,
and the window `status` was computed against.

`size_bytes` is measured as BSON of the document as stored, which is what the ceiling applies
to -- the JSON text is a different number by enough to matter at that size. `limit_bytes` is
reported beside it rather than left for a client to carry, since a deployment may change
`candidate_max_bytes` and a client holding its own copy would warn at the wrong number.

`expiring_within_days` sits next to the `status` it explains. The warning at the top of this
file -- that `status` is recomputed on every read, so a client branching on it reads a different
answer once an Admin changes the setting -- is now something a response says for itself.

`POST /api/v1/candidate/reset-resource` is new, and Resets one staged change: the resource goes
back as the Candidate's base Snapshot has it, and every other staged change stays -- including
other people's in the same Section. The smallest undo used to be a Section Revert, which takes
everyone's edits to that Section with it. It works for Client Certificates, which re-sending a
Snapshot's version from outside cannot, and it is a `404` when the resource has nothing staged.

The resource is named in the body rather than the path, because Resource Groups keys its
selector list as `#selectors` and `#` in a URL starts the fragment. A separate route rather than
an optional body on `POST /candidate/reset`, because that one discards everything and a client
that forgot the body would get exactly that. Audited as `reset` with `resource` set, which is
what distinguishes it from a Reset of the whole Candidate.

`GET /api/v1/audit/export` is new, and is the audit listing with no paging: every event
matching the same filters, as NDJSON, streamed. One JSON object per line, each line exactly the
`AuditEvent` the listing returns -- so nothing had to decide a column order or how to flatten a
null, and `openapi.json` already documents the contents. A page holds at most 500 and retention
is off by default, so until now the longest-lived collection in the product was the one that
could not be taken out of it.

Two things to know about it. It is **not Admin-only**: the listing is Operator-readable
deliberately (ADR-0002), and the same data in a different shape does not deserve a different
gate. And an export is **itself recorded in the trail** -- category `audit`, a new third
non-Section category, action `exported`, with the filters it used in the detail -- where reading
a page still is not. Taking a copy leaves somebody holding the trail outside Apchi's rules
about it; reading a page in a console does not. `Category` and `Action` each gained a value, so
a client with an exhaustive `switch` over either sees a value it does not know.

`GET /api/v1/cluster` gains `permissions_enforced`. Whether grants are enforced was readable
only through `GET /api/v1/admin/permissions/enforcement`, which answers `403` to an Operator --
so the people reading a screen of grants had no way to discover that the rules end in a
catch-all allowing everything to everyone, and that is the **default**. The switch stays
Admin-only; only the reading moved, onto the route that is already the one to poll. One more
MongoDB read and still nothing outbound.

`GET /api/v1/catalogs/connectors` and `GET /api/v1/event-listeners/types` are new, and return
the curated property schemas the write endpoints already validate against: required, optional,
branches, open-ended properties, accepted prefixes, and which properties carry a credential.
They existed only in code, so anything building a form carried a second copy of them that drifts
silently -- which is what the frontend was doing for all seven connectors.

`PropertySchema` gains `secrets`, marked per connector rather than inferred from the name.
A name test cannot do this job: `mongodb.connection-url` embeds the password because that
connector has no separate one, and `s3.aws-access-key` reads like a secret while being an
identifier that travels in every signed request. The log redaction filter in `app.logging` stays
a name test, deliberately -- it errs towards hiding too much, which is right for a log file and
wrong for a form that has to say which field takes a password.

`POST /{section}/revert` and `POST /api/v1/candidate/rollback` take `dry_run` in the request
body, and `RevertEffect` gains `staged`. A revert staged immediately and reported its effect
afterwards, so the only way to see what a Full Rollback amounted to -- every staged change in
all six Sections discarded, including other people's -- was to cause it. The effect was always
computed from the Candidate in memory, so a dry run is the real answer rather than an estimate;
what the real call adds is the save, and a dry run skips it. It therefore records no audit entry
either, because the audit is written by that save.

`staged` is additive but worth reading: it is the only field distinguishing a preview from the
act, so a client that ignores it cannot tell them apart.

The flag is in the body rather than a query parameter deliberately. `RevertRequest` forbids
unknown fields, so `dryrun` is a `422`; a mistyped query parameter would have been ignored and
the revert it was meant to make harmless would have gone through.

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
