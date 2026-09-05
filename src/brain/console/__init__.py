"""The console: the screens a person actually uses.

Rendering and routing only. Every question it asks goes through the same gate as a
question arriving from Lark or email, because a second path into the data is a second
place for a permission rule to be missing.

**What does not belong here.** Any query that reaches a table directly. The console
is a channel, and a channel that can read a row without passing the gate is not a
channel, it is a hole.
"""
