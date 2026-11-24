# Apchi — product and interface brief

What Apchi is, what it does, and what any interface for it has to express. Written for a
designer or a design system starting from nothing, so it assumes no prior knowledge.

**This describes the product only — its idea, its logic and its functionality.** There is no
interface to inherit from, no existing layout, navigation, palette or component language to
preserve or react against. Every visual and interaction decision is open.

The product is backed by a public REST API of 91 operations across 17 areas. Every screen
described here is a consumer of it, and the API is the limit of what any interface can know or
do — which is why this document describes capability and data rather than screens.

A small number of things genuinely cannot change: a required sentence, a vocabulary word, a
safety property. Each is marked **FIXED** with the reason. Nothing else in here is a
constraint — where a need is described, how it is met is the designer's.

---

## 1. What Apchi is, in one paragraph

Apchi is a control plane for configuring **one** Trino cluster. Trino is a distributed SQL
query engine; running it means maintaining a pile of configuration files — which databases it
can reach, who may read which table, how queries share CPU and memory, where query logs go,
which certificates it presents and trusts. Normally a platform engineer edits those files by
hand, copies them into Kubernetes, and restarts the coordinator. Apchi replaces that with a
REST API and a web interface: people work with high-level concepts (a *Catalog*, a *grant*, a
*resource group*) and Apchi renders the files, delivers them, restarts what needs restarting,
checks the cluster survived, and records an immutable numbered version of what it did.

**The single most important property:** nothing an operator types reaches the live cluster
until they explicitly **Apply**. Everything before that is staged and reversible.

---

## 2. Who uses it

Three roles. Only the first two ever see the interface.

| Role | Who they are | What they can do |
|---|---|---|
| **Operator** | Platform staff at the organisation Apchi is run for. The primary user. | Everything in sections 5 and 6 below: all six configuration areas, plus Validate, Apply, Reset, Revert, Rollback. |
| **Admin** | The people who run the underlying platform. | Everything an Operator can, plus: write arbitrary low-level Trino config, write raw files, engage Maintenance Mode, turn permission enforcement on and off. |
| **End User** | People who run SQL queries against Trino. | **Never an Apchi user.** They are the *subject* of permissions and resource groups and never an audience for this interface. Nothing is designed for them. |

They are platform engineers. They live in terminals, read dense tables comfortably, reach for
the keyboard before the mouse, and are hostile to interfaces that hide what they are about to
do. They are also **trusted** — there is deliberately no requirement to hide secret values
from them (see §12).

### Authentication modes

The deployment is configured in one of three modes, and the interface must handle all three.
The UI discovers which by **asking the API** — one question that returns the mode and nothing
else: no issuer, no client id, no hint about which users exist. It cannot be inferred from a
failed request, because the refusal is deliberately identical in `password` and `sso` mode.

- **`none`** — the default. There is no sign-in, no session, and every caller is treated as an
  Admin. The interface shows no login screen and no sign-out control, because there is no
  session to end. The caller is reported as an anonymous Admin, named `anonymous`.
- **`password`** — a username and password form. One shared refusal message for every kind of
  failure (see §11).
- **`sso`** — a full-page redirect to the organisation's identity provider and back. Nothing
  degrades on the way: a signature that does not verify, a claim that is unacceptable, a
  callback replayed from somebody's logs are all plain refusals, never a partial sign-in. The
  role comes from a group claim, and a mapping that matches nothing yields **Operator** — the
  safe failure being a person who can configure the six Sections but not reach `/admin/*`.

### What the interface actually knows about a person

Four fields, and there is no fifth: the **subject** (a username), the **role** (`operator` or
`admin`), **how they authenticated** (`none`, `password`, `sso` or `token`), and which token is
acting, when one is. No display name, no email address, no avatar, no profile, no preferences,
no "last seen". A design that wants a richer identity than a username will not be given one.

### Sessions

- **Server-side rows, not signed cookies.** A sign-out takes effect immediately, and so does
  disabling somebody — neither waits for a cookie to expire.
- **An HTTP-only cookie**, which the interface can never read. It never holds a credential and
  cannot tell whether it has a usable one except by making a request and seeing what comes back.
- **Expiry is absolute, not idle-based** — twelve hours by default. A session that began at 09:00
  ends at 21:00 whatever the person is doing. So a `401` can arrive on *any* request, including
  the submit of a form somebody spent ten minutes filling in, and the interface has to handle
  that without discarding work it could have kept.
- **The role is re-read on every request**, never taken from the session. Somebody can be
  demoted or disabled while signed in: their session continues at the new role, or stops
  working entirely. An Admin-only control that was visible a minute ago can start answering
  `403`, and that is a normal event rather than a bug to hide.

### There is deliberately no account administration

None of this exists in the API, in any mode, and its absence is deliberate rather than
unfinished: no self-registration, no email verification, no password reset, no
change-your-own-password, no user list, no role editor, no invitations. Apchi's users are
platform staff somebody onboards, not members of the public, and those flows are most of what a
batteries-included auth library would have brought.

Concretely, by mode:

- In **`password`** mode, one bootstrap Admin is created from deployment settings, and only on a
  deployment with no users at all. Any further account exists because the platform put it in
  Apchi's database. No request and no screen can create one.
- In **`sso`** mode there is no provisioning step whatsoever: whoever the identity provider
  authenticates has an Apchi user, recorded at login. The user records are a **mirror** of what
  the provider last said, not a second place to administer people, and the role is re-read from
  the claim at every login — so a group change takes effect the next time the person signs in,
  and not before.

People are **disabled, never deleted**, because the audit trail names actors and an actor that
no longer resolves reads as a bug in the trail rather than as somebody who has left.

