"""Identity-provider rules that must never break. A failure here blocks deploy.

Six rules, each one cheap to keep now and impossible to retrofit after the first incident:

- nothing in a token is read before its signature is checked (M1.1.2);
- `alg: none` and algorithm confusion are refused outright, and an unknown `kid` never
  falls back to another key (M1.1.2);
- clock skew is bounded, so expiry is not decorative (M1.1.2);
- a claim maps to a role and never to a capability (M1.1.3, M1.1.5);
- an unmapped subject is not a principal (M1.1.3);
- logout propagates, and a token issued before it is refused after it (M1.1.6, M1.1.7).

Several are asserted structurally, over the modules' own namespaces and signatures, rather
than by exercising a path. That is deliberate and follows `test_identity_invariants.py`: a
test that calls a function proves the function behaves, and a test that reads the module
proves nobody added a second function that does not.

Nothing here contacts a Keycloak. The signature check is the injected verifier the module
declares as a seam.

Task ids: M1.1.2, M1.1.3, M1.1.5, M1.1.6, M1.1.7
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType

import pytest

from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.principal import Employment, Principal, PrincipalKind
from brain.core.scope import Scope
from brain.gate.admission import Assurance
from brain.gate.context import Channel
from brain.gate.ingress import Unrecognised
from brain.identity import oidc, sessions
from brain.identity.oidc import (
    ALLOWED_ALGORITHMS,
    MAX_LEEWAY,
    SIGN_IN_PROMPT,
    ClaimMapping,
    GroupRoleRule,
    KeySet,
    MappedIdentity,
    RawToken,
    SigningKey,
    SyncedRoles,
    TokenRefusal,
    TokenRefusedError,
    UnmappedSubject,
    VerifiedClaims,
    assert_no_capability_from_claims,
    map_claims,
    principal_for,
    roles_from_groups,
    validate_token,
)
from brain.identity.packs import subtractive_state
from brain.identity.roles import IdentityError, Role, role_capability_leaks
from brain.identity.sessions import (
    SESSION_ABSOLUTE_MAX,
    ServiceAccount,
    Session,
    SessionRegistry,
    assurance_for_service_account,
    authenticate_service_account,
    open_session,
    reach_for,
)

pytestmark = pytest.mark.invariant

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
ISSUER = "https://id.verz.example/realms/brain"
AUDIENCE = "brain-api"
KID = "k1"
SUBJECT = "1f2e3d4c-0000-4000-8000-000000000001"

#: Every module this milestone added. Named once, so a new module is added here or it is
#: swept by none of the structural checks below.
OIDC_MODULES: tuple[ModuleType, ...] = (oidc, sessions)


# ------------------------------------------------------------------- fixtures
def marker_for(kid: str) -> bytes:
    return b"signed-by:" + kid.encode()


def signing_key(kid: str = KID, algorithm: str = "RS256") -> SigningKey:
    return SigningKey(kid=kid, algorithm=algorithm, material=f"-----PUBLIC {kid}-----")


def key_set(*keys: SigningKey) -> KeySet:
    return KeySet(issuer=ISSUER, keys=keys or (signing_key(),), fetched_at=NOW)


def payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": SUBJECT,
        "sid": "sess-1",
        "exp": int((NOW + timedelta(minutes=5)).timestamp()),
        "iat": int(NOW.timestamp()),
        "groups": ["/brain/member"],
    }
    base.update(overrides)
    return base


def person(
    pid: str = "u_priya",
    employment: Employment = Employment.STAFF,
    not_after: datetime | None = None,
) -> Principal:
    return Principal(
        id=pid,
        kind=PrincipalKind.HUMAN,
        employment=employment,
        display_name="Priya Menon",
        not_after=not_after,
    )


class RecordingPayload(Mapping[str, object]):
    """A claim mapping that writes down every read, in order.

    A `Mapping` rather than a `dict` subclass so `get` goes through `__getitem__` and every
    read is caught, including the ones written as `payload.get(...)`.
    """

    def __init__(self, data: dict[str, object], log: list[str]) -> None:
        self._data = data
        self._log = log

    def __getitem__(self, key: str) -> object:
        self._log.append(f"claim:{key}")
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


def recorded_token(log: list[str], **claims: object) -> RawToken:
    return RawToken(
        header={"alg": "RS256", "kid": KID},
        payload=RecordingPayload(payload(**claims), log),
        signing_input=b"header.payload",
        signature=marker_for(KID),
    )


def recording_verifier(log: list[str], *, result: bool = True) -> Callable[..., bool]:
    def verify(*, signing_input: bytes, signature: bytes, key: SigningKey) -> bool:
        log.append("verify")
        return result and signature == marker_for(key.kid)

    return verify


def token(
    *, kid: str = KID, alg: str = "RS256", signed_by: str | None = None, **claims: object
) -> RawToken:
    header: dict[str, object] = {"alg": alg}
    if kid:
        header["kid"] = kid
    return RawToken(
        header=header,
        payload=payload(**claims),
        signing_input=b"header.payload",
        signature=marker_for(signed_by or kid),
    )


def verifier(*, signing_input: bytes, signature: bytes, key: SigningKey) -> bool:
    return signature == marker_for(key.kid)


def check(
    raw: RawToken, *, keys: KeySet | None = None, now: datetime = NOW, **kwargs: object
) -> VerifiedClaims:
    return validate_token(
        raw,
        keys=keys or key_set(),
        verify=verifier,
        expected_issuer=ISSUER,
        expected_audience=AUDIENCE,
        now=now,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------- INV: nothing is read before it is checked
def test_no_claim_is_read_before_the_signature_has_been_checked() -> None:
    """M1.1.2. The rule the whole module is arranged around.

    Delete this and the natural refactor is to look up the issuer's key set from the `iss`
    claim, or find the person from `sub` and check the signature afterwards. Both read
    attacker input as though it were an identity, and neither looks wrong in a diff because
    the happy path is identical. This asserts the order rather than the outcome, because the
    outcome is the same either way for a token that happens to be valid.
    """
    log: list[str] = []
    validate_token(
        recorded_token(log),
        keys=key_set(),
        verify=recording_verifier(log),
        expected_issuer=ISSUER,
        expected_audience=AUDIENCE,
        now=NOW,
    )

    assert "verify" in log
    first_claim = next(i for i, event in enumerate(log) if event.startswith("claim:"))
    assert log.index("verify") < first_claim, log


def test_a_bad_signature_is_refused_as_a_bad_signature_and_not_as_a_bad_claim() -> None:
    """M1.1.2, behaviourally. A token that is wrong in several ways at once is refused for
    the first thing checked, and the first thing checked has to be the signature.

    Delete this and a validator that checks claims first still passes the happy path, still
    refuses this token, and reports the wrong reason to the log an operator is reading.
    """
    hostile = token(
        signed_by="attacker",
        iss="https://id.attacker.example/realms/brain",
        exp=int((NOW - timedelta(days=1)).timestamp()),
    )
    with pytest.raises(TokenRefusedError) as caught:
        check(hostile)
    assert caught.value.reason is TokenRefusal.BAD_SIGNATURE


def test_the_mapping_functions_accept_only_a_verified_token() -> None:
    """M1.1.2 as a signature. "I checked it upstream" must not be something a caller can
    assert by habit.

    Delete this and somebody adds a `Mapping[str, object]` overload for convenience, and
    from that moment an unverified payload can be mapped straight onto a principal.
    """
    for fn in (map_claims, principal_for):
        first = next(iter(inspect.signature(fn).parameters.values()))
        assert first.annotation == "VerifiedClaims", (fn.__name__, first.annotation)


# ----------------------------- INV: alg none, confusion, and no key fallback
def test_a_token_declaring_alg_none_is_refused_outright() -> None:
    """M1.1.2. The canonical attack, and the one whose absence is invisible: a token with no
    signature at all verifies against nothing and would be accepted by any code path that
    treats a missing algorithm as "nothing to check"."""
    for spelling in ("none", "None", "NONE", ""):
        with pytest.raises(TokenRefusedError) as caught:
            check(token(alg=spelling))
        assert caught.value.reason is TokenRefusal.ALG_NONE, spelling


def test_no_symmetric_algorithm_is_acceptable_anywhere() -> None:
    """M1.1.2. Algorithm confusion, refused structurally rather than case by case.

    With HS256 in the allow-list, an attacker takes the published RSA public key, uses it as
    the HMAC secret, and every token they mint verifies. Asserted over the constant so that
    adding one is a failing test rather than a plausible-looking configuration change.
    """
    assert all(not alg.startswith("HS") for alg in ALLOWED_ALGORITHMS), ALLOWED_ALGORITHMS
    with pytest.raises(ValueError, match="allow-list"):
        SigningKey(kid="k-hmac", algorithm="HS256", material="shared-secret")


def test_a_token_naming_a_key_published_for_a_different_algorithm_is_refused() -> None:
    """M1.1.2. The same attack with a real key id: the token asks for one algorithm and
    names a key published for another. Delete this and the key's declared algorithm is
    documentation."""
    with pytest.raises(TokenRefusedError) as caught:
        check(token(alg="ES256"))
    assert caught.value.reason is TokenRefusal.ALG_MISMATCH


def test_a_token_signed_with_an_unknown_key_is_refused() -> None:
    """M1.1.2. No fallback, not even when the set holds exactly one key and the answer is
    obvious.

    "Try the others" gives one attempt per key on file, and retired keys stay on file. This
    asserts the single-key case on purpose, because that is the one where a helpful
    fallback looks harmless.
    """
    with pytest.raises(TokenRefusedError) as caught:
        check(token(kid="k-not-ours"), keys=key_set(signing_key()))
    assert caught.value.reason is TokenRefusal.UNKNOWN_KEY

    with pytest.raises(TokenRefusedError) as unnamed:
        check(token(kid=""))
    assert unnamed.value.reason is TokenRefusal.NO_KEY_ID


def test_a_refusal_never_tells_the_presenter_which_check_failed() -> None:
    """Every reason collapses to one sentence, the discipline `gate.ingress` applies to an
    unrecognised sender. Delete this and the refusal message becomes a debugging aid for
    whoever is forging the token: "unknown key" and "wrong audience" say what to fix."""
    for reason in TokenRefusal:
        refused = TokenRefusedError(reason, "internal detail with the answer in it")
        assert refused.public_message == SIGN_IN_PROMPT
        assert reason.value not in refused.public_message
        assert "detail" not in refused.public_message


# ------------------------------------------- INV: clock skew is bounded
def test_clock_skew_is_bounded_and_explicit() -> None:
    """M1.1.2. An unbounded leeway makes expiry decorative, and it arrives as a one-line
    change by somebody debugging a badly synchronised VPS at the wrong moment.

    The bound is asserted as a value as well as a behaviour, so raising it is a visible
    edit here rather than a default nobody reads.
    """
    assert timedelta(seconds=120) == MAX_LEEWAY

    with pytest.raises(IdentityError, match="clock skew"):
        check(token(), leeway=MAX_LEEWAY + timedelta(seconds=1))
    with pytest.raises(IdentityError, match="clock skew"):
        check(token(), leeway=timedelta(seconds=-1))

    # And the bound actually bites: a token expired by more than the maximum is refused
    # however generous the caller was allowed to be.
    with pytest.raises(TokenRefusedError):
        check(token(), now=NOW + timedelta(minutes=5) + MAX_LEEWAY + timedelta(seconds=1))


# --------------------------------- INV: a claim maps to a role and nothing else
@dataclass(frozen=True)
class LeakyResult:
    """What a group sync would return if somebody added capabilities to it in a hurry."""

    roles: frozenset[Role]
    capabilities: tuple[Capability, ...]


def leaky_by_return(identity: MappedIdentity) -> tuple[Grant, ...]:
    """A sync that hands back grants. The obvious version of the mistake."""
    raise NotImplementedError


def leaky_by_field(identity: MappedIdentity) -> LeakyResult:
    """A sync whose signature is unchanged and whose result now carries capabilities."""
    raise NotImplementedError


def test_an_identity_provider_claim_can_never_confer_a_capability() -> None:
    """M1.1.5, and the most consequential rule in this milestone.

    If a `groups` claim could add a capability, the answer to "who can see the margin on
    this client" would live in a directory that nobody in this company reviews, that a
    client's own IdP administrator may control, and that has no scope grammar. The guard is
    on the signature, and on one level of the returned type's fields, because the way this
    actually breaks is not a changed return type: it is a `capabilities` field added to the
    result while the signature keeps reading `-> SyncedRoles`.
    """
    assert_no_capability_from_claims(roles_from_groups)
    assert_no_capability_from_claims(map_claims)

    with pytest.raises(IdentityError, match="never confer a capability"):
        assert_no_capability_from_claims(leaky_by_return)
    with pytest.raises(IdentityError, match="never confer a capability"):
        assert_no_capability_from_claims(leaky_by_field)


def test_group_sync_returns_roles_and_the_groups_that_matched_nothing() -> None:
    """M1.1.5, behaviourally. `SyncedRoles` holds a `frozenset[Role]`, which cannot carry a
    scope or a capability whatever anybody later puts in the function body.

    The unmapped half matters as much: a renamed group and a group that is none of our
    business look identical from here, and silence makes the first one present as "my
    permissions disappeared overnight" with nothing in any log.
    """
    rules = [GroupRoleRule(group="/brain/member", role=Role.MEMBER)]
    identity = MappedIdentity(
        subject=SUBJECT,
        issuer=ISSUER,
        display_name="Priya Menon",
        primary_department="web",
        email=None,
        groups=("/brain/member", "/finance/everyone"),
    )
    synced = roles_from_groups(identity, rules)

    assert isinstance(synced, SyncedRoles)
    assert synced.roles == frozenset({Role.MEMBER})
    assert synced.unmapped_groups == ("/finance/everyone",)
    assert all(isinstance(role, Role) for role in synced.roles)


def test_no_role_is_mapped_to_anything_capability_shaped_in_either_module() -> None:
    """M1.3.5, extended to the two modules this milestone added.

    `role_capability_leaks` reads shapes rather than names, because the way the rule gets
    broken is not somebody writing `ROLE_CAPABILITIES`; it is a convenience mapping added
    in a hurry and called something innocent. These modules are outside the sweep in
    `test_identity_invariants.py`, so they are swept here or by nobody.
    """
    findings = [f for module in OIDC_MODULES for f in role_capability_leaks(vars(module))]
    assert findings == [], findings


def test_nothing_in_these_modules_lets_a_row_subtract() -> None:
    """M1.4.2, extended the same way. Logout is the obvious place to reach for a `revoked`
    flag, and a tombstone is a negative row with a friendlier name: from the moment one
    exists, resolution has an order and two rows can disagree.

    Logout here deletes the session and raises a floor over `iat`, which is a fact about
    time rather than a row that subtracts.
    """
    findings = [f for module in OIDC_MODULES for f in subtractive_state(module)]
    assert findings == [], findings


# ------------------------------- INV: an unmapped subject is not a principal
def test_an_unmapped_subject_is_not_a_principal_and_holds_no_entitlement_set() -> None:
    """M1.1.3. A valid token from somebody we have never onboarded is a normal event, and an
    empty `Principal` is the wrong answer: it type-checks everywhere a real one does, so it
    flows into the gate, resolves to an empty set and produces a confident "I could not find
    that" for a person who should have been told to ask an administrator.

    Deliberately asserted as an absence of attributes, exactly as the partner invariant is:
    what makes this safe is that it cannot be intersected, hashed or cached.
    """

    class EmptyDirectory:
        def principal_for_subject(self, issuer: str, subject: str) -> Principal | None:
            return None

    result = principal_for(check(token()), EmptyDirectory(), now=NOW)

    assert isinstance(result, UnmappedSubject)
    assert not isinstance(result, Principal)
    for attribute in ("intersect", "ent_hash", "holds", "grants", "employment"):
        assert not hasattr(result, attribute), attribute
    # The same shape `ingress.Unrecognised` uses: a prompt and nothing to compute with.
    assert set(vars(Unrecognised(Channel.CONSOLE)).keys()) <= {"channel", "prompt"}


def test_a_principal_who_has_expired_never_reaches_a_caller_as_a_principal() -> None:
    """M1.1.3 and M1.2.3 together. A leaver whose Keycloak account still works must not
    reach a code path holding a real principal object, even one that would fail later.

    Delete this and the failure moves to entitlement time, where it presents as an empty
    answer rather than as "this account is closed".
    """

    class Directory:
        def principal_for_subject(self, issuer: str, subject: str) -> Principal | None:
            return person(employment=Employment.CONTRACTOR, not_after=NOW - timedelta(days=1))

    assert isinstance(principal_for(check(token()), Directory(), now=NOW), UnmappedSubject)


# ----------------------------------------- INV: logout propagates
def logged_in(registry: SessionRegistry, *, second_factor: bool = False) -> Session:
    session = open_session(
        claims=check(token()), principal=person(), now=NOW, second_factor=second_factor
    )
    registry.register(session)
    return session


def test_a_token_issued_before_a_logout_is_refused_after_it() -> None:
    """M1.1.6. The property that makes logout mean anything at all.

    A bearer token is valid because of what is inside it, so deleting a row on our side
    changes nothing about the copy already in somebody's hand. Delete this test and "sign
    out" becomes a screen that clears a cookie: the stolen laptop, the shared machine and
    the Friday leaver all keep working until the token expires on its own.
    """
    registry = SessionRegistry()
    logged_in(registry)
    claims = check(token())

    assert registry.admit(claims, person(), NOW).session_id == "sess-1"

    registry.end_session("sess-1", NOW + timedelta(minutes=1))
    with pytest.raises(TokenRefusedError) as caught:
        registry.admit(claims, person(), NOW + timedelta(minutes=2))
    assert caught.value.reason is TokenRefusal.LOGGED_OUT


def test_a_token_issued_in_the_same_second_as_the_logout_is_refused() -> None:
    """M1.1.6. `iat` has one-second granularity, so a token minted in the same second as the
    logout cannot be ordered against it. The safe reading of an ambiguous case is to refuse.

    Delete this and the comparison becomes `<`, which silently exempts exactly the token
    most likely to have been minted by whoever was racing the logout.
    """
    registry = SessionRegistry()
    logged_in(registry)
    claims = check(token())

    registry.end_all_for("u_priya", claims.issued_at)
    with pytest.raises(TokenRefusedError) as caught:
        registry.admit(claims, person(), NOW + timedelta(seconds=1))
    assert caught.value.reason is TokenRefusal.LOGGED_OUT


def test_logout_propagates_to_a_replica_that_never_saw_the_session() -> None:
    """M1.1.6. The case that only the floor covers.

    Behind a load balancer, the replica handling the next request may never have held the
    session, and after a restart no replica has. An implementation that ends a session by
    deleting a row works in every single-process test and refuses nothing in production.
    """
    fresh_replica = SessionRegistry()
    claims = check(token())

    fresh_replica.end_all_for("u_priya", NOW + timedelta(minutes=1))

    assert fresh_replica.get("sess-1") is None
    with pytest.raises(TokenRefusedError) as caught:
        fresh_replica.admit(claims, person(), NOW + timedelta(minutes=2))
    assert caught.value.reason is TokenRefusal.LOGGED_OUT


def test_a_session_can_never_be_refreshed_into_a_standing_grant() -> None:
    """M1.1.6. An idle window that slides on every refresh never closes, so a sign-in from
    March still authenticates requests in September.

    The absolute expiry is set when the session opens and is never moved, which is what
    makes it a bound rather than a suggestion.
    """
    registry = SessionRegistry()
    session = logged_in(registry)

    assert session.absolute_expiry == NOW + SESSION_ABSOLUTE_MAX
    assert not session.is_live(session.absolute_expiry)
    with pytest.raises(TokenRefusedError):
        registry.refresh("sess-1", person(), session.absolute_expiry)


def test_a_live_session_is_authenticated_and_a_second_factor_is_what_makes_it_strong() -> None:
    """M1.1.6. These two values are the assurance ceiling `brain.gate.admission` narrows
    every request by, so they decide whether `approve` and `admin` are reachable at all.

    Delete this and "the account has MFA configured" starts standing in for "a second factor
    was presented in this session", which are different facts about different moments.
    """
    registry = SessionRegistry()
    password_only = logged_in(registry)
    registry_two = SessionRegistry()
    with_factor = logged_in(registry_two, second_factor=True)

    assert password_only.assurance(NOW) is Assurance.AUTHENTICATED
    assert with_factor.assurance(NOW) is Assurance.STRONG
    assert password_only.assurance(NOW + SESSION_ABSOLUTE_MAX) is Assurance.UNVERIFIED


# --------------------------------- INV: a service account never exceeds its owner
def service_account(**overrides: object) -> ServiceAccount:
    base: dict[str, object] = {
        "client_id": "brain-sync",
        "subject": "service-account-subject",
        "owner_principal_id": "u_priya",
        "ceiling": (
            Capability(value="read:client.name"),
            Capability(value="read:hr.salary"),
            Capability(value="admin:connector"),
        ),
        "not_after": NOW + timedelta(days=90),
    }
    base.update(overrides)
    return ServiceAccount(**base)  # type: ignore[arg-type]


def test_a_service_account_can_never_hold_more_than_its_owner() -> None:
    """M1.1.7. The architecture's words: a service principal with its own grants is union
    authority, which is the classic escalation.

    Grant the integration one capability its owner lacks and the pair of them can do more
    than either. Here the ceiling names three capabilities and the owner holds one of them,
    so the account holds exactly one. Delete this and the first "the sync job needs
    read:hr.salary" ticket turns the robot into a way around its owner's permissions.
    """
    owner = person()
    held = EntitlementSet(
        principal_id="u_priya",
        grants=(
            Grant(capability=Capability(value="read:client.name"), scope=Scope.department("web")),
            Grant(capability=Capability(value="read:client.margin"), scope=Scope.department("web")),
        ),
    )
    reach = reach_for(service_account(), owner, held, NOW)

    assert isinstance(reach, EntitlementSet)
    held_values = {g.capability.value for g in held.grants}
    reach_values = {g.capability.value for g in reach.grants}
    assert reach_values <= held_values, reach_values
    assert reach_values == {"read:client.name"}
    # And the scope is the owner's, not something the ceiling invented.
    assert reach.scope_for(Capability(value="read:client.name"), NOW) == Scope.department("web")


def test_widening_a_service_account_ceiling_grants_nothing_on_its_own() -> None:
    """M1.1.7, as the safety property that makes the ceiling reviewable: it can only ever
    narrow, so an operator who adds a capability by mistake has granted nobody anything.

    This is the same asymmetry `brain.gate.admission` relies on for the channel and
    assurance ceilings, and it is what makes getting it backwards catastrophic.
    """
    owner = person()
    held = EntitlementSet(
        principal_id="u_priya",
        grants=(
            Grant(capability=Capability(value="read:client.name"), scope=Scope.department("web")),
        ),
    )
    narrow = reach_for(
        service_account(ceiling=(Capability(value="read:client.name"),)), owner, held, NOW
    )
    wide = reach_for(service_account(), owner, held, NOW)

    assert isinstance(narrow, EntitlementSet)
    assert isinstance(wide, EntitlementSet)
    assert {g.capability.value for g in wide.grants} == {g.capability.value for g in narrow.grants}


def test_a_service_account_credential_is_never_strong() -> None:
    """M1.1.7. A client secret held by a process is one factor and is the only one a machine
    can present. Treating it as strong would give every integration `approve` and `admin`,
    which `brain.gate.admission` reserves for a live session with a second factor in it."""
    assert assurance_for_service_account(service_account(), NOW) is Assurance.AUTHENTICATED
    assert (
        assurance_for_service_account(service_account(), NOW + timedelta(days=365))
        is Assurance.UNVERIFIED
    )


def test_the_two_authentication_paths_cannot_be_crossed() -> None:
    """M1.1.7. A person's browser token must not act as a service account, and a service
    account's token must not open an interactive session.

    Crossing them in either direction unions two ceilings: the signed-in person picks up the
    robot's reach, or the robot picks up the AUTHENTICATED assurance the API channel is
    deliberately not given.
    """
    accounts = {"service-account-subject": service_account()}

    with pytest.raises(TokenRefusedError) as as_service:
        authenticate_service_account(check(token()), accounts, NOW)
    assert as_service.value.reason is TokenRefusal.NOT_A_SERVICE_ACCOUNT

    machine = check(token(sid=None, sub="service-account-subject"))
    with pytest.raises(TokenRefusedError) as as_person:
        open_session(claims=machine, principal=person(), now=NOW)
    assert as_person.value.reason is TokenRefusal.SESSION_UNKNOWN


def test_the_claim_mapping_cannot_name_a_claim_that_would_set_employment() -> None:
    """M1.1.3. `ClaimMapping` has no `employment_claim` and no `capability_claim`, and the
    model forbids extras, so adding one is a diff somebody reviews.

    Employment is what carries a contractor's mandatory expiry. A claim that could set it to
    STAFF would erase that expiry, which `brain.core.principal` calls the single most common
    way a permission model rots.
    """
    assert set(ClaimMapping.model_fields) == {
        "display_name_claim",
        "department_claim",
        "groups_claim",
        "email_claim",
    }
    with pytest.raises(ValueError, match=r"extra_forbidden|Extra inputs"):
        ClaimMapping(employment_claim="employment")  # type: ignore[call-arg]
