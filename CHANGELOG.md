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
- **Apchi runs on OpenShift's `restricted-v2`.** The image runs as any uid: its tree belongs
  to group 0 and its `USER` is the number `10001`. Every pod the chart renders now sets
  `runAsNonRoot`, the `RuntimeDefault` seccomp profile, no privilege escalation and every
  capability dropped. A cluster that forbids pinning `fsGroup` sets `restrictedPodSecurity`
  (`APCHI_RESTRICTED_POD_SECURITY`). The Validation pod then carries no `fsGroup` and the
  same security context, and Apchi accepts `0644` credential mounts on the Trino pods
  instead of refusing them. `validation.resources` (`APCHI_VALIDATION_RESOURCES`) gives the
  Validation pod requests and limits.

## 0.1.1 — 2026-11-02

The first published release, and the baseline this file measures from. The six Sections
through the full lifecycle — stage, review, validate, apply, verify, commit — on both
deployment shapes, with Adoption, three authentication modes, the audit history, and outbound
TLS to all four destinations. Earlier history is the commit log.

Published as `ghcr.io/infinityhub123/apchi:0.1.1` and
`oci://ghcr.io/infinityhub123/charts/apchi` at version and appVersion `0.1.1`.