What that means for a design: there is no account-settings screen, no team-management screen and
no "users" entry in whatever navigation gets built. The only person-shaped surfaces in the whole
product are *who am I* — with a sign-out where there is a session to end — and the API tokens of
§6.12. **API tokens** are long-lived credentials an operator mints for automation; they never
log a person in.

---

## 3. The one idea the whole interface is built around

```
EDIT  →  VALIDATE  →  APPLY  →  VERIFY  →  COMMIT
```

- **EDIT** — operators change things. These changes accumulate in a **Candidate**, which is a
  staging area. Nothing has happened to the cluster yet.
- **VALIDATE** — optional, on demand. Apchi starts a *throwaway Trino coordinator* in
  Kubernetes, feeds it the proposed configuration, and sees whether it would start. Takes
  minutes. Touches the live cluster not at all.
- **APPLY** — the operator commits to it. Apchi runs validation again, writes the real
  configuration, restarts the coordinator if the change requires it, verifies the cluster came
  back healthy, and only then records a **Snapshot**.
- **VERIFY** — six checks against the live cluster (coordinator responding, workers registered,
  a real query runs, the catalogs are actually there, the resource group rules took effect).
- **COMMIT** — a numbered, immutable Snapshot is created. This is the only thing that creates
  one.

If anything fails before COMMIT, **no Snapshot is created** and the previous one remains the
known-good configuration.

**FIXED — the interface must make "staged" unmistakable.** At every moment, on every screen, a
person must be able to tell whether what they are looking at has reached the cluster or not.
This is the product's core safety property. How it is expressed is entirely open.

---

## 4. The vocabulary

**FIXED.** These are the product's own words. They are the identifiers in the code and the
nouns in the API, and the interface must use these and no synonyms — a UI that says
"transaction" where the system says "Candidate" produces support conversations where two people
mean different things. Only the words are fixed; nothing about how they are presented is.

| Term | Meaning |
|---|---|
| **Candidate** | The single shared staging area holding every edit not yet applied. There is exactly one, shared by everyone. Not a per-user draft, not a branch, not a transaction. |
| **Snapshot** | A numbered, immutable record of a configuration that was applied **and verified**. Numbered sequentially: 1, 2, 3… Never "v12" or a hash. |
| **Section** | One of the six configuration areas (§5). |
| **Apply** | Promoting the Candidate to the live cluster. Runs the whole pipeline. |
| **Rollout** | Restarting the Trino coordinator. Destroys every running query. |
| **Validation** | The pre-flight check against a throwaway coordinator. |
| **Verification** | The post-apply health check against the live cluster. |
| **Commit** | Creating the Snapshot. The final step of a successful Apply. |
| **Reset** | Discarding everything staged, returning the Candidate to the latest Snapshot. |
| **Section Revert** | Replacing *one* Section of the Candidate with its content from an earlier Snapshot. Still has to be Applied afterwards. |
| **Full Rollback** | Replacing the *entire* Candidate with an earlier Snapshot. Still has to be Applied afterwards. |
| **Auto Rollback** | Automatic, not an operator action. Apchi undoing a failed Apply by itself. Creates no Snapshot and never retries. |
| **Maintenance Mode** | Operator edits disabled. Engaged by an Admin, or automatically after an incident. |
| **Trino Identity** | Whoever runs a query — the subject of permissions. |
| **Identity** | An Apchi caller. **Different from a Trino Identity**, and the two are easy to conflate: one is a person using this interface, the other is a database user. |
| **Client Certificate** | A certificate **Trino presents outward** when connecting to an external system. Not a certificate presented to Trino. |
| **Certificate Mapping** | How a caller authenticating **inward to Trino** becomes a Trino Identity. The opposite direction from the above. |
| **Raw File** | An arbitrary Trino config file an Admin writes directly. Never part of a Snapshot. |
| **Audit Event** | One recorded action: who did what, when, and whether it succeeded. |

---

## 5. The six Sections, and what each costs

The Candidate is divided into six Sections. **The critical property is that they do not cost
the same to apply.** Three can be applied to a running cluster with no interruption; three
require restarting the coordinator, which kills every query in flight.

| Section | What it is | Cost to apply |
|---|---|---|
| **Catalogs** | The external systems Trino can query — a PostgreSQL database, a Kafka cluster, an Iceberg warehouse. Each has a *connector* type and a bag of properties. | **No restart.** Applied by issuing SQL directly to the running cluster. |
| **Client certificates** | Certificate + private key pairs Trino presents when it connects outward (e.g. to a TLS-requiring PostgreSQL). Uploaded as a ZIP; Apchi extracts, validates the pair matches, converts formats. | **No restart.** |
| **Permissions** | Who may do what: a Trino Identity, a catalog, optionally a schema and table, and a set of privileges: `SELECT`, `INSERT`, `UPDATE`, `DELETE`, `OWNERSHIP`, `GRANT_SELECT`. Operators never see the underlying authorization JSON. | **No restart.** |
| **Certificate mapping** | A single regular expression that turns the certificate a caller presents into a Trino Identity. One controlled pattern, not a per-user table. | **Rollout.** Read only at coordinator start-up. |
| **Resource groups** | A tree of groups with CPU, memory and concurrency limits, plus an *ordered* list of selectors that route queries into them. First matching selector wins, so order is meaningful. | **Rollout.** The file is parsed once at start-up. |
| **Event listeners** | Where Trino sends query events — an HTTP endpoint, a Kafka topic. Typed with pass-through properties. | **Rollout.** |

The API reports which Sections an Apply's restart is actually for, so this does not have to be
read off the table above. A Section can also appear there with **nothing staged in it**, which
means what Apchi would write is not what the cluster has mounted — a restart that no visible
change accounts for, and one the interface has to be able to explain.

**FIXED — the interface must show which changes force a restart and which do not, per change,
not only in aggregate.** An operator deciding whether to apply now or wait for a quiet window
needs to know that three of their six edits are free and one costs every running query.

