"""Admin raw files: a path and its content, with no schema over it (§14).

An Admin writes a file path and what should be in it. Apchi delivers it and forms no opinion
about the contents: if Trino accepts it, it works; if not, Validation refuses it; if the
Cluster fails anyway, Auto Rollback puts it back.

Curating the properties an Admin may set would mean predicting which ones an incident needs,
and the cases an escape hatch exists for are the ones nobody predicted. Trino's configuration
surface is large, versioned, and changes between releases; a model of it here would be a
second, worse copy that is wrong after every upgrade.

**The content is treated as a secret in its entirety.** Apchi cannot know which part of an
arbitrary file is a credential, so it assumes all of it is. The redaction filter in
`app.logging` keys on field *names* and would not notice a password inside an opaque blob, so
this content is kept out by origin instead: never logged, and never returned by the API that
lists what exists.

What *is* modelled here is the path, because three things about a path are Apchi's business
and getting any of them wrong is worse than refusing:

* a path Apchi already owns would let a raw file silently replace generated configuration
* a path the deployment cannot mount a single file at would fail minutes later, during an
  Apply, rather than at the moment somebody wrote it
* a path outside Trino's configuration directory is not configuration
"""

from pathlib import PurePosixPath
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

#: Where Trino reads its configuration. A raw file lives here or nowhere: this is an escape
#: hatch for Trino configuration, not a way to write to a container's filesystem.
CONFIG_DIR: Final = "/etc/trino"

#: Generous for a configuration file and small enough that the Admin document stays something
#: MongoDB will store -- see §4's ceiling, which is the same constraint one level up.
MAX_CONTENT_BYTES: Final = 256 * 1024

#: As many as an incident plausibly needs. Not a quota so much as a tripwire: an Admin with
#: fifty raw files has stopped using an escape hatch and started maintaining a fork.
MAX_FILES: Final = 25

#: What Kubernetes lets one Secret hold, summed over its values, and every raw file is in one.
#: The two bounds above allow several times this, so it is checked on its own.
SECRET_MAX_BYTES: Final = 1024 * 1024


def secret_bytes(files: dict[str, str]) -> int:
    """What the raw files' Secret would hold: Kubernetes sums its values, not its keys."""
    return sum(len(content.encode()) for content in files.values())


def too_large(files: dict[str, str]) -> str | None:
    """Why these files cannot be delivered together, or None when they can."""
    size = secret_bytes(files)
    if size <= SECRET_MAX_BYTES:
        return None
    return (
        f"The raw files would take {size} bytes, past the {SECRET_MAX_BYTES} one Kubernetes "
        "Secret may hold, and every raw file is in one. Remove a file that is no longer "
        "needed, or move what belongs in Trino's own files to "
        "PUT /api/v1/admin/server-configuration."
    )


class RawFileWrite(BaseModel):
    """What an Admin sends. The path is in the URL; this is the content."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{"content": "http-server.authentication.type=CERTIFICATE\n"}]
        },
    )

    content: str = Field(
        max_length=MAX_CONTENT_BYTES,
        description=(
            "The file's exact contents. Apchi does not parse, validate or merge it -- it is "
            "delivered as written and Trino decides. Treated as a secret in its entirety, so "
            "it is never logged and never returned by a listing."
        ),
    )


class RawFile(BaseModel):
    """A raw file as it is listed back. **Without its content.**

    Deliberately not a round trip. Apchi cannot tell which part of an arbitrary file is a
    credential, so it treats all of it as one and does not hand it back -- an Admin who needs
    to know what is in a file reads it from the Cluster, where it is a file. What a listing
    answers is which paths exist and how big they are, which is what the question "what have
    we injected here" actually means.
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    bytes: int = Field(description="The content's size. Its value is deliberately not here.")


def normalised(path: str) -> str:
    """A path in the one form everything else compares against.

    `/etc/trino/../etc/trino/x` and `/etc/trino/x` are the same file, and only one of them
    matches the set of paths Apchi owns. Normalising before comparing is the difference
    between a rule and a rule with a hole in it.
    """
    return str(PurePosixPath(path.strip()))


def within_config_dir(path: str) -> bool:
    """Whether a path is somewhere Trino reads configuration from.

    Checked against the normalised path, so `..` cannot walk out of it.
    """
    candidate = PurePosixPath(normalised(path))
    if not candidate.is_absolute():
        return False
    return candidate != PurePosixPath(CONFIG_DIR) and candidate.is_relative_to(CONFIG_DIR)
