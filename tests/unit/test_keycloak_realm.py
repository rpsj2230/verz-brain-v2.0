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

**M1.1.1 is claimed now, and it was not before.** These tests were written first and the
leaf was left open, because the realm's own header said it had never been imported into a
running Keycloak and that one real import was the condition for calling it done. Passing a
test suite over a JSON file is not evidence that the file imports.

It has been imported since: 2026-09-06, Keycloak 26.0, an ephemeral container on a throwaway
server, with what the server stored read back and compared to what the file says. The header
of `ops/keycloak/realm-export.json` records the run and what it found.

The import earned its keep immediately. It logged three warnings that no amount of reading
would have produced, because they are a fact about how Keycloak treats a full import rather
than about the file's contents: `brain-console` referenced the `openid`, `profile` and
`email` client scopes, a full import replaces the client scope set with the one the file
declares, and the file declares only `brain-identity`. All three were silently discarded.
`test_every_client_scope_a_client_asks_for_is_defined_in_this_file` is that finding turned
into a check.

Still not claimed by anything: `ops/keycloak/setup.sh`. The import mounted this file and
started the server with `--import-realm`; it never went through kcadm, so the script's own
"has never been run" header is still accurate.

What these tests catch day to day is drift between this file and `brain.identity`, which
would otherwise be discovered as "random logouts" or as a token nobody should have accepted.

Task ids: M1.1.1
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from brain.identity.roles import ROLE_COUNT, SCOPE_REQUIRED, Role
from brain.identity.sessions import SESSION_ABSOLUTE_MAX

REPO = Path(__file__).resolve().parents[2]
REALM_PATH = REPO / "ops" / "keycloak" / "realm-export.json"


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


def test_every_client_scope_a_client_asks_for_is_defined_in_this_file() -> None:
    """**Written because the first real import found three that were not.**

    A full realm import replaces the client scope set with the one this file declares, so a
    client naming a scope the file does not define gets nothing. Keycloak does not refuse
    it: it logs `Referenced client scope 'profile' doesn't exist. Ignoring` and carries on,
    which means the client imports looking configured and is missing the claims somebody
    thought they had assigned.

    `brain-console` listed `openid`, `profile` and `email`. None was defined here and all
    three were discarded on import against Keycloak 26.0 on 2026-09-06. `openid` was wrong
    twice over: it is a scope value a client asks for in a request, never a client scope an
    administrator defines.

    Nothing was lost, because `brain.identity.oidc` reads exactly two claims, `groups` and
    `department`, and the `brain-identity` scope maps both. What was lost was a silent
    import, and an import that prints warnings is one where the next person cannot tell the
    harmless lines from the real ones.

    Delete this and a scope name can be added here that resolves to nothing on the server,
    which is invisible in the file and visible only in a log nobody keeps."""
    realm = _realm()
    defined = {s["name"] for s in realm.get("clientScopes", [])}

    dangling: dict[str, list[str]] = {}
    for client in _clients():
        asked = list(client.get("defaultClientScopes") or []) + list(
            client.get("optionalClientScopes") or []
        )
        missing = sorted(set(asked) - defined)
        if missing:
            dangling[str(client["clientId"])] = missing

    assert not dangling, f"these clients name client scopes this file does not define: {dangling}"


def test_the_scope_the_console_uses_maps_the_two_claims_the_code_reads() -> None:
    """The other half of the check above: the scope exists, and it carries what is consumed.

    `brain.identity.oidc` defaults `groups_claim` to "groups" and `department_claim` to
    "department". Those two claims are the whole interface between the identity provider and
    this system's permission model: groups become roles, department becomes scope. A scope
    that exists but maps neither would satisfy the dangling-reference test and still produce
    a token this system can do nothing with.

    Delete this and the mappers can be removed from the scope while every other test here
    stays green."""
    scopes = {s["name"]: s for s in _realm().get("clientScopes", [])}

    assert "brain-identity" in scopes
    mapped = {
        m.get("config", {}).get("claim.name")
        for m in scopes["brain-identity"].get("protocolMappers", [])
    }

    assert {"groups", "department"} <= mapped, f"brain-identity maps {mapped}"