### Shape of the data, for layout purposes

- **A catalog** has a name, a connector type, an optional client certificate, and an open-ended
  property bag (typically 2–20 key/value pairs, values ranging from short identifiers to long
  JDBC URLs). Some connectors are *curated* (Apchi knows their schema and validates); others
  pass through unvalidated, and the interface should say which.
- **The curated schemas are readable from the API**, one call per Section, so a form never
  carries its own copy of them. Each returns, per connector or listener type: the required
  properties, the optional ones, the *branches*, the open-ended ones, accepted name prefixes,
  and **which properties carry a credential**. Four facts about that shape:
  - **A branch is a property whose value decides what else is required.** Iceberg's
    `iceberg.catalog.type` has six values, each bringing its own required properties and one
    bringing none; Hive, Kafka and Elasticsearch each have one too. A form that asks for a flat
    list of fields cannot express this — the second half of the form depends on one answer in
    the first.
  - **A branch's allowed values can be a closed list or open.** Null means any value is
    accepted and the listed ones are merely the values Apchi knows what to do with, which is a
    dropdown in the first case and something more permissive in the second.
  - **Which properties are secrets is stated, not guessable from the name.**
    `mongodb.connection-url` holds the password because that connector has no separate one;
    `s3.aws-access-key` reads like a secret and is an identifier. A design that masks by name
    gets both wrong. See §12 for what masking is and is not for.
  - **Curated is not the same as supported.** Any connector Trino has a plugin for is accepted;
    an uncurated one just gets no property checking until Apply. The curated list is seven
    connectors and two listener types, and each Catalog also reports whether its own connector
    is one of them.
- **A grant** has an identity, a catalog, optional schema, optional table, and one or more of six privileges.
  Omitting schema means "all schemas"; omitting table means "all tables". There are also
  system-owned rules Apchi maintains that nobody may edit, shown read-only.
- **A resource group** has a dotted path (`global.etl.batch`), a required concurrency limit, and
  eight optional limits — memory as either an absolute (`10GB`) or a share (`80%`), CPU as a
  duration, scheduling policy and weight.
- **A selector** has a target group and up to eight optional matchers, most of them regular
  expressions. **Order matters and must be editable.**
- **The resource-groups Section has its own settings** beyond the groups themselves — currently
  one: how often CPU limits reset. It is not optional decoration: a real coordinator **refuses
  to start** when a group sets a CPU limit and this is unset, so it belongs somewhere a person
  setting a CPU limit will encounter it.
- **A client certificate** has a name, subject, common name, issuer, SHA-256 fingerprint, valid-
  from and valid-until dates, and a status of `valid`, `expiring` or `expired`. "Expiring" means
  inside a configurable window (default 30 days) and is recomputed on every read.
- **A certificate mapping** is one regular expression, a replacement template (`$1` refers to a
  capture group), and a case-folding choice.
- **An event listener** has a name, a type, and a property bag.

---

## 6. Every screen the product needs

Not a proposed information architecture — a list of jobs the interface has to do. Grouping,
navigation and naming are the designer's to decide.

### 6.1 What must always be reachable

**FIXED as content, not as layout.** The specification requires all of this to be available at
all times. It does not say how — a persistent bar, a panel, a summary that follows the person,
or something else entirely are all open; what is fixed is that none of it may be somewhere a
person has to remember to go and look.

1. Cluster health, and the Snapshot currently committed. The API answers this in **two
   parts, which cost different things**, and a design has to decide what it shows by default.
   One read returns the committed Snapshot, whether Maintenance Mode is engaged, whether
   grants are being enforced, and the last Apply with whether its Verification passed — it
   touches nothing outside Apchi's own database, so it can be polled. A second, separate read asks the coordinator how it is
   right now, and optionally counts active workers. The live answer therefore costs a request
   to Trino each time, and the cheap one is **history**: it says what happened at the last
   Apply, not what is true now. Rendering the last Verification as a current reading would
   tell somebody the cluster is healthy because it was healthy when it was last applied to.
   A **third** read, the most expensive of the three, answers whether the cluster still holds
   what Apchi committed to it — see §6.15. It is not required to be always visible, but what
   it reports changes the meaning of everything that is.
2. Whether the Candidate has staged changes, **and by whom**.
3. The full Candidate diff — **every Section, not only the current operator's own changes**.
4. Which changes apply without a restart and which force one.
5. The available actions: Reset, Validate, Apply.

Point 3 matters more than it looks: the Candidate is *shared*. Two operators can stage changes
simultaneously, and applying ships both. Someone who stages one change and presses Apply must
see the other five they are also about to ship.

**The Candidate has a size ceiling, and the diff reports how close it is.** It is one database
document, so the limit is on everything staged together rather than on any one Section — and
because it is shared, somebody can be refused by configuration they did not stage. The review
reports the current size and the limit, both in bytes, so an interface can warn before an edit
is rejected rather than explain afterwards. The limit is reported rather than something a
client should carry its own copy of: a deployment may change it.

### 6.2 Reviewing what is staged

Two questions have to be answerable, and they are different questions:

- **What is in the Candidate** — which resources, in which Sections, were added, changed or
  removed; who staged each; and what each costs.
- **What one change actually does** — the fields that differ. "events_kafka changed" does not
  tell anyone whether a broker address moved or the entire topic list was replaced, and an
  operator about to apply needs to know which.

A Candidate can hold anything from one change to several dozen across all six Sections, and the
second question is only asked about a few of them. Whether that is one view or two, and how a
person moves between them, is open.

One content rule rather than a layout one: a diff is read to find what changed, so fields
identical on both sides are noise.

### 6.3 Editing each of the six Sections

Create, edit and delete for each, shaped by the data in §5. What each one demands:

- **Catalogs and event listeners** carry an open-ended property bag. Some types have a schema
  Apchi knows and validates; others accept arbitrary keys and pass them through unchecked. Both
  cases have to be editable, and a person should be able to tell which one they are in.
