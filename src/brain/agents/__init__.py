"""Agents: lenses over a caller's reach, never principals of their own.

An agent narrows what its caller can already do. `E_run(caller, agent)` is `E(caller)`
intersected with the agent's ceiling, and the intersection is the whole idea: an agent
that could add reach would be a way to escalate by installing something.

**What does not belong here.** Anything that decides whether a call is allowed. That
is `brain.gate`, and it stays there so there is one place to audit rather than one
per agent. An agent chooses what to try; the gate decides what happens.

**Where audience sits against that rule.** `brain.agents.model` answers who may see and
start an agent, and that is a property of the agent rather than a decision about a call:
it produces the set `brain.gate.select.select_agent` consults, and the gate does the
consulting. The ceiling is the same arrangement seen from the other side. Nothing here
intersects an entitlement, computes a reach or admits a request; the two producers hand
`brain.gate` the shapes it already takes and the gate goes on being the only place a call
is decided.
"""
