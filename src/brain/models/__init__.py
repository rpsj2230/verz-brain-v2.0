"""Language models: which one answers, in what order, and when we refuse instead.

`models` here means language models. It is not the ORM layer; SQLAlchemy tables live in
`brain.db` and the domain types live in `brain.core`. The name is ambiguous in most Python
projects and it is spelled out here because a package called `models` that turns out to
hold routing policy is exactly the kind of thing a reader guesses wrong once and then
distrusts for the rest of the file.
"""

from __future__ import annotations
