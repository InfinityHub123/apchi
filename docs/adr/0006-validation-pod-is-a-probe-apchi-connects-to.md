# The validation pod is a probe Apchi connects to, not a job that reports back

Trino validation needs an ephemeral coordinator, and there are two ways to get a verdict out
of it. Apchi can start a plain Trino, connect to it, and issue the Candidate's statements
itself; or the pod can run the statements on its own — the image ships the Trino CLI — and
report through its exit status and logs.

We chose the probe Apchi connects to. The failures come back as `TrinoQueryError`s with the
message Trino produced, attributable to the resource that caused them, which is what lets a
Validation name the resource and the reason. The alternative turns every failure into log
parsing, and the statements have to survive being embedded in a shell script with arbitrary
operator-supplied property values in them.

The cost is a network dependency: Apchi connects to the pod's own address on port 8080, which
assumes Apchi runs in the Cluster and that nothing stands between them. A namespace with
default-deny ingress breaks Validation, and the symptom — a coordinator that never served — does
not point at the cause. The reporting job needs only the Kubernetes API, which Apchi must reach
anyway. If Apchi ever has to run outside the Cluster, or clusters with default-deny become the
norm, this is the decision to revisit; §6 records the NetworkPolicy requirement in the meantime.
