---
status: accepted
---

# The Event Listener is the Admin's, in Server Configuration

The Event Listener is `event-listener.properties` in the coordinator's Server Configuration, set
by the Admin, and no longer an Operator Section. The chart mounts the coordinator's Server
Configuration Secret a second time, whole, at `/etc/trino/apchi`; Apchi renders the listener
there and names it in `event-listener.config-files` only when one is configured. Apchi adds or
removes nothing on the coordinator Deployment for it.

Where query events go is a platform decision made once, not a day-to-day Operator change. And
as a Section the listener could only be optional by Apchi editing the Deployment -- a volume
added when there was a listener and taken away when there was none -- because Trino refuses to
start on a file it was told to read that is missing, and a `subPath` mount of an absent key is a
directory Trino dies on. The Deployment is the chart's, and Argo CD reconciles it against Git.

Measured on Trino 483 before deciding: a file named by `event-listener.config-files` in a
subdirectory loads; a broken one stops startup with numbered reasons the validation probe
quotes; and with that property set, a file at Trino's default path is ignored -- so an install
upgrading from the Section keeps working off the old mount until an Admin Apply moves it.

## Considered

- **The chart renders it**, since it rarely changes. The file is under `/etc/trino`, which
  ADR-0010 gives to Apchi, so the ninth precondition would need an exception to the rule it
  enforces. Its properties usually carry credentials, which would sit in Git and in Argo CD in
  plain text. And a listener the chart rendered would never reach the validation probe: a typo
  would be a coordinator that does not start after a sync.
- **Keep the Section and keep editing the Deployment.** Argo CD tolerated it when measured, but
  the chart and Apchi would go on disagreeing about the coordinator's volumes, and a chart change
  restructuring them could drop the listener's mount while Apchi believed it delivered.
- **A fixed `subPath` mount of the key.** A Secret without the key makes that path a
  directory, and Trino dies reading it. A whole-Secret mount is the only fixed mount that can be
  empty.

## Consequences

- The Operator API and screen for Event Listeners are gone. An existing listener is carried into
  the Admin values at startup, and the next Admin Apply takes the old mount off and deletes its
  Secret -- the last Deployment edit Apchi makes for it.
- `event-listener.config-files` is wiring: Apchi writes it, an Admin's `config.properties` may
  not. Raw Files at Trino's default listener path and under `/etc/trino/apchi` are refused.
- Resource Groups and Raw Files still have Apchi editing the Deployment. Resource Groups could
  move the same way only if Trino let `resource-groups.properties` live elsewhere, which it
  does not.
