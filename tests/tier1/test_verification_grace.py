"""Verification's tolerance for the gap between a ready pod and a routable Service.

Kubernetes calls a rollout complete the instant the new coordinator pod is ready, and the
Service follows a fraction of a second later. Verification's first request lands in that
window, and before this it failed an Apply that had worked -- and then rolled the Cluster
back for a reason that would have cured itself.
"""

import pytest

from app.pipeline import verification
from app.pipeline.verification import VerificationFailed, _await_response


class StubTrino:
    """Answers `is_starting` from a script, so a test can place the gap precisely."""

    def __init__(self, *answers: bool | None) -> None:
        self._answers = list(answers)
        self.calls = 0

    async def is_starting(self) -> bool | None:
        self.calls += 1
        # The last answer repeats, so a test need only describe the interesting prefix.
        return self._answers[min(self.calls - 1, len(self._answers) - 1)]


async def test_a_coordinator_that_answers_at_once_is_not_waited_for() -> None:
    trino = StubTrino(False)

    await _await_response(trino)  # type: ignore[arg-type]

    assert trino.calls == 1


async def test_the_endpoint_gap_is_waited_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verification, "_POLL_SECONDS", 0.01)
    trino = StubTrino(None, None, False)

    await _await_response(trino)  # type: ignore[arg-type]

    assert trino.calls == 3


async def test_a_still_starting_coordinator_is_waited_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verification, "_POLL_SECONDS", 0.01)
    trino = StubTrino(True, False)

    await _await_response(trino)  # type: ignore[arg-type]

    assert trino.calls == 2


async def test_a_coordinator_that_never_answers_still_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Rollout already proved the pod ready, so silence past the grace is a broken
    coordinator -- which is what Verification exists to catch."""
    monkeypatch.setattr(verification, "_POLL_SECONDS", 0.01)
    monkeypatch.setattr(verification, "_RESPONSE_GRACE_SECONDS", 0.05)

    with pytest.raises(VerificationFailed, match="did not respond within"):
        await _await_response(StubTrino(None))  # type: ignore[arg-type]


async def test_a_coordinator_stuck_starting_fails_saying_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verification, "_POLL_SECONDS", 0.01)
    monkeypatch.setattr(verification, "_RESPONSE_GRACE_SECONDS", 0.05)

    with pytest.raises(VerificationFailed, match="still starting"):
        await _await_response(StubTrino(True))  # type: ignore[arg-type]
