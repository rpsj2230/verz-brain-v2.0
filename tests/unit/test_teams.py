"""Teams: a token minted for somebody else's bot, a tenant that is not ours, and a reply
address the request chose.

Four claims carry this file.

**Two strangers, two checks, and each is held while the other passes.** A token carrying
another bot's app id is signed by Microsoft, arrives from the right issuer and is perfectly
live, so the audience is the only thing that refuses it. Our own bot installed in a stranger's
tenant produces a token that satisfies the audience honestly, so the tenant is the only thing
that refuses that. A suite that only ever failed both at once could not say which one is doing
the work, and the cheap implementation that checks the signature and stops passes every test
where something else is wrong too.

**A bot registration answers more channels than Teams.** An activity that came over Direct
Line carries a valid token from the same issuer and a sender who is whoever opened a web page,
so it is tested with everything else correct.

**The identity is the AAD object id.** Every activity in this file carries a `name` and a
`userPrincipalName` naming somebody else, because that is the shape of the attack: a leaver's
address is handed to a new starter, and the convenient parser keys on it.

**An answer computed at one person's reach cannot reach a room.** Held by the types rather
than by a check, so it is asserted against the types: `Notice` is the only plan that can
address a conversation with more than one reader and it has no payload field to put an answer
in.

Task ids: M10.5.2
"""

from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import inspect
import json
import textwrap
from datetime import UTC, datetime, timedelta

import pytest

from brain.channels.adapter import Feature
from brain.channels.cards import CardRefusedError
from brain.channels.teams import (
    ALLOWED_NOTICES,
    BOT_FRAMEWORK_ISSUER,
    NOT_ACCEPTED,
    ROOM_DEFLECTION,
    TEAMS_ASSURANCE_CEILING,
    TEAMS_CHANNEL_ID,
    TEAMS_CLOCK_SKEW,
    TEAMS_FEATURES,
    Answer,
    ConversationType,
    Notice,
    TeamsAdapter,
    TeamsMessage,
    TeamsRefusedError,
    VerifiedActivity,
    assert_configured,
    audience_is_one_person,
    bearer_token,
    deliver,
    normalise_activity,
    reply_privately,
    room_deflection,
    unrecognised_reply,
    validate_activity_token,
    verified_activity,
)
from brain.core.field_policy import Classification
from brain.core.redaction import OPAQUE_LABEL, ChannelPayload, assert_channel_adapter
from brain.gate.admission import Assurance, verbs_for_channel
from brain.gate.context import Channel, TrafficClass, traffic_class_for
from brain.gate.ingress import (
    LEAKING_PATTERNS,
    ChannelEvent,
    Unrecognised,
    identity_hash,
)
from brain.identity.oidc import KeySet, SigningKey, parse_unverified

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
TIMESTAMP = "2026-09-06T12:00:00.0000000Z"

APP_ID = "8f3c1e2a-1111-4000-8000-abcdef123456"
OTHER_APP_ID = "0d4b7c99-2222-4000-8000-fedcba654321"
TENANT = "1b2c3d4e-3333-4000-8000-0123456789ab"
OTHER_TENANT = "9e8d7c6b-4444-4000-8000-ba9876543210"
SERVICE_URL = "https://smba.trafficmanager.net/emea/"
ATTACKER_URL = "https://smba.trafficmanager.net.example.invalid/emea/"

KID = "bf-k1"
OBJECT_ID = "3a1f0c22-5555-4000-8000-1a2b3c4d5e6f"
OTHER_OBJECT_ID = "6f5e4d3c-6666-4000-8000-0f0e0d0c0b0a"
PERSONAL = "a:1qPvXoJm7YkQ0w"
OTHER_PERSONAL = "a:2rQwYpKn8ZlR1x"
ROOM = "19:9d4f2a1b6c3e@thread.tacv2"
ACTIVITY_ID = "1757160000000"

DIGEST = identity_hash(Channel.TEAMS, PERSONAL)
ROOM_DIGEST = identity_hash(Channel.TEAMS, ROOM)


# ------------------------------------------------------------------------ the fixtures
def _key(kid: str = KID, algorithm: str = "RS256", use: str = "sig") -> SigningKey:
    return SigningKey(kid=kid, algorithm=algorithm, material=f"-----PUBLIC {kid}-----", use=use)


def _keys(*keys: SigningKey) -> KeySet:
    return KeySet(issuer=BOT_FRAMEWORK_ISSUER, keys=keys or (_key(),), fetched_at=NOW)


def _signature(signing_input: bytes, kid: str = KID) -> bytes:
    """The stand-in for Microsoft's signature.

    An HMAC over the real signing input rather than a marker, so that a claim edited after
    signing fails exactly as it would in production. Keyed on the key id, so presenting a
    token signed by one key while naming another fails too.
    """
    return hmac.new(kid.encode(), signing_input, hashlib.sha256).digest()


def _verifier(*, signing_input: bytes, signature: bytes, key: SigningKey) -> bool:
    assert signing_input, "the signing input must be the bytes that were signed"
    return hmac.compare_digest(_signature(signing_input, key.kid), signature)


def _b64(blob: bytes) -> str:
    return base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")


def _claims(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "iss": BOT_FRAMEWORK_ISSUER,
        "aud": APP_ID,
        "serviceurl": SERVICE_URL,
        "nbf": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(minutes=30)).timestamp()),
    }
    base.update(overrides)
    return base


def _compact(
    *,
    header: dict[str, object] | None = None,
    signed_by: str | None = None,
    signature: bytes | None = None,
    **claims: object,
) -> str:
    """One compact JWS, signed over its own header and payload."""
    head = header if header is not None else {"alg": "RS256", "kid": KID, "typ": "JWT"}
    parts = f"{_b64(json.dumps(head).encode())}.{_b64(json.dumps(_claims(**claims)).encode())}"
    kid = signed_by or str(head.get("kid", KID))
    sealed = signature if signature is not None else _signature(parts.encode("ascii"), kid)
    return f"{parts}.{_b64(sealed)}"


def _authorization(**kwargs: object) -> str:
    return f"Bearer {_compact(**kwargs)}"  # type: ignore[arg-type]


