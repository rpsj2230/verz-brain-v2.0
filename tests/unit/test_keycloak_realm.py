"""The realm file, held to the claims its own header makes.

`ops/keycloak/realm-export.json` is a careful document. Its header argues for RS256 over
HS256, for a session lifespan that matches ours, for refresh-token revocation, against the
password grant, against the implicit flow, and for groups that carry roles and never
capabilities. Every one of those is a security decision and, until this file, nothing
compared any of them to the code they are supposed to agree with.

**A configuration file that argues well and is checked by nothing is a file that was right
on the day it was written.** The realm is edited by whoever is fixing a login problem at the
time, and the edits that matter here do not look dangerous: turning on the password grant to
get a script working, lengthening a session because people complain about logouts, adding a
client without pinning its algorithm. Each is one line and each undoes a paragraph.

**M1.1.1 is deliberately not claimed by this file.** The realm header says plainly that it
has never been imported into a running Keycloak, that none was contacted, and that it needs
one real import before anybody calls the leaf done. That is still true: there is no Keycloak
in the compose file. These tests make the document trustworthy as a document; they cannot
make it a realm that exists, and passing them is not evidence that it imports.

What they do catch is drift between this file and `brain.identity`, which is the failure
that would otherwise be discovered as "random logouts" or as a token nobody should have
accepted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from brain.identity.roles import ROLE_COUNT, SCOPE_REQUIRED, Role
from brain.identity.sessions import SESSION_ABSOLUTE_MAX

REALM_PATH = Path(__file__).resolve().parents[2] / "ops" / "keycloak" / "realm-export.json"


def _realm() -> dict[str, Any]:
    # `json.loads` is typed as returning Any, and a cast here says the shape is a mapping
    # without claiming anything about its contents, which is exactly what the tests below
    # go on to check one key at a time.
    loaded: dict[str, Any] = json.loads(REALM_PATH.read_text(encoding="utf-8"))
    return loaded


def _clients() -> list[dict[str, Any]]:
    return list(_realm()["clients"])


def _group_names(groups: list[dict[str, Any]]) -> set[str]:
    """Every group name at every depth, flattened."""
    found: set[str] = set()
    for group in groups:
        found.add(group["name"])
        found |= _group_names(group.get("subGroups", []))
    return found


def _top_level_role_groups() -> dict[str, dict[str, Any]]:
    """The role groups under `/brain`, keyed by name."""
    brain = next(g for g in _realm()["groups"] if g["name"] == "brain")
    return {g["name"]: g for g in brain["subGroups"]}


# --------------------------------------------------------------- tokens and signatures
def test_the_realm_signs_with_rs256_and_every_client_is_pinned_to_it() -> None:
    """`brain.identity.oidc` refuses HS256 outright, because an HMAC-signed token is signed
    by anything holding the client secret, and the classic confusion attack presents the
    published RSA public key as that secret. Both ends agreeing means a misconfiguration
    fails at login rather than becoming an accepted token.

    Pinned per client as well as on the realm, because a client that names no algorithm
    inherits whatever the realm default becomes next. Delete this and a new client can be
    added unpinned, which is the state the confusion attack needs."""
    realm = _realm()

    assert realm["defaultSignatureAlgorithm"] == "RS256"
    for client in _clients():
        algorithm = client.get("attributes", {}).get("access.token.signed.response.alg")
        assert algorithm == "RS256", f"{client['clientId']} is not pinned to RS256: {algorithm}"


def test_a_stolen_refresh_token_is_detectable_rather_than_a_ten_hour_credential() -> None:
    """Replaying a revoked refresh token invalidates the chain, which is what makes theft
    visible. Without it a copied refresh token is a full-lifespan credential and nothing
    anywhere notices it was taken.

    `refreshTokenMaxReuse` is asserted at nought rather than merely present: any positive
    value permits the replay this setting exists to detect."""
    realm = _realm()

    assert realm["revokeRefreshToken"] is True
    assert realm["refreshTokenMaxReuse"] == 0


def test_the_realm_session_lifespan_matches_the_one_our_own_registry_enforces() -> None:
    """**Two clocks that disagree present as a bug rather than as a policy.** If the realm
    allowed longer than `SESSION_ABSOLUTE_MAX`, a refresh loop would keep a March sign-in
    alive in September on the Keycloak side while our registry refused it, and people would
    report random logouts rather than an expiry they could understand.

    Delete this and the realm can be lengthened to stop the complaints, which moves the
    disagreement rather than fixing it."""
    assert _realm()["ssoSessionMaxLifespan"] == int(SESSION_ABSOLUTE_MAX.total_seconds())


# --------------------------------------------------------------- flows that can be bypassed
@pytest.mark.parametrize("client", _clients(), ids=lambda c: str(c["clientId"]))
def test_no_client_offers_the_password_grant(client: dict[str, Any]) -> None:
    """The password grant skips the browser flow, and so skips the second factor and every
    conditional step configured in it. A flow whose checks can be avoided by asking a
    different endpoint is not a flow.

    Parametrised per client so a new one is covered the day it is added rather than the day
    somebody remembers to extend a loop."""
    assert client.get("directAccessGrantsEnabled") is False


@pytest.mark.parametrize("client", _clients(), ids=lambda c: str(c["clientId"]))
def test_no_client_offers_the_implicit_flow(client: dict[str, Any]) -> None:
    """The implicit flow puts the token in a URL fragment, which lands in browser history and
    in referrer headers. Neither is a place a credential can be withdrawn from."""
    assert client.get("implicitFlowEnabled") is False


@pytest.mark.parametrize("client", _clients(), ids=lambda c: str(c["clientId"]))
def test_every_public_client_requires_pkce_with_s256(client: dict[str, Any]) -> None:
    """A public client holds no secret, so the authorisation code is the whole credential
    between the redirect and the exchange. Without PKCE anything that can observe the code
    can spend it, and `plain` is not PKCE: the verifier travels in the clear beside the code
    it is meant to protect.

    Confidential clients are exempt rather than skipped silently, and the assertion says
    which is which, so a client that quietly becomes public is covered."""
    if not client.get("publicClient"):
        return
    method = client.get("attributes", {}).get("pkce.code.challenge.method")
    assert method == "S256", f"{client['clientId']} is public and its PKCE method is {method}"


# --------------------------------------------------------------- groups carry roles only
def test_every_platform_role_has_a_group_and_there_are_no_others() -> None:
    """The realm and `brain.identity.roles` have to name the same six things. A role with no
    group can never be granted through the directory, and a group naming no role assigns
    something the code has never heard of.

    Asserted in both directions. A one-way check passes while the realm accumulates groups
    nobody maps, and those are exactly the ones an administrator will assume mean
    something."""
    expected = {role.value.replace("_", "-") for role in Role}
    found = set(_top_level_role_groups())

    assert found == expected
    assert len(expected) == ROLE_COUNT


def test_exactly_the_roles_that_require_a_scope_have_scoped_subgroups() -> None:
    """A Department Admin with no scope is a Super Admin nobody appointed, and an Approver
    with no scope approves anything anyone asks. `SCOPE_REQUIRED` says which two those are,
    and the realm gives exactly those two a level of subgroups to carry the scope.

    Both directions again. A scoped role with no subgroups can only be granted unscoped,
    and an unscoped role that grows subgroups is offering a distinction the resolver will
    ignore, which is worse than refusing it because somebody will rely on it."""
    groups = _top_level_role_groups()
    scoped = {name for name, group in groups.items() if group.get("subGroups")}
    expected = {role.value.replace("_", "-") for role in SCOPE_REQUIRED}

    assert scoped == expected


def test_no_group_or_role_in_the_realm_names_a_capability() -> None:
    """**The claim the realm's own header makes most strongly, and the one nothing checked.**
    Groups carry roles and never capabilities. A capability named here would move the answer
    to "who can read the margin on this client" into a directory nobody in this company
    reviews, and `brain.core.entitlement` would no longer be the only place reach is decided.

    Capabilities are `verb:object` and roles are bare words, so the colon is the tell.
    Delete this and a helpful `read:price_list.cost` group appears in the directory, granted
    by whoever administers Keycloak rather than by anybody who reviewed a grant."""
    realm = _realm()
    names = _group_names(realm["groups"])
    names |= {r["name"] for r in realm.get("roles", {}).get("realm", [])}
    for client in _clients():
        names |= {r["name"] for r in client.get("defaultRoles", []) if isinstance(r, dict)}

    offenders = sorted(name for name in names if ":" in name)

    assert not offenders, f"these name capabilities rather than roles: {offenders}"


def test_the_realm_is_the_one_the_application_expects() -> None:
    """A realm renamed is every issuer wrong at once, and the failure is a token rejected
    with no explanation of which end moved."""
    realm = _realm()

    assert realm["realm"] == "brain"
    assert realm["enabled"] is True


def test_brute_force_protection_is_on() -> None:
    """A sign-in page with no lockout is a password-guessing service. Cheap to leave off and
    unpleasant to discover, because nothing about it looks wrong until somebody is inside."""
    assert _realm()["bruteForceProtected"] is True
