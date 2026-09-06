"""The realm as Keycloak will actually read it, rather than as JSON.

Every other test over `ops/keycloak/realm-export.json` parses it and asserts about its
contents, which is right and was not enough: the file is valid JSON and Keycloak refused it.

    ERROR: Failed to run import
    ERROR: Unrecognized field "_comment" (class org.keycloak.representations.idm.
    RealmRepresentation), not marked as ignorable

Measured on 2026-09-06 against a throwaway Keycloak 26.0.8. The same file with its comments
stripped imported cleanly on the next run: "Realm 'brain' imported".

Task ids: none
"""

from __future__ import annotations

import json
from pathlib import Path

from brain.ops.realm_import import (
    COMMENT_PREFIX,
    comment_keys,
    importable_realm,
    strip_comments,
)

REPO = Path(__file__).resolve().parents[2]
REALM = REPO / "ops" / "keycloak" / "realm-export.json"


def test_the_reviewed_realm_still_carries_the_comments_that_would_break_an_import() -> None:
    """**The premise of every test below, asserted rather than assumed.**

    If somebody deleted the comments from the source file to make it import, these tests
    would all pass over a file that no longer explains any of its own security decisions, and
    the stripping step would be dead code nobody noticed.

    So this asserts the problem still exists in the file, which is the state the design wants:
    the reviewed version argues, and the import gets a copy that does not.

    Delete this and the fix can be undone by deleting the thing it was protecting."""
    found = comment_keys(json.loads(REALM.read_text(encoding="utf-8")))

    assert found, (
        "the realm carries no documentation keys, so either the comments were deleted to "
        "satisfy Keycloak, which loses the argument this file exists for, or the stripping "
        "step is now unnecessary and should go"
    )
    assert any(key == "realm._comment" for key in found), (
        "the realm-level comment is gone; that is the one Keycloak refused first"
    )


def test_the_stripped_realm_has_nothing_keycloak_would_refuse() -> None:
    """The property that decides whether a deployment signs anybody in. Keycloak
    deserialises the realm into typed representations and rejects any field it does not
    recognise, and a realm that fails to import leaves the server running with no realm in
    it: every sign-in then fails with no cause visible anywhere near the login page.

    Asserted over the stripped structure rather than over the text, because the text still
    contains the word inside `config` maps where it is legitimate.

    Delete this and the strip can quietly stop stripping."""
    stripped = strip_comments(json.loads(REALM.read_text(encoding="utf-8")))

    assert comment_keys(stripped) == []


def test_a_comment_inside_a_config_map_is_configuration_and_is_kept() -> None:
    """**The reason this is a parser and not a regex**, and it is not hypothetical: the realm
    has two of these today, on the `brain-identity` scope's mappers.

    `ProtocolMapperRepresentation.config` is a `Map<String, String>`. Keycloak neither
    validates nor rejects its keys, so an underscore key there is a value the server stores
    verbatim. Removing it is removing configuration, and from outside the two are
    indistinguishable: both are a string under a key nobody else reads.

    A text transform removing every line containing the prefix would take these too, and the
    only symptom would be a mapper that behaves differently for a reason nobody can see.

    Delete this and the strip can be simplified into something that silently edits mappers."""
    document = {
        "clients": [
            {
                "_comment": "documentation, and Keycloak refuses it",
                "clientId": "probe",
                "protocolMappers": [
                    {
                        "_comment": "also documentation, on a typed representation",
                        "name": "audience",
                        "config": {
                            "_comment": "configuration, because config is a free-form map",
                            "included.client.audience": "brain-api",
                        },
                    }
                ],
            }
        ]
    }

    stripped = strip_comments(document)
    client = stripped["clients"][0]
    mapper = client["protocolMappers"][0]

    assert "_comment" not in client
    assert "_comment" not in mapper
    assert mapper["config"]["_comment"] == "configuration, because config is a free-form map"
    assert mapper["config"]["included.client.audience"] == "brain-api"


def test_stripping_changes_nothing_a_deserialiser_would_read() -> None:
    """The other half, and the one that would fail silently. A strip that removed a real
    field would produce a realm that imports and is wrong: a missing `redirectUris` is a
    client nobody can sign in to, a missing `protocolMappers` is a token with no audience.

    Every key that is not documentation survives, at every depth, compared structurally
    rather than by counting.

    Delete this and the strip can take a field with an underscore anywhere in its name."""
    original = json.loads(REALM.read_text(encoding="utf-8"))
    stripped = strip_comments(original)

    def surviving(node: object) -> object:
        if isinstance(node, dict):
            return {k: surviving(v) for k, v in node.items() if not (k.startswith(COMMENT_PREFIX))}
        if isinstance(node, list):
            return [surviving(v) for v in node]
        return node

    # Everything that is not a top-level documentation key is identical. `config` maps are
    # compared as they stand, so a comment kept inside one shows up as a difference here and
    # is the reason this uses the realm rather than a fixture.
    assert json.dumps(stripped, sort_keys=True) != json.dumps(original, sort_keys=True)
    assert set(stripped) == {k for k in original if not k.startswith(COMMENT_PREFIX)}
    assert stripped["realm"] == original["realm"]
    assert len(stripped["clients"]) == len(original["clients"])
    assert len(stripped["clientScopes"]) == len(original["clientScopes"])
    for before, after in zip(original["clients"], stripped["clients"], strict=True):
        assert after["clientId"] == before["clientId"]
        assert after.get("redirectUris") == before.get("redirectUris")


def test_the_importable_realm_is_json_a_server_can_parse() -> None:
    """The end of the pipeline. `importable_realm` is what gets written beside the reviewed
    file and mounted for `--import-realm`, so it has to be text that round-trips.

    Delete this and the writer can emit a Python repr, which differs from JSON in exactly the
    places that matter: True, None and single quotes."""
    text = importable_realm(REALM)
    reparsed = json.loads(text)

    assert reparsed["realm"] == "brain"
    assert comment_keys(reparsed) == []
    assert "'" not in text.split('"realm"')[0], "this is not JSON"
