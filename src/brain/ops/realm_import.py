"""Turning the reviewed realm into one Keycloak will accept.

**The realm file did not import, and nothing knew.** `ops/keycloak/realm-export.json` is a
carefully argued document and seventeen tests hold it to its claims, and on 2026-09-06 a
throwaway Keycloak 26.0 refused it outright:

    ERROR: Failed to run import
    ERROR: Unrecognized field "_comment" (class org.keycloak.representations.idm.
    RealmRepresentation), not marked as ignorable (144 known properties: ...)

Keycloak deserialises the realm into strongly typed representations and rejects any field it
does not recognise. The file carries eleven `_comment` keys, which are the reason it is worth
reading, and every one of them outside a `config` map is fatal.

**Why the tests could not have caught it.** They parse the file as JSON and assert about its
contents, and it is perfectly valid JSON. What they could not know is what Keycloak's
deserialiser does with a key it has never heard of, and no amount of reading the file
produces that fact. It took an import to find, which is the same lesson `M1.1.1` recorded
when the first import found three silently discarded client scopes.

**Deleting the comments was the obvious fix and is the wrong one.** They are the argument for
every security decision in that file: why RS256 over HS256, why no password grant, why the
redirect URIs are exact paths. A realm nobody can read is a realm that gets edited by whoever
is fixing a login problem at the time, which is precisely what the comments exist to prevent.

So the reviewed file keeps its argument and the import gets a copy without it. `strip_comments`
is that copy, and it is deliberately not a text transform: an underscore-prefixed key can
appear inside a `config` map, where Keycloak stores free-form strings and accepts anything,
and a regex over the text would remove those too and quietly change what the mapper does.

**`config` is the one place an underscore key survives.** `ProtocolMapperRepresentation.config`
is a `Map<String, String>`, so Keycloak neither validates nor rejects its keys. That is why
the realm's two mapper comments live there and why they are left alone: removing them would
be removing configuration, not documentation, and the two are indistinguishable from outside.

Task ids: none
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: The prefix that marks a key as documentation rather than configuration. One character,
#: chosen because Keycloak has no field starting with it and never will: its representations
#: are Java beans, and a leading underscore is not a legal Java identifier start for a
#: property name that Jackson would map.
COMMENT_PREFIX = "_"

#: The one key whose contents are a free-form map rather than a typed representation.
#:
#: Keycloak stores `config` as `Map<String, String>` and accepts any key in it, so a comment
#: there is configuration as far as the server is concerned and removing it changes what was
#: imported. Everything else in the file is a bean with a fixed set of properties.
FREE_FORM_KEY = "config"

#: Why the comments are stripped rather than deleted from the source.
A_REALM_NOBODY_CAN_READ_IS_A_REALM_THAT_GETS_CLICKED = (
    "The comments are the argument for every security decision in the realm: RS256 over "
    "HS256, no password grant, no implicit flow, exact redirect URIs rather than a wildcard. "
    "A configuration file that states none of its reasons is one that gets edited by "
    "whoever is fixing a login problem at the time, and each of those edits is one line and "
    "undoes a paragraph. So the reviewed file keeps the argument and the import gets a copy "
    "without it, rather than the argument being deleted to satisfy a deserialiser."
)


def strip_comments(node: Any, *, inside_free_form: bool = False) -> Any:
    """The realm with its documentation removed, ready for `--import-realm`.

    Recursive over the parsed structure rather than over the text, because the same key is
    documentation in one place and configuration in another. Inside a `config` map every key
    is a string Keycloak stores verbatim, so a comment there is data and is kept; everywhere
    else it is a field name Keycloak will refuse.

    `inside_free_form` is carried down rather than checked at each level, because a `config`
    map may itself hold nested structures and everything under one is equally free-form.
    """
    if isinstance(node, dict):
        return {
            key: strip_comments(value, inside_free_form=inside_free_form or key == FREE_FORM_KEY)
            for key, value in node.items()
            if inside_free_form or not key.startswith(COMMENT_PREFIX)
        }
    if isinstance(node, list):
        return [strip_comments(item, inside_free_form=inside_free_form) for item in node]
    return node


def importable_realm(source: Path) -> str:
    """The reviewed realm as JSON Keycloak accepts, ready to write beside it.

    Returns text rather than writing, so a caller decides where it goes and a test can read
    it without a temporary directory.
    """
    return json.dumps(strip_comments(json.loads(source.read_text(encoding="utf-8"))), indent=2)


def comment_keys(node: Any, *, path: str = "realm", inside_free_form: bool = False) -> list[str]:
    """Every documentation key Keycloak would refuse, with where it is.

    Used by the check that runs before an import rather than after a failure. The failure
    itself is legible enough once you have seen it once; what is expensive is seeing it for
    the first time in a deployment, because a realm that fails to import leaves Keycloak
    running with no realm in it and every sign-in fails with no obvious cause.
    """
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            free_form = inside_free_form or key == FREE_FORM_KEY
            if key.startswith(COMMENT_PREFIX) and not inside_free_form:
                found.append(f"{path}.{key}")
            else:
                found.extend(comment_keys(value, path=f"{path}.{key}", inside_free_form=free_form))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(
                comment_keys(item, path=f"{path}[{index}]", inside_free_form=inside_free_form)
            )
    return found
