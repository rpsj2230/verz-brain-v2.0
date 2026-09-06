"""Write the API's internal OpenAPI document to a file, without starting the server.

**Generation is a development-time step and never a runtime fetch, and that is forced by
the application rather than chosen here.** `brain.app.create_app` sets `openapi_url=None`
in production, so there is no schema route to fetch from a deployed instance. A console
that generated its client by calling a live server would therefore work against staging
and fail against production, which is the worst possible place to discover that the
document was never meant to be public: `brain.openapi.A_SCHEMA_IS_A_PERMISSION_MAP` says
why the route is closed, and it names the exact leak.

So the schema is produced from the application object in this repository, at the commit
the console is being built against. `FastAPI.openapi()` builds the document from the
mounted routes and does not consult `openapi_url` at all, so this works even against a
production-configured `Settings`. No socket is opened. `create_app` builds routes only;
everything that touches a database, a cache or a secret store lives in the lifespan, and
the lifespan does not run unless something serves the app.

**The internal document, not the public one.** The public projection is deliberately just
the routes that are already served unauthenticated, with the component schemas pruned to
what those routes reach. Generating a client from it would produce a typed client for the
health check and nothing else, for ever, and the omission would look like an empty API
rather than like the wrong audience.

**It also checks that the console's lock text is still the backend's lock text.** That is
not scope creep. `brain.core.redaction.render_lock` takes no arguments so that a withheld
field cannot render differently for different people, and the console re-implements the
rendering because the payload carries the fact of the lock rather than its text. Two
copies of one string is a drift risk, and this is the moment in the workflow when the two
are both in reach: whoever regenerates the schema after changing the backend is exactly
the person who should be told. The check matches the whole assignment expression rather
than searching for the word "Restricted", because the word also appears in the prose
around it and a check a comment can satisfy is not a check.

Rejected: committing the generated output. A committed copy is correct until somebody
changes a response model without rerunning this, and from that moment the console compiles
against an API that no longer exists while every check stays green. Regenerating needs no
server and no network, so keeping a stale copy buys nothing.

Run it from anywhere:

    uv run python console/scripts/export-openapi.py

Task ids: M32.5.1.1
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import NoReturn

#: This file is `<repo>/console/scripts/export-openapi.py`, so the repository root is two
#: directories up from the script's own directory. Derived rather than passed in, because a
#: path argument is a thing that gets typed wrongly once and then written into a runbook.
REPO_ROOT = Path(__file__).resolve().parents[2]
CONSOLE_ROOT = REPO_ROOT / "console"
OUT_DIR = CONSOLE_ROOT / "src" / "api" / "generated"
SCHEMA_PATH = OUT_DIR / "openapi.internal.json"

#: Where the console's copy of the lock text lives, and the exact shape it must have.
#: Anchored to the export statement so that renaming the constant fails this check loudly
#: instead of quietly turning it off.
LOCK_SOURCE = CONSOLE_ROOT / "src" / "ui" / "Lock.tsx"
LOCK_ASSIGNMENT = re.compile(r'^export const LOCK_TEXT = "(?P<value>[^"]*)";$', re.M)

#: Written down because the failure it describes is silent. The console renders a withheld
#: field from a constant of its own; if that constant drifts from the backend's, one
#: channel says "Restricted" and another says something else, and a reader comparing two
#: screens learns which system withheld which field.
LOCK_TEXT_HAS_ONE_SOURCE = (
    "brain.core.redaction.LOCK_TEXT is the only definition of what a withheld field says. "
    "The console holds a copy because it renders the lock itself. This script is where the "
    "two are compared, and a difference is an error rather than a warning."
)

#: Every route in the application is either public or under the versioned prefix. The
#: versioned ones are listed after a run and the empty case is called out, because a
#: generated client with no operations in it looks exactly like a broken generator. That
#: branch was the whole output until the first routes landed on 2026-09-06; it is kept
#: because unmounting a router is a one-line change and this is where it would show.
API_PREFIX = "/api/v1"


def _fail(message: str) -> NoReturn:
    sys.stderr.write(f"export-openapi: {message}\n")
    raise SystemExit(1)


def check_lock_text(backend_value: str) -> None:
    """Refuse to generate anything if the console's lock text has drifted."""
    if not LOCK_SOURCE.exists():
        _fail(f"{LOCK_SOURCE} is missing; the console must render the lock somewhere")
    found = LOCK_ASSIGNMENT.search(LOCK_SOURCE.read_text(encoding="utf-8"))
    if found is None:
        _fail(
            f"no LOCK_TEXT export of the expected shape in {LOCK_SOURCE.name}. "
            f"{LOCK_TEXT_HAS_ONE_SOURCE}"
        )
    console_value = found.group("value")
    if console_value != backend_value:
        _fail(
            f"the console renders a withheld field as {console_value!r} and the backend "
            f"renders it as {backend_value!r}. {LOCK_TEXT_HAS_ONE_SOURCE}"
        )


def main() -> None:
    sys.path.insert(0, str(REPO_ROOT / "src"))

    from brain.app import Settings, create_app
    from brain.core.redaction import LOCK_TEXT
    from brain.openapi import Audience, document

    check_lock_text(LOCK_TEXT)

    # Explicit settings rather than whatever is in the developer's environment. The
    # document must describe the application, not one machine's configuration, and
    # `run_migrations=False` states that nothing here is allowed to touch a database even
    # if a URL happens to be set in the shell that ran this.
    app = create_app(Settings(env="development", database_url="", run_migrations=False))
    doc = document(app, audience=Audience.INTERNAL)

    paths = doc.get("paths", {})
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # newline="\n" is not decoration on this machine: the default rewrites every line
    # ending to CRLF on Windows, which makes a regenerated file differ from itself.
    SCHEMA_PATH.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")

    versioned = sorted(p for p in paths if p.startswith(API_PREFIX))
    print(f"wrote {SCHEMA_PATH.relative_to(REPO_ROOT)} with {len(paths)} paths")
    if not versioned:
        print(
            f"  note: no operation is mounted under {API_PREFIX} yet, so the generated "
            "types describe the health and build-documentation routes only. The client is "
            "wired and there is nothing yet for it to be typed against."
        )
    else:
        for path in versioned:
            print(f"  {path}")


if __name__ == "__main__":
    main()
