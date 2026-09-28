"""Do workers need Event Listener configuration?

The design doc recorded the scope as unresolved and said to restart both, conservatively.
Trino's documentation settles it -- the plugin is installed *on the coordinator*, and the
events a listener receives are query-created and query-completed, which the coordinator
produces -- and the implementation restarts the coordinator only.

That divergence is not something to leave resting on an absence of documentation, because
being wrong means silently losing events rather than failing visibly. So this watches events
actually arrive while the worker pods are never restarted.

The receiver is the smallest thing that can receive an HTTP POST and show it: busybox `nc`
in a loop, four megabytes, already the kind of image any cluster can pull. It is not a
general-purpose echo server and does not need to be -- the question is only whether a request
arrives.
"""

import asyncio
from collections.abc import Iterator

import pytest
from httpx import AsyncClient

from app.adapters.trino import Trino
from app.pipeline.applies import TERMINAL
from tests.tier2.conftest import PortForward, kubectl

pytestmark = pytest.mark.tier2

RECEIVER = "apchi-event-receiver"
LISTENER = {
    "name": "audit",
    "type": "http",
    "properties": {
        "http-event-listener.connect-ingest-uri": f"http://{RECEIVER}:8080/events",
        "http-event-listener.log-completed": "true",
        # nc serves one connection at a time and is restarted by the loop between them, so
        # a retry covers the gap. Nothing about Trino requires this.
        "http-event-listener.connect-retry-count": "5",
        "http-event-listener.connect-retry-delay": "1s",
    },
}

#: A bare 200 with no body. nc is not an HTTP server, so the response is written by hand.
#: It has to live in a YAML block scalar: the `Content-Length: 0` in it reads as a mapping
#: key in a plain one, and kubectl rejects the container command as an object.
_RESPONSE = "HTTP/1.1 200 OK\\r\\nContent-Length: 0\\r\\n\\r\\n"

_MANIFEST = f"""
apiVersion: v1
kind: Pod
metadata:
  name: {RECEIVER}
  labels: {{app: {RECEIVER}}}
spec:
  restartPolicy: Never
  containers:
    - name: echo
      image: busybox:1.37
      command:
        - sh
        - -c
        - >-
          while true; do printf '{_RESPONSE}' | nc -l -p 8080; done
      ports: [{{containerPort: 8080}}]
---
apiVersion: v1
kind: Service
metadata:
  name: {RECEIVER}
spec:
  selector: {{app: {RECEIVER}}}
  ports: [{{port: 8080, targetPort: 8080}}]
"""


@pytest.fixture
def event_receiver() -> Iterator[str]:
    """Somewhere for the events to go. Without one there is no way to tell a listener that
    loaded from a listener that is delivering."""
    kubectl("apply", "-f", "-", stdin=_MANIFEST)
    try:
        kubectl("wait", "--for=condition=ready", f"pod/{RECEIVER}", "--timeout=180s")
        yield RECEIVER
    finally:
        kubectl("delete", "pod", RECEIVER, "--ignore-not-found", "--wait=false")
        kubectl("delete", "svc", RECEIVER, "--ignore-not-found", "--wait=false")


def _worker_pods() -> dict[str, str]:
    """Worker pod names to their uids. A restarted worker is a different uid, and a
    Deployment that was never touched keeps both."""
    listed = kubectl(
        "get",
        "pods",
        "-l",
        "app=trino,component=worker",
        "-o",
        "jsonpath={range .items[*]}{.metadata.name} {.metadata.uid}{'\\n'}{end}",
    )
    return dict(
        line.split()
        for line in listed.splitlines()
        if len(line.split()) == 2  # noqa: PLR2004
    )


async def test_events_arrive_while_the_workers_are_never_restarted(
    e2e_client: AsyncClient, forward: PortForward, event_receiver: str
) -> None:
    workers_before = _worker_pods()
    assert workers_before, "the fixture needs at least one worker to prove anything about"

    await e2e_client.post("/api/v1/event-listeners", json=LISTENER)
    started = await e2e_client.post("/api/v1/applies")
    apply_id = started.json()["id"]
    deadline = asyncio.get_running_loop().time() + 900
    record: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        record = (await e2e_client.get(f"/api/v1/applies/{apply_id}")).json()
        if record["stage"] in TERMINAL:
            break
        await asyncio.sleep(1)
    assert record["stage"] == "succeeded", record.get("failure_reason")
    assert record["rolled_out"] is True

    # A query the coordinator completes is what produces an event.
    forward.restart()
    trino = Trino(host="127.0.0.1", port=forward.port)
    await trino.query("SELECT 1 FROM system.runtime.nodes LIMIT 1")

    deadline = asyncio.get_running_loop().time() + 120
    log = ""
    while asyncio.get_running_loop().time() < deadline:
        log = kubectl("logs", f"pod/{event_receiver}")
        if "POST /events" in log:
            break
        await asyncio.sleep(2)

    assert "POST /events" in log, f"no event reached the receiver; its log was:\n{log}"
    assert _worker_pods() == workers_before, "a worker was restarted, so the scope is wrong"
