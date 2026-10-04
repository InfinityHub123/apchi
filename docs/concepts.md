# Concepts

Apchi has one idea in it: configuration changes are staged, reviewed and applied as a unit,
and what has been applied is kept forever. Everything else follows from that.

If you want the terms alone, `CONTEXT.md` is the glossary and its words are the identifiers
in the code. This page is the same material in the order you meet it.

## The loop

```
         ┌──────────── you edit here ────────────┐
         ▼                                       │
  Configuration Candidate ──review──▶ validate ──┘
         │
      apply ──▶ rollout (if needed) ──▶ verify ──▶ commit ──▶ Snapshot N
         │                                 │
         │                            failed? auto rollback
         ▼
   the running cluster
```

**Configuration Candidate.** The one mutable configuration. Every edit any operator makes
lands here, and nothing in it reaches Trino. There is exactly one — it is not per-user and
not per-change — which is why review shows you every section rather than only the one you
touched. An apply promotes all of it.

**Review.** The diff between the Candidate and the Snapshot the Candidate came from, plus
what applying it would cost the running cluster. Not a dump of what is staged: if you stage a
catalog and then delete it again, review shows nothing.

**Validation.** Whether the Candidate *should* be applied, decided before the cluster is
touched. For anything a coordinator could refuse, Apchi starts a throwaway coordinator, feeds
it the generated files and asks it — so a resource group file that would stop Trino booting
fails your validation instead of your cluster. It names the resource responsible rather than
handing you a coordinator log. An apply validates first; validating separately is for
checking without committing.

**Apply.** The only moment anything reaches Trino. One at a time, and the Candidate is frozen
while one is in flight.

**Rollout.** Restarting the coordinator, for configuration Trino adopts no other way. Trino
cannot drain a coordinator, so a rollout destroys every running and queued query — there is
no way to soften it, which is why review tells you the number beforehand.

**Verification.** Whether the cluster adopted the configuration and is still healthy, decided
after the apply. Trino exposes nothing saying which configuration is live, so verification is
functional rather than introspective: the coordinator answers, the workers have registered,
and a real query runs — and then each section says what it can about its own configuration.
What a section can say varies, and the honest answer is sometimes nothing. Resource groups
check which group that real query actually landed in. Catalogs ask the coordinator which
catalogs it has. Permissions and client certificates assert nothing yet: proving a rule is in
force means asking the cluster what an identity may do, and whether a certificate works is
something only the data source behind it knows.

**Commit.** Recording the verified Candidate as the next **Snapshot** — immutable, numbered
sequentially, and the complete configuration rather than a diff.

**Auto rollback.** If an apply or verification fails, Apchi returns the cluster to the latest
Snapshot: one attempt, bounded. It creates no Snapshot. If it also fails, Apchi engages
maintenance mode and alerts, because at that point no automatic action is safe.

## Undoing things

Four different things, deliberately not one:

| | What it does | Reaches the cluster |
| --- | --- | --- |
| **Reset** | Throws away everything staged, re-derives the Candidate from the latest Snapshot | No — nothing staged ever reached it |
| **Section revert** | Replaces one section of the Candidate with its content from an earlier Snapshot | Only when you then apply |
| **Full rollback** | Replaces the whole Candidate with an earlier Snapshot | Only when you then apply |
| **Auto rollback** | Returns the cluster to the latest Snapshot after a failure | Yes, by itself |

Section revert is the routine recovery: one catalog broke, the other five sections are
nobody's business. Full rollback is for a disaster and is never implicit.

Neither rewrites history. Rolling back to Snapshot 1 while Snapshot 2 is current produces
Snapshot 3, whose content equals Snapshot 1's. So a rollback is itself something you can roll
back, and the sequence of Snapshots is always the sequence of things that were really live.

## Sections, and what an apply costs

A **section** is one managed area of the configuration, and the unit of revert. There are six,
and which of three apply engines a section uses is a fact about how Trino consumes that
configuration — never about when your edit takes effect. Everything waits for apply.

| Section | Engine | Cost |
| --- | --- | --- |
| Catalogs | DDL — `CREATE CATALOG` against the running coordinator | None |
| Permissions | A file Trino re-reads on its own refresh timer | None |
| Client certificates | A new file in a directory Trino already has mounted | None |
| Certificate mapping | A file read once, when the authenticator is built | **Coordinator restart** |
| Resource groups | A file read once, at startup | **Coordinator restart** |
| Event listeners | A file read once, at startup | **Coordinator restart** |

A single apply mixing both kinds restarts once, at the end, after everything is delivered.

The no-restart sections are cheap, not instant, and this is the one place where a
successful apply does not mean the cluster is already running what you asked for. A file
Apchi writes reaches the pod when the kubelet next syncs its volume — up to its
`syncFrequency`, 60 seconds by default — and Trino then re-reads it on its own
`security.refresh-period` timer. The two delays add, Apchi does not wait them out, and
there is nothing it can ask the cluster to find out whether the file has landed: Trino
exposes no view of the rules it loaded. So a permissions apply succeeding means the file was
written correctly and the cluster is healthy. Expect the grant itself a minute or two later.

## Who owns what

**Operators** are the platform staff Apchi is for. They own all six sections, which is most
of the API.

**Admins** run the underlying platform. They own the few decisions that are not an operator's
to make, and those decisions are deliberately not part of a Snapshot, so a rollback never
undoes one:

- **Permissions enforcement.** Apchi ships a catch-all rule allowing everything no grant
  mentions, because writing grants on a cluster already serving users must not cut off
  everyone you have not reached yet. Removing that catch-all — making grants actually
  restrictive — is an admin's switch, and from then on an identity with no grant has no
  access.
- **Maintenance mode.** Operators keep read access; every configuration change is refused.
  For platform upgrades, and what an auto rollback failure engages.
- **Preserved certificate mapping patterns**, for a migration that has to keep an old pattern
  working.

**End users** query Trino. They are never Apchi users — only the subject of permissions and
resource groups.

## What Apchi is not

It does not install or upgrade Trino, and it does not own the Trino deployment. It configures
one cluster that somebody else deployed, and it needs that deployment to satisfy a set of
preconditions — a writable catalog store, the generated files mounted where Apchi puts them,
a refresh period on the access control file. Apchi checks them before every apply and names
the one that failed rather than half-applying.

It manages one cluster. One Apchi, one Trino, one namespace.

And it manages what it configured. Configuration that predates Apchi is not imported yet
(see **Adoption** in `CONTEXT.md`), which matters most for catalogs: the durable copy of every
catalog is a Secret Apchi rewrites, and the coordinator reseeds from it at every start, so a
catalog Apchi does not hold disappears at the next restart.

## Further reading

- [`getting-started.md`](getting-started.md) — the loop above, run for real.
- `CONTEXT.md` — the glossary.
- `apchi_implementation.md` — the design, and the reasoning behind every choice in it.
- `adr/` — the decisions that went the other way first, and why they were changed.
