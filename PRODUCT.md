# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Delegated. React + Vite + TypeScript, TanStack Query for server state, Tailwind for styling,
`openapi-typescript` + `openapi-fetch` for a client generated from the committed
`openapi.json`. Chosen because §23 says a conventional choice beats a clever one, and because
the API is mostly request/response polling plus a single SSE stream, which is what TanStack
Query is shaped for.

The SPA is served by FastAPI from the same origin. Not a preference: sessions are `httponly`,
`samesite=lax` cookies (`app/api/auth.py:63`), and a `lax` cookie is not sent on a
cross-origin request, so a separately-hosted SPA could not authenticate at all. Vite proxies
`/api` in development to keep one origin there too.

## Users

**Operators** — platform staff at the organisation Apchi is operated for. The primary users.
They own all six Sections: catalogs, client certificates, certificate mapping, permissions,
resource groups, event listeners (§3).

**Admins** — the people operating the underlying platform. Everything an Operator can do,
plus injecting arbitrary low-level Trino configuration and freezing Operator mutation (§14).

**End Users** issue queries to Trino and are *never* Apchi users. They are the subject of
permissions and resource groups, never an audience for this interface (§3). Nothing in the UI
is designed for them.

### The situation

Routine-primary, incident-capable. The staged loop — stage, review, validate, apply, verify,
commit — is everyday configuration work done deliberately, often by several Operators against
one shared Candidate. The incident path exists and is rarer: a failed Apply, an Auto Rollback,
Maintenance Mode. The console is optimised for the first and must not be slow or confusing in
the second.

## Product Purpose

Let Operators work with high-level Trino concepts instead of hand-editing authorization rules,
catalog property files, certificate mappings and resource-group files (§1).

Nothing reaches the cluster until an Apply. Validation runs the configuration past a real
throwaway Trino first, so a coordinator that would refuse to start fails the validation rather
than the live cluster. A failed verification rolls the cluster back by itself. What survives is
a numbered, immutable Snapshot that can be returned to.

Success is an Operator making a configuration change they understand the cost of, without
reading a Trino properties file or restarting a coordinator by hand.

## Positioning

Every capability in the UI is also in the REST API, and the UI consumes that same public API
with no privileged backdoor (§1, §22). So "nobody needs to automate against or scrape the UI"
is structural rather than a rule someone has to remember — a neighbouring product with a UI
built on private endpoints could not truthfully claim it.

The second claim a neighbour could not copy: an Apply states its cost before it runs. The
review response carries whether a rollout is required and `queries_at_risk` — "queries running
or queued on the Cluster right now, every one of which a Rollout terminates. A live reading and
not a promise."

## Constraints and product facts future work must preserve

- **One cluster.** Apchi configures exactly one Trino cluster and reads and writes exactly one
  Kubernetes namespace. There is no cluster switcher and no multi-tenancy to design for.
- **The Candidate is shared.** It is one document, not per-user drafts (ADR-0002). The UI must
  show the *whole* Candidate diff, every Section, not only the current Operator's changes, and
  say who changed what (§21).
- **Staged is not live.** Staged changes must be clearly marked as not yet affecting
  production (§21).
- **A rollout kills queries, and the UI must say so in those words.** When a Candidate requires
  a rollout, the Apply confirmation must state that running queries will be terminated, in
  those words, with a live running-query count from the coordinator (§21).
- **Three failure messages are specified verbatim** in §21 for Validation, Apply and
  Verification failure. They are product copy, not placeholder text, and must not be reworded.
- **Apply and Validate are resources, not calls.** They take minutes. `POST` returns `202` with
  an id; progress is a stage history in MongoDB plus an SSE stream (§19). The UI reflects
  durable state, not in-process progress.
- **Auth has three modes.** `none` (every caller is an Admin, the default), `password`, and
  `sso`. In `none` there is no login and no user to show. Sessions are httponly cookies, so
  the SPA never holds a token and cannot read one.
- **Vocabulary is fixed.** `CONTEXT.md`'s terms are the identifiers in the code and must be the
  words in the interface: Snapshot, Candidate, Section, Apply, Rollout, Validation,
  Verification, Commit, Reset, Section Revert, Full Rollback, Auto Rollback, Maintenance Mode,
  Trust Bundle. An `Identity` is an Apchi caller; a `Trino Identity` is whoever runs a query.
  No synonyms.
- **Six Sections, and they differ in cost.** Catalogs apply by DDL with no restart; permissions
  and client certificates are file applies with no restart; certificate mapping, resource
  groups and event listeners force a rollout. The UI marks which is which (§21, §7).

## Accessibility

WCAG 2.2 AA, keyboard-complete. Not specified anywhere in the repository — recorded here as a
decision rather than a discovered fact. The reasoning: these are platform engineers who work in
terminals and will reach for the keyboard before the mouse, and a dense dark console makes
contrast a genuine failure mode rather than a compliance checkbox.

## Voice

Plain and exact, taken from the existing documentation and the specified failure messages. It
names the mechanism and the cost, and does not reassure. "Configuration was applied, but the
Trino cluster did not pass health verification. The previous Snapshot remains the known-good
configuration." No exclamation marks, no congratulation on success, no softening of what a
rollout destroys.

## Open decisions

- Whether the UI eventually needs a view for the audit history beyond the single `/audit`
  endpoint that exists today.
- Whether Maintenance Mode gets its own screen or is a banner plus an action on the shell.