- **Resource group selectors are ordered, and the order is the logic** — first match wins. A
  person has to be able to see and change the order, and to understand that moving a rule
  changes which queries land where.
- **Client certificates arrive as an uploaded ZIP.** Apchi opens it, works out which part is the
  certificate and which the key, checks they match, converts formats, and reports what it found.
  The person never converts anything by hand. Upload can fail in ways worth explaining.

  **The archive can be read before it is staged**, on a separate endpoint, and this is worth
  designing around rather than treating as a nicety. Uploading under a name that already exists
  **replaces that certificate immediately** — which is how renewal works — so a wrong file
  silently swaps out something that works. The inspection answers, without staging anything:
  which member of the ZIP was taken as the certificate and which as the key, what would be
  converted, what the resulting certificate is, **what it would replace** (the whole certificate,
  so two expiry dates can be compared), and **which Catalogs currently present that name** and
  would therefore be re-pointed. It refuses in exactly the cases the upload refuses.

  Two things follow. Apchi identifies ZIP members by **what they parse as, never by their
  name** — so "we took `ca.pem` as your certificate" is the only way somebody notices they
  zipped the issuing CA instead of their leaf, and the interface should show it. And the
  inspection is a **separate request, not a flag**, because this is a file upload: a mistyped
  flag on a form submission cannot be refused the way one in a JSON body can, so it would have
  staged the certificate for real.

  **Each certificate reports the expiry window its status was computed against.** `status` is
  one of valid, expiring or expired, and it is derived from the expiry date on **every read**
  against a deployment-wide setting — so the same certificate reads `valid` under a 30-day
  window and `expiring` under a 400-day one, with nothing about it having changed. An interface
  that shows the status should be able to say *expiring within 30 days* rather than just
  *expiring*, and that number is on the certificate.
- **Permissions is the high-volume one**: many rows of five fields, usually read filtered to one
  identity or one catalog. It also contains system-owned rules nobody may edit.
- **Every Section can be reverted** to its content from an earlier Snapshot.
- **Any one staged change can be undone on its own** — a Reset of a single resource. It goes
  back as the Snapshot the Candidate was derived from has it: an edit loses its edit, a removal
  is restored, an addition disappears. Every other staged change stays, including other
  people's in the same Section, which is the difference from a Section Revert and why this is
  the natural per-row action in a diff. It works for every Section, certificates included, and
  it is refused when the resource has nothing staged rather than quietly succeeding.

### 6.4 Validating

Starts a background job that takes minutes. Returns:

- an outcome: `running`, `passed`, `failed`;
- **every** failure, not just the first — each naming the Section, the resource and a reason;
- warnings that did not stop it (e.g. the probe could not reach a database, which may just mean
  the probe's network differs from the coordinator's).

A Validation changes nothing. It can be run repeatedly, and does not freeze the Candidate.

### 6.5 Applying — the most consequential screen in the product

An Apply takes minutes and moves through stages:

```
validating → applying → rolling_out → verifying → committing → succeeded
                                                             ↘ rolling_back → failed
                                                                            ↘ incident
```

The interface needs to show the current stage, the history of stages, and the final outcome.
Progress is available as a live stream as well as by polling, and the record is durable — so a
person who closes the tab and comes back can see what happened. Each record says which pipeline
ran (`kind`: an Operator's `apply` or an `admin_apply`) and who started it (`started_by`,
`started_via`), so a history list needs no second read of the audit to fill either column.

**FIXED — the confirmation before a restarting Apply.** When the Candidate requires a rollout,
the confirmation must state **that running queries will be terminated, in those words**,
together with a live count of queries running or queued on the cluster right now. That count is
read live and is explicitly "a live reading and not a promise" — it is already stale when it
arrives, and the interface should not present it as a guarantee.

### 6.6 Failure, and the four fixed messages

**FIXED — these four messages are product copy and must not be reworded.** They were written
deliberately; a designer may decide entirely how they are presented, but not what they say.

> **Validation failed** — "Resource Groups configuration could not be loaded by Trino. No
> changes were applied."

> **Apply failed** — "Configuration application failed. The previous configuration is being
> restored."

> **Verification failed** — "Configuration was applied, but the Trino cluster did not pass
> health verification. The previous Snapshot remains the known-good configuration. Please
> contact our team if the cluster does not recover."

> **Incident** — "The Trino cluster is currently unhealthy after applying configuration. Please
> contact our team."

Alongside the fixed sentence, the record carries a **diagnostic failure reason** an operator
needs in order to work out what happened without access to the logs. Both should be present:
the sentence tells them what it means, the diagnostic tells them what broke.

### 6.7 The incident state

The worst case, and it has its own behaviour:

- An Apply failed, Apchi attempted **Auto Rollback once**, and that also failed.
- **Maintenance Mode engages automatically**, so nobody edits a cluster in an unknown state.
- The cluster is in an unknown configuration, and Apchi stops touching it.

The interface must make this state impossible to miss and must make clear that editing is now
disabled and why. It is distinct from an ordinary `failed` Apply, where the cluster *was* put
back and work can continue.

A separate case: an Apply can be marked **interrupted**, meaning Apchi itself restarted
mid-apply rather than the configuration failing. It should read differently from a failure.

### 6.8 Snapshot history and recovery

- A list of Snapshots, newest first, each with its number, when it was committed, and which
  Apply produced it.
- One Snapshot's full configuration, and a portable export of it.
- **Section Revert** and **Full Rollback**, both of which stage into the Candidate and then
  require a normal Apply.

**FIXED — what a revert must say.** A Section Revert produces a configuration **that has never
existed anywhere**: reverting resource groups to Snapshot 10 while everything else stays at
Snapshot 13 is a novel combination that was never validated or run. The interface must say what
it actually does — *"resource groups will be restored to Snapshot 10; all other sections stay at
Snapshot 13"* — and must **never imply a return to a known-good state**.

