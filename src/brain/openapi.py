"""The API's own description: how a caller authenticates, and what they are not told.

FastAPI generates a schema for free, and serving that schema is the default. It is also
the mistake this module exists to prevent, because the generated document is a complete
inventory: every path, every operation, every model, every field name, for every caller
including one who has presented nothing. In a system whose whole proposition is that two
people asking the same question get different answers, an unauthenticated inventory of the
capabilities is a permission map. It names the tools, it names the admin surface, and it
tells an attacker exactly which door to spend their time on. See
`A_SCHEMA_IS_A_PERMISSION_MAP`.

So there are two documents, produced by one function from one app.

**The public one is a projection**, containing only the operations that are already served
without authentication, with their component schemas pruned to what those operations
actually reference. Pruning is not tidiness. FastAPI collects every response model in the
application into `components.schemas` regardless of which path uses it, so a document with
the private paths removed and the components left alone still publishes the shape of every
private response, field names included.

**The internal one is the whole thing**, and it is what a signed-in integrator gets.

Both describe authentication, because a document that omits it makes every client author
guess, and they guess the same way: an API key in a query string. What neither does is
enumerate scopes. An OAuth2 scheme listing every scope in the estate is the permission map
again, in the one place a reader is most likely to trust it. See
`SCOPES_ARE_NOT_ENUMERATED`.

Membership of the public set is decided by tag and by prefix, deny-by-default in both:
an operation is public only if it carries a tag, every tag it carries is in `PUBLIC_TAGS`,
and its path is not under `API_PREFIX`. An untagged route is private, a route with one
public tag and one private tag is private, and a route under the versioned API prefix is
private however it is tagged. A new route added by somebody who has never read this file
is therefore private without anyone having to remember, which is the only kind of rule
that survives.

Task ids: M31.1.4.2
"""

from __future__ import annotations

import copy
import enum
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any

from brain.api import API_PREFIX

if TYPE_CHECKING:  # pragma: no cover - a type-only import, never a runtime dependency
    from fastapi import FastAPI

# ------------------------------------------------------------------- written-down reasons
#: Why the generated schema is not simply served.
A_SCHEMA_IS_A_PERMISSION_MAP = (
    "The generated schema is a complete inventory of paths, operations, models and field "
    "names. Served unauthenticated in a permission-aware system it is a permission map: "
    "it names the admin surface, names the tools, and tells anyone which door is worth "
    "their time. The public document is a projection of the routes that are already "
    "unauthenticated, and nothing else."
)

#: Why the security scheme names no scopes.
SCOPES_ARE_NOT_ENUMERATED = (
    "An OAuth2 scheme listing every scope is the capability list again, in the place a "
    "reader trusts most. What a token may do is decided per request from the holder's "
    "grants, narrowed by the channel and by how strongly they are signed in, and it is "
    "not a property of the API that can be written down once for everybody."
)

#: Why membership of the public set is decided negatively.
PUBLIC_BY_TAG_PRIVATE_BY_DEFAULT = (
    "Public membership requires a tag, and every tag on the operation must be a public "
    "one, and the path must be outside the versioned API prefix. Anything else is "
    "private. A rule that lists what is private instead would be one route behind the "
    "codebase permanently: the leak would be a route somebody forgot to add to a list, "
    "which is the same as no rule at all."
)

#: Stated plainly so this document is not mistaken for more, or less, than it is.
#:
#: **This used to say no authentication was mounted at all, and that is no longer true.**
#: Every route under `API_PREFIX` takes `brain.api_routes.asking`, which validates the
#: bearer token through `brain.identity.oidc.validate_token` and refuses without one, so the
#: requirement described below is enforced on every operation this document marks as needing
#: it. What is still ahead of the code is narrower and worth naming precisely: no signature
#: verifier is wired into the deployed process, so today the enforcement is a refusal of
#: everything rather than an acceptance of the right tokens.
DOCUMENTED_BEFORE_IT_IS_ENFORCED = (
    "The security requirement here is enforced rather than described: every operation under "
    "the versioned prefix takes a dependency that validates the bearer token and refuses "
    "the request without one. What is not yet wired is the signature verifier, which is an "
    "injected callback because the standard library cannot check RS256, so a deployed "
    "instance refuses every credential rather than accepting a wrong one. The document is "
    "deliberately explicit about that rather than silent, because the alternative is a "
    "client author guessing why nothing works."
)


# --------------------------------------------------------------------------- membership
#: Tags whose operations may appear in the public document. Both are already served
#: without authentication and carry no company data: `docs` is the build tracker and the
#: reserved product URLs, `health` is liveness and readiness.
PUBLIC_TAGS: frozenset[str] = frozenset({"docs", "health"})


class Audience(enum.StrEnum):
    """Who the document is being built for.

    Two values, not a permission check. This module produces documents; deciding which one
    a given caller receives belongs to the route that serves them, where the identity is.
    """

    #: Anyone at all, including an unauthenticated scanner.
    PUBLIC = "public"
    #: A signed-in integrator. Never served from an unauthenticated route.
    INTERNAL = "internal"


def is_public_path(path: str, operation: Mapping[str, Any]) -> bool:
    """Whether this one operation may appear in the public document.

    Three conditions, each sufficient on its own to make an operation private. They are
    deliberately independent: the tag rule is the one an author sets on purpose, and the
    prefix rule is the one that holds when they forget.
    """
    if path.startswith(API_PREFIX):
        return False
    tags = operation.get("tags") or []
    if not isinstance(tags, list) or not tags:
        return False
    return all(isinstance(tag, str) and tag in PUBLIC_TAGS for tag in tags)


# ----------------------------------------------------------------------------- security
#: The name the security requirement refers to. One scheme, because there is one way in.
BEARER_SCHEME = "bearer"

