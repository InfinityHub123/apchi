"""Apchi's own version, read from the installed package metadata.

One source of truth, `pyproject.toml`. This used to be a literal in the OpenAPI document,
which is harmless until something else needs it: an export records the version that took it
(§18, #113), and a copy that drifts from the real one makes that provenance a lie.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    VERSION = version("apchi")
except PackageNotFoundError:
    # Imported from a source tree with nothing installed. Serving requests matters more
    # than knowing the version, but an export must not claim a version it cannot read.
    VERSION = "0+unknown"
