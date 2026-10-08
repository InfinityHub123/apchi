"""Rendering Event Listeners to the file Trino reads.

Trino reads `etc/event-listener.properties` at startup if it is there, and ignores its
absence. There is no reload: `loadEventListeners()` is guarded by a compareAndSet
permitting one call per process lifetime, so the file is read exactly once per pod.
"""

from typing import Any

from app.sections.base import Resources

#: The key inside the Secret, and the filename Trino expects.
FILE_KEY = "event-listener.properties"

#: Where Trino looks. Fixed by the image's `etc` directory rather than configurable: the
#: alternative, `event-listener.config-files`, makes Trino refuse to start when the file it
#: names is missing, which would make Event Listeners mandatory.
MOUNT_PATH = f"/etc/trino/{FILE_KEY}"


def render_properties(listener: dict[str, Any]) -> str:
    lines = [f"event-listener.name={listener['type']}"]
    lines.extend(f"{key}={value}" for key, value in sorted(listener.get("properties", {}).items()))
    return "\n".join(lines) + "\n"


def render_secret(listeners: Resources) -> dict[str, str]:
    """The Secret's full contents.

    Empty when no Event Listener is configured, which is what makes the mount removable --
    and removing the mount is the only way to tell Trino there is no listener.
    """
    if not listeners:
        return {}
    name = sorted(listeners)[0]
    return {FILE_KEY: render_properties(listeners[name])}


class Unreadable(Exception):
    """A file this module cannot read. Translated by the Section, so the generator keeps
    knowing nothing about the pipeline."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


def parse_properties(path: str, content: str) -> tuple[str, dict[str, str], list[tuple[str, Any]]]:
    """The inverse of `render_properties`: the listener's type and its properties.

    **The name does not survive.** An Event Listener is stored under a name an Operator
    chose, and none of it reaches the file -- Trino's format has a type and properties and
    nowhere to put a name. So a parse can only name the listener after its type, and a
    round trip through the file renames a listener called `audit` to one called `http`.

    That is a real loss and not a normalisation, which is why it is said here rather than
    hidden: the only alternative is inventing a name, and a listener silently renamed is
    better than a listener whose name Apchi made up and an Operator cannot find.
    """
    properties: dict[str, str] = {}
    unaccounted: list[tuple[str, Any]] = []
    listener_type: str | None = None

    for number, raw in enumerate(content.splitlines(), start=1):
        line = raw.strip()
        # Trino's properties loader ignores blanks and # comments, so neither is a problem
        # worth reporting -- but neither is something Apchi can write back, so a comment is
        # lost by a round trip and that is what `unaccounted` is for.
        if not line:
            continue
        if line.startswith("#"):
            unaccounted.append((f"a comment on line {number}", line))
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise Unreadable(path, f"line {number} is not key=value: {line!r}")
        key, value = key.strip(), value.strip()
        if key == "event-listener.name":
            listener_type = value
            continue
        properties[key] = value

    if listener_type is None:
        raise Unreadable(path, "there is no event-listener.name, so nothing says what plugin")
    return listener_type, properties, unaccounted