#: How a caller authenticates, described once. No `oauth2` flows block and no `scopes`
#: map: see `SCOPES_ARE_NOT_ENUMERATED`. No `openIdConnect` discovery URL either, because
#: it differs per deployment and a wrong one in a published document sends every
#: integrator to a host that does not exist.
SECURITY_SCHEMES: Mapping[str, Mapping[str, Any]] = {
    BEARER_SCHEME: {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": (
            "An access token issued by the company's identity provider, presented as "
            "`Authorization: Bearer <token>`. What the token may do is decided per "
            "request from the holder's own grants, narrowed by the channel it arrives on "
            "and by how strongly they are signed in, so it is not listed here."
        ),
    }
}

#: Prose that has to survive being read by somebody integrating at four in the afternoon.
#: The 404 sentence is the one that saves a support conversation: a caller who treats 404
#: as "definitely does not exist" will build a cache that is wrong for exactly the records
#: they are not allowed to see.
AUTH_DESCRIPTION = (
    "\n\n## Authentication\n\n"
    "Every operation requires a bearer token from the company's identity provider unless "
    "it is explicitly marked as needing none. Send it as `Authorization: Bearer <token>`.\n\n"
    "What a token is permitted to do is computed per request, from the holder's grants "
    "narrowed by the channel and by the strength of the sign-in. It is not a fixed list "
    "of scopes and is deliberately not enumerated in this document.\n\n"
    "A `404` means either that the thing does not exist or that nothing you hold reaches "
    "it, and the two are deliberately indistinguishable: a `403` on a hidden record would "
    "confirm the record exists. Treat `404` as 'not available to you', never as proof of "
    "absence.\n"
)


# ----------------------------------------------------------------------- the document
def _refs(node: Any) -> Iterator[str]:
    """Every `$ref` target anywhere below this node, however deeply nested."""
    if isinstance(node, dict):
        target = node.get("$ref")
        if isinstance(target, str):
            yield target
        for value in node.values():
            yield from _refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _refs(item)


def _schema_names(refs: Iterator[str]) -> set[str]:
    prefix = "#/components/schemas/"
    return {ref.removeprefix(prefix) for ref in refs if ref.startswith(prefix)}


def _prune_schemas(doc: dict[str, Any]) -> None:
    """Drop component schemas no surviving path can reach, transitively.

    This is the half of the projection that is easy to forget and expensive to get wrong.
    FastAPI puts every response model in the application into `components.schemas`, so
    removing the private paths and stopping there leaves the shape of every private
    response, field names and all, in a document served to anyone who asks.
    """
    components = doc.get("components")
    if not isinstance(components, dict):
        return
    schemas = components.get("schemas")
    if not isinstance(schemas, dict):
        return

    reachable = _schema_names(_refs(doc.get("paths", {})))
    # Fixed point rather than one pass: a surviving schema can reference another, and a
    # single sweep would drop the second one and leave a dangling `$ref`, which renders as
    # an empty object in every viewer rather than as an error anyone would notice.
    frontier = set(reachable)
    while frontier:
        found = set()
        for name in frontier:
            body = schemas.get(name)
            if body is not None:
                found |= _schema_names(_refs(body))
        frontier = found - reachable
        reachable |= found

    kept = {name: body for name, body in schemas.items() if name in reachable}
    if kept:
        components["schemas"] = kept
    else:
        components.pop("schemas", None)


def document(app: FastAPI, *, audience: Audience = Audience.PUBLIC) -> dict[str, Any]:
    """The OpenAPI document for this audience, with authentication described.

    Deep-copied before anything is touched. `FastAPI.openapi()` memoises its result on the
    application and hands back the same dict every time, so editing it in place would
    change what `/docs` and the framework's own schema route serve, for the life of the
    process, from anywhere this function happened to be called.
    """
    source = app.openapi()
    doc: dict[str, Any] = copy.deepcopy(source)

    info = doc.setdefault("info", {})
    info["description"] = (info.get("description") or "").rstrip() + AUTH_DESCRIPTION

    components = doc.setdefault("components", {})
    components["securitySchemes"] = copy.deepcopy(dict(SECURITY_SCHEMES))
    # Applied at the top level so authentication is the default and each exception has to
    # be written down. The other way round, marking the private operations one at a time,
    # means a new route is public until somebody remembers it.
    doc["security"] = [{BEARER_SCHEME: []}]

    paths = doc.get("paths")
    if isinstance(paths, dict):
        for path, item in list(paths.items()):
            if not isinstance(item, dict):
                continue
            for method, operation in list(item.items()):
                if not isinstance(operation, dict):
                    continue
                if is_public_path(path, operation):
                    # An empty requirement is the OpenAPI way of saying "this one needs
                    # nothing", and it is stated rather than left to the reader, who would
                    # otherwise apply the top-level requirement and send a token to the
                    # health check.
                    operation["security"] = []
                elif audience is Audience.PUBLIC:
                    del item[method]
            if not item:
                del paths[path]

    if audience is Audience.PUBLIC:
        _prune_schemas(doc)
        # Top-level tag metadata for tags nothing public uses would describe the private
        # surface in prose after the operations themselves were removed.
        tags = doc.get("tags")
        if isinstance(tags, list):
            doc["tags"] = [
                tag for tag in tags if isinstance(tag, dict) and tag.get("name") in PUBLIC_TAGS
            ]

    return doc


def public_operations(app: FastAPI) -> tuple[str, ...]:
    """Every path that survives into the public document, sorted. For a test, and for a
    reviewer who wants the answer without reading a schema."""
    doc = document(app, audience=Audience.PUBLIC)
    paths = doc.get("paths", {})
    return tuple(sorted(paths)) if isinstance(paths, dict) else ()