For catalogs, a revert issues real creates and drops against a live cluster. The warning must be
specific, and it must **name the catalogs that will be dropped, not count them** — an operator
about to lose one should see which one.

**Both take a `dry_run` flag, and the effect is the same either way.** Sending
`{"snapshot": 10, "dry_run": true}` reports exactly what the real call would do — which catalogs
would be created, dropped and replaced, which Snapshot the other Sections stay at, whether it
restarts the coordinator, and what that costs in running queries — and stages nothing. The
response carries `staged`, which is the only field that distinguishes a preview from the act, so
an interface that ignores it cannot tell them apart.

Three things follow for a design:

- **The confirmation step can show the real consequence rather than a description of one.** A
  dry run is not an estimate; it is the answer, computed the same way.
- **The flag belongs in the request body.** A mistyped query parameter would be ignored and the
  revert would happen for real, so Apchi only accepts it in the body, where an unknown field is
  refused. Worth knowing because it rules out building the preview as a URL somebody can hand
  round.
- **A preview needs the same permission as the act**, so it is refused while Maintenance Mode is
  engaged or an Apply is in flight. That costs little — when the preview is refused the revert
  is too — but a design must not rely on previewing during a freeze.

A `dry_run` leaves no audit entry, because it changed nothing. Only the real revert appears in
the trail.

### 6.9 Import and export

Configuration moves between clusters as a portable document, and this is a real workflow, not
a developer convenience.

- **Export** what is staged, or any Snapshot, as a document.
- **Import** a document, which **replaces the entire Candidate**. This is not a Reset: a Reset
  returns to the latest Snapshot and always leaves an empty diff, whereas an import replaces
  from a file and is *expected* to produce a large one. The interface should not conflate them.
- An import is **rejected whole, naming every problem**, before anything is stored — the
  Candidate is shared, so a partially-applied import would leave someone else's work sitting on
  top of a half-replaced configuration.
- A document says what the configuration **is**, not what to change: an omitted Section means
  that Section becomes empty, not that it is left alone. That is a genuinely dangerous
  misreading and worth designing against.
- Admins export and import a wider document that also carries the Admin values. An Operator's
  document and an Admin's document are the same shape, and feeding an Operator document to the
  Admin route must not silently wipe the Admin values.

### 6.10 Apply and Validation history

Both are durable records with their own browsable histories, not just a transient progress bar.
An operator investigating "what happened last Tuesday" reads these: every past Apply with its
stages, outcome, warnings, whether it rolled out, whether it was interrupted, and which Snapshot
it produced; every past Validation with its failures.

**An Apply records each of Verification's checks, not only whether it passed.** In order: the
coordinator responding, workers registered, a smoke query, and then one per Section confirming
the cluster adopted it — nine in all today, and the per-Section ones follow the Sections, so the
count is not a constant to design around. Each check is one of **three** outcomes:

- **passed**, often with what it found ("3 of 3 workers registered");
- **failed**, with why — and at most one check is ever failed;
- **not reached.** Verification stops at the first failure, deliberately: there is no point
  asking a broken cluster the next question, and Auto Rollback is waiting on the answer. The
  checks after a failure therefore did not pass and did not fail, and must not be rendered as
  either. A row of nine red marks for one failure is the wrong picture.

The list is **empty** for an Apply that never reached Verification — a Validation failure means
nothing was delivered and nothing was checked, which is a different statement from "Verification
started and stopped". And it is this Apply's own Verification only: Auto Rollback verifies again
against what it restored, and that result is reported separately as the rollback's outcome.

### 6.11 Audit

Every action, newest first, attributed. A listing endpoint, cursor-paginated, and an export
that is the same listing without the paging. Each entry
carries when it happened, who did it, how they authenticated, which token if a token did it, a
category, what was acted on, the action, the outcome, the Apply it belongs to, the Snapshot it
produced, and a detail.

Nine facts about that data shape any view of it:

- **Categories** are the six Sections plus `admin`, `pipeline` and `audit`. Admin changes are
  audited and are not a Section; the pipeline's own acts — an Apply, a rollback — are neither;
  and `audit` is an act on the trail itself, which changes no configuration at all.
- **Actions are a closed set of fourteen**, not free text: attempted, created, updated, removed,
  reset, imported, reverted, applied, rolled back, engaged, released, minted, revoked,
  exported.
- **Outcome is `succeeded`, `refused` or `failed`,** and **refusals are recorded** — an attempt
  that was rejected is part of the trail, with a detail saying why. Worth knowing before
  colour-coding by severity: a refusal is usually somebody correctly being told no, not an
  incident.
- **The actor can be Apchi itself,** recorded as `apchi`: startup recovery, an Auto Rollback, a
  Reset that a failed Apply performed. "apchi rolled back" is a correct entry and must not
  render as a missing or unknown actor.
- **How they authenticated is on the record,** not derived, because "jo did this" and "an
  automation jo minted did this" are different answers to the same question.
- **A revoked token still resolves in the trail.** Its id is kept deliberately — revocation is
  exactly when somebody comes to read these.
- **One Apply writes several entries, joined by its id** — which is also the id in the server's
  logs. They belong together rather than being matched up by timestamp.
- **The Snapshot is null for a staged edit, and null for *every* Admin change,** because an
  Admin Apply creates no Snapshot. Null here is a fact, not missing data.
- **Entries never contain configuration values** — only which resource changed. The contents live
  in Snapshots under rules of their own. So the audit answers *who changed what, and when*, and
  never *to what*: a design must not promise a before-and-after here.

