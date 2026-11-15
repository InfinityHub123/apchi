---
version: 1
slug: "web-src-app-tsx"
primary_target: "web/src/App.tsx"
related_targets: []
---

## Direction contract

THESIS: One Trino cluster's configuration, staged as a shared Candidate whose cost is stated
before anyone applies it. It refuses the category default of a settings app — a tree of forms
that save on blur — because nothing here reaches the cluster until an Apply, and the Operator
has to be able to see the whole pending change and what it will destroy.

OWN-WORLD: Light, dense, administrative. Ground #f5f6f8 with #fafbfc and #f1f3f6 surfaces,
#e4e7eb hairlines, a six-step ink ramp from #16191d to #98a1ad, and #1f5fd0 as the only
primary. Five semantic tones carry every state as fg/bg/border triples — green #15803d, blue
#1f5fd0, amber #9a5b06, red #a81f1f, gray #5c6672 — rendered as 22px pills at 11.5px/550 with
an optional 6px dot. Inter for prose, JetBrains Mono for every identifier, path and number.
Tables are the main instrument: 34px headers at 10.5px uppercase on #fafbfc, 44px rows at
12.5px. Lucide icons at 1.8 stroke. Recognizable with all content removed by the hairline
table grid, the mono identifiers, and the pill vocabulary.

STORY: The Operator understands that what they are looking at is staged and not live, that
someone else may have staged part of it, and exactly which changes force a coordinator
rollout. They believe the number next to "queries at risk" because it is read live and
labelled as already stale. They either apply it deliberately or reset it.

FIRST VIEWPORT: A 44px top bar — mark, cluster name, health pill, worker count, caller. Under
it, when the cluster is unhealthy, a full-width #a81f1f band naming the restored Snapshot.
Under that, a #eef4ff Candidate strip: "Candidate · based on Snapshot 118 · 4 changes · 3
apply immediately, 1 forces a rollout" with Reset, Validate and Apply pushed right. Left rail
of the six Sections with change counts. The work area is Overview: Cluster, Committed
configuration, Candidate, and a Recent changes table whose last column is the cost pill. The
primary action sits in the Candidate strip, present on every screen.

FORM: Pinned whole by the user's own Claude Design prototype, so no roll and no concept
tournament. seed: none (pinned) — "what about this one?
https://claude.ai/artifact/MgHQneJ58aRQNvvhesQcXE#artboard-fd9d9c194cfe", then "yes, use the
glossary terms and start slice 1". The prototype's visual system is adopted wholesale; its
vocabulary is not. Every term is translated to CONTEXT.md: transaction → Candidate, version →
Snapshot, Verify & Commit → Apply, Discard → Reset, Restore → Full Rollback, Identity →
Trino Identity where it means whoever runs a query, ConfigAPI → Apchi. ADR-0002 is "A single
Configuration Candidate, not per-user transactions", so "transaction" is not a synonym this
product may use.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the
verdict, DESIGN.md, and every shipping raster carrying its provenance.

## Two places the pin does not satisfy the spec

The prototype's confirmation says "roughly 2 minutes of rolling worker restarts". §21 requires
the Apply confirmation to state that running queries will be terminated, in those words, with
a live running-query count from the coordinator — `ApplyCost.queries_at_risk`, described in the
contract as "a live reading and not a promise". Both go in.

§21 fixes three failure messages verbatim, for Validation, Apply and Verification. They lead;
the prototype's sharper diagnostics ("Only 6 / 8 workers became ready within the 90s health
window") sit beneath as the detail.

## Two screens outrun the API

There is no per-catalog connection test — connectivity is checked inside Validation, and
`catalog_connectivity_blocks_apply` decides whether an unreachable catalog warns or fails. The
prototype's "Test connection" button has nothing to call and is dropped rather than faked.

Audit has exactly one endpoint, `GET /api/v1/audit`. The prototype's filters and Export imply
a surface that does not exist; the first build renders what the endpoint returns and no
affordance that lies about the rest.
