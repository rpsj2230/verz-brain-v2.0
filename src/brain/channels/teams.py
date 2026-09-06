"""Microsoft Teams: a token Microsoft signed, a tenant that may not be ours, and a
conversation with nowhere private to put an answer.

Teams looks like Slack. Both are work chat behind a company login, both have channels and
one-to-one chats, and the four things that differ are the four things this module is about.

**What arrives is authenticated by a bearer token Microsoft minted, and not by a secret
shared with one workspace.** The Bot Framework posts an activity to an endpoint of our
choosing with `Authorization: Bearer <jwt>`, signed with a key published at Microsoft's
OpenID metadata document. That is not a signature over the body the way `channels.slack` and
`channels.webhook` have one: nothing binds the token to these bytes except the claims inside
it, so the whole of the authenticity decision is which claims are compared against what.
`validate_activity_token` is that decision and it is a pure function over keys somebody else
fetched. **Fetching them is deployment's job and is deliberately not here.** It is an HTTP
call against a document that rotates, wanting a cache, a stale window and a rate-limited
refetch on an unknown key id, and `identity.oidc.JwksCache` is that shape already. Taking a
`KeySet` as an argument is also what makes the cases worth testing testable at all.

**Two different strangers are refused by two different checks, and neither check does the
other's job.** Microsoft signs every bot's tokens with the same keys, so a token minted for
somebody else's bot verifies here perfectly; the only thing that refuses it is comparing the
`aud` claim against our own Microsoft App Id. That is what stops another tenant's *bot*
posting to us. It does nothing whatever about the second stranger, which is our own bot
installed in a tenant that is not the client's: those tokens carry our app id, because they
were genuinely minted for us. What separates them is the tenant the activity came from, which
Teams puts in `channelData.tenant.id` and which a single-tenant install has to pin. Getting
either wrong leaves a bot that answers a stranger with a company's own data, and neither
failure looks like anything from the inside. See
`THE_AUDIENCE_AND_THE_TENANT_REFUSE_DIFFERENT_STRANGERS`.

**A bot registration is reachable from every channel enabled on it, and only one of them is
Teams.** The same app id answers Web Chat, Direct Line and the emulator, and an activity from
any of those arrives with a valid token from the same issuer. None of them is behind the
tenant's identity provider, so a sender there is whoever opened a web page. Every argument
this module makes about an Entra identity is void for them, which is why `channelId` must say
`msteams` and anything else is refused rather than normalised. See
`A_BOT_REGISTRATION_IS_REACHABLE_FROM_EVERY_CHANNEL_ENABLED_ON_IT`.

**The reply address is in the token, and the copy in the body is not evidence.** An activity
carries `serviceUrl`, which is where a reply is posted, and the token carries the same value
as a signed claim. Comparing them is the difference between answering into Teams and posting
a company's data to a host the request named, which is a disclosure with no permission check
anywhere near it. Microsoft documents the comparison as a required validation step and it is
the one people skip, because the activity works without it. See
`THE_REPLY_ADDRESS_IS_SIGNED_AND_THE_BODY_IS_NOT`.

**The sender is the AAD object id and never the name or the user principal name.** A Teams
display name is set by the person in some tenants and by the directory in others, so it is at
best attacker-influenced, exactly as `channels.whatsapp` refuses the WhatsApp profile name and
`channels.email.sender_address` discards the display name. The user principal name is worse
than either: it is reassignable. A leaver's `priya@client.example` can be handed to a new
starter months later, and a binding made against it would greet them as somebody else with
that person's reach. `aadObjectId` is the directory's own identifier, it survives a rename,
and no code path in this file reads either of the other two. `from.id` is not read either: a
`29:` id is issued by Teams rather than chosen, so it is honest, but it is scoped to one bot
registration and means nothing to the directory sync that has to map a person to a
`Principal`. See `THE_OBJECT_ID_IS_ISSUED_AND_THE_PRINCIPAL_NAME_IS_REASSIGNED`.

**A conversation with more than one reader gets fixed words and no answer.** Teams has three
conversation kinds and only `personal` has one reader. Slack answers a room by computing
`channels.room.floor` over everybody present and posting at that floor, with the asker told
more through `chat.postEphemeral`; the second half of that is not available here, because
Teams has no per-viewer message. The nearest thing is an Adaptive Card refreshing to a
user-specific view, which is not one: the base card is what the conversation is posted, what a
client that does not refresh keeps showing, and what a notification quotes.

That leaves the floor on its own, and here the floor is not enough. A standard channel's
audience is the whole team and a joiner is handed the history, which is the unbounded audience
`channels.slack` holds its ceiling down for; a `groupChat` grows members after the fact; and a
shared channel carries people from *other tenants*, so the audience of a message posted into
one crosses the boundary the tenant pin exists to enforce, having satisfied the pin on the way
in. So this surface decides as `channels.telegram` decided, and the rule is carried by the
types rather than by a check, because a check is a thing a later branch goes around: `Answer`
carries a payload and is only ever built from a `personal` conversation, and `Notice`
addresses a conversation of any size and **has no payload field at all**. See
`AN_ANSWER_AT_ONE_REACH_MAY_ONLY_GO_WHERE_ONE_PERSON_READS`.

**An Adaptive Card `Action.Submit` arrives as an ordinary message activity carrying a
`value`.** Not as its own type, and usually with no `text` at all, so a normaliser that reads
`text` and ignores the rest turns a button press into a blank question. It is refused for the
reason `channels.whatsapp` declines reply buttons: `gate.admission.CHANNEL_VERBS` gives Teams
`read` alone, so no press could ever be honoured as an approval, and the person who pressed
would reasonably believe they had approved something. There is a second reason and it is the
stronger one: this adapter declares no `Feature.CARDS` and sends no card, so a submission
addressed to it is a press on something this system did not send. See
`A_PRESS_HERE_COULD_NEVER_BE_AN_APPROVAL`.

**Rejected: reusing `identity.oidc.validate_token` for the token check.** The shapes are
reused and the function is not, which is the split `channels.slack` makes when it reuses
`webhook.assert_raw_bytes` and writes its own `verify`. `KeySet`, `SigningKey`,
`SignatureVerifier`, `RawToken`, `parse_unverified` and `ALLOWED_ALGORITHMS` are the parts
with no vendor in them: a key set with unique ids, an algorithm published on the key rather
than taken from the token, and a compact JWS that is three segments and not five. What is not
reused is the validation itself, because that function is about somebody signing in. It
requires `sub` and `iat`, which are claims about a person and about a logout floor; a Bot
Framework channel token identifies the channel, carries no person at all, and the person is in
the activity body. Calling it here would refuse every genuine delivery for want of a claim
this credential has never had, and loosening it there to suit this would put two credentials'
security properties in one function.

Nothing in this file opens a socket, imports an SDK or holds a credential. The app id and the
tenant id are arguments to a pure function, the keys are handed in, and the transport lives on
the other side of `sent`, for the reason `channels.whatsapp` gives about itself: the cases
worth testing are the permission ones, and a module that owned an HTTP client could only be
tested for them against a live tenant.

Task ids: M10.5.2
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, assert_never

from brain.channels.adapter import ChannelCapabilities, Feature, assert_can_send
from brain.channels.cards import assert_label_survives, render_body
from brain.core.field_policy import Classification
from brain.core.redaction import ChannelPayload
from brain.gate.admission import Assurance
from brain.gate.context import Channel
from brain.gate.ingress import ChannelEvent, Unrecognised, identity_hash
from brain.identity.oidc import (
    ALG_NONE,
    ALLOWED_ALGORITHMS,
    KeySet,
    RawToken,
    SignatureVerifier,
    SigningKey,
    TokenRefusedError,
    parse_unverified,
)

# ------------------------------------------------------------------ written-down reasons

#: Why there are two checks against two different strangers rather than one against both.
THE_AUDIENCE_AND_THE_TENANT_REFUSE_DIFFERENT_STRANGERS: Final = (
    "Microsoft signs every bot's tokens with the same keys, so a token minted for another "
    "bot verifies here and only the audience refuses it; our own bot installed in a "
    "stranger's tenant is refused by nothing but the tenant, because those tokens name our "
    "app id honestly"
)

#: Why the channel the activity arrived over is checked at all.
A_BOT_REGISTRATION_IS_REACHABLE_FROM_EVERY_CHANNEL_ENABLED_ON_IT: Final = (
    "the same app id answers Web Chat, Direct Line and the emulator, and an activity from "
    "any of them carries a valid token from the same issuer; none of those is behind the "
    "tenant's identity provider, so a sender there is whoever opened a web page"
)

#: Why the reply address is taken from the token rather than from the activity.
THE_REPLY_ADDRESS_IS_SIGNED_AND_THE_BODY_IS_NOT: Final = (
    "serviceUrl is where a reply is posted and the body is not evidence about anything; the "
    "token carries the same value as a signed claim, and comparing them is the difference "
    "between answering into Teams and posting an answer to a host the request chose"
)

#: Why the identity is the directory's id and neither name beside it.
THE_OBJECT_ID_IS_ISSUED_AND_THE_PRINCIPAL_NAME_IS_REASSIGNED: Final = (
    "aadObjectId is the directory's own identifier and survives a rename; a display name is "
    "attacker-influenced, and a user principal name is reassignable, so a leaver's address "
    "given to a new starter hands them a binding made for somebody else"
)

#: Why nothing the gate computed goes into a conversation with more than one reader.
AN_ANSWER_AT_ONE_REACH_MAY_ONLY_GO_WHERE_ONE_PERSON_READS: Final = (
    "Teams has no per-viewer message, so there is nowhere private to say more inside a "
    "shared conversation; and a channel's audience grows after the message is posted and, "
    "in a shared channel, reaches other tenants entirely"
)

#: Why a submission is refused instead of read.
A_PRESS_HERE_COULD_NEVER_BE_AN_APPROVAL: Final = (
    "this channel carries read alone, so no press could be honoured as an approval; and "
    "this adapter sends no card, so an Action.Submit addressed to it is a press on "
    "something this system did not send rather than an input to be handled"
)

#: Why the conversation id has to agree with the plan before anything reaches the wire.
A_PLAN_IS_BOUND_TO_ONE_CONVERSATION: Final = (
    "a plan names its destination by salted digest and the conversation id is checked "
    "against it; without that the id is a second argument, and the mistake that puts one "
    "person's answer in front of a room is a variable name"
)

# ------------------------------------------------------ authenticating the delivery

#: The issuer on a token the Bot Framework mints for a channel delivery. The public cloud's;
#: a sovereign cloud publishes its own, which is why `validate_activity_token` takes this as
#: an argument rather than reading the constant. Compared exactly, never normalised: a
#: comparison written to be forgiving about a trailing slash is how
#: `https://api.botframework.com.example.invalid/` gets accepted.
BOT_FRAMEWORK_ISSUER: Final = "https://api.botframework.com"

#: The claim carrying the address a reply is posted to. Lower case in the token and camel
#: case in the activity body, which is the vendor's doing rather than a typo here; naming
#: both is how the two spellings stay visible to a reader.
SERVICE_URL_CLAIM: Final = "serviceurl"

#: The same value where the activity carries it.
SERVICE_URL_KEY: Final = "serviceUrl"

#: What `channelId` says on an activity that came from Teams.
TEAMS_CHANNEL_ID: Final = "msteams"

#: The scheme an activity's credential is presented under. Compared case-insensitively,
#: because RFC 7235 makes an auth scheme case-insensitive and a client that sends `bearer`
#: is not doing anything wrong.
BEARER_SCHEME: Final = "bearer"

#: How far out of step the token's clock may be. Microsoft's own five minutes, from their
#: guidance on validating an inbound activity. Not zero, because clocks differ; small,
#: because it bounds how long a captured token stays useful.
TEAMS_CLOCK_SKEW: Final = timedelta(minutes=5)

#: What every refusal a sender can cause says, whichever check failed.
#:
#: One message for every reason, for the argument `channels.webhook.WebhookRefusedError`
#: makes: "wrong audience" and "bad signature" and "wrong tenant" tell somebody probing which
#: part to change next, and the difference between the three is the whole map of what to try.
#: A deployment fault says something else, and that asymmetry is deliberate; see
#: `assert_configured`.
NOT_ACCEPTED: Final = "this activity was not accepted"

#: A sha256 hexdigest, the shape `gate.ingress.identity_hash` produces.
_DIGEST_RE: Final = re.compile(r"^[0-9a-f]{64}$")

#: Not data. The constructor guard, in the shape `gate.catalogue.ProjectedCatalogue` uses and
#: `channels.telegram.VerifiedUpdate` copies.
_VERIFIED_TOKEN: Final = object()


class TeamsRefusedError(Exception):
    """Raised when an activity must not be read, or an answer must not be sent.

    Not a `BrainError`, for the reason `adapter.DeliveryRefusedError` gives about itself: a
    forged delivery and an answer planned for the wrong conversation are wiring faults or
    attacks rather than outcomes of somebody's question, and degrading either into an answer
    hides them behind a shrug.
    """


def _mapping(node: object, what: str) -> Mapping[str, Any]:
    """One JSON object out of the parsed body, or a refusal.

    Defined before anything that reads a body, because `VerifiedActivity` promises a mapping
    and the check that it is one belongs on the way in rather than at each later `.get`.
    """
    if not isinstance(node, Mapping):
        msg = f"{what} is {type(node).__name__} and not an object; this is not a Teams activity"
        raise TeamsRefusedError(msg)
    return node


def _text(node: Mapping[str, Any], key: str, what: str) -> str:
    value = node.get(key)
    if not isinstance(value, str) or not value:
        msg = f"{what} has no {key}; a Teams activity without one cannot be read"
        raise TeamsRefusedError(msg)
    return value


def assert_configured(*, app_id: str, tenant_id: str, keys: KeySet) -> None:
    """Refuse a deployment that cannot decide anything, loudly and by name (M10.5.2).

    **These refusals say what is wrong, unlike every refusal a sender can cause, and the
    asymmetry is the point.** Each of them fires on every delivery including the honest ones,
    so none is a distinguisher anybody can use to learn something; and each takes the channel
    down noisily, which is what an unconfigured authenticator should do.
    `channels.telegram.assert_from_telegram` makes the same split for its minimum secret
    length.

    **There is no multi-tenant mode and `tenant_id` has no default.** This platform is
    single-tenant and client-hosted, so an install that could not name its tenant would be one
    that answers whichever tenant installed the bot, and the token would say nothing was
    wrong. A default of "any" is not offered, because the day somebody needs one is the day
    they need to think about it in a diff rather than reach for an argument.

    The clock is not checked here although it is equally a caller's mistake, because it is
    consulted in `validate_activity_token` and a check belongs where the value is used. Two
    enforcement points that are really one is worse than one, for the reason
    `channels.slack.plan_reply` gives: the next person to edit this deletes whichever of the
    two they find first.
    """
    if not app_id:
        msg = (
            "this deployment has no Microsoft App Id configured, so the audience claim could "
            "only be compared against nothing; a token minted for any other bot would verify "
            f"here. {THE_AUDIENCE_AND_THE_TENANT_REFUSE_DIFFERENT_STRANGERS}"
        )
        raise TeamsRefusedError(msg)
    if not tenant_id:
        msg = (
            "this deployment names no tenant, so an activity from any tenant that installed "
            "the bot would be answered with this company's data. "
            f"{THE_AUDIENCE_AND_THE_TENANT_REFUSE_DIFFERENT_STRANGERS}"
        )
        raise TeamsRefusedError(msg)
    if not keys.keys:
        msg = (
            "this deployment holds no Bot Framework signing keys, so nothing could be "
            "verified; fetching the OpenID metadata document is the deployment's job and a "
            "channel that cannot see a key refuses everything rather than trusting anything"
        )
        raise TeamsRefusedError(msg)


def bearer_token(authorization: str) -> str:
    """The credential out of an `Authorization` header, or a refusal (M10.5.2).

    Microsoft's first validation step is that the token arrived in this header under the
    Bearer scheme, and it is worth keeping rather than skipping to the interesting part: a
    caller that accepted a token from a query string or a body field would accept one out of
    a URL, and a URL is written to every access log between here and the sender.

    Exactly two parts. A header with more is not a bearer credential with an odd shape, it is
    something this function does not understand, and splitting on the first space alone would
    quietly accept whatever followed.
    """
    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0].lower() != BEARER_SCHEME or not parts[1]:
        raise TeamsRefusedError(NOT_ACCEPTED)
    return parts[1]


def _key_for(header: Mapping[str, object], keys: KeySet) -> SigningKey:
    """The key this token names, or a refusal. The header is hostile throughout.

    `identity.oidc.validate_token` argues each of these and this is the same argument, not a
    second one: `alg: none` is the canonical attack, an allow-list of asymmetric algorithms
    makes the RSA-public-key-as-HMAC-secret confusion unrepresentable rather than merely
    checked for, and a `kid` we do not hold is refused outright because trying the other keys
    is how a retired key, or an attacker's, gets one free attempt per key on file.

    The algorithm is checked twice, against the allow-list and against the key Microsoft
    published for that id. Taking it from the key set alone would mean a compromised or
    misconfigured metadata document could introduce a symmetric key and this client would
    follow it.
    """
    alg = header.get("alg")
    if not isinstance(alg, str) or alg in ALG_NONE or alg not in ALLOWED_ALGORITHMS:
        raise TeamsRefusedError(NOT_ACCEPTED)
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise TeamsRefusedError(NOT_ACCEPTED)
    key = keys.by_kid(kid)
    if key is None or key.algorithm != alg or key.use != "sig":
        raise TeamsRefusedError(NOT_ACCEPTED)
    return key


def _epoch_claim(payload: Mapping[str, object], name: str) -> datetime | None:
    """A numeric date claim as an aware datetime, or None when it is absent.

    `bool` is refused explicitly. `isinstance(True, int)` holds in Python, so `exp: true`
    would otherwise become 1 January 1970 and read as an *expired* token rather than as the
    malformed one it is. A string is refused rather than coerced for the same reason: a
    coercion that turns `"0"` into zero makes a forged token look merely stale.
    """
    value = payload.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TeamsRefusedError(NOT_ACCEPTED)
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (ValueError, OverflowError, OSError) as exc:
        raise TeamsRefusedError(NOT_ACCEPTED) from exc


@dataclass(frozen=True)
class ValidatedToken:
    """A token whose signature, issuer, audience and lifetime all checked out.

    Constructed by `validate_activity_token` and by nothing else. It carries `key_id` and
    `verified_at` for the reason `identity.oidc.VerifiedClaims` carries its own: a claim about
    a token should say what made it believable, because an audit entry reading "verified by
    kid abc123 at 09:14" can be argued with and "trusted" cannot.

    `service_url` is the signed copy. Everything downstream that needs a reply address takes
    it from here rather than from the activity, so there is one address in play and it is the
    one Microsoft signed. See `THE_REPLY_ADDRESS_IS_SIGNED_AND_THE_BODY_IS_NOT`.
    """

    issuer: str
    audience: str
    service_url: str
    key_id: str
    expires_at: datetime
    verified_at: datetime


def validate_activity_token(
    token: RawToken,
    *,
    keys: KeySet,
    verify: SignatureVerifier,
    app_id: str,
    now: datetime,
    expected_issuer: str = BOT_FRAMEWORK_ISSUER,
    skew: timedelta = TEAMS_CLOCK_SKEW,
) -> ValidatedToken:
    """Refuse anything that is not a live token Microsoft minted for this bot (M10.5.2).

    The order is the argument, and it is `identity.oidc.validate_token`'s order for the same
    reason. The header is read first because it selects a key, and it is treated as hostile:
    `alg` is checked against a list we own rather than used to choose an implementation. The
    signature is verified before a single claim is read, so nothing this function decides is
    derived from unverified input.

    **`aud` is required to be one string equal to the app id.** A list is refused rather than
    searched. The Bot Framework mints a token for one bot, so a multi-audience token is not a
    shape this credential has, and a search through a list is how a token issued for several
    parties gets accepted by the one that happens to be named third. See
    `THE_AUDIENCE_AND_THE_TENANT_REFUSE_DIFFERENT_STRANGERS`.

    **Expiry is checked in both directions.** A token from the future is as wrong as one from
    the past, and `nbf` is what says so; checking only `exp` is the mistake that reads as
    thorough, because a sender choosing the times it signs would be accepted for as long as it
    liked.

    Raises rather than returning a bool or an outcome. A function returning False is one whose
    result can be ignored by writing it on a line of its own, and that line reads as a check.

    A naive `now` is refused before anything else and says so by name, for the reason
    `channels.slack.verify` gives about its own: it is not something a sender can cause, and
    the failure it produces is every activity refused on any machine that is not set to UTC,
    which presents as Teams being broken rather than as a bug in one argument. Without it the
    comparison below raises `TypeError` instead, which arrives as a 500.
    """
    if now.tzinfo is None:
        msg = (
            "the token clock needs an aware time; a naive one is compared against an epoch "
            "read in the server's own zone, so expiry silently becomes that offset wide"
        )
        raise TeamsRefusedError(msg)

    key = _key_for(token.header, keys)
    if not verify(signing_input=token.signing_input, signature=token.signature, key=key):
        raise TeamsRefusedError(NOT_ACCEPTED)

    # --- below this line the payload is evidence rather than input ------------------------
    issuer = token.payload.get("iss")
    if not isinstance(issuer, str) or issuer != expected_issuer:
        raise TeamsRefusedError(NOT_ACCEPTED)

    audience = token.payload.get("aud")
    if not isinstance(audience, str) or audience != app_id:
        raise TeamsRefusedError(NOT_ACCEPTED)

    expires_at = _epoch_claim(token.payload, "exp")
    if expires_at is None or now - skew >= expires_at:
        raise TeamsRefusedError(NOT_ACCEPTED)

    not_before = _epoch_claim(token.payload, "nbf")
    if not_before is not None and now + skew < not_before:
        raise TeamsRefusedError(NOT_ACCEPTED)

    service_url = token.payload.get(SERVICE_URL_CLAIM)
    if not isinstance(service_url, str) or not service_url:
        # A token with no reply address cannot be bound to the activity's, so the activity's
        # would be believed on its own. See `THE_REPLY_ADDRESS_IS_SIGNED_AND_THE_BODY_IS_NOT`.
        raise TeamsRefusedError(NOT_ACCEPTED)

    return ValidatedToken(
        issuer=issuer,
        audience=audience,
        service_url=service_url,
        key_id=key.kid,
        expires_at=expires_at,
        verified_at=now,
    )


@dataclass(frozen=True)
class VerifiedActivity:
    """One activity body that came from Microsoft, over Teams, from the tenant we serve.

    **This type cannot be constructed outside `verified_activity`**, and that is the whole
    point of it. The alternative is a `verify` a caller has to remember to call before
    parsing, which is a check that goes missing from the one call site somebody adds later,
    and the missing one is reachable by anybody who can reach the endpoint.
    `channels.telegram.VerifiedUpdate` argues the same and
    `gate.catalogue.ProjectedCatalogue` is where the token pattern comes from.

    It carries the signed `service_url` and deliberately not the token, the app id or the
    tenant. Keeping any of those on the object is how one of them reaches a log line
    describing the activity; the tenant in particular is by construction the pinned one, and a
    field that can only ever hold one value is a field somebody eventually sets to another.
    """

    body: Mapping[str, Any]
    #: The reply address, from the token rather than from the body.
    service_url: str
    #: Not data. See the class docstring.
    token: object = None

    def __post_init__(self) -> None:
        if self.token is not _VERIFIED_TOKEN:
            msg = (
                "an activity may only be marked verified by brain.channels.teams."
                "verified_activity; a body constructed as verified elsewhere is a body "
                "nobody checked a token on"
            )
            raise TeamsRefusedError(msg)


def verified_activity(
    raw: object,
    *,
    authorization: str,
    keys: KeySet,
    verify: SignatureVerifier,
    app_id: str,
    tenant_id: str,
    now: datetime,
    expected_issuer: str = BOT_FRAMEWORK_ISSUER,
    skew: timedelta = TEAMS_CLOCK_SKEW,
) -> VerifiedActivity:
    """The only way to get an activity body into the rest of this module (M10.5.2).

    Five refusals in one function, and the order matters. The configuration is checked first,
    so an install that cannot decide anything says so rather than refusing every delivery as
    though it were an attack. Then the token, before the body is so much as type-checked: a
    parser reached before the authenticator is a parser an anonymous caller can run.

    A JWT has to be split and decoded before it can be checked, which is a parse an
    unauthenticated caller reaches and there is no arrangement of this that avoids it. What
    can be avoided is reading a *claim* before the signature, and `validate_activity_token`
    does not. `identity.oidc.parse_unverified` does the splitting rather than a second
    splitter written here, because five segments is a JWE and a hand-written split is always
    shorter than this one because it leaves the segment count out. Its refusals are translated
    on the way past: that module's vocabulary is about somebody signing in to the console, its
    public message is a sign-in prompt, and its reasons are specific where this surface says
    one sentence.

    Then the three questions about the body that the token has now made answerable: did this
    come over Teams, does the reply address match the one that was signed, and is this the
    tenant we serve.
    """
    assert_configured(app_id=app_id, tenant_id=tenant_id, keys=keys)
    try:
        token = parse_unverified(bearer_token(authorization))
    except TokenRefusedError as exc:
        raise TeamsRefusedError(NOT_ACCEPTED) from exc
    validated = validate_activity_token(
        token,
        keys=keys,
        verify=verify,
        app_id=app_id,
        now=now,
        expected_issuer=expected_issuer,
        skew=skew,
    )

    body = _mapping(raw, "the activity")
    if body.get("channelId") != TEAMS_CHANNEL_ID:
        raise TeamsRefusedError(NOT_ACCEPTED)
    if body.get(SERVICE_URL_KEY) != validated.service_url:
        raise TeamsRefusedError(NOT_ACCEPTED)
    if not tenant_matches(body, tenant_id):
        raise TeamsRefusedError(NOT_ACCEPTED)
    return VerifiedActivity(body=body, service_url=validated.service_url, token=_VERIFIED_TOKEN)


def tenant_matches(body: Mapping[str, Any], tenant_id: str) -> bool:
    """Whether this activity came from the tenant this install serves (M10.5.2).

    The tenant is at `channelData.tenant.id` and an activity without one is not from Teams
    at all, so an absent tenant answers False rather than raising: it is refused by the same
    sentence as a stranger's, and telling the two apart is the map of what to try next.

    Compared case-insensitively, which is correct rather than lax: the textual form of a GUID
    is case-insensitive, so two tenants cannot differ only by case, while a configuration
    typed in upper case would otherwise refuse every activity from the right tenant. It is
    `str.casefold` rather than `str.lower` because that is what `gate.ingress.identity_hash`
    uses, and two spellings of "the same string" in one codebase is one too many.
    """
    channel_data = body.get("channelData")
    if not isinstance(channel_data, Mapping):
        return False
    tenant = channel_data.get("tenant")
    if not isinstance(tenant, Mapping):
        return False
    found = tenant.get("id")
    return isinstance(found, str) and found.casefold() == tenant_id.casefold()


# --------------------------------------------------------- who is in the conversation


class ConversationType(enum.StrEnum):
    """Teams' own three words for a conversation. Closed, and checked closed.

    The values are the vendor's, so the wire shape needs no translation table that could
    drift, exactly as `channels.lark.ChatType` and `channels.slack.Surface` argue. The member
    *name* for a channel is ours: `TEAM_CHANNEL` rather than `CHANNEL`, because
    `ConversationType.CHANNEL` sitting beside `gate.context.Channel` in one file is a
    confusion waiting for a tired reader. `channels.telegram.ChatKind.BROADCAST` renames the
    same collision.
    """

    #: A one-to-one chat with the bot. The only kind where the asker is the whole audience.
    PERSONAL = "personal"
    #: A group chat. More than one reader, and members can be added afterwards.
    GROUP_CHAT = "groupChat"
    #: A channel in a team. The audience is the team, and a shared channel reaches other
    #: tenants entirely.
    TEAM_CHANNEL = "channel"


def audience_is_one_person(conversation: ConversationType) -> bool:
    """Whether the only reader is the person who asked.

    The declaration every conversation kind has to make, in the shape
    `gate.context.traffic_class_for` makes it. A dictionary with a default would accept a new
    kind silently, and neither default is safe: `True` answers a room at one person's reach,
    and `False` sends a person fixed words instead of their answer.

    A `groupChat` answers False and it is the member most likely to be got wrong, because
    Teams presents it in the same list as a one-to-one chat and a chat with two other people
    in it looks personal on the way past.
    """
    match conversation:
        case ConversationType.PERSONAL:
            return True
        case ConversationType.GROUP_CHAT | ConversationType.TEAM_CHANNEL:
            return False
        case _:
            assert_never(conversation)


# ------------------------------------------------------------- reading what arrived

#: The one activity type this normaliser reads.
MESSAGE_ACTIVITY: Final = "message"

#: What an Adaptive Card `Action.Execute` arrives as, and what a Teams task module posts.
#: Named so the refusal can be specific about it.
INVOKE_ACTIVITY: Final = "invoke"

#: The field an `Action.Submit` puts its form data in. It arrives on an ordinary message
#: activity, which is why its absence is not something a type check would notice.
SUBMIT_VALUE_KEY: Final = "value"

#: The directory's identifier for the sender, and the only identity key read in this file.
OBJECT_ID_KEY: Final = "aadObjectId"

#: What `from.role` says on an activity another bot sent.
BOT_ROLE: Final = "bot"


def _received_at(activity: Mapping[str, Any]) -> datetime:
    """The activity's `timestamp`, which is an ISO 8601 instant in UTC.

    Converted here rather than passed through, because `ChannelEvent.received_at` is a
    datetime for every channel and a channel handing over a string would make every
    downstream comparison a per-channel special case. WhatsApp's is whole seconds as a
    string, Telegram's is an integer and Lark's is milliseconds; the difference living in each
    normaliser is the point of one internal shape.

    **`localTimestamp` is deliberately not read.** Teams sends it beside this one and it is
    the sender's own clock in the sender's own zone, so reading it would let whoever sent the
    activity choose when it happened, which is a value a dedupe window and a trace both
    believe.

    A timestamp with no offset is refused rather than assumed to be UTC. An assumption here
    is silently wrong by the server's own offset, and the symptom is a message that looks
    hours old.
    """
    raw = _text(activity, "timestamp", "the activity")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        msg = f"activity timestamp {raw!r} is not an ISO 8601 instant"
        raise TeamsRefusedError(msg) from exc
    if parsed.tzinfo is None:
        msg = (
            f"activity timestamp {raw!r} carries no offset; reading it as UTC would be "
            "silently wrong by whatever the sender's clock is set to"
        )
        raise TeamsRefusedError(msg)
    return parsed


@dataclass(frozen=True)
class TeamsMessage:
    """One inbound message, normalised, with the Teams-shaped facts the gate does not carry.

    `event` is the shape every other channel produces, so everything downstream reads one
    type. Beside it sit the conversation this arrived in, how many people can read it, and the
    address a reply is posted to.

    `service_url` is carried rather than looked up again, so that whoever wires the transport
    uses the address Microsoft signed instead of reading the body a second time. By the time
    a `TeamsMessage` exists the two are known to be equal; carrying the checked one is what
    keeps them equal after somebody edits the transport.
    """

    event: ChannelEvent
    conversation: str
    conversation_type: ConversationType
    service_url: str

    @property
    def to_identity(self) -> str:
        """The salted digest of the conversation this arrived in.

        Derived rather than stored, so a plan cannot name a destination the message beside it
        disagrees with.
        """
        return identity_hash(Channel.TEAMS, self.conversation)


def normalise_activity(activity: VerifiedActivity) -> TeamsMessage:
    """One verified activity as the shape the gate reads, or a refusal (M10.5.2).

    Takes a `VerifiedActivity` and there is deliberately no overload taking a mapping. The
    token check is the whole security of this channel and a normaliser that could be handed a
    raw body is one that will be.

    **A message carrying a `value` is refused.** That is what an Adaptive Card `Action.Submit`
    posts: an ordinary message activity, usually with no `text` at all, carrying the form's
    fields. A normaliser that read `text` and ignored the rest would put a blank question
    through the gate on every press. See `A_PRESS_HERE_COULD_NEVER_BE_AN_APPROVAL`.

    **The sender is `from.aadObjectId`.** See
    `THE_OBJECT_ID_IS_ISSUED_AND_THE_PRINCIPAL_NAME_IS_REASSIGNED`. There is no code path here
    that reads `name`, `userPrincipalName` or `from.id`, and an activity without an object id
    is refused rather than falling back to any of them: a guest or an anonymous meeting
    participant has no directory identity, and the honest answer for somebody with no identity
    in the tenant is that there is nobody here to be.

    `external_id` is the conversation and the activity id together. An activity id is unique
    within one conversation and the vendor promises nothing about it across conversations, so
    keying the dedupe on it alone would let one message suppress another that happened to
    share it. `channels.slack` builds its own from a conversation and a `ts` for the same
    reason.

    A sender whose `role` says `bot` is refused, for the reason `channels.email.is_automatic`
    refuses machine-generated mail: two systems answering each other end at a rate limit. The
    field is optional in practice, so this is not a complete guard and is not presented as
    one. What actually bounds the loop is that Teams only delivers a channel message to a bot
    that was mentioned in it.
    """
    body = activity.body
    kind = _text(body, "type", "the activity")
    if kind != MESSAGE_ACTIVITY:
        msg = (
            f"this normaliser reads {MESSAGE_ACTIVITY!r} activities and this one is {kind!r}; "
            f"a {INVOKE_ACTIVITY!r} is a card action and a conversationUpdate carries no "
            "question at all, so reading either as a message is a blank question"
        )
        raise TeamsRefusedError(msg)
    if SUBMIT_VALUE_KEY in body:
        msg = (
            f"this message carries {SUBMIT_VALUE_KEY!r}, which is what an Adaptive Card "
            f"Action.Submit posts. {A_PRESS_HERE_COULD_NEVER_BE_AN_APPROVAL}"
        )
        raise TeamsRefusedError(msg)

    sender = _mapping(body.get("from"), "the sender")
    if sender.get("role") == BOT_ROLE:
        msg = (
            "this message was sent by a bot, and answering software invites an answer back; "
            "two systems answering each other end at a rate limit"
        )
        raise TeamsRefusedError(msg)
    object_id = _text(sender, OBJECT_ID_KEY, "the sender")

    conversation = _mapping(body.get("conversation"), "the conversation")
    conversation_id = _text(conversation, "id", "the conversation")
    raw_type = _text(conversation, "conversationType", "the conversation")
    try:
        conversation_type = ConversationType(raw_type)
    except ValueError as exc:
        msg = (
            f"conversation type {raw_type!r} is none of "
            f"{tuple(c.value for c in ConversationType)}; how many people read this decides "
            "what may be said in it, and there is no safe guess"
        )
        raise TeamsRefusedError(msg) from exc

    activity_id = _text(body, "id", "the activity")
    return TeamsMessage(
        event=ChannelEvent(
            channel=Channel.TEAMS,
            external_id=f"{conversation_id}:{activity_id}",
            channel_identity=object_id,
            text=_text(body, "text", "the activity"),
            received_at=_received_at(body),
        ),
        conversation=conversation_id,
        conversation_type=conversation_type,
        service_url=activity.service_url,
    )


# ---------------------------------------------------------------- planning a reply

#: What a conversation with more than one reader is told instead of an answer. Fixed words,
#: with nothing interpolated into them: no name, no echo of the question and no hint that
#: there was an answer to give.
#:
#: It asks the person to open a chat rather than promising to open one for them. A bot can
#: message somebody first in Teams, but only by creating a conversation, which is a write this
#: adapter does not do; promising it here would be an instruction this module cannot carry
#: out, and doing it would make the answer to a question a side effect somewhere else.
ROOM_DEFLECTION: Final = (
    "I answer in a one-to-one chat rather than in a shared conversation. Open a chat with me "
    "and send the same question there."
)

#: Everything a `Notice` is allowed to say. An allowlist rather than a free string, because a
#: free string field aimed at a room is one somebody eventually interpolates the asker's name
#: into, and then a value out of the answer.
ALLOWED_NOTICES: Final[frozenset[str]] = frozenset({ROOM_DEFLECTION})

#: The most a Teams binding is ever worth, whatever the token said. Equal to `Assurance.BOUND`
#: and stated here so the ceiling is visible in this file rather than inferred from nothing
#: raising it.
#:
#: This is the closest call of any channel. Teams is behind the tenant's identity provider,
#: so the person genuinely is signed in as themselves, which Slack cannot say. What is missing
#: is evidence about *this* request: the token is about the Bot Framework rather than about
#: the person, nothing here sees the age of their session or whether a second factor was
#: presented, and a binding is evidence about the day it was made. Raising this would mean
#: running an actual sign-in through Teams SSO and reading the token that came back, which is
#: a thing this adapter does not do.
TEAMS_ASSURANCE_CEILING: Final = Assurance.BOUND

#: What this surface can do, which is send text to one person. Five absences with five
#: separate reasons, and none of them is "nobody got round to it":
#:
#: `EPHEMERAL`, because Teams has no per-viewer message. Slack has one and that difference is
#: why this is a decision rather than an oversight; a card refreshing to a user-specific view
#: is not one, because the base card is what the conversation is posted.
#: `CARDS`, because `gate.admission.CHANNEL_VERBS` gives this channel read alone, so a press
#: could never be honoured; `channels.whatsapp` declines reply buttons on the same ground.
#: `EDIT_IN_PLACE`, which the vendor genuinely supports through `updateActivity`; the feature
#: exists in this codebase to disarm a card once the decision it offers has been taken, and
#: this surface has no cards, so declaring it would tell a caller about a path that is not
#: here. `channels.telegram.EDITING_A_MESSAGE_HERE_HAS_NOTHING_TO_CLOSE` is the same absence.
#: `STREAMING`, because streaming here is one update per token against a rate limit, which is
#: the argument `channels.lark.LARK_FEATURES` makes about spending a budget on cosmetics.
#: `ATTACHMENTS`, because this adapter has no path for a file in either direction, and a
#: declared capability is read by callers deciding what to do rather than as trivia.
TEAMS_FEATURES: Final[frozenset[Feature]] = frozenset()


@dataclass(frozen=True)
class Answer:
    """One reply, carrying what the gate computed, addressed to one conversation.

    An `Answer` is by construction for a one-to-one chat, because the only functions that
    build one refuse a message that arrived anywhere else.

    **`to_identity` is the digest of the conversation and not of the person**, which is where
    this differs from `channels.telegram.Answer` and from `channels.slack.Posting`. Telegram
    gets both from one number because a private chat's id is the user's id; Teams does not,
    and a Teams conversation id is what actually addresses the wire. A digest of something the
    transport does not use would be a check that passes while the address is wrong, which is
    the failure the check exists to catch.

    It is a digest and never the id, for the reason `gate.ingress.Binding` stores one: a list
    of Teams conversation ids beside the answers they received is a directory of the company
    joined to what each person asked.
    """

    to_identity: str
    payload: ChannelPayload
    body: str

    def __post_init__(self) -> None:
        if not _DIGEST_RE.match(self.to_identity):
            msg = (
                f"an answer names {self.to_identity!r} as its destination. "
                f"{A_PLAN_IS_BOUND_TO_ONE_CONVERSATION}"
            )
            raise TeamsRefusedError(msg)
        if not self.body:
            msg = "an empty message reads as the system being broken rather than as an answer"
            raise TeamsRefusedError(msg)


@dataclass(frozen=True)
class Notice:
    """Fixed words into a conversation that may have any number of readers.

    **There is deliberately no payload field and no free body.** This is the only type in this
    module that can address a room, so it is the only one where a value computed at one
    person's reach could reach people whose reach nobody asked about. A field to put one in
    does not exist, and `body` is checked against `ALLOWED_NOTICES`, so what a room can be told
    is the fixed set of things this module wrote. `channels.whatsapp.SlotSource` leaves out a
    `value` field for the same reason and `channels.email.Reply` leaves out `cc`.

    See `AN_ANSWER_AT_ONE_REACH_MAY_ONLY_GO_WHERE_ONE_PERSON_READS`.
    """

    to_identity: str
    body: str

    def __post_init__(self) -> None:
        if not _DIGEST_RE.match(self.to_identity):
            msg = (
                f"a notice names {self.to_identity!r} as its destination. "
                f"{A_PLAN_IS_BOUND_TO_ONE_CONVERSATION}"
            )
            raise TeamsRefusedError(msg)
        if self.body not in ALLOWED_NOTICES:
            msg = (
                "a notice may only say one of the fixed things this module wrote. "
                f"{AN_ANSWER_AT_ONE_REACH_MAY_ONLY_GO_WHERE_ONE_PERSON_READS}"
            )
            raise TeamsRefusedError(msg)


def _assert_teams(message: TeamsMessage) -> None:
    if message.event.channel is not Channel.TEAMS:
        msg = (
            f"this message arrived over {message.event.channel} and would be answered over "
            f"{Channel.TEAMS}; the reply belongs on the surface the question came from"
        )
        raise TeamsRefusedError(msg)


def reply_privately(message: TeamsMessage, payload: ChannelPayload) -> Answer:
    """Plan a reply to one person, in the one-to-one chat they asked from (M10.5.2).

    Refuses a message that arrived in a group chat or a channel. There is no fallback that
    quietly answers a smaller version of the question: the smaller version would be computed
    at `channels.room.floor` over everybody present, and this surface cannot deliver the other
    half of that arrangement, which is telling the asker more privately. See
    `AN_ANSWER_AT_ONE_REACH_MAY_ONLY_GO_WHERE_ONE_PERSON_READS`. `room_deflection` is what a
    room gets, and it carries nothing.

    Takes the message rather than a conversation id, so the destination is where the question
    came from and cannot be somewhere a caller passed in. The only way to address a different
    conversation is to answer a different message, which is the shape `channels.email.reply_to`
    and `channels.telegram.reply_privately` both have for the same reason.

    The body comes from `cards.render_body`, which every channel shares, so a Teams message and
    a Lark message cannot disagree about what a payload says or about carrying its label.
    """
    _assert_teams(message)
    if not audience_is_one_person(message.conversation_type):
        msg = (
            f"this message arrived in a {message.conversation_type} conversation and the "
            f"answer was computed at one person's reach. "
            f"{AN_ANSWER_AT_ONE_REACH_MAY_ONLY_GO_WHERE_ONE_PERSON_READS}"
        )
        raise TeamsRefusedError(msg)
    return Answer(
        to_identity=message.to_identity,
        payload=payload,
        body=render_body(payload),
    )


def room_deflection(message: TeamsMessage) -> Notice:
    """What a conversation with more than one reader is told (M10.5.2).

    **This function takes the message and nothing else, and the signature is the property.**
    There is no reach, no binding, no entitlement set and no payload to pass, so the words a
    room sees cannot depend on who asked or on whether they are bound to anybody. That is the
    DENIED-and-ABSENT rule applied where it is easiest to break: a room whose bound members got
    a different sentence from its unbound ones would publish each member's binding status to
    everybody else in it, one question at a time.

    Refuses a one-to-one chat. A person who asked privately gets their answer, and a deflection
    built for them would be fixed words in place of one.
    """
    _assert_teams(message)
    if audience_is_one_person(message.conversation_type):
        msg = (
            "this message arrived in a one-to-one chat, which is where an answer belongs; a "
            "deflection here would send fixed words in place of the answer"
        )
        raise TeamsRefusedError(msg)
    return Notice(to_identity=message.to_identity, body=ROOM_DEFLECTION)


def unrecognised_reply(reach: Unrecognised, message: TeamsMessage) -> Answer:
    """What a sender with no binding is told, in the words `gate.ingress` already wrote.

    **This module defines no prompt of its own**, for the reason `channels.whatsapp` and
    `channels.email` both give: `UNRECOGNISED_PROMPT` answers an unknown identity, a known but
    unbound one, and one whose binding was revoked this morning with the same words, and a
    second prompt written here is a second thing to get wrong in the direction that confirms an
    account belongs to somebody.

    **Refused outside a one-to-one chat, and that refusal is the interesting one.** The prompt
    is careful not to confirm whether an account is bound. Posting it into a channel announces
    exactly that to every member of the team, about a colleague who did nothing but ask a
    question in front of them. `channels.slack` answers the same problem with an ephemeral
    reply, which is not available here; `room_deflection` is what a room gets, and it says the
    same thing to everybody.
    """
    _assert_teams(message)
    if reach.channel is not Channel.TEAMS:
        msg = (
            f"this reach was built for {reach.channel} and would be sent over {Channel.TEAMS}; "
            "the prompt a person is given is per channel"
        )
        raise TeamsRefusedError(msg)
    if not audience_is_one_person(message.conversation_type):
        msg = (
            f"this message arrived in a {message.conversation_type} conversation, and the "
            "prompt for an unbound sender posted there tells every member whether this person "
            "is bound"
        )
        raise TeamsRefusedError(msg)
    return Answer(
        to_identity=message.to_identity,
        payload=ChannelPayload(),
        body=reach.prompt,
    )


# ------------------------------------------------------------------------- the adapter


@dataclass(frozen=True)
class SentMessage:
    """One message this adapter delivered. What a test reads instead of a Teams tenant.

    `to_identity` is the digest and not the conversation id it was addressed with. The id is
    needed once, to reach the wire, and a list of them kept beside the messages they received
    is the directory `gate.ingress.Binding` declines to be.
    """

    to_identity: str
    body: str


@dataclass
class TeamsAdapter:
    """The Teams surface, with the transport left out on purpose.

    No HTTP client, no SDK, no app password and no token. The vendor's `sendToConversation`
    belongs on the other side of `sent`, and keeping it there is what makes the case that
    matters testable: an answer reaching a conversation it was not computed for is a bug in
    the planning, and a module that opened a socket could only be tested for it against a live
    tenant.

    `reachable` is what `healthy` answers, for the reason `adapter.ChannelAdapter.healthy`
    gives: configured-and-unreachable and never-set-up send a person to different places.
    """

    sent: list[SentMessage] = field(default_factory=list)
    reachable: bool = True

    def capabilities(self) -> ChannelCapabilities:
        """What this surface may carry, declared rather than inferred.

        `INTERNAL` and not `CONFIDENTIAL`, and this is the closest call of any channel here.
        Lark carries `CONFIDENTIAL` because it is the tenant identity provider's own client,
        and by that test Teams qualifies: it is Entra's own client, so a Teams account is a
        directory account, which is exactly what a Slack workspace is not. Three things hold
        it down anyway.

        A message in Teams is retained by the tenant rather than by the conversation. Purview
        retention, eDiscovery and compliance export put every one-to-one chat into a
        tenant-wide store readable by roles nobody in this system granted anything to, and
        that applies to the private answers this adapter sends, not only to what is posted in
        a channel.

        A tenant is not a staff list. Guest accounts and shared channels put people from other
        companies inside the same tenant, so being addressable in Teams is not evidence of
        being staff the way holding a Lark account is.

        And a channel's audience outlives the message: a joiner is handed the history, so the
        audience for something posted today includes people who were not present when it was
        sent, which is the argument `channels.slack` makes about a public channel.

        Raising this is a decision somebody makes deliberately, not one that arrives by a
        constant being edited to make a send work.

        `can_carry_label` is true. A Teams message is text with no template to fall out of, so
        the label `cards.render_body` puts at the top renders wherever the body does.
        """
        return ChannelCapabilities(
            channel=Channel.TEAMS,
            features=TEAMS_FEATURES,
            max_classification=Classification.INTERNAL,
            can_carry_label=True,
        )

    def normalise(self, raw: object) -> ChannelEvent:
        """One inbound message, as the shape the gate reads.

        Takes a `VerifiedActivity` and refuses anything else, rather than accepting the mapping
        Teams posts. A mapping would let a caller hand over a body nobody checked a token on,
        which is the forgery this module exists to refuse, and it is the same reason
        `channels.email.EmailAdapter.normalise` insists on an `InboundEmail` instead of reading
        a verdict out of a dict.

        Returns the event alone, which is what the protocol promises. Anything that needs to
        know how many people read the conversation calls `normalise_activity` and gets the
        whole thing.
        """
        if not isinstance(raw, VerifiedActivity):
            msg = (
                f"this adapter normalises a VerifiedActivity and was handed a "
                f"{type(raw).__name__}; the bearer token has to have been checked first. "
                f"{THE_AUDIENCE_AND_THE_TENANT_REFUSE_DIFFERENT_STRANGERS}"
            )
            raise TeamsRefusedError(msg)
        return normalise_activity(raw).event

    def send(self, payload: ChannelPayload, *, to: str, body: str = "") -> None:
        """Put one message into one conversation (M10.5.2).

        `body` empty means render the payload. Whichever it is, the produced string is checked
        against the payload's label, so a caller cannot hand over a body that dropped it.

        `assert_can_send` runs first and is not restated here, so this adapter cannot disagree
        with any other about labels and classifications. Nothing here re-checks the audience:
        that is decided when a plan is built, by which of `Answer` and `Notice` it is, and a
        second enforcement point at the wire is one that the next person to edit this deletes
        whichever of the two they find first.
        """
        assert_can_send(self.capabilities(), payload)
        rendered = body or render_body(payload)
        assert_label_survives(rendered, payload)
        self.sent.append(SentMessage(to_identity=identity_hash(Channel.TEAMS, to), body=rendered))

    def healthy(self, now: datetime) -> bool:
        """Whether this adapter can currently deliver. See `adapter.registered`."""
        del now  # No time-based health here; the parameter is the protocol's.
        return self.reachable


def deliver(adapter: TeamsAdapter, plan: Answer | Notice, *, to_conversation_id: str) -> None:
    """Send one planned message, to the conversation it was planned for (M10.5.2).

    The conversation id arrives here and nowhere else. A plan holds a digest, so whoever
    resolved the destination supplies the address at the wire and this checks that the two
    agree. See `A_PLAN_IS_BOUND_TO_ONE_CONVERSATION`: without the check the id is simply a
    second argument, and handing this function the wrong one is a mistake that looks like a
    variable name.

    What this proves is that the caller and the planner agree about where an answer is going.
    It does not prove that a given conversation has one reader, which is decided when the plan
    is built and carried by which type it is, and it does not prove a conversation belongs to
    the person the gate computed for, which is Teams' own mapping and not something this module
    can see. `channels.telegram.deliver` gets the second of those free from a vendor fact about
    id spaces; there is no such fact here, and claiming one would be a comment that is wrong
    rather than a check that is missing.

    `identity_hash` casefolds, so two conversation ids differing only in case produce one
    digest. Teams ids are long opaque strings and a case variant of somebody else's is not
    something a caller can obtain, so this is a note rather than a hole: the comparison is an
    agreement between two callers and was never a proof that two ids are distinct.

    The mapping from a plan to a send lives here rather than on the adapter, so `send` keeps
    the signature `redaction.assert_channel_adapter` can check: a parameter typed `Answer` names
    no `ChannelPayload`, and an adapter taking one could not be shown safe by reading it.
    """
    if identity_hash(Channel.TEAMS, to_conversation_id) != plan.to_identity:
        # Names neither the conversation id nor the digest it was expected to match. Both
        # reach a log from here, and the pair of them is the directory this module declines to
        # keep.
        msg = f"this message was planned for somewhere else. {A_PLAN_IS_BOUND_TO_ONE_CONVERSATION}"
        raise TeamsRefusedError(msg)
    match plan:
        case Answer():
            adapter.send(plan.payload, to=to_conversation_id, body=plan.body)
        case Notice():
            # An empty payload rather than the plan's, because a `Notice` has none. Passed
            # explicitly so the adapter's label check runs over the fixed words too.
            adapter.send(ChannelPayload(), to=to_conversation_id, body=plan.body)
        case _:
            assert_never(plan)
