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


# ------------------------------------------- how long somebody stays signed in
#
# The confirmed policy is ten hours absolute and thirty minutes idle. It is written twice:
# as `timedelta` constants the registry enforces, and as seconds in the realm above. The
# tests below are the only thing that keeps the two copies equal.
#
# Rejected: generating this JSON from the Python constants. The realm export is
# version-sensitive and Keycloak owns the field names, so a generator would have to model a
# schema that changes underneath it, and a wrong field name would be silently ignored on
# import rather than caught. A test that reads both and compares needs no such model, and it
# fails on the machine of whoever edits one side.
#
# The failure being prevented is not a security hole; it is worse in one particular way.
# Whichever copy is shorter wins, and neither end says why, so people are signed out at
# times that match no stated policy. That reads as flakiness, and flakiness in a sign-in is
# how a team learns to click through whatever the login page asks them.


def test_the_realm_and_the_registry_agree_on_the_longest_a_session_may_live() -> None:
    """Delete this and the two copies of the ten-hour ceiling drift apart at the first edit.

    Compared in seconds because that is the realm's unit; `SESSION_ABSOLUTE_MAX` is the
    stated policy and the realm is a second implementation of it."""
    from brain.identity.sessions import SESSION_ABSOLUTE_MAX

    assert _realm()["ssoSessionMaxLifespan"] == SESSION_ABSOLUTE_MAX.total_seconds()


def test_the_realm_and_the_registry_agree_on_the_idle_window() -> None:
    """The other half of the same drift. Thirty minutes is the number the console tells
    people about; the realm is what actually ends the session."""
    from brain.identity.sessions import SESSION_IDLE

    assert _realm()["ssoSessionIdleTimeout"] == SESSION_IDLE.total_seconds()


def test_a_client_session_cannot_outlive_the_sign_in_it_belongs_to() -> None:
    """Keycloak keeps a per-client session inside the SSO session, with its own two
    timeouts. Raising those past the SSO ones does nothing, which is why somebody would
    raise them: it looks like the setting that is not working.

    Asserted as ceilings rather than equalities so a deliberately shorter client session
    stays legal. Shorter is always safe here; longer is the edit worth stopping."""
    realm = _realm()
    assert realm["clientSessionMaxLifespan"] <= realm["ssoSessionMaxLifespan"]
    assert realm["clientSessionIdleTimeout"] <= realm["ssoSessionIdleTimeout"]


def test_an_offline_session_is_bounded_at_all() -> None:
    """`offlineSessionMaxLifespanEnabled` defaults to false, and false means an offline
    token never expires. That is a permanent credential wearing the same word as everything
    else on this page, and it is one boolean away at all times.

    Delete this and the ten-hour ceiling holds for every session except the one kind that
    outlives the laptop it was issued to."""
    realm = _realm()
    assert realm["offlineSessionMaxLifespanEnabled"] is True
    assert realm["offlineSessionMaxLifespan"] <= realm["ssoSessionMaxLifespan"]
    assert realm["offlineSessionIdleTimeout"] <= realm["ssoSessionIdleTimeout"]


def test_an_access_token_expires_well_inside_the_idle_window() -> None:
    """The stated idle window is a lie by exactly one token lifetime. A token minted just
    before somebody walks away keeps working until it expires, so the real time between the
    last action and the last possible request is idle plus token lifespan.

    So the quantity to bound is that sum, not the token lifespan on its own. Thirty-five
    minutes against a stated thirty is a rounding error; a token lifespan near the idle
    window would nearly double it, and nothing in the realm would say so.

    A quarter is the tolerance: the true window still rounds to the stated one at the
    granularity anybody reasons about. Note this is strictly tighter than the 900-second
    ceiling asserted above, which on its own would permit a fifty percent overshoot - the
    two tests bound different things and neither implies the other."""
    realm = _realm()
    effective = realm["ssoSessionIdleTimeout"] + realm["accessTokenLifespan"]
    assert effective <= realm["ssoSessionIdleTimeout"] * 1.25, (
        f"the real idle window is {effective}s against a stated {realm['ssoSessionIdleTimeout']}s"
    )


def test_remember_me_cannot_extend_a_session_past_the_ceiling() -> None:
    """Remember-me is off, and if it is ever turned on it carries its own pair of lifespans
    that override the ones above. Zero means inherit, which is the safe value and the one
    set here.

    Written as a conditional rather than asserting the feature stays off, because turning it
    on is a reasonable product decision and losing the ten-hour ceiling to it is not. Delete
    this and the ceiling has an off switch labelled with a convenience feature."""
    realm = _realm()
    if realm["rememberMe"] is True:
        for key in ("ssoSessionMaxLifespanRememberMe", "ssoSessionIdleTimeoutRememberMe"):
            override = realm[key]
            base = realm[key.removesuffix("RememberMe")]
            assert override == 0 or override <= base, (
                f"{key} lets remember-me outlive the confirmed ceiling"
            )
