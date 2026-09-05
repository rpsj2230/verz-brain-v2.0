"""Extension points: where a client's own code plugs in.

Everything here is loaded from outside this repository, so everything here is
untrusted by construction. An extension runs with the reach of whoever invoked it
and never more.

**What does not belong here.** Anything this system ships and depends on. Code that
must work is code that must be tested here, and an extension point is precisely the
place where 'must work' stops being true.
"""
