"""Event Listener property schemas, and the name of the Section that held listeners once.

The listener is the Admin's now, as `event-listener.properties` in the coordinator's Server
Configuration (#269). What is left here is what that still uses -- the schemas a curated
listener's properties are checked against -- and the retired Section's name, which a Snapshot
written before the move still holds.
"""

SECTION = "event_listeners"
