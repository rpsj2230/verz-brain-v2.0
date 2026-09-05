"""The identity provider's realm, read as data.

A realm export is the single largest piece of configuration in this system and the one with
no type checker behind it. Every setting here fails the same way: login keeps working, and
something that was supposed to be impossible quietly becomes possible. A public client that
gains a secret still logs people in. A redirect URI widened to a wildcard still logs people
in. A signature algorithm changed to one the verifier does not expect fails closed, which is
the only one of these that would be noticed.

So the settings that matter are asserted, each with the reason it matters, because the
export is 318 lines and a diff over it does not read as anything.

`brain.identity.oidc` is the other half: it refuses `alg: none` and checks the signature
before reading a claim. These tests are about the provider being configured to issue tokens
that half can trust in the first place.

Task ids: M1.1.1
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
KEYCLOAK = REPO / "ops" / "keycloak"


def _realm() -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(
        (KEYCLOAK / "realm-export.json").read_text(encoding="utf-8")
    )
    return parsed


def _client(client_id: str) -> dict[str, Any]:
    for client in _realm()["clients"]:
        if client["clientId"] == client_id:
            found: dict[str, Any] = client
            return found
    msg = f"no client {client_id!r} in the realm export"
    raise AssertionError(msg)


def _setup() -> str:
    return (KEYCLOAK / "setup.sh").read_text(encoding="utf-8")


# --------------------------------------------------------------- the realm itself
def test_the_export_is_valid_json_and_names_its_realm() -> None:
    """The first thing to break, and it breaks at import time on the server rather than
    here. An export that will not parse means a realm that does not exist, which presents as
    every login failing with a provider error nobody can read."""
    assert _realm()["realm"] == "brain"


def test_every_client_the_system_needs_exists() -> None:
    """Three, and they are not interchangeable: the console is a browser app, the API is a
    confidential resource server, and sync is a machine with no person behind it. A missing
    one is a component that cannot authenticate at all, discovered when somebody tries."""
    ids = {c["clientId"] for c in _realm()["clients"]}
    assert {"brain-console", "brain-api", "brain-sync"} <= ids


def test_tokens_are_signed_with_an_algorithm_the_verifier_accepts() -> None:
    """`brain.identity.oidc` refuses anything outside its allow-list, and refuses `none` by
    name. This is the other end of that: a realm issuing something the verifier will not
    accept fails closed, which is the right direction and still an outage."""
    assert _realm()["defaultSignatureAlgorithm"] == "RS256"


def test_an_access_token_does_not_live_long() -> None:
    """A token is a bearer credential: whoever holds it is the person until it expires.
    Five minutes is the difference between a leaked token being a problem for five minutes
    and a problem for a working day.

    Asserted as a ceiling rather than a value, because shortening it is always safe and
    lengthening it is the change worth stopping."""
    assert _realm()["accessTokenLifespan"] <= 900


def test_brute_force_protection_is_on() -> None:
    """Off, the login form is an unlimited password oracle for 126 known usernames. It is one
    boolean, it defaults to false, and nothing else in the system can compensate for it."""
    assert _realm()["bruteForceProtected"] is True


# ------------------------------------------------------------------ the clients
def test_the_console_is_a_public_client_and_holds_no_secret() -> None:
    """A browser app cannot keep a secret: whatever it holds is in the bundle a user can
    read. Marking it confidential does not make it safer, it makes the secret a shared
    password that also appears in the network tab."""
    console = _client("brain-console")
    assert console["publicClient"] is True
    assert not console.get("secret")


def test_the_console_uses_the_authorisation_code_flow_and_not_the_implicit_one() -> None:
    """The implicit flow returns the token in the URL fragment, where it reaches the browser
    history, the referrer header and any script on the page. It exists for browsers that
    could not do PKCE and those browsers are gone."""
    console = _client("brain-console")
    assert console["standardFlowEnabled"] is True
    assert console.get("implicitFlowEnabled") is not True


def test_the_console_requires_pkce() -> None:
    """Without it, an authorisation code intercepted on the redirect is redeemable by
    whoever intercepted it. A public client has no secret to prove it is the one that asked,
    so the proof key is the only thing that binds the code to the request."""
    console = _client("brain-console")
    attrs = console.get("attributes", {})
    assert attrs.get("pkce.code.challenge.method") == "S256"


@pytest.mark.parametrize("client_id", ["brain-console", "brain-api", "brain-sync"])
def test_no_client_accepts_a_wildcard_redirect(client_id: str) -> None:
    """A wildcard redirect URI is an open redirect, and an open redirect on an OAuth client
    is a way to have the provider hand somebody's code to an attacker's page. It reads as a
    convenience while a developer is setting up a second environment."""
    for uri in _client(client_id).get("redirectUris") or []:
        assert "*" not in uri, f"{client_id} accepts a wildcard redirect: {uri}"
        assert not uri.startswith("http://") or "localhost" in uri, (
            f"{client_id} redirects over plain http: {uri}"
        )


def test_the_api_client_grants_no_interactive_flow() -> None:
    """A resource server has no login page. Leaving a flow enabled on it gives an attacker a
    second route to a token that nobody is watching, because nobody expects that client to
    be used for logging in."""
    api = _client("brain-api")
    assert api["publicClient"] is False
    assert api.get("standardFlowEnabled") is not True
    assert api.get("directAccessGrantsEnabled") is not True


def test_the_sync_client_is_a_service_account_and_nothing_else() -> None:
    """It runs with no person behind it. A service account that also allows an interactive
    flow is a machine identity somebody can log in as, and its permissions are sized for a
    machine."""
    sync = _client("brain-sync")
    assert sync["serviceAccountsEnabled"] is True
    assert sync.get("standardFlowEnabled") is not True


def test_no_client_allows_the_password_grant() -> None:
    """Direct access grants send the user's password through this application. That makes
    every component in the path a place a password can be logged, and it defeats the entire
    point of federating identity to a provider."""
    for client in _realm()["clients"]:
        assert client.get("directAccessGrantsEnabled") is not True, (
            f"{client['clientId']} accepts a username and password directly"
        )


# ------------------------------------------------------------------- the script
def test_the_setup_script_parses_under_the_shell_that_runs_it() -> None:
    """It runs on the server under dash, not under the bash on this laptop. A script that
    parses here and not there fails at the one moment somebody is setting up a realm."""
    import subprocess

    result = subprocess.run(
        ["sh", "-n", str(KEYCLOAK / "setup.sh")], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_the_script_imports_the_exported_realm_rather_than_building_one() -> None:
    """Two sources for one realm is two realms. If the script created clients itself, the
    export would be documentation that drifts, and the drift would show up as a setting
    somebody swears they configured."""
    assert "realm-export.json" in _setup()


def test_the_script_does_not_carry_a_password() -> None:
    """A setup script is committed, copied and pasted into a terminal, and ends up in shell
    history on the server. A credential in it is a credential in all three."""
    text = _setup()
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        assert "password=" not in line.lower().replace(" ", "") or "$" in line, (
            f"a literal password in the setup script: {line.strip()[:60]}"
        )