**Filters:** category, actor, outcome, resource, action, one Apply's id, and a time range.
They combine as an *and*, not an *or*. Two are worth designing around: `outcome` is how you ask
for refusals alone, and a refusal is Apchi working correctly rather than an error; `resource`
follows one catalog or certificate across every actor and action that touched it. Note that
`resource` is null on an act against a whole Section or Cluster, so filtering by resource
counts a different set from the unfiltered listing — a total shown beside a resource filter is
not a total of everything.

There is no free-text search, deliberately: the only prose on a record is why something was
refused, and making that searchable would turn Apchi's sentences into a contract clients match
on.

**Export.** A second endpoint takes exactly the same filters and returns every match with no
limit, as **NDJSON** — one JSON object per line, each line the same entry the listing returns.
It streams, so there is no page count to show and no total to show either: the response has no
length until it ends. Three consequences for a design:

- It is a **download**, not a view. The response is offered as an attachment with a generated
  filename; the natural shape is a button beside the filters that exports *what is currently
  filtered*, and saying so is what makes it predictable.
- **There is no CSV.** NDJSON is for another system — a log pipeline, a SIEM — rather than for a
  spreadsheet. A design should not promise "export to Excel".
- **Exporting is itself audited; reading is not.** An export appears in the trail as category
  `audit`, action `exported`, with its filters recorded in the detail ("an export filtered by
  actor=sam, outcome=refused"). Paging the listing leaves no trace. So "who has taken a copy of
  this trail" is an answerable question, and the exported file contains the record of its own
  export as its first line.

**Retention is forever by default** — a deployment may set a window, but nothing is discarded
unless somebody chose that, so the history can be very long and the oldest entry is not a
recent date.

This is also where "who staged what" comes from: the Candidate deliberately stores no
authorship, and the audit is the only record of whose work an Apply is shipping. That record is
organised **by attempt, not by resource**, so authorship is something the interface reconstructs
by reading events — and where the trail is silent it says so rather than guessing (§14, point 8).

### 6.12 API tokens

Operators mint long-lived tokens for automation. Four facts shape the screen:

- **The secret is shown once, at creation, and is never recoverable.** Apchi stores only a digest
  of it, so a lost token is replaced rather than looked up. That single reveal is the only moment
  the value exists anywhere in the interface, and it has to survive the person being interrupted
  — a view that loses it on a re-render has lost it permanently.
- **A token acts as its owner.** It takes their role on every request and loses access when they
  do. It is not a principal with privileges of its own, and nobody can mint a token more
  powerful than themselves.
- **You see your own tokens and nobody else's** — an Admin included. There is no deployment-wide
  token inventory, so no screen can show what automation exists across the platform.
- **Revoked and expired tokens stay in the list.** The question it answers is "what has been
  running as me, and what stopped", and hiding the dead ones would leave somebody unable to tell
  a token they revoked from one that never existed. So the list mixes live, expired and revoked
  rows, and the difference has to be unmissable.

Per token: a name the owner chose to tell theirs apart, the owner, when it was created, when it
was last used (null if never), its expiry and its revocation. An expiry is given in days, up to
ten years, and **omitting it is a legitimate choice** rather than an oversight — a pipeline token
that does not expire, stopped with one revocation when it should stop.

### 6.13 Admin-only surfaces

Visible only to Admins; an Operator should not see them at all.

- **Maintenance Mode** — engage/disengage with a reason, showing who engaged it and when, and
  distinguishing an Admin engaging it from it engaging itself after an incident.
- **Raw files** — arbitrary Trino config files written directly. Listed by path and size; the
  **content is deliberately not readable back**, so this is not a round trip and the interface
  must not pretend it is.
- **Arbitrary configuration values** — an escape hatch that overrides generated configuration.
  Admin values win on conflict, and are never part of a Snapshot.
- **Permission enforcement** — a switch turning grant enforcement on or off cluster-wide, and
  the most consequential control in the product after Apply. While it is off, the grants in the
  permissions Section are configuration that nothing acts on; turning it **on** against a
  cluster already serving queries denies every identity without a grant everything it had. An
  Operator cannot touch it — they would be closing a cluster by editing configuration — and the
  interface has to make the off state legible wherever grants are shown, because a screen full
  of grants that are not being enforced reads as protection that is not there. **The switch is
  Admin-only; whether it is on is readable by anyone** — on the same polled read as Maintenance
  Mode (§6.1, point 1) — precisely so that an Operator reading grants can be told they are not
  in force. It is **off by default**, which is the state every deployment is in until an Admin
  decides otherwise, so a design that treats "not enforced" as the exceptional case has it
  backwards. It also stages nothing by itself: the cluster receives it at the next Apply,
  costing no restart.
- **Preserved certificate-mapping patterns** — additional patterns a cluster ran *before* Apchi,
  kept alive while its clients migrate. They are **ordered**, and are evaluated beneath the
  Operator's single pattern, so a subject matching both resolves to the Operator's. This is a
  migration aid with an expected end date.
- **Admin Apply** — applies admin changes through the same pipeline but creates no Snapshot.

**One deployment caveat worth knowing:** Trino clusters mount their configuration directory in
one of two ways, and on the more common one (a single whole-directory mount) **raw files do not
work at all**, because their path is the Admin's to choose and Apchi cannot place a single file
inside a read-only directory. A design should not assume every Admin surface is available on
every cluster.

### 6.14 Onboarding an existing cluster

Not a flow in this interface. A cluster configured before Apchi is brought under it by an
Admin importing a configuration document (§6.9, at the Admin scope), and the first Apply after
that produces **Snapshot 1**.

### 6.15 Drift: when the cluster is not what Apchi committed

A Snapshot records what was delivered. It does not keep the cluster that way, and two things
move it underneath:

- somebody edits the underlying configuration by hand, outside Apchi;
- an admin changes a platform-level value, which stages nothing and changes no Section, and
  still changes what several Sections would write.

Either way the next Apply writes files nobody staged — and for the three Sections that force a
restart, it terminates every running query to do it. So this is the read somebody takes
**before** an Apply, to find out whether the restart they are about to pay for is theirs.

It is a separate, on-demand read, because answering it means reading the cluster's actual
configuration. It is not pollable and must not sit behind a refresh timer.

**Per Section it gives one of four answers, and the fourth is the one a design must not
flatten:**

| Answer | Means |
| --- | --- |
| in sync | What Apchi would write is byte-identical to what the cluster holds. |
| diverged | It is not. The next Apply will rewrite this Section. |
| unknown | The cluster could not be read. Not clean, not dirty. |
| **not comparable** | This Section does not reach the cluster as a file at all, so no comparison is possible. |

**Catalogs are permanently *not comparable*.** They are delivered as statements rather than as
a configuration file, so there is nothing to diff, and claiming they are in sync would be an
answer the product has not earned. A design that renders four statuses as a green/red pair
will report catalogs as healthy on a cluster whose catalogs were changed behind Apchi's back.

**The overall verdict is three-valued, for the same reason.** True means at least one Section is
known to have drifted. Null means none is, but at least one could not be read — an all-clear
nobody verified is not an all-clear. False means every Section that *can* be compared is in
sync, and says nothing about catalogs.

Each Section also reports whether reconciling it restarts the coordinator, so a design never
keeps its own table of which Sections are expensive.

**Nothing fixes drift but an Apply.** There is deliberately no reconcile button: an Apply is
where the cost is shown and agreed to, and a one-press "fix" that restarts the cluster is that
promise inverted. A drift screen's only action is to send somebody to Review and Apply.

---

## 7. States every screen has to handle

The product generates these constantly; they are not edge cases.

- **Empty Candidate** — nothing staged. The most common state.
- **Fresh cluster** — no Snapshot has ever been committed. History is empty, and the Candidate
  has no base.
- **Staged, no restart** — changes pending, cheap to apply.
- **Staged, restart required** — changes pending, expensive. Different weight.
- **Validating / Applying** — a minutes-long operation in flight. The Candidate is **frozen**
  during an Apply; edits are refused.
- **Validation failed** — a list of failures, each naming a resource.
- **Applied with warnings** — it succeeded; the warnings are still worth reading.
- **Apply failed, rolled back** — the cluster is back on its previous Snapshot.
- **Incident** — rollback failed, Maintenance Mode on, nobody may edit.
- **Maintenance Mode (deliberate)** — an Admin disabled editing, with a reason.
- **Cluster unreachable** — Apchi is up but Trino is not. Nothing announces it: it is the
  answer to a probe somebody took, an operation failing, or a Validation warning that a probe
  could not connect. The coordinator **starting** is its own state and not a kind of down: one
  coming back from a restart refuses queries and is not broken.
- **Drifted** — the cluster is no longer running what Apchi committed to it (§6.15). Nothing
  announces it either; it is the answer to a read somebody took, and the next Apply will write
  changes nobody staged. Distinct from a staged change: there is nothing in the diff.
- **Apchi degraded** — its own database is unreachable; it reports this rather than crashing.
- **Not signed in**, and **signed in with no permission** for an Admin-only area.
- **Session expired mid-work** — the twelve-hour absolute expiry elapsed while somebody was
  filling in a form. Not a startup state: it arrives on a request that was expected to succeed.
- **Role changed under you** — someone was demoted or disabled while signed in, so controls that
  were visible begin refusing.
- **Concurrent edit** — someone else staged something while this person was looking.
- **Rolling back** — a visible stage of its own during a failing Apply, between the failure and
  knowing whether the cluster was recovered.

---

## 8. Scale and density

Sizes that should inform layout decisions.

**The per-collection figures below are expectations, not measurements.** Nothing in the project
records real deployment sizes, and this product has one release and no known production
installs. The two hard facts are the first and the last bullet; treat the rest as a sketch to
design against and worth checking before anything depends on them.

- **1 cluster.** There is no cluster switcher, no multi-tenancy, no project concept. Ever.
- **Catalogs:** typically 5–50. Each with up to ~20 properties.
- **Permissions:** the largest collection. Hundreds of grants is normal; filtering and grouping
  by identity matters.
- **Resource groups:** 5–30 groups, 5–50 selectors.
- **Client certificates:** a handful to a few dozen.
- **Event listeners:** 1–5.
- **Certificate mapping:** exactly one pattern.
- **Snapshots:** grows forever, one per successful Apply. Hundreds over a cluster's life.
- **Audit:** grows fastest; paginated, with filters.
- **Operators using it concurrently:** a handful. Enough that the shared Candidate matters.
- **The Candidate has a hard size ceiling** (15 MB by default) because it is a single database
  document. Approaching it is worth surfacing.

Frequency: configuration work is **routine, not constant** — an operator might apply a few times
a week. The incident path is rare but is where the interface matters most.

---

## 9. Timing

Nothing here is instant, and the interface should not pretend otherwise.

| Action | Duration |
|---|---|
| Any edit to the Candidate | immediate |
| Validation | **1–5 minutes** (starts a Trino pod) |
| Apply, no restart | ~10–30 seconds |
| Apply with rollout | **2–10 minutes** (coordinator restart plus worker re-registration) |
| Auto Rollback | the same again |

Apply and Validate are **resources, not requests**: starting one returns an identifier
immediately, and progress is read afterwards. Closing the browser does not cancel anything and
losing the tab loses no information.

---

## 10. Technical constraints a designer should know

- **The UI is served from the same origin as the API.** Session cookies are `SameSite=Lax`, so a
  separately hosted front end cannot authenticate at all. This is settled and not negotiable.
- **Everything in the UI is in the public API.** There is no privileged back channel; the
  interface is a consumer of the same documented REST API as any script. If a screen needs data,
  that data is public API, and if the API cannot answer it, the screen cannot have it.
- **The API is the contract.** It is versioned (`/api/v1`), committed to the repository, and
  changes to it are reviewed. Several things a designer might reasonably want do not exist yet
  (see §13).
- **No websockets** except the Apply event stream. Everything else is polling. That stream is
  **resumable** — a reconnecting client picks up from the last event it saw rather than
  restarting — so a dropped connection mid-Apply loses nothing.
- **Long lists are cursor-paginated**, not page-numbered. Snapshots, Applies, Validations, audit
  entries and tokens all return a batch plus a pointer to the next one. There is no total count
  and no "page 7 of 24"; infinite scroll or an explicit "load more" fits the API, a numbered
  pager does not.
- **The browser is modern**; no legacy support burden.
- There is **no mobile requirement**, though nothing forbids it. This is used at a desk, during
  the working day, often with a terminal open beside it.

---

## 11. Copy and tone

The product's voice — established in its specification and its own error messages — is plain
and exact. It names mechanisms and costs, and it does not reassure.

- No exclamation marks. No congratulation on success.
- Errors name the problem and the recovery.
- **One refusal for every kind of authentication failure.** Unknown username, wrong password and
  disabled account produce the *same* message — "That username and password do not match an
  enabled account." — because distinguishing them lets someone enumerate which platform staff
  have accounts. A designer must not "improve" this into specific feedback.
- Destructive things are described in full rather than softened. "Running queries will be
  terminated" is the register.

---

## 12. Secrets — read this before designing any detail view

Configuration genuinely contains credentials: database passwords, private keys.

**The specification is explicit that these users are trusted and that there is no requirement to
hide secret values from them.** The API returns them in full. A design that hides them behind
permissions or omits them would break a real workflow — operators need to read back and edit
what they stored.

What is worth designing for is accident rather than access: a configuration diff gets read
during a screen-share, in a pairing session, or over a shoulder, and a credential that appears
without being asked for appears in all of them.

Whatever answer a design reaches, one thing must be true of it: it cannot be presented as a
security control. Anyone who can see the interface can read the same value straight from the
API, so any concealment here is about not showing things by accident — and saying otherwise
would mislead someone into relying on it.

Related facts worth knowing: Snapshots store actual secret values, not references, so the history
is a permanent record of every credential the cluster has ever used, and a compromised key cannot
be purged without breaking Snapshot immutability. This is a recorded, accepted trade-off.

---

## 13. Known gaps — things a design may want that do not exist yet

Honest list. Designing around these is fine; assuming they exist is not.

- **No per-catalog "test connection".** Connectivity is only checked inside a full Validation.
  The curated property *rules* are readable (§7's data shape), so a form can be built and
  validated client-side before anything is staged — but whether the credentials in it actually
  reach the database is not answerable short of an Apply.
- **The audit export is NDJSON only, and it is all-or-filtered — never a report.** Filtering
  and export both exist now (§6.11), but the output is one JSON object per line for another
  system to read. There is no CSV, no spreadsheet, no PDF, and nothing that aggregates or
  summarises — no "changes per actor this month", no counts of anything. Every total a design
  shows has to be counted client-side from events it fetched itself.
- **No user, role or account management in the API at all** — see §2. Provisioning happens
  outside Apchi, and no screen can change it.
- **No way to see another person's API tokens**, so nothing can inventory the automation running
  against a deployment.
- **No per-resource revert to an *older* Snapshot.** A single staged change can be undone
  (§6.3), which takes it back to the Snapshot the Candidate started from. Taking one resource
  back to Snapshot 10 while the rest stay put is still only possible a whole Section at a
  time.
- **Drift is detectable but not watched, and not for catalogs.** Apchi can compare what it
  would write against what the cluster holds, on demand, per Section (§6.15) — but nothing
  checks on a timer, so drift is only ever found because somebody asked, and catalogs cannot be
  compared at all. There is also no way to see *what* diverged: the answer is per Section, not a
  diff of the two files.
- **No continuous health monitoring.** Nothing inside Apchi watches the cluster on a timer —
  there is no scheduler in the product at all — so a live reading exists only while somebody is
  asking for one. A health indicator that looks live is a design that polls, and it should know
  that each poll is a request arriving at a coordinator whose job is queries.
- **No cluster topology.** An active worker count is available on demand; there is no node
  list, no per-worker state and no view of the cluster's shape.
- **No notifications** of any kind. No email, no in-app inbox. An alert webhook exists but only
  fires for a failed Auto Rollback.
- **No scheduling.** Nothing can be applied later or during a window.
- **No search** across configuration.
- **One expiry window** for certificates, configured deployment-wide, not per certificate. It
  is now *reported* on every certificate (§6.3), so an interface can name the threshold it is
  rendering — but it cannot be set per certificate, so "warn me 90 days before this one" is not
  expressible.

---

## 14. What any design must not break

The short version, for a reviewer checking a proposal. These are the product's safety
properties, not style preferences — a design that misses one is wrong however it looks.

1. **Staged is never confusable with live.**
2. **The cost of an Apply is visible before it is taken** — including, when a rollout is
   involved, that running queries will be terminated, in those words, with the live count.
3. **The whole shared Candidate is visible**, not only the current person's changes.
4. **Which changes force a restart is shown per change.**
5. **The four fixed messages are used verbatim.**
6. **The vocabulary in §4 is used exactly**, with no synonyms.
7. **A revert never implies a return to a known-good state.**
8. **The interface says when it does not know** rather than inferring — particularly about who
   made a change.
9. **The incident state is unmissable** and clearly distinct from an ordinary failure.
10. **Admin surfaces are invisible to Operators.**

Everything else — layout, navigation model, colour, typography, density, motion, how the six
Sections are organised, how the diff is presented, how the Apply flow is paced — is open.
