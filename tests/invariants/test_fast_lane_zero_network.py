"""The fast lane answers a question without opening a socket, proved rather than asserted.

M6.1.5 asks for a zero-network test with egress blocked, and the ordinary way to write one is
to patch `httpx`, run the code and assert the mock was not called. That proves one library was
not reached through the name it was patched under. It says nothing about a lazy import inside
a branch nobody took, a driver reaching the network in C, or a module that stashed a client at
import time, and every one of those is how this would actually go wrong.

So the block is a CPython audit hook in a child process. `socket.__new__` and
`socket.getaddrinfo` are raised by the interpreter when a socket is constructed or a name is
resolved, whoever did it and by whatever route, and a hook that raises aborts the operation. A
child process rather than this one because `sys.addaudithook` has no matching remove: the hook
would otherwise stay on the interpreter for every test that ran afterwards.

**The controls are what make this a test.** A hook that was never armed produces exactly the
same green run as a fast lane that never dials, so `tests/fixtures/zero_network_probe.py`
makes three deliberate attempts under the same arming as the answer, one of them from inside a
`respond` call, and reports whether each was refused. If any of them says False, the hook was
not doing anything and the run below fails, whatever else it found.

Task ids: M6.1.5
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
PROBE = REPO / "tests" / "fixtures" / "zero_network_probe.py"


@pytest.fixture(scope="module")
def probe() -> dict[str, Any]:
    """Run the probe once and hand back what it reported.

    Module scoped because it starts an interpreter, and the four assertions below are four
    readings of one run rather than four runs. A failure prints the child's stderr, which is
    where the traceback names the audit event that fired.
    """
    finished = subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=REPO,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert finished.returncode == 0, (
        "the fast lane reached the network while answering, or the probe could not run:\n"
        f"{finished.stderr}"
    )
    return dict(json.loads(finished.stdout.strip().splitlines()[-1]))


@pytest.mark.invariant
def test_answering_a_fast_lane_question_opens_no_socket_and_resolves_no_name(
    probe: dict[str, Any],
) -> None:
    """**The leaf.** The fast lane's guarantee is that it answers from the local projection
    with no model and no network, and everything downstream of it was designed on that basis:
    the empty tool catalogue, the reads restricted to projected tables, the timing budget.

    The probe exits non-zero if any blocked audit event fires while it answers, so the useful
    assertion here is on the answer itself: a run that produced nothing would exit zero too
    and would have proved that not answering opens no socket.

    Delete this and the one property that distinguishes this lane from the others is
    checked by nothing."""
    assert probe["records"] == 1
    assert probe["field"] == "hours_remaining"
    assert probe["value"] == "12"


@pytest.mark.invariant
def test_the_block_refuses_a_socket_and_a_name_lookup_in_the_same_process(
    probe: dict[str, Any],
) -> None:
    """The control for the test above, and the reason this file is not a mock.

    A hook that was never armed, or whose prefix list stopped matching, gives the same green
    run as a fast lane that never dials. These two attempts are made after the answer and
    before the hook is disarmed, so they are evidence about the arming the answer ran under
    rather than about some other moment.

    Delete this and the whole file passes with the hook commented out."""
    assert probe["controls"]["socket"] is True
    assert probe["controls"]["resolve"] is True


@pytest.mark.invariant
def test_the_block_is_live_inside_the_call_under_test(probe: dict[str, Any]) -> None:
    """The third control, and the strongest of them: a `respond` call whose reader dials out,
    which has to raise. The other two prove the hook was armed in the process; this proves it
    was armed on the code path, so a future edit that ran the answer somewhere the hook does
    not reach fails here rather than passing quietly.

    Delete this and the arming could be moved to after the answer with two controls still
    green."""
    assert probe["controls"]["inside_respond"] is True
