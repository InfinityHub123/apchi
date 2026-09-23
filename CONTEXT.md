# Apchi

Apchi is the control plane for configuring a Trino cluster. It exposes high-level
configuration resources through a Web UI and a versioned REST API, so that the people
running Trino never have to hand-edit Trino's configuration files.

## Language

### People

**Operator**:
A member of the platform staff at the organisation Apchi is operated for. The primary Apchi
user; owns every Section of the configuration.

**Admin**:
A member of the team that provides and operates the underlying platform. Owns
infrastructure, the arbitrary low-level Trino configuration escape hatch, and the
Maintenance Mode.

**End User**:
A person or application issuing queries to Trino. Never an Apchi user; only ever the
subject of permissions and resource groups.

### Configuration

**Cluster**:
The one Trino cluster a given Apchi deployment manages. Apchi is deployed one-to-one
with a Cluster.

**Snapshot**:
An immutable, complete record of the Apchi-managed desired configuration that has passed
validation, been applied to the Cluster, and been verified against it. Numbered
sequentially.

**Configuration Candidate**:
The single mutable configuration Operators edit, derived from the latest Snapshot. Every
Operator change lands here; nothing reaches the Cluster until Apply.

**Section**:
One managed area of a Configuration Candidate — catalogs, client certificates, certificate
mapping, permissions, resource groups, event listeners. The unit of Section Revert.

**Review**:
The diff between the Configuration Candidate and the latest Snapshot. What an Operator sees
before Apply.

**Effective Cluster State**:
The configuration the Cluster is actually running right now. Changed only by Apply.

**Apply Engine**:
One of the three ways a change reaches the Cluster: DDL, file without restart, or file with
rollout. Which engine a Section uses is a property of how Trino consumes that configuration,
never of when an Operator's edit takes effect — everything waits for Apply.

**Rollout-Required**:
Of a Section: one Trino adopts only by restarting, and so applied by the third engine.

### Lifecycle

**Adoption**:
The one-time onboarding of a Cluster whose configuration predates Apchi: existing
configuration is imported into a Candidate and taken through the full lifecycle to
produce the first Snapshot.

**Validation**:
Deciding whether the Candidate should be applied to the Cluster. Happens before the
Cluster is touched.

**Apply**:
Making the Effective Cluster State match the Candidate. The first and only point at which
Operator changes reach the Cluster.

**Rollout**:
Restarting Trino so it adopts configuration it cannot adopt while running. One possible
mechanism of Apply.

**Verification**:
Deciding whether the Cluster successfully adopted the configuration and is still healthy.
Happens after Apply. Distinct from Validation, which happens before.

**Commit**:
Recording a verified Candidate as a new Snapshot.

**Reset**:
Discarding the Candidate's changes and re-deriving it from the latest Snapshot. Because
nothing reaches the Cluster before Apply, Reset leaves the Effective Cluster State
untouched.

**Section Revert**:
Replacing one Section of the Candidate with its content from an earlier Snapshot, leaving
every other Section untouched. The routine recovery action.

**Full Rollback**:
Replacing the entire Candidate with an earlier Snapshot. Reserved for disaster; never
implicit. Produces a new Snapshot and never rewrites history.

**Auto Rollback**:
The bounded, single attempt to return the Cluster to the latest Snapshot after an Apply
or Verification failure. Creates no Snapshot, and is not a Full Rollback.

**Maintenance Mode**:
An Admin-controlled state in which Operators retain read access but all Operator
configuration changes are rejected. Used during platform upgrades and maintenance.

### Identity

**Certificate Mapping Pattern**:
The single Operator-configurable rule that derives a Trino Identity from the subject of a
certificate presented by a caller.

**Client Certificate**:
A certificate and private key Trino presents when connecting outward to an external system.
Distinct from the Certificate Mapping Pattern, which governs callers authenticating inward.

**Trino Identity**:
The username a client certificate resolves to, and the subject of permissions and
resource group selection. Establishes who someone is, never what they may do.

**Catalog**:
A Trino-visible data source, as Operators configure it.

**Connector**:
The Trino plugin a Catalog uses to reach an external system.
