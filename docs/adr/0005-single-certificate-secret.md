# All client certificates live in one Kubernetes Secret

Every Client Certificate Apchi manages is stored as keys in a single Kubernetes Secret,
mounted once at a fixed directory on the coordinator and on workers. Adding a certificate
adds keys to that Secret; the pod spec never changes.

## Considered options

**A Secret and a volume per certificate.** Isolates each certificate's key material and makes
the mapping from certificate to mount obvious. Rejected: adding a certificate would add a
volume to the pod spec, and a pod spec change is a rollout — so uploading a certificate would
restart the Cluster and destroy every running query. That is a disproportionate cost for an
upload, and it would make Client Certificates a rollout-required Section for no benefit.

**One Secret for all of them (selected).** The volume already exists, so a new certificate
simply appears as a new file in a directory Trino is already mounting. No pod spec change, no
restart, no query loss.

## Consequences

- **"No restart" is not "immediate".** The file appears only after the kubelet projects the
  updated Secret — normally seconds, bounded by `syncFrequency` (default 1 minute). Apply
  writes the Secret first and then retries the catalog DDL that references it, rather than
  sleeping a fixed duration.
- **A `subPath` mount would break this silently.** A `subPath`-mounted Secret never receives
  updates, so every certificate added after pod creation would fail to appear with no error.
  This is the second reason the `subPath` precondition is checked before every Apply.
- **No isolation between certificates.** Every process in the Trino pod can read every client
  key in the mounted directory. Accepted: the same pod already holds catalog credentials, so
  this grants no access that was not already there.
- **Kubernetes caps a Secret at 1MB.** That is a large number of certificates, but it is a
  ceiling, and Apchi should fail a certificate upload that would exceed it with a clear error
  rather than a rejected API write.
- Client Certificates are a Section, so a Section Revert rewrites the whole Secret rather than
  individual keys.
