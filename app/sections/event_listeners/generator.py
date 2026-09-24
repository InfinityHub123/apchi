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
