"""A fast-lane answer produced in a process where opening a socket raises (M6.1.5).

Run as a script by `tests/invariants/test_fast_lane_zero_network.py`, which reads the JSON
this prints on the last line of stdout. It is a separate process on purpose and for two
reasons, and neither is tidiness.

**An audit hook cannot be removed once installed.** `sys.addaudithook` is one way: there is
no matching remove, by design, because a hook that could be taken off is a hook an attacker
takes off. Installing one inside the test session would leave it on the interpreter for every
test that ran afterwards. Here it lasts as long as the process, which is the length of one
answer.

**The claim is about what the code does, not about what a mock was asked.** A test that
patches `httpx` and asserts the mock was not called proves that one library was not used
through the name it was patched under. The audit hook is below all of that: `socket.__new__`
and `socket.getaddrinfo` are raised by CPython itself when a socket is constructed or a name
resolved, whoever constructed it and by whatever route. There is no import, no monkeypatch and
no `del sys.modules` that gets past it while the hook is armed.

**The controls are the point of the file.** A hook that was never armed, or one whose prefix
list stopped matching, would produce a green run and prove nothing at all, which is the exact
failure the leaf is written against. So three deliberate attempts are made *under the same
arming as the answer itself*: constructing a socket, resolving a name, and reaching the
network from inside a `respond` call, which is the one that shows the hook is live during the
code under test rather than merely afterwards. All three have to be refused or this probe
reports failure.

**Arming happens after the imports and before the answer.** Importing SQLAlchemy, pydantic
and the gate is not part of answering a question, and a library that opened a socket at import
time would fail this for a reason that has nothing to do with the fast lane. What is claimed
is narrower and true: **producing a fast-lane answer opens no socket and resolves no name.**

Task ids: M6.1.5
"""

from __future__ import annotations

import json
import socket
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

#: Audit events that mean something is reaching, or about to reach, off this machine.
#: `socket.` covers construction, connection, binding and name resolution; the rest are the
#: protocol libraries that would otherwise get there through their own C code, and the
#: process-spawning events, because shelling out to curl is egress with extra steps.
BLOCKED = (
    "socket.",
    "urllib.Request",
    "http.client",
    "ftplib.",
    "smtplib.",
    "imaplib.",
    "poplib.",
    "ssl.",
    "subprocess.",
    "os.system",
    "os.exec",
    "os.spawn",
    "os.posix_spawn",
    "webbrowser.open",
)


class EgressAttemptedError(RuntimeError):
    """Raised from inside the audit hook, so it surfaces where the attempt was made."""


_armed = False


def _hook(event: str, args: tuple[Any, ...]) -> None:
    """Refuse anything that reaches the network while armed.

    Raising from an audit hook aborts the audited operation with this exception, which is
    what makes the hook an enforcement rather than a log. `args` is deliberately unread: it
    holds the address being dialled, and a probe that printed it would put a hostname into
    CI output for the sake of a message nobody needs.
    """
    del args
    if _armed and event.startswith(BLOCKED):
        raise EgressAttemptedError(event)


sys.addaudithook(_hook)


def _refused(attempt: str) -> bool:
    """Whether one deliberate attempt to reach the network was stopped."""
    try:
        if attempt == "socket":
            socket.socket().close()
        elif attempt == "resolve":
            socket.getaddrinfo("example.invalid", 80)
    except EgressAttemptedError:
        return True
    except OSError:
        # The attempt got past the hook and failed for an ordinary reason: no route, no
        # resolver, a firewall. That is not the hook working, so it is reported as a
        # failure. A probe that counted this as a refusal would pass on any machine with
        # no network, which is most build agents.
        return False
    return False


def main() -> int:
    """Answer one fast-lane question with egress blocked, then prove the block was live."""
    # Imported here rather than at module scope so that the arming below is unambiguously
    # after every import: a hook armed while a module is still loading would be testing the
    # import machinery as much as the answer.
    from brain.core.entitlement import Capability, EntitlementSet, Grant
    from brain.core.field_policy import Classification
    from brain.core.scope import Scope
    from brain.gate.fast_lane import FastPathRule, respond
    from brain.knowledge.columns import ColumnRule, TableClassification
    from brain.knowledge.rows import RowQuery, RowTool

    clients = TableClassification(
        entity="client",
        rules=(
            ColumnRule(
                column="name",
                required_capability=Capability(value="read:client.name"),
                classification=Classification.INTERNAL,
            ),
            ColumnRule(
                column="hours_remaining",
                required_capability=Capability(value="read:client.hours_remaining"),
                classification=Classification.CONFIDENTIAL,
            ),
        ),
    )
    tool = RowTool(source="laravel", classification=clients, description="Read a client.")
    rule = FastPathRule(
        rule_id="client_hours_remaining",
        template="hours left on {client}",
        slot="client",
        source="laravel",
        entity="client",
        match_field="name",
        answer_field="hours_remaining",
    )
    entitlement = EntitlementSet(
        principal_id="p_priya",
        grants=tuple(
            Grant(capability=Capability(value=c), scope=Scope())
            for c in ("read:client", "read:client.name", "read:client.hours_remaining")
        ),
    )

    class LocalRows:
        """Rows from this process. Nothing here is fetched from anywhere."""

        def rows(self, query: RowQuery) -> Sequence[Mapping[str, Any]]:
            del query
            return [{"entity": "client", "id": "c_447", "name": "Acme", "hours_remaining": "12"}]

    class DiallingRows:
        """A reader that reaches the network, so the hook can be caught doing its job."""

        def rows(self, query: RowQuery) -> Sequence[Mapping[str, Any]]:
            del query
            socket.getaddrinfo("example.invalid", 80)
            return []

    readers = {("laravel", "client"): tool.reader(LocalRows())}
    dialling = {("laravel", "client"): tool.reader(DiallingRows())}

    global _armed
    _armed = True
    try:
        answer = respond(
            "hours left on Acme",
            rules=[rule],
            readers=readers,
            entitlement=entitlement,
            now=datetime(2026, 9, 7, 9, 0),
        )
        assert answer is not None, "the probe's own question stopped matching its own rule"
        record = answer.result.records[0].model_dump()

        controls = {
            "socket": _refused("socket"),
            "resolve": _refused("resolve"),
            "inside_respond": False,
        }
        try:
            respond(
                "hours left on Acme",
                rules=[rule],
                readers=dialling,
                entitlement=entitlement,
            )
        except EgressAttemptedError:
            controls["inside_respond"] = True
        except OSError:
            # The reader's dial got past the hook and failed on its own, which means the
            # hook was not live on this path. Recorded as a failed control rather than
            # allowed to end the process, so the test that reads it fails by name instead
            # of by traceback.
            controls["inside_respond"] = False
    finally:
        _armed = False

    # A single JSON line, so the test reads a value rather than parsing prose. Printed last
    # so that anything the run wrote to stdout before it does not have to be skipped over.
    print(
        json.dumps(
            {
                "records": len(answer.result.records),
                "field": answer.field,
                "value": record.get(answer.field),
                "controls": controls,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
