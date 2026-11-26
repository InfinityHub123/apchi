"""Carrying configuration out of Sections that no longer exist.

The Event Listener was an Operator Section until #269, which made it part of the Admin's Server
Configuration: where query events go is set once by the platform, and keeping its file in a
Secret the chart mounts at a fixed path is what ends Apchi editing the coordinator Deployment
for it. A Cluster upgrading from before that runs a listener the latest Snapshot records, so it
is carried into the Admin values once, at startup.

Only the Admin values change here -- never the Cluster, because restarting Apchi must not. The
coordinator keeps reading the listener from the old mount until the next Admin Apply renders it
the new way and takes that mount off.
"""

import logging

from pydantic import ValidationError

from app.pipeline.admin_values import AdminStore
from app.pipeline.snapshots import SnapshotStore
from app.sections.event_listeners import SECTION as EVENT_LISTENERS
from app.sections.server_configuration import LISTENER_NAME_KEY, CoordinatorFiles

logger = logging.getLogger(__name__)


async def carry_over_event_listener(snapshots: SnapshotStore, admin: AdminStore) -> bool:
    """Move the listener the latest Snapshot holds into the Admin values. True if it did."""
    retired = await snapshots.retired_section(EVENT_LISTENERS)
    if not retired:
        return False
    values = await admin.load()
    if values.server_configuration.coordinator.event_listener_properties:
        # Already carried over, or set by an Admin since: theirs is the one that counts.
        return False
    # The Section held at most one.
    name = sorted(retired)[0]
    stored = retired[name]
    listener = {
        LISTENER_NAME_KEY: str(stored["type"]),
        **{key: str(value) for key, value in (stored.get("properties") or {}).items()},
    }
    try:
        CoordinatorFiles.model_validate({"event-listener.properties": listener})
    except ValidationError as exc:
        # Not carried rather than carried broken: the listener keeps running from its old
        # mount, and the Admin is told what to fix. A Cluster that cannot start is worse.
        logger.error(
            "could not carry the Event Listener into the Server Configuration; set it as "
            "event-listener.properties under coordinator before the next Admin Apply, or it "
            "stops when that Apply restarts the coordinator",
            extra={"listener": name, "problems": str(exc)},
        )
        return False
    await admin.carry_over_listener(listener)
    logger.warning(
        "carried the Event Listener into the Admin's Server Configuration",
        extra={"listener": name, "plugin": listener[LISTENER_NAME_KEY]},
    )
    return True