def test_the_audience_the_api_demands_is_minted_into_the_console_s_own_tokens() -> None:
    """**The realm was broken here and every test passed.** `validate_token` refuses a token
    whose `aud` does not contain the expected audience, with `TokenRefusal.WRONG_AUDIENCE`.
    An `oidc-audience-mapper` is what puts it there, and where that mapper sits decides
    whether it ever runs.

    It sat in `brain-api`'s own `protocolMappers`. A dedicated mapper applies to tokens
    issued **for** that client, and `brain-api` has standardFlow, directAccess,
    serviceAccounts and implicit all disabled, because it is a resource server nobody signs
    in to. So no token was ever issued for it, the mapper could never fire, and the console's
    tokens would have carried no audience for the API at all. Every sign-in would have
    succeeded at Keycloak and then been refused by this system, which reads as "login is
    broken" and is nowhere near the file that caused it.

    The mapper's own `_comment` described exactly that failure as the thing it existed to
    prevent. It is this repository's most common defect in its purest form: correct,
    documented, and never invoked.

    The two checks either side of this one could not see it. One asserts every scope a client
    asks for is defined, and the mapper was not in a scope. The other asserts `brain-identity`
    maps groups and department, and would pass with no audience mapper anywhere in the file.
    Nothing joined the audience the code demands to the client that actually signs in.

    So this test walks the join: take the audience from the client that a person authenticates
    through, follow its default scopes, and require a mapper reached that way to mint it.
    Asserting the mapper is merely present somewhere would pass again on the day somebody
    moves it back.

    Delete this and the mapper can return to a client that mints no tokens, which is where it
    was written in the first place and looks like the obvious place for it."""
    realm = _realm()
    clients = {c["clientId"]: c for c in realm.get("clients", [])}
    scopes = {s["name"]: s for s in realm.get("clientScopes", [])}

    console = clients["brain-console"]
    assert console.get("standardFlowEnabled") is True, (
        "this test assumes the console is the client a person signs in through"
    )

    # Every mapper that can reach a token minted for the console: its own dedicated ones,
    # plus those on the scopes it carries by default. An optional scope is deliberately not
    # counted, because the console would have to ask for it and nothing here asks.
    reachable = list(console.get("protocolMappers") or [])
    for name in console.get("defaultClientScopes") or []:
        reachable.extend(scopes.get(name, {}).get("protocolMappers") or [])

    audiences = {
        m.get("config", {}).get("included.client.audience")
        for m in reachable
        if m.get("protocolMapper") == "oidc-audience-mapper"
        and m.get("config", {}).get("access.token.claim") == "true"
    }

    assert "brain-api" in audiences, (
        "no audience mapper reachable from brain-console mints aud=brain-api, so "
        f"validate_token refuses every token it issues; reachable audiences: {audiences}"
    )


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


CALLBACK_PATH = "/auth/callback"
SIGNED_OUT_PATH = "/signed-out"


def test_the_console_can_actually_be_signed_in_to() -> None:
    """**The realm shipped with `.invalid` placeholders, so sign-in could not complete from
    any address, including localhost.** That was deliberate while nobody had given an address:
    Keycloak hands an authorisation code to any URI listed here, so a stale one is an open
    redirect, and a placeholder is safer than somebody else's hostname.

    An address exists now, so the placeholder is the thing that would be wrong. This asserts
    the client can be signed in to at all, which is the property the placeholders removed.

    Exact paths rather than a wildcard, because a wildcard readmits exactly the risk the
    placeholders were guarding against: `https://example.com/*` accepts a code at any path on
    that host, including one an attacker controls.

    Delete this and the realm can go back to a state where every sign-in fails at the last
    step, which reads to a user as "login is broken" and is nowhere near the file causing it."""
    console = {c["clientId"]: c for c in _realm()["clients"]}["brain-console"]
    uris = console["redirectUris"]

    assert uris, "the console has no redirect URI, so no sign-in can complete"
    for uri in uris:
        assert ".invalid" not in uri, f"{uri} is a placeholder, so sign-in cannot complete"
        assert not uri.rstrip("/").endswith("*"), (
            f"{uri} is a wildcard, and Keycloak will hand a code to any path under it"
        )
        assert uri.endswith(CALLBACK_PATH), (
            f"{uri} does not end in the path the console actually returns to"
        )

    origins = console["webOrigins"]
    assert origins, "no web origin, so the browser cannot call the API after signing in"
    for uri in uris:
        assert any(uri.startswith(origin) for origin in origins), (
            f"{uri} has no matching webOrigin, so its CORS preflight is refused"
        )


def test_every_registered_address_agrees_with_the_paths_the_console_uses() -> None:
    """The join between two files that must not drift. `console/src/auth/constants.ts` decides
    where the browser comes back to, and the realm decides where Keycloak is willing to send
    it. If they disagree the sign-in fails at the final redirect, after the person has already
    typed their password, which is the least debuggable place for it to fail.

    Read out of the TypeScript rather than repeated here, so this compares the two records
    instead of comparing the realm against a third copy of the same string.

    Delete this and either file can be edited alone."""
    import re

    source = (REPO / "console" / "src" / "auth" / "constants.ts").read_text(encoding="utf-8")
    callback = re.search(r'CALLBACK_PATH\s*=\s*"([^"]+)"', source)
    signed_out = re.search(r'SIGNED_OUT_PATH\s*=\s*"([^"]+)"', source)

    assert callback and signed_out, "the console no longer declares its own paths"
    assert callback.group(1) == CALLBACK_PATH
    assert signed_out.group(1) == SIGNED_OUT_PATH

    console = {c["clientId"]: c for c in _realm()["clients"]}["brain-console"]
    post_logout = console["attributes"]["post.logout.redirect.uris"]
    for entry in post_logout.split("##"):
        assert entry.endswith(SIGNED_OUT_PATH), (
            f"{entry} is not where the console goes after signing out"
        )