def _activity(
    *,
    activity_type: str = "message",
    activity_id: str = ACTIVITY_ID,
    channel_id: str = TEAMS_CHANNEL_ID,
    service_url: str = SERVICE_URL,
    object_id: str | None = OBJECT_ID,
    display_name: str = "Priya Menon",
    principal_name: str = "priya@client.example",
    role: str | None = None,
    conversation_id: str = PERSONAL,
    conversation_type: str = "personal",
    tenant: str | None = TENANT,
    text: str | None = "what is outstanding",
    timestamp: str | None = TIMESTAMP,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """The activity Teams posts, with the knobs each test needs to turn.

    `name` and `userPrincipalName` are present on every one of these on purpose. They are what
    Teams sends beside the object id, so every test that reads a sender is also a test that
    neither of them was the thing read.
    """
    sender: dict[str, object] = {
        "id": "29:1kQvXoJm7YkQ0wA",
        "name": display_name,
        "userPrincipalName": principal_name,
    }
    if object_id is not None:
        sender["aadObjectId"] = object_id
    if role is not None:
        sender["role"] = role

    activity: dict[str, object] = {
        "type": activity_type,
        "id": activity_id,
        "channelId": channel_id,
        "serviceUrl": service_url,
        "from": sender,
        "conversation": {"id": conversation_id, "conversationType": conversation_type},
        "recipient": {"id": f"28:{APP_ID}", "name": "Brain"},
        "localTimestamp": "2026-09-06T20:00:00.0000000+08:00",
    }
    if text is not None:
        activity["text"] = text
    if timestamp is not None:
        activity["timestamp"] = timestamp
    if tenant is not None:
        activity["channelData"] = {"tenant": {"id": tenant}}
    activity.update(extra or {})
    return activity


def _verified(
    raw: object | None = None,
    *,
    authorization: str | None = None,
    keys: KeySet | None = None,
    app_id: str = APP_ID,
    tenant_id: str = TENANT,
    now: datetime = NOW,
    **activity: object,
) -> VerifiedActivity:
    return verified_activity(
        _activity(**activity) if raw is None else raw,  # type: ignore[arg-type]
        authorization=authorization if authorization is not None else _authorization(),
        keys=keys or _keys(),
        verify=_verifier,
        app_id=app_id,
        tenant_id=tenant_id,
        now=now,
    )


def _message(**activity: object) -> TeamsMessage:
    return normalise_activity(_verified(**activity))  # type: ignore[arg-type]


def _payload() -> ChannelPayload:
    return ChannelPayload(records=({"invoice": "INV-1"},))


# ---------------------------------------------- the deployment has to be able to decide
def test_a_deployment_that_names_no_app_id_refuses_everything_loudly() -> None:
    """Without an app id there is nothing to compare the audience against, and a token minted
    for any other bot on the Bot Framework verifies here: Microsoft signs them all with the
    same keys.

    This refusal names the problem, unlike every refusal a sender can cause, and the difference
    is deliberate. It fires on every delivery including the honest ones, so it tells an attacker
    nothing they could not learn by sending anything at all, and a misconfigured authenticator
    should fail loudly rather than quietly.

    Delete this and an install with a blank app id accepts every bot's tokens while looking
    perfectly healthy."""
    with pytest.raises(TeamsRefusedError, match="no Microsoft App Id"):
        assert_configured(app_id="", tenant_id=TENANT, keys=_keys())


def test_a_deployment_that_names_no_tenant_refuses_everything_loudly() -> None:
    """**The sharpest difference from Slack, and the one that has no equivalent there.** A
    Slack app is installed in one workspace and the signing secret is that workspace's. A Teams
    bot is one registration that any tenant can install, and the tokens it then receives carry
    our app id honestly, because they were minted for us.

    So an install that names no tenant is one that answers a stranger's Teams with this
    company's data, with nothing anywhere reporting a fault.

    Delete this and a blank tenant becomes "any tenant", which is the configuration nobody
    means and which reads as deliberate."""
    with pytest.raises(TeamsRefusedError, match="names no tenant"):
        assert_configured(app_id=APP_ID, tenant_id="", keys=_keys())


def test_a_deployment_holding_no_signing_keys_refuses_rather_than_trusting() -> None:
    """Fetching the OpenID metadata document is the deployment's job, which means a fetch that
    failed leaves this module with nothing to verify against. The safe answer is to refuse
    everything and say so; the tempting one is to treat an empty key set as "no key check
    configured".

    Delete this and every activity is refused as though it were an attack, so the one message
    that would send somebody to look at the metadata fetch never appears."""
    with pytest.raises(TeamsRefusedError, match="no Bot Framework signing keys"):
        assert_configured(
            app_id=APP_ID,
            tenant_id=TENANT,
            keys=KeySet(issuer=BOT_FRAMEWORK_ISSUER, keys=(), fetched_at=NOW),
        )


def test_a_naive_clock_is_refused_rather_than_compared_against_an_epoch() -> None:
    """Not a sender's doing, so it gets its own message. A caller passing `datetime.utcnow()`
    on a machine that is not set to UTC gets an expiry check silently offset by hours, and
    without the guard the comparison raises `TypeError` instead, which arrives as a 500 and
    reads to an operator as the integration being broken.

    Delete this and the symptom points at Microsoft."""
    with pytest.raises(TeamsRefusedError, match="aware time"):
        validate_activity_token(
            parse_unverified(_compact()),
            keys=_keys(),
            verify=_verifier,
            app_id=APP_ID,
            now=NOW.replace(tzinfo=None),
        )


# ---------------------------------------------- the credential arrives in a header
def test_a_bearer_credential_is_read_from_the_authorization_header() -> None:
    """The positive case, and the case of the scheme is part of it: RFC 7235 makes an auth
    scheme case-insensitive, so a client sending `bearer` is not doing anything wrong and
    refusing it would be this module inventing a rule."""
    assert bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"
    assert bearer_token("bearer abc.def.ghi") == "abc.def.ghi"


@pytest.mark.parametrize(
    "header",
    ["", "abc.def.ghi", "Basic abc.def.ghi", "Bearer ", "Bearer abc.def.ghi extra", "Bearer"],
)
def test_a_credential_presented_any_other_way_is_refused(header: str) -> None:
    """Microsoft's first validation step is that the token arrived in this header under this
    scheme. A caller that accepted one from a query string would accept one out of a URL, and a
    URL is written to every access log between here and the sender.

    The three-part case is in this list deliberately: splitting on the first space alone reads
    as tolerant and quietly accepts whatever followed the token."""
    with pytest.raises(TeamsRefusedError, match=NOT_ACCEPTED):
        bearer_token(header)


def test_a_token_that_is_not_a_compact_jws_comes_back_as_this_surfaces_refusal() -> None:
    """`identity.oidc.parse_unverified` does the splitting, because five segments is a JWE and
    a splitter written here would be shorter for having left the segment count out. Its
    refusals belong to a different surface: that module's vocabulary is about somebody signing
    in to the console and its public message is a sign-in prompt, which is the wrong sentence
    to put in front of a bot webhook, and its reasons are specific where this one says one
    thing.

    Delete this and a malformed token raises `TokenRefusedError` out of a Teams endpoint, which
    is both the wrong taxonomy and a distinguisher: `expected 3 segments, got 2` tells whoever
    is probing exactly what to change."""
    with pytest.raises(TeamsRefusedError) as caught:
        _verified(authorization="Bearer not-a-jwt")

    assert str(caught.value) == NOT_ACCEPTED


# ---------------------------------------------- the token names a key and the key checks
def test_a_live_token_from_microsoft_for_this_bot_is_accepted() -> None:
    """The positive case. A validator that refused everything would satisfy every refusal in
    this file and mean no Teams activity ever reaches the gate, which is a channel that looks
    built and answers nobody."""
    validated = validate_activity_token(
        parse_unverified(_compact()), keys=_keys(), verify=_verifier, app_id=APP_ID, now=NOW
    )

    assert validated.issuer == BOT_FRAMEWORK_ISSUER
    assert validated.audience == APP_ID
    assert validated.service_url == SERVICE_URL
    assert validated.key_id == KID
    assert validated.verified_at == NOW


@pytest.mark.parametrize(
    "header",
    [
        {"alg": "none", "kid": KID},
        {"alg": "", "kid": KID},
        {"alg": "HS256", "kid": KID},
        {"alg": "RS256"},
        {"alg": "RS256", "kid": ""},
        {"alg": "RS256", "kid": "not-a-key-we-hold"},
        {"alg": "RS384", "kid": KID},
    ],
)
def test_a_header_that_chooses_its_own_verification_is_refused(header: dict[str, object]) -> None:
    """**The token must not be allowed to say how the token is checked.** `alg: none` is the
    canonical attack and an allow-list of asymmetric algorithms is what makes the other one,
    presenting an RSA public key as an HMAC secret, unrepresentable rather than merely checked
    for. `identity.oidc` argues both at length and this module reuses that allow-list rather
    than writing a second one.

    A `kid` we do not hold is refused outright rather than tried against the keys on file:
    falling back to "the only key" or "the newest key" gives a forged token one free attempt
    per key, and retired keys stay on file. `RS384` against a key published for `RS256` is the
    confusion caught even with a known key.

    Delete this and each of these becomes a way to have a token verified by something other
    than the key Microsoft published for it."""
    with pytest.raises(TeamsRefusedError, match=NOT_ACCEPTED):
        validate_activity_token(
            parse_unverified(_compact(header=header, signed_by=KID)),
            keys=_keys(),
            verify=_verifier,
            app_id=APP_ID,
            now=NOW,
        )


def test_a_key_published_for_encryption_never_verifies_a_signature() -> None:
    """JWKS carries `use`, and a key published for encryption is not a key anybody signs with.
    Accepting one widens the set of keys a forged token can name to every key in the document."""
    with pytest.raises(TeamsRefusedError, match=NOT_ACCEPTED):
        validate_activity_token(
            parse_unverified(_compact()),
            keys=_keys(_key(use="enc")),
            verify=_verifier,
            app_id=APP_ID,
            now=NOW,
        )


def test_a_token_nobody_signed_is_refused_while_everything_else_is_perfect() -> None:
    """The authenticity half, held while every claim is correct and the clock is exactly right.

    Delete this and the verifier call can be dropped while every claim test stays green, and a
    forged token is a forged tenant: everything this module goes on to trust about who is
    asking comes out of an activity that arrived with it."""
    with pytest.raises(TeamsRefusedError, match=NOT_ACCEPTED):
        validate_activity_token(
            parse_unverified(_compact(signature=b"not the signature")),
            keys=_keys(),
            verify=_verifier,
            app_id=APP_ID,
            now=NOW,
        )


def test_the_signature_covers_the_claims_so_an_audience_cannot_be_swapped_in() -> None:
    """The claims and the signature are not two independent facts: the signature is over the
    header and payload exactly as they arrived. Asserted by keeping a genuine signature and
    replacing the payload beside it, which is what an attacker holding a captured token for
    another bot would do.

    Delete this and a verifier that ignores its `signing_input` passes everything else here,
    which is the shape of a verifier somebody stubs out during an incident and forgets."""
    genuine = _compact()
    head, _payload_segment, signature = genuine.split(".")
    swapped = f"{head}.{_b64(json.dumps(_claims(aud=OTHER_APP_ID)).encode())}.{signature}"

    with pytest.raises(TeamsRefusedError, match=NOT_ACCEPTED):
        validate_activity_token(
            parse_unverified(swapped), keys=_keys(), verify=_verifier, app_id=APP_ID, now=NOW
        )


def test_a_token_minted_for_another_bot_is_refused_by_the_audience_alone() -> None:
    """**Half of the module's subject.** Microsoft signs every bot's tokens with the same keys,
    so this token is genuinely signed, genuinely from the Bot Framework and genuinely live. The
    audience is the only thing standing between somebody else's bot and this endpoint.

    Delete this and `aud` becomes decoration, and any Bot Framework token at all is accepted
    here: the ones minted for a bot somebody registered this morning included."""
    with pytest.raises(TeamsRefusedError, match=NOT_ACCEPTED):
        validate_activity_token(
            parse_unverified(_compact(aud=OTHER_APP_ID)),
            keys=_keys(),
            verify=_verifier,
            app_id=APP_ID,
            now=NOW,
        )


def test_an_audience_that_arrives_as_a_list_is_refused_rather_than_searched() -> None:
    """The Bot Framework mints a token for one bot, so a multi-audience token is not a shape
    this credential has. Searching a list for our id is how a token issued to several parties
    is accepted by the one that happens to be named third, and it fails open where this fails
    closed."""
    with pytest.raises(TeamsRefusedError, match=NOT_ACCEPTED):
        validate_activity_token(
            parse_unverified(_compact(aud=[APP_ID, OTHER_APP_ID])),
            keys=_keys(),
            verify=_verifier,
            app_id=APP_ID,
            now=NOW,
        )


def test_a_token_from_another_issuer_is_refused_and_the_comparison_is_exact() -> None:
    """A token from a tenant's own Entra issuer is signed by keys we would not hold, but a
    look-alike issuer is not: the comparison is what refuses
    `https://api.botframework.com.example.invalid/`. Normalising trailing slashes or comparing
    hosts is how that one gets accepted by a comparison somebody wrote to be forgiving about
    configuration typos, which is `identity.oidc.validate_token`'s recorded argument."""
    for issuer in (
        "https://api.botframework.com.example.invalid",
        "https://api.botframework.com/",
        "https://sts.windows.net/1b2c3d4e/",
    ):
        with pytest.raises(TeamsRefusedError, match=NOT_ACCEPTED):
            validate_activity_token(
                parse_unverified(_compact(iss=issuer)),
                keys=_keys(),
                verify=_verifier,
                app_id=APP_ID,
                now=NOW,
            )


def test_an_expired_token_is_refused_and_the_skew_is_the_vendors_five_minutes() -> None:
    """The boundary, where an off-by-one is either a live activity refused or a captured token
    admitted. Microsoft's own allowance is five minutes, and a token stays useful for exactly
    that long past its expiry and not a moment more.

    Delete this and the comparison can drift to an hour, or drop the skew entirely and refuse
    every activity from a server whose clock is a second out, and nothing anywhere would say
    the bound had moved."""
    assert TEAMS_CLOCK_SKEW.total_seconds() == 5 * 60, "the vendor's own five minutes"
    expired = int((NOW - timedelta(hours=1)).timestamp())

    with pytest.raises(TeamsRefusedError, match=NOT_ACCEPTED):
        validate_activity_token(
            parse_unverified(_compact(exp=expired)),
            keys=_keys(),
            verify=_verifier,
            app_id=APP_ID,
            now=NOW,
        )

    edge = int((NOW - TEAMS_CLOCK_SKEW + timedelta(seconds=1)).timestamp())
    validate_activity_token(
        parse_unverified(_compact(exp=edge)),
        keys=_keys(),
        verify=_verifier,
        app_id=APP_ID,
        now=NOW,
    )


def test_a_token_that_is_not_valid_yet_is_refused_exactly_as_an_expired_one() -> None:
    """Checking only expiry is the mistake that reads as thorough. A sender choosing the times
    it signs would otherwise be accepted for as long as it liked, because nothing would look at
    the end of the window it chose to open."""
    ahead = int((NOW + TEAMS_CLOCK_SKEW + timedelta(minutes=1)).timestamp())

    with pytest.raises(TeamsRefusedError, match=NOT_ACCEPTED):
        validate_activity_token(
            parse_unverified(_compact(nbf=ahead)),
            keys=_keys(),
            verify=_verifier,
            app_id=APP_ID,
            now=NOW,
        )


@pytest.mark.parametrize("value", [None, True, "1788000000", {"at": 1788000000}])
def test_an_expiry_that_is_not_a_number_is_refused_rather_than_coerced(value: object) -> None:
    """`bool` is in this list on purpose: `isinstance(True, int)` holds in Python, so `exp:
    true` would otherwise become 1 January 1970 and read as an expired token rather than as the
    malformed one it is. A string is refused rather than coerced for the same reason, and an
    absent `exp` is refused rather than treated as a token that never expires, which is the
    default that reads as harmless."""
    with pytest.raises(TeamsRefusedError, match=NOT_ACCEPTED):
        validate_activity_token(
            parse_unverified(_compact(exp=value)),
            keys=_keys(),
            verify=_verifier,
            app_id=APP_ID,
            now=NOW,
        )


def test_a_boolean_where_a_time_belongs_is_refused_and_never_read_as_1970() -> None:
    """The half of the same rule that the parameters above cannot show. `exp: true` becomes 1
    January 1970 and is then refused as *expired*, which is the right answer for the wrong
    reason and would keep passing with the exclusion removed. `nbf: true` becomes a token that
    became valid in 1970, which is refused by nothing at all.

    Delete this and `isinstance(value, bool)` looks like a redundant line in front of an
    integer check, because on `exp` it genuinely is."""
    with pytest.raises(TeamsRefusedError, match=NOT_ACCEPTED):
        validate_activity_token(
            parse_unverified(_compact(nbf=True)),
            keys=_keys(),
            verify=_verifier,
            app_id=APP_ID,
            now=NOW,
        )


def test_a_token_carrying_no_reply_address_is_refused() -> None:
    """A token with no `serviceurl` claim cannot be bound to the activity's, so the activity's
    would be believed on its own, which is the whole of the reply-address problem below."""
    with pytest.raises(TeamsRefusedError, match=NOT_ACCEPTED):
        validate_activity_token(
            parse_unverified(_compact(serviceurl=None)),
            keys=_keys(),
            verify=_verifier,
            app_id=APP_ID,
            now=NOW,
        )


def test_every_refusal_a_sender_can_cause_says_the_same_thing() -> None:
    """ "wrong audience" and "bad signature" and "wrong tenant" tell somebody probing which
    part to change next, and the difference between the three is the whole map of what to try.
    This is `channels.webhook.WebhookRefusedError`'s rule and it does not become optional for
    being made a fourth time.

    Delete this and a diagnostic improvement gives each check its own message, which reads as a
    kindness to an operator and is a kindness to an attacker."""
    messages = set()
    cases: list[dict[str, object]] = [
        {"authorization": "Bearer not-a-jwt"},
        {"authorization": _authorization(aud=OTHER_APP_ID)},
        {"authorization": _authorization(iss="https://api.botframework.com.example.invalid")},
        {"authorization": _authorization(exp=int((NOW - timedelta(days=1)).timestamp()))},
        {"authorization": _authorization(signature=b"forged")},
        {"channel_id": "directline"},
        {"service_url": ATTACKER_URL},
        {"tenant": OTHER_TENANT},
        {"tenant": None},
    ]

    for case in cases:
        with pytest.raises(TeamsRefusedError) as caught:
            _verified(**case)  # type: ignore[arg-type]
        messages.add(str(caught.value))

    assert messages == {NOT_ACCEPTED}


# ---------------------------------------------- the activity has to be ours, over Teams
def test_an_activity_from_this_tenant_over_teams_is_accepted() -> None:
    """The positive case for the whole authenticity path, and the one that proves the eight
    refusals around it are not a function that refuses everything."""
    activity = _verified()

    assert activity.body["id"] == ACTIVITY_ID
    assert activity.service_url == SERVICE_URL


def test_our_own_bot_in_a_strangers_tenant_is_refused_by_the_tenant_alone() -> None:
    """**The other half of the module's subject, and the one the audience check cannot do.**
    This token names our app id honestly, because Microsoft minted it for us: somebody
    installed our bot in their own Teams and asked it a question. Everything else about the
    delivery is correct.

    Delete this and a company that installs this bot gets another company's answers, and there
    is nothing in the request to say anything was wrong."""
    with pytest.raises(TeamsRefusedError, match=NOT_ACCEPTED):
        _verified(tenant=OTHER_TENANT)

    with pytest.raises(TeamsRefusedError, match=NOT_ACCEPTED):
        _verified(tenant=None)


def test_a_tenant_that_differs_only_in_case_is_the_same_tenant() -> None:
    """The textual form of a GUID is case-insensitive, so two tenants cannot differ only by
    case, while a configuration typed in upper case would otherwise refuse every activity from
    the right tenant and present as the bot being broken.

    Delete this and the comparison tightens to an exact one during a tidy-up, and the outage
    arrives at whichever client wrote their tenant id in capitals."""
    activity = _verified(tenant=TENANT.upper())

    assert activity.body["id"] == ACTIVITY_ID


def test_an_activity_from_any_other_channel_on_this_registration_is_refused() -> None:
    """**One app id answers Web Chat, Direct Line and the emulator as well as Teams.** Each of
    those arrives with a valid token from the same issuer, carrying our audience, inside the
    tenant's own `channelData` if somebody put one there. None of them is behind the tenant's
    identity provider, so the sender is whoever opened a web page, and every argument this
    module makes about an Entra identity is void for them.

    Delete this and enabling Web Chat on the bot for a demo opens the company's data to the
    internet, in a console nobody in this repository can see."""
    for channel_id in ("directline", "webchat", "emulator", "msteams-sync", ""):
        with pytest.raises(TeamsRefusedError, match=NOT_ACCEPTED):
            _verified(channel_id=channel_id)


def test_a_reply_address_the_activity_chose_is_refused_against_the_signed_one() -> None:
    """**The disclosure with no permission check anywhere near it.** `serviceUrl` is where a
    reply is posted, and the copy in the body is data the sender wrote. The token carries the
    same value as a claim Microsoft signed, so comparing them is the difference between
    answering into Teams and posting a company's answer to a host the request named.

    Microsoft documents this comparison as a required validation step and it is the one people
    skip, because the integration works perfectly without it.

    Delete this and a forged activity redirects every reply, and the bot goes on reporting that
    it answered."""
    with pytest.raises(TeamsRefusedError, match=NOT_ACCEPTED):
        _verified(service_url=ATTACKER_URL)


def test_the_reply_address_carried_forward_is_the_one_that_was_signed() -> None:
    """The positive half. The checked address travels on the message so whoever wires the
    transport uses it rather than reading the body a second time; the two are equal by the time
    a `TeamsMessage` exists, and carrying the checked one is what keeps them equal after
    somebody edits the transport."""
    assert _message().service_url == SERVICE_URL


def test_the_token_is_checked_before_the_body_is_even_looked_at() -> None:
    """**The order is the property.** A parser reached before the authenticator is a parser an
    anonymous caller can run, and a parser is code. Asserted by presenting a bad token together
    with a body that is not an object at all: the refusal that comes back has to be the token's,
    because the body's refusal would prove the parse ran first.

    Delete this and the two blocks can be swapped during a tidy-up, which changes no other test
    and puts the body reader in front of the internet."""
    with pytest.raises(TeamsRefusedError) as refused:
        _verified("not an object at all", authorization="Bearer not-a-jwt")
    assert str(refused.value) == NOT_ACCEPTED

    with pytest.raises(TeamsRefusedError, match="not a Teams activity"):
        _verified("not an object at all")


def test_an_activity_cannot_be_marked_verified_by_anything_but_the_verifier() -> None:
    """The check is unskippable because the type carrying a checked body cannot be built
    without it, which is `gate.catalogue.ProjectedCatalogue`'s constructor token and the shape
    `channels.telegram.VerifiedUpdate` uses.

    Delete this and the token guard can be removed as ceremony, leaving a `verify` a caller has
    to remember to call, which is a check that goes missing from the call site somebody adds
    later."""
    with pytest.raises(TeamsRefusedError, match="only be marked verified"):
        VerifiedActivity(body={"id": ACTIVITY_ID}, service_url=SERVICE_URL)


def test_the_adapter_refuses_a_raw_mapping_and_takes_only_a_verified_activity() -> None:
    """A mapping would let a caller hand over a body nobody checked a token on, which is
    exactly the forgery this module exists to refuse. `channels.email.EmailAdapter.normalise`
    insists on an `InboundEmail` for the same reason."""
    with pytest.raises(TeamsRefusedError, match="VerifiedActivity"):
        TeamsAdapter().normalise(_activity())


# ---------------------------------------------- the sender is the directory's id
def test_the_identity_is_the_object_id_and_neither_name_beside_it_is_read() -> None:
    """**Asserted in both directions, because an absence asserted over the source text passes
    on the strength of the module's own prose.** `tests/unit/test_email.py` records two attempts
    at that and why both were wrong.

    A display name is attacker-influenced, which is why `channels.whatsapp` refuses the WhatsApp
    profile name. A user principal name is worse: it is reassignable, so a leaver's address
    handed to a new starter months later would arrive holding a binding made for the person who
    had it before.

    Delete this and the friendlier field becomes the identity, and a binding lookup answers with
    somebody else's principal."""
    renamed = _message(display_name="Somebody Else", principal_name="somebody@client.example")
    impersonator = _message(
        object_id=OTHER_OBJECT_ID,
        display_name="Priya Menon",
        principal_name="priya@client.example",
    )

    assert renamed.event.channel_identity == OBJECT_ID
    assert impersonator.event.channel_identity == OTHER_OBJECT_ID
    assert renamed.event.channel is Channel.TEAMS


def test_a_sender_with_a_name_and_no_object_id_has_nobody_it_could_be_from() -> None:
    """The half the test above cannot show. An activity carrying only the names must be refused
    rather than fall back to one, and a fallback is what somebody writes the first time a
    payload shape surprises them.

    `from.id` is not a fallback either, although it is issued by Teams rather than chosen: a
    `29:` id is scoped to one bot registration and means nothing to the directory sync that has
    to map a person to a `Principal`. A guest or an anonymous meeting participant has no object
    id, and the honest answer for somebody with no identity in the tenant is that there is
    nobody here to be."""
    with pytest.raises(TeamsRefusedError, match="has no aadObjectId"):
        _message(object_id=None)


def test_a_verified_personal_message_becomes_an_ordinary_channel_event() -> None:
    """The positive case, and the one that pins every field the gate reads. A normaliser that
    refused everything would satisfy every refusal in this file and mean no Teams message ever
    reaches the gate."""
    message = _message()

    assert message.event.channel is Channel.TEAMS
    assert message.event.channel_identity == OBJECT_ID
    assert message.event.text == "what is outstanding"
    assert message.event.received_at == NOW
    assert message.conversation == PERSONAL
    assert message.conversation_type is ConversationType.PERSONAL
    assert message.to_identity == DIGEST


def test_a_message_is_identified_by_its_conversation_and_its_activity_id_together() -> None:
    """An activity id is unique within one conversation and the vendor promises nothing about
    it across conversations, so a dedupe key built from it alone lets one message suppress
    another that happened to share it, in a different chat, from a different person.
    `channels.slack` builds its own from a conversation and a `ts` for the same reason.

    Delete this and the dedupe silently drops questions, which is the failure nobody traces
    because the evidence is a message that was never processed."""
    here = _message()
    there = _message(conversation_id=OTHER_PERSONAL)

    assert here.event.external_id == f"{PERSONAL}:{ACTIVITY_ID}"
    assert here.event.dedupe_key == (Channel.TEAMS.value, f"{PERSONAL}:{ACTIVITY_ID}")
    assert here.event.dedupe_key != there.event.dedupe_key


def test_the_time_read_is_the_vendors_and_never_the_senders_own_clock() -> None:
    """Teams sends `localTimestamp` beside `timestamp`, and it is the sender's own clock in the
    sender's own zone. Reading it would let whoever sent the activity choose when it happened,
    which is a value a dedupe window and a trace both believe.

    Every activity in this file carries a `localTimestamp` eight hours ahead, so any test that
    reads a time is also a test that this one was not the field read."""
    assert _message().event.received_at == NOW


@pytest.mark.parametrize("stamp", ["2026-09-06T12:00:00", "yesterday", "1757160000"])
def test_a_timestamp_without_an_offset_is_refused_rather_than_assumed_to_be_utc(
    stamp: str,
) -> None:
    """An assumption here is silently wrong by whatever the sending machine's offset is, and
    the symptom is a message that looks hours old, which a window somewhere then treats as
    stale. Refusing says so at the one point that can still tell."""
    with pytest.raises(TeamsRefusedError, match="timestamp"):
        _message(timestamp=stamp)


def test_a_message_with_no_text_is_not_read_as_a_blank_question() -> None:
    """A file share, a reaction or a card arrives with no `text` at all. Reading one as text
    puts an empty question through the gate, which answers it from whatever the empty string
    retrieves."""
    with pytest.raises(TeamsRefusedError, match="has no text"):
        _message(text=None)


def test_a_conversation_kind_teams_does_not_document_is_refused_rather_than_guessed() -> None:
    """How many people read this decides what may be said in it, and neither default is safe:
    treating an unknown kind as personal answers a room at one person's reach, and treating it
    as a room sends a person fixed words instead of their answer."""
    with pytest.raises(TeamsRefusedError, match="conversation type"):
        _message(conversation_type="meeting")


def test_a_message_from_another_bot_is_not_answered() -> None:
    """Two systems answering each other end at a rate limit, which is what
    `channels.email.is_automatic` refuses for the same reason.

    `role` is optional in practice, so this is not a complete guard and the module says so
    rather than implying otherwise: what actually bounds the loop is that Teams only delivers a
    channel message to a bot that was mentioned in it."""
    with pytest.raises(TeamsRefusedError, match="sent by a bot"):
        _message(role="bot")


# ---------------------------------------------- what is not a question
def test_an_adaptive_card_submission_is_refused_rather_than_read_as_a_question() -> None:
    """**The Teams-shaped version of a trap every chat surface has.** An `Action.Submit` does
    not arrive as its own activity type: it is an ordinary message activity carrying the form's
    fields in `value`, usually with no `text` at all. A normaliser that reads `text` and ignores
    the rest turns every press into a blank question through the gate.

    Two reasons to refuse it, and the second is the stronger. `gate.admission.CHANNEL_VERBS`
    gives this channel `read` alone, so a press could never be honoured as an approval, and the
    person who pressed would reasonably believe they had approved something. On top of that this
    adapter declares no `Feature.CARDS` and sends no card, so a submission addressed to it is a
    press on something this system did not send.

    Delete this and a handler is added for it, because a press with a `value` looks like an
    input waiting to be used."""
    with pytest.raises(TeamsRefusedError, match=r"Action\.Submit"):
        _message(extra={"value": {"decision": "approve"}})

    assert Feature.CARDS not in TEAMS_FEATURES
    assert "approve" not in verbs_for_channel(Channel.TEAMS)


@pytest.mark.parametrize(
    "activity_type", ["invoke", "conversationUpdate", "messageReaction", "typing"]
)
def test_only_a_message_activity_is_read(activity_type: str) -> None:
    """An `invoke` is a card action or a task module, a `conversationUpdate` is somebody being
    added to a team and carries no question at all, and a `messageReaction` is a thumb on
    something already answered. Reading any of them as a message puts something that is not a
    question through the gate, which then answers it from whatever the wrong field retrieves."""
    with pytest.raises(TeamsRefusedError, match="reads 'message' activities"):
        _message(activity_type=activity_type)


# ---------------------------------------------- an answer goes where one person reads
@pytest.mark.parametrize("kind", ["groupChat", "channel"])
def test_an_answer_is_refused_for_a_conversation_with_more_than_one_reader(kind: str) -> None:
    """**The failure this module exists to prevent, and it is invisible in a diff.** The answer
    was computed at the asker's reach; everybody else in the conversation has their own and none
    of it was consulted.

    There is deliberately no fallback that answers a smaller version of the question. Slack
    computes `channels.room.floor` over everybody present and posts at that floor, telling the
    asker more through `chat.postEphemeral`; the second half is not available here, because
    Teams has no per-viewer message. And a floor alone is not enough: a joiner is handed a
    channel's history, a group chat grows members afterwards, and a shared channel carries
    people from other tenants, so the audience crosses the very boundary the tenant pin was
    satisfied against on the way in.

    Delete this and a question asked in a channel gets a private answer posted in front of the
    team, and the message looks like every other message."""
    message = _message(conversation_id=ROOM, conversation_type=kind)

    with pytest.raises(TeamsRefusedError, match="computed at one person's reach"):
        reply_privately(message, _payload())


def test_an_answer_to_a_one_to_one_chat_carries_the_payload_the_gate_built() -> None:
    """The positive case. A planner that refused every conversation would satisfy the refusal
    above and make the channel answer nobody at all."""
    answer = reply_privately(_message(), _payload())

    assert answer.to_identity == DIGEST
    assert "INV-1" in answer.body
    assert answer.payload == _payload()


def test_the_only_plan_that_can_address_a_room_has_nowhere_to_put_an_answer() -> None:
    """**The rule is carried by the types, so it is asserted against the types.** A check is a
    thing a later branch goes around; a field that does not exist is not.

    `Notice` is the only plan that can be addressed to a conversation with more than one reader,
    and it has no payload field, so there is nothing for a value computed at one person's reach
    to travel in. `channels.whatsapp.SlotSource` leaves out a `value` field for the same reason
    and `channels.email.Reply` leaves out `cc`.

    Delete this and a payload field is added to `Notice` because a deflection looked unhelpfully
    bare."""
    assert set(Notice.__dataclass_fields__) == {"to_identity", "body"}
    assert set(Answer.__dataclass_fields__) == {"to_identity", "payload", "body"}


def test_a_notice_may_only_say_one_of_the_fixed_things_this_module_wrote() -> None:
    """The other half of the same rule. Without it `body` is a free string aimed at a room, and
    the first thing somebody interpolates into it is the asker's name, then a value out of the
    answer."""
    with pytest.raises(TeamsRefusedError, match="fixed things this module wrote"):
        Notice(to_identity=ROOM_DIGEST, body="Priya, invoice INV-1 is outstanding.")

    assert set(ALLOWED_NOTICES) == {ROOM_DEFLECTION}


def test_a_plan_names_its_destination_by_digest_and_never_by_conversation_id() -> None:
    """A raw conversation id on a plan is one interpolation away from being in a message body
    and one copy away from a table of them, which is the directory `gate.ingress.Binding`
    declines to keep.

    Delete this and the planner can pass the id straight through, and the digest becomes
    decoration."""
    with pytest.raises(TeamsRefusedError, match="as its destination"):
        Answer(to_identity=PERSONAL, payload=_payload(), body="anything")

    with pytest.raises(TeamsRefusedError, match="as its destination"):
        Notice(to_identity=ROOM, body=ROOM_DEFLECTION)


def test_a_room_is_told_the_same_thing_whoever_asked() -> None:
    """**The signature is the property.** `room_deflection` takes the message and nothing else:
    no reach, no binding, no entitlement set and no payload, so the words a room sees cannot
    depend on who asked or on whether they are bound to anybody.

    That is the DENIED-and-ABSENT rule applied where it is easiest to break. A team whose bound
    members got a different sentence from its unbound ones would publish each member's binding
    status to everybody else in the channel, one question at a time.

    Delete this and a `reach` parameter is added so the deflection can be more helpful to people
    who are not set up yet."""
    parameters = inspect.signature(room_deflection).parameters

    assert list(parameters) == ["message"]

    notice = room_deflection(_message(conversation_id=ROOM, conversation_type="channel"))

    assert notice.body == ROOM_DEFLECTION
    assert notice.to_identity == ROOM_DIGEST


def test_the_room_deflection_confirms_nothing_about_anybody() -> None:
    """Checked against `gate.ingress.LEAKING_PATTERNS`, the same rule an unrecognised prompt is
    held to, because this sentence is read by everybody in the conversation rather than by one
    person holding a handset. It also says nothing about the question, which the asker already
    put in front of the room, and nothing about there having been an answer to give.

    Delete this and the wording is improved into "I have no record of you here"."""
    for pattern in LEAKING_PATTERNS:
        assert pattern.search(ROOM_DEFLECTION) is None, ROOM_DEFLECTION
    assert "invoice" not in ROOM_DEFLECTION.lower()


def test_a_deflection_is_refused_for_a_one_to_one_chat() -> None:
    """The sibling of the room refusal. A person who asked privately gets their answer, and a
    deflection built for them is fixed words in place of one."""
    with pytest.raises(TeamsRefusedError, match="where an answer belongs"):
        room_deflection(_message())


def test_a_group_chat_is_a_room_however_teams_lists_it() -> None:
    """The member most likely to be got wrong. Teams shows a group chat in the same list as a
    one-to-one chat, and a chat with two other people in it looks personal on the way past, so
    answering it at the asker's reach puts one person's answer in front of the others."""
    assert audience_is_one_person(ConversationType.PERSONAL) is True
    assert audience_is_one_person(ConversationType.GROUP_CHAT) is False
    assert audience_is_one_person(ConversationType.TEAM_CHANNEL) is False


def test_a_message_from_another_channel_is_not_answered_over_teams() -> None:
    """The reply belongs on the surface the question came from. Delete this and a Slack question
    can be answered into a Teams chat, which is a different audience."""
    foreign = TeamsMessage(
        event=ChannelEvent(
            channel=Channel.SLACK,
            external_id="C0FINANCE:1757160000.000100",
            channel_identity="U0ABCDEFG",
            text="what is outstanding",
            received_at=NOW,
        ),
        conversation=PERSONAL,
        conversation_type=ConversationType.PERSONAL,
        service_url=SERVICE_URL,
    )

    with pytest.raises(TeamsRefusedError, match="surface the question came from"):
        reply_privately(foreign, _payload())


# ---------------------------------------------- the unrecognised sender
def test_an_unrecognised_sender_is_told_the_words_the_gate_already_wrote() -> None:
    """This module defines no prompt of its own. `gate.ingress.UNRECOGNISED_PROMPT` answers an
    unknown identity, a known but unbound one, and one whose binding was revoked this morning
    with the same words, and a second prompt written here would be a second thing to get wrong
    in the direction that confirms an account belongs to somebody."""
    reach = Unrecognised(channel=Channel.TEAMS)

    answer = unrecognised_reply(reach, _message())

    assert answer.body == reach.prompt
    assert answer.to_identity == DIGEST
    assert answer.payload == ChannelPayload()


def test_the_unrecognised_prompt_is_never_posted_into_a_room() -> None:
    """**The most interesting refusal in the file.** The prompt is carefully written not to
    confirm to the person holding the device whether their account is bound. Posting it into a
    channel announces exactly that to every member of the team, about a colleague who did
    nothing but ask a question in front of them.

    Slack answers the same problem with an ephemeral reply, which is the mechanism this surface
    does not have, so here it is a refusal rather than a quieter reply.

    Delete this and the unbound case in a channel gets the helpful reply, which is the leak the
    whole prompt exists to avoid, delivered to an audience instead of to one person."""
    message = _message(conversation_id=ROOM, conversation_type="channel")

    with pytest.raises(TeamsRefusedError, match="whether this person is bound"):
        unrecognised_reply(Unrecognised(channel=Channel.TEAMS), message)


def test_a_reach_built_for_another_channel_is_not_sent_over_teams() -> None:
    """The prompt a person is given is per channel: the widget's differs and argues at length
    why, and none of that argument transfers to a directory account."""
    with pytest.raises(TeamsRefusedError, match="per channel"):
        unrecognised_reply(Unrecognised(channel=Channel.WIDGET), _message())


# ---------------------------------------------- the conversation id, used once
def test_a_plan_cannot_be_delivered_to_a_conversation_it_was_not_planned_for() -> None:
    """Without the check the conversation id is simply a second argument, and the mistake that
    puts one person's answer somewhere else is a variable name.

    Both wrong destinations are held: another person's one-to-one chat, and a room. Neither is
    refused by anything else at the wire, because `send` deliberately re-checks no audience.

    Delete this and `deliver` becomes a two-argument function whose arguments are not required
    to agree."""
    adapter = TeamsAdapter()
    answer = reply_privately(_message(), _payload())

    with pytest.raises(TeamsRefusedError, match="planned for somewhere else"):
        deliver(adapter, answer, to_conversation_id=OTHER_PERSONAL)

    with pytest.raises(TeamsRefusedError, match="planned for somewhere else"):
        deliver(adapter, answer, to_conversation_id=ROOM)

    assert adapter.sent == [], "nothing reaches the wire when the destination disagrees"


def test_the_refusal_names_neither_the_conversation_id_nor_the_digest_it_expected() -> None:
    """Both reach a log from here, and the pair of them is the directory of Teams conversations
    joined to what each person asked. Delete this and a diagnostic improvement puts the id in
    the message."""
    answer = reply_privately(_message(), _payload())

    with pytest.raises(TeamsRefusedError) as caught:
        deliver(TeamsAdapter(), answer, to_conversation_id=OTHER_PERSONAL)

    assert OTHER_PERSONAL not in str(caught.value)
    assert DIGEST not in str(caught.value)


def test_a_delivered_message_records_the_digest_and_never_the_conversation_id() -> None:
    """The positive case, and the one showing the id is used once and not kept. A list of Teams
    conversation ids beside the answers they received is the directory `gate.ingress.Binding`
    refuses to be."""
    adapter = TeamsAdapter()
    answer = reply_privately(_message(), _payload())

    deliver(adapter, answer, to_conversation_id=PERSONAL)

    assert len(adapter.sent) == 1
    assert adapter.sent[0].to_identity == DIGEST
    assert PERSONAL not in adapter.sent[0].to_identity
    assert "INV-1" in adapter.sent[0].body


def test_a_deflection_reaches_the_room_it_was_planned_for_and_carries_nothing() -> None:
    """The positive case for the other plan. The room gets fixed words and no payload at all,
    which is the whole of what a conversation with more than one reader may be told."""
    adapter = TeamsAdapter()
    notice = room_deflection(_message(conversation_id=ROOM, conversation_type="groupChat"))

    deliver(adapter, notice, to_conversation_id=ROOM)

    assert adapter.sent == [type(adapter.sent[0])(to_identity=ROOM_DIGEST, body=ROOM_DEFLECTION)]


def test_the_adapter_cannot_be_handed_anything_but_a_payload_and_some_scalars() -> None:
    """`redaction.assert_channel_adapter` reads the signature: an adapter whose parameters are a
    `ChannelPayload` and scalars cannot serialise unredacted data, because it was never handed
    any. `deliver` exists to keep `send` that shape, so an `Answer` never appears in it."""
    assert_channel_adapter(TeamsAdapter().send)


def test_the_wire_asks_the_shared_check_rather_than_holding_its_own_opinion() -> None:
    """`adapter.assert_can_send` holds the two refusals every adapter shares, so an adapter
    that answered either of them itself would be a second opinion, and the permissive half wins
    the day the two disagree.

    **Asserted over the parsed function rather than by behaviour, because with this surface's
    own capabilities neither refusal can fire.** `can_carry_label` is true, so the label branch
    is unreachable; the ceiling is `INTERNAL`, which is also the default `highest`, so the
    classification branch is too. That is exactly why the call is worth pinning: it does
    nothing today and it is the only thing standing between a ceiling somebody lowers next
    month and a send that carries on regardless. Found by mutation: deleting the call left
    every other test in this file green.

    Parsed rather than searched, because a substring test for the name is satisfied by the
    docstring beside it, which is the trap `CLAUDE.md` records twice."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(TeamsAdapter.send)))
    shared = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "assert_can_send"
    ]

    assert len(shared) == 1, "send asks the shared check exactly once"


def test_a_body_that_dropped_the_payload_label_does_not_reach_the_wire() -> None:
    """`cards.assert_label_survives` is reused rather than restated, so this adapter and every
    other cannot disagree about labels. The check is on the produced string, which is the
    question `adapter.assert_can_send` cannot ask.

    Delete this and a caller can hand over a body it composed itself that quietly dropped "here
    is something nobody checked"."""
    adapter = TeamsAdapter()
    labelled = ChannelPayload(records=({"invoice": "INV-1"},), label=OPAQUE_LABEL)

    with pytest.raises(CardRefusedError, match="drops the payload label"):
        adapter.send(labelled, to=PERSONAL, body="Your invoice is ready.")

    assert adapter.sent == []


# ---------------------------------------------- the surface and the ceilings
def test_this_surface_declares_no_features_at_all_and_each_absence_has_a_reason() -> None:
    """Five absences with five separate reasons, and none of them is "nobody got round to it".

    `EPHEMERAL`, because Teams has no per-viewer message; Slack has one, and that difference is
    why this is a decision rather than an oversight. `CARDS`, because this channel carries `read`
    alone, so a press could never be honoured. `EDIT_IN_PLACE`, which the vendor genuinely
    supports through `updateActivity`: the feature exists in this codebase to disarm a card once
    somebody has taken the decision it offers, and this surface has no cards. `STREAMING`,
    because streaming here is one update per token against a rate limit. `ATTACHMENTS`, because
    this adapter has no path for a file in either direction.

    Delete this and `EDIT_IN_PLACE` gets declared because `updateActivity` works, which is true
    and is not the question."""
    features = TeamsAdapter().capabilities().features

    assert features == TEAMS_FEATURES
    assert set(TEAMS_FEATURES) == set()
    for feature in Feature:
        assert feature not in features


def test_the_classification_ceiling_is_internal_and_not_confidential() -> None:
    """**The closest call of any channel here, and the reason it is argued rather than
    inherited.** Lark carries `CONFIDENTIAL` because it is the tenant identity provider's own
    client, and by that test Teams qualifies: it is Entra's own client, so a Teams account is a
    directory account, which is exactly what a Slack workspace is not.

    Three things hold it down anyway. A message in Teams is retained by the tenant rather than
    by the conversation, so Purview retention, eDiscovery and compliance export put every
    one-to-one chat into a store readable by roles nobody in this system granted anything to.
    Guests and shared channels put people from other companies inside the tenant, so being
    addressable in Teams is not evidence of being staff. And a joiner is handed a channel's
    history, so the audience for a message grows after it is sent.

    Delete this and the ceiling gets raised to match Lark on the strength of both being behind
    the identity provider, which is the one thing they have in common."""
    ceiling = TeamsAdapter().capabilities().max_classification

    assert ceiling is Classification.INTERNAL
    assert ceiling.rank < Classification.CONFIDENTIAL.rank


def test_a_teams_binding_is_never_worth_more_than_bound() -> None:
    """The token proves the activity came from the Bot Framework. It says nothing at all about
    the person, whose session age and second factor are not visible here, and a binding is
    evidence about the day it was made rather than about this request.

    Raising this would mean running an actual sign-in through Teams SSO and reading the token
    that came back, which is a thing this adapter does not do.

    Delete this and a verified activity is read as evidence about who is asking, which it has
    never been."""
    assert TEAMS_ASSURANCE_CEILING is Assurance.BOUND
    assert TEAMS_ASSURANCE_CEILING < Assurance.AUTHENTICATED


def test_this_channel_carries_read_and_nothing_else() -> None:
    """The declaration `gate.admission.verbs_for_channel` requires of every channel, asserted
    here as well because this is the file where the reasons for it live. An Adaptive Card
    submission is pressable by everybody who can see the card, and nothing in an activity is
    evidence about the sender's session, so an effect authorised here would be an effect
    attributable to a bot registration.

    Delete this and the verb set can be widened in `admission.py` with nothing in the channel's
    own tests noticing."""
    assert verbs_for_channel(Channel.TEAMS) == frozenset({"read"})
    assert traffic_class_for(Channel.TEAMS) is TrafficClass.HUMAN_INTERACTIVE


def test_an_adapter_that_cannot_be_reached_reads_as_unhealthy_rather_than_absent() -> None:
    """Configured-and-unreachable and never-set-up send a person to different places, which is
    what `adapter.registered` reports apart."""
    assert TeamsAdapter().healthy(NOW) is True
    assert TeamsAdapter(reachable=False).healthy(NOW) is False
