# Apchi supports one deployment shape: file by file

Supersedes [ADR-0007](0007-the-deployment-owns-the-pinned-pointers.md).

Apchi supports only a Trino coordinator that mounts `/etc/trino` file by file, as its own
chart does. A coordinator that mounts the whole directory as one ConfigMap -- the official
`trinodb/charts` chart, and most hand-written manifests -- is refused before an Apply, by the
precondition that already caught it.

ADR-0007 made that second shape work. It cost a second way of delivering Resource Groups and
Event Listeners, two settings to select it, a precondition checking pointers the deployment
claimed to provide, a rule that Event Listeners could never be emptied there, and Admin raw
files that could not work there at all. In exchange it supported a chart nothing in CI ever
installed: the one real install was a manual check, and a newer release of that chart could
have broken it without anything noticing.

Apchi now ships its Trino in the same chart as itself, so the shape it needs is the shape it
gets. Keeping the official chart working would mean installing it in CI, against every release
of it, to keep a promise no deployment of Apchi depends on.

## Considered

- **Keep both shapes.** The cost above, permanently, for a path nobody used.
- **Keep the settings and drop the guide.** Code that no test exercises against the thing it
  is for is not support, only the appearance of it.

## Consequences

- `resource_groups_properties_from_deployment` and
  `event_listener_config_files_from_deployment` are gone; an install that set them now stops
  at startup, naming them, because Apchi refuses settings it does not read.
- The sixth precondition stays. It is what turns the official chart into a named refusal rather
  than a coordinator that crashloops with `not a directory` and no log.
- Event Listeners can always be emptied, and Admin raw files work wherever Apchi does.
