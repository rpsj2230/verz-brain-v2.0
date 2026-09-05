"""Agents: lenses over a caller's reach, never principals of their own.

An agent narrows what its caller can already do. `E_run(caller, agent)` is `E(caller)`
intersected with the agent's ceiling, and the intersection is the whole idea: an agent
that could add reach would be a way to escalate by installing something.

**What does not belong here.** Anything that decides whether a call is allowed. That
is `brain.gate`, and it stays there so there is one place to audit rather than one
per agent. An agent chooses what to try; the gate decides what happens.
"""
