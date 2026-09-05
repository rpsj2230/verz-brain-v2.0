"""Turning a payload into something a person reads, and the approval card in particular.

Two jobs live here because they share one rule. A plain message and a card are the same
thing to `brain.core.redaction`: both are a `ChannelPayload` that has to arrive with its
label intact. **One renderer, so a card template cannot be the place the label goes
missing.** `adapter.assert_can_send` refuses a payload a surface cannot label, and that
check is about the surface; nothing in it looks at the string actually produced. A card
built by hand, with a title and a body and three buttons and nowhere obvious to put "nobody
checked this", is exactly the shape that drops it while every capability check goes on
passing. `assert_label_survives` closes that by asking the produced string.

**An approval card is built for the person reading it, not the person who asked.** This is
item 14 in `docs/needs-rupash.md`, recorded there as a known gap to be fixed with the
approval work, and it is fixed here rather than made worse. `gate.leash.SuspendedAction`
carries an `artefact` rendered from the asker's action, and the obvious card design is
`card_for(suspension)`: pass the suspension in, render `artefact` into the body, post it to
whoever can approve. That design *is* the gap. A junior asks, a manager with narrower reach
on that client approves, and the artefact hands the manager a value they could not look up.

So `build_approval_card` refuses a `SuspendedAction` as an argument. It takes a
`ChannelPayload` and the approver's own `EntitlementSet`, and refuses unless the payload was
computed at the approver's reach - checked by comparing the caller's stated `ent_hash`
against `approver.ent_hash()`, which is the same comparison `gate.context.GateContext` makes
about its own pair. The channel still decides nothing about who may see what: the gate
computes the approver's payload, and this refuses to render one computed for somebody else.
What survives of the asker's request is the identifiers - `suspension_id` and
`action_digest` - which bind the decision to one action and carry no values at all.

**A card that cannot be patched is not left standing.** Patching is rate limited by the
vendor, and a stale approval card is one somebody acts on. Three things together, because no
one of them is enough:

*The card is disarmed locally before anything goes on the wire.* `close_card` returns the
card with `armed=False`, and `assert_press_is_live` refuses a press on it. Whatever the
surface still shows, this process has stopped treating the decision as open. That is the
half that holds when the network does not.

*The close budget is reserved, so a close is never refused because of opens.* Opens and
closes draw on one vendor ceiling, so "fall back to a text message" is not a fallback when
the reason the patch failed is that there was no budget: the text costs the same request.
The reserve is what makes the fallback mean something. See `EVERY_CARD_OPENED_MUST_BE_CLOSABLE`.

*And where the budget admits but the surface cannot edit in place, the fallback is a text
message that says the card above is out of date.* Never nothing.

The rejected alternative was to trust the surface: leave the card, and let the button press
be refused server side by `SuspendedAction.is_open`. That refusal is real and it stays, but
it is not sufficient. It arrives after somebody has read a card saying a decision is open,
decided, and pressed - and the thing they were deciding about may have been taken by
somebody else in between. A refusal after the fact is an error message; a disarmed card is
an answer.

Nothing here opens a connection and nothing here re-implements a limiter. The sliding window
is `ops.limits`, the ceiling is the verified one in `ops.limits.SOURCE_CEILINGS`, and this
module supplies the policy on top of them.

Task ids: M10.2.3, M10.2.4
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final

from brain.channels.adapter import ChannelCapabilities, Feature, assert_can_send
from brain.core.entitlement import EntitlementSet
from brain.core.field_policy import Classification
from brain.core.redaction import (
    ENTITY_KEYS,
    ID_KEYS,
    RESERVED_KEYS,
    ChannelPayload,
    render_lock,
)
from brain.gate.leash import DIGEST, ApprovalState
from brain.ops.limits import (
    MINUTE_SECONDS,
    Limit,
    LimitDecision,
    LimiterState,
    LimitScope,
    check,
    connector_ceiling,
)

# ------------------------------------------------------------------ written-down reasons

#: Why a plain message and a card body go through one function.
#:
#: `adapter.assert_can_send` asks whether the *surface* can carry a label. Nothing in it
#: looks at the string that comes out, so a template with nowhere to put the label satisfies
#: it and still delivers "here is an answer" where the payload said "nobody checked this".
#: Two renderers means two places to forget, and the one that forgets is the one added last.
ONE_RENDERER_SO_THE_LABEL_CANNOT_BE_DROPPED: Final = (
    "a payload carrying a label renders that label or refuses to send; the check is on the "
    "string produced, because a capability check cannot see a template with nowhere to put it"
)

#: Why an approval card is built at the approver's reach and not the asker's.
#:
#: Item 14 in docs/needs-rupash.md. The asker's action is what is being decided; the
#: approver's reach is what may be shown while deciding it. Those are different people and
#: the card is read by the second one.
A_CARD_IS_BUILT_FOR_THE_PERSON_READING_IT: Final = (
    "an approval card shows what the approver may see, never what the asker may see; a "
    "junior's question approved by a manager with narrower reach must not hand the manager "
    "a value they could not look up themselves"
)

#: Why opens are capped below the ceiling.
#:
#: Opens and closes share one vendor ceiling. If opens may take all of it, a minute of new
#: cards leaves nothing to close the ones already out, and every one of those stays showing
#: an open decision. Splitting the ceiling in half is what makes the split a proof rather
#: than a hope: in a steady state one close follows one open, so a close rate equal to the
#: open rate always keeps up.
EVERY_CARD_OPENED_MUST_BE_CLOSABLE: Final = (
    "opens may take at most the share of the ceiling that closes are not reserved, so a "
    "close is never refused because of opens; a text fallback drawn from an exhausted "
    "budget is not a fallback, it is the same refusal one call later"
)

#: Why the card object refuses a press rather than trusting what the surface shows.
A_STALE_CARD_IS_ONE_SOMEBODY_ACTS_ON: Final = (
    "a card is a view and never the authority; it is disarmed before the patch is attempted, "
    "so a press on a decision already taken is refused whatever the surface still displays"
)


class CardRefusedError(Exception):
    """Raised when a card must not be built, sent or acted on.

    Not a `BrainError`, for the reason `adapter.DeliveryRefusedError` gives about itself:
    it is a wiring fault or a policy boundary rather than an outcome of somebody's question,
    and reporting it as a degraded answer would put "this card was built for the wrong
    person" in the same bucket as "the provider is down".
    """


class CardStaleError(CardRefusedError):
    """The decision has been taken and the surface has not been told.

    Raised rather than returned, and that is the whole point of it. A function returning an
    outcome enum is one whose result can be ignored by not branching on it, and the ignored
    branch here leaves an approval card showing an open decision that is closed. The same
    argument `brain.channels.webhook.verify` makes about not returning a bool.

    Carries the disarmed card so a caller can retry without rebuilding it. Retrying the
    *same* close is admitted by `close_card`; a different one is not.
    """

    def __init__(self, message: str, *, card: ApprovalCard, retry_after_seconds: float) -> None:
        super().__init__(message)
        self.card = card
        self.retry_after_seconds = retry_after_seconds


# ------------------------------------------------------------------------- rendering

#: How a label is rendered. Bracketed and first, so it is above the answer rather than
#: beside it: a warning underneath something already read is a warning that arrives late.
LABEL_PREFIX: Final = "[!] "

#: What a payload with nothing in it renders as. One sentence for a record that was withheld
#: and for a record that does not exist, because those two must not be distinguishable; see
#: the collapse of DENIED into ABSENT in `brain.core.redaction`.
NOTHING_TO_SAY: Final = "I do not have an answer for that."


def assert_label_survives(body: str, payload: ChannelPayload) -> None:
    """Refuse a rendered body that dropped the payload's label (M10.1.5, M10.2.3).

    See `ONE_RENDERER_SO_THE_LABEL_CANNOT_BE_DROPPED`. This asks the produced string,
    which is the question `adapter.assert_can_send` cannot ask: that one knows whether the
    surface *could* render a label, and a template that could and did not satisfies it.

    The message names the label and not the payload. A refusal quoting the records would
    write the unredacted thing into whatever log records the refusal, which is the one
    place it must not reach.
    """
    if payload.label and payload.label not in body:
        msg = (
            f"this body drops the payload label {payload.label!r}. "
            f"{ONE_RENDERER_SO_THE_LABEL_CANNOT_BE_DROPPED}"
        )
        raise CardRefusedError(msg)


def _first(record: Mapping[str, Any], keys: Sequence[str]) -> str:
    """The first of these keys present as a string. Both spellings of the tag are accepted.

    `brain.core.redaction` writes `@entity` and `@id`; `brain.core.envelope` models the same
    two as ordinary fields named `entity` and `id`. A renderer that understood one spelling
    would silently head half the system's records with a blank line.
    """
    for key in keys:
        value = record.get(key)
        if isinstance(value, str):
            return value
    return ""


def _record_lines(record: Mapping[str, Any]) -> list[str]:
    """One record, tag line first and fields sorted underneath.

    Sorted by name rather than by the order the record arrived in, for the reason
    `redaction.redact` sorts its locks: the order a source returned its columns in differs
    between callers and is readable as a signal about what each of them was refused.
    """
    head = f"{_first(record, ENTITY_KEYS)} {_first(record, ID_KEYS)}".strip()
    lines = [head] if head else []
    lines.extend(f"  {key}: {record[key]}" for key in sorted(record) if key not in RESERVED_KEYS)
    return lines


def render_body(payload: ChannelPayload) -> str:
    """The one path from a payload to a string a person reads.

    Locks are rendered as `entity.field` and deduplicated, never once per record. A line per
    record would let a reader count the records a field was withheld on, which is a
    hidden-item count assembled one line at a time; `RedactionTrace.withheld_field_names`
    deduplicates for the same reason.

    Nothing here counts anything. There is no "and 3 more", no total and no length, because
    every one of those is the subtraction `brain.core.redaction` spends a module preventing.
    """
    lines: list[str] = []
    if payload.label:
        lines.append(LABEL_PREFIX + payload.label)
    for record in payload.records:
        lines.extend(_record_lines(record))
    lines.extend(
        f"{name}: {render_lock()}"
        for name in sorted({f"{lock.entity}.{lock.field}" for lock in payload.locked})
    )
    if not lines or (payload.label and len(lines) == 1):
        lines.append(NOTHING_TO_SAY)
    body = "\n".join(lines)
    assert_label_survives(body, payload)
    return body


# ------------------------------------------------------------- the approval card (M10.2.3)

_DIGEST_RE: Final = re.compile(DIGEST)


@dataclass(frozen=True)
class Button:
    """One thing a person can press. Text and a decision, and nothing else.

    Deliberately carries no payload of its own. Everything a press needs to be checked
    against is on the card, and a button holding its own copy is a second copy to disagree
    with the first: a button whose digest was rendered before an amendment would approve
    the action nobody is looking at.
    """

    text: str
    decision: ApprovalState


@dataclass(frozen=True)
class ApprovalCard:
    """One decision, in front of one named person, bound to one action.

    `rendered_for` and `ent_hash` are the two halves of item 14's fix. The first says who
    may read this card; the second says whose reach the body was computed at. They are
    checked against each other at construction and never afterwards recombined, so a card
    cannot be re-aimed at a second reader by copying it.

    `armed` is what a press is checked against, and it is local state on purpose. The
    surface is remote, editable only within a rate limit, and sometimes not editable at all;
    treating what it displays as the truth about an open decision is the failure this whole
    type exists to avoid. See `A_STALE_CARD_IS_ONE_SOMEBODY_ACTS_ON`.
    """

    card_id: str
    suspension_id: str
    #: `gate.leash.Action.digest`. Binds this card to exactly one action, so an amended
    #: action is a different card rather than the same one saying something else.
    action_digest: str
    #: The principal who may read and press this card.
    rendered_for: str
    #: What they are shown, computed by the gate at their reach.
    payload: ChannelPayload
    #: The reach `payload` was computed at, as `EntitlementSet.ent_hash` renders it.
    ent_hash: str
    raised_at: datetime
    expires_at: datetime
    state: ApprovalState = ApprovalState.PENDING
    armed: bool = True

    def __post_init__(self) -> None:
        if not _DIGEST_RE.match(self.action_digest):
            msg = (
                f"action digest {self.action_digest!r} is not a sha256 digest; a card that "
                "cannot name one action can be satisfied by a different one"
            )
            raise CardRefusedError(msg)
        if not self.rendered_for:
            msg = "a card with no reader is one anybody may read"
            raise CardRefusedError(msg)
        if self.expires_at <= self.raised_at:
            msg = "a card that expires before it was raised can never be pressed"
            raise CardRefusedError(msg)

    def is_live(self, now: datetime) -> bool:
        """Whether a press on this card may still be honoured."""
        return self.armed and self.state is ApprovalState.PENDING and now < self.expires_at


def buttons() -> tuple[Button, ...]:
    """The two decisions a card offers. Amend is deliberately absent.

    An amend button would have to carry a changed action, and a changed action has a
    different `gate.leash.Action.digest`, so amending is raising a new suspension rather
    than pressing a button on this one. Offering it here would produce a press whose digest
    matches a card nobody read.
    """
    return (
        Button(text="Approve", decision=ApprovalState.APPROVED),
        Button(text="Reject", decision=ApprovalState.REJECTED),
    )


def press_value(card: ApprovalCard, button: Button) -> dict[str, str]:
    """What comes back when this button is pressed.

    The suspension and the digest travel with the press so the decision names the action it
    was granted for. Nothing about the body is in here: a press carrying a rendered value
    would put that value into whatever logs the callback, for every card ever pressed.
    """
    return {
        "suspension_id": card.suspension_id,
        "action_digest": card.action_digest,
        "decision": button.decision.value,
    }


def build_approval_card(
    *,
    card_id: str,
    suspension_id: str,
    action_digest: str,
    payload: ChannelPayload,
    body_ent_hash: str,
    approver: EntitlementSet,
    raised_at: datetime,
    expires_at: datetime,
    capabilities: ChannelCapabilities,
    highest: Classification = Classification.INTERNAL,
) -> ApprovalCard:
    """Build a card for the approver, refusing one built for anybody else (M10.2.3).

    **There is deliberately no `SuspendedAction` parameter.** That is the design item 14
    describes: the suspension carries an artefact rendered from the asker's action, and a
    card that rendered it would show the approver whatever the asker could see. What comes
    from the suspension is its id and its action digest, both of which are identifiers and
    neither of which is a value.

    `body_ent_hash` is the caller's statement of whose reach `payload` was computed at, and
    it is checked against the approver's own. The channel computes no entitlements and
    redacts nothing - that is the gate's work and doing it twice would be a second opinion -
    but it can refuse a payload the caller has told it was computed for the wrong person.

    The surface refusals are delegated to `adapter.assert_can_send` rather than restated,
    so a card and a plain message cannot disagree about what a channel may carry.
    """
    if body_ent_hash != approver.ent_hash():
        msg = (
            f"this body was computed at {body_ent_hash!r} and the approver's reach is "
            f"{approver.ent_hash()!r}. {A_CARD_IS_BUILT_FOR_THE_PERSON_READING_IT}"
        )
        raise CardRefusedError(msg)

    assert_can_send(capabilities, payload, highest=highest)
    if not capabilities.supports(Feature.CARDS):
        msg = (
            f"{capabilities.channel} does not do cards, so an approval here would degrade to "
            "a link; that is a decision for the caller and not something to do silently"
        )
        raise CardRefusedError(msg)

    card = ApprovalCard(
        card_id=card_id,
        suspension_id=suspension_id,
        action_digest=action_digest,
        rendered_for=approver.principal_id,
        payload=payload,
        ent_hash=body_ent_hash,
        raised_at=raised_at,
        expires_at=expires_at,
    )
    # Rendered here rather than left to the sender, so a card whose template drops the label
    # is refused at the point it is built rather than at the point it is delivered.
    render_card(card)
    return card


def render_card(card: ApprovalCard) -> str:
    """The card body, through the one renderer, with the label kept.

    The buttons are named in the text as well as offered as buttons. A surface that renders
    the card as a notification, a digest email or a screen reader announcement shows the
    text and not the controls, and a decision nobody can see how to take reads as broken.
    """
    lines = [
        f"Approval {card.suspension_id}",
        render_body(card.payload),
        "Actions: " + ", ".join(button.text for button in buttons()),
    ]
    body = "\n".join(lines)
    assert_label_survives(body, card.payload)
    return body


def assert_press_is_live(
    card: ApprovalCard,
    *,
    presser_id: str,
    action_digest: str,
    now: datetime,
) -> None:
    """Refuse a press that must not take effect (M10.2.3, M10.2.4).

    Four refusals and one message, in the way `webhook.verify` uses one message for its
    four: naming which check failed tells somebody probing which part to change next. The
    difference is that this one is a colleague rather than an attacker, so the message says
    plainly that the decision is no longer open without saying which of the four reasons.

    The digest check is what makes a stale card safe rather than merely disarmed. A card
    patched into a decided state, an action amended since, or a second card for a different
    action all produce a press whose digest does not match the card in hand.
    """
    if (
        not card.is_live(now)
        or presser_id != card.rendered_for
        or action_digest != card.action_digest
    ):
        msg = (
            f"this decision is no longer open to {presser_id}. "
            f"{A_STALE_CARD_IS_ONE_SOMEBODY_ACTS_ON}"
        )
        raise CardRefusedError(msg)


# ------------------------------------------------------- the patch budget (M10.2.4)

#: The connector whose verified ceiling every Lark call is budgeted against.
#:
#: `ops.limits.SOURCE_CEILINGS` records it as 100 requests a minute, fixed, with the vendor
#: stating it cannot be raised. It is the only verified Lark figure this repository holds,
#: and sizing a card budget against a higher number would be sizing against a number that
#: does not exist. Read rather than restated, so raising the entry raises this too.
LARK_CONNECTOR: Final = "lark_base"

#: The share of that ceiling opens may take. A half, and the half is an argument rather than
#: a round number: in a steady state one close follows one open, so opens capped at half the
#: ceiling leave a close rate that always keeps up. See `EVERY_CARD_OPENED_MUST_BE_CLOSABLE`.
#:
#: Below 0.5 buys a deeper close reserve and refuses more new approvals; above 0.5 admits
#: more approvals than can be closed in the same minute, and the backlog is exactly the
#: stale cards this budget exists to prevent.
OPEN_SHARE: Final = 0.5


class CardCall(enum.StrEnum):
    """What a card request is for. Closed, because each member has its own window."""

    #: Posting a new card into a chat.
    OPEN = "open"
    #: Patching one to its decided state, or posting the text that replaces it. Both are
    #: one request and both are the same need, so they share one window: a fallback drawn
    #: from a different allowance would be admitted while the patch it replaces was not.
    CLOSE = "close"


def card_ceiling_per_minute() -> int:
    """The verified vendor ceiling. Read from `ops.limits`, never restated here.

    Raises rather than defaulting when the entry is missing. A default would be a literal
    invented in this file, which is the "hoped-for number" the whole budget exists to avoid,
    and it would be wrong silently.
    """
    ceiling = connector_ceiling(LARK_CONNECTOR)
    if ceiling is None:
        msg = (
            f"{LARK_CONNECTOR} has no verified ceiling in ops.limits.SOURCE_CEILINGS; a card "
            "budget guessed here would be a number nobody checked"
        )
        raise CardRefusedError(msg)
    return ceiling.per_minute


def card_split() -> tuple[int, int]:
    """How many opens and how many closes a minute. They sum to the ceiling exactly.

    Returned together rather than as two functions, because the property that matters is
    the relationship: a change that raised opens without lowering closes would overcommit
    the ceiling, and two functions is where that change looks correct in a diff.
    """
    ceiling = card_ceiling_per_minute()
    opens = int(ceiling * OPEN_SHARE)
    return opens, ceiling - opens


def card_limit(call: CardCall) -> Limit:
    """The window governing one kind of card request.

    Two windows rather than one shared window with a policy on top. A shared window cannot
    express a reserve: whichever caller arrives first spends it, which is precisely the
    behaviour that leaves a card open because somebody else posted fifty new ones.
    """
    opens, closes = card_split()
    allowance = opens if call is CardCall.OPEN else closes
    return Limit(
        scope=LimitScope.CHANNEL,
        subject=f"lark:card_{call.value}",
        period="minute",
        limit=allowance,
        window_seconds=MINUTE_SECONDS,
        raisable=False,
        reason=EVERY_CARD_OPENED_MUST_BE_CLOSABLE,
    )


def may_open(*, now: datetime, state: LimiterState) -> LimitDecision:
    """Whether another card may be posted. Nothing is recorded; the caller records the hit.

    A card refused here is one that was never posted, so there is nothing stale about it and
    nothing to close. That asymmetry is the reserve doing its job: the cost of the guarantee
    is paid by an approval that waits, never by one that is already in front of somebody.
    """
    return check(now=now, limits=(card_limit(CardCall.OPEN),), state=state)


class PatchOutcome(enum.StrEnum):
    """How the surface was told. Closed, and there is deliberately no `NOTHING` member."""

    #: The card was edited in place.
    PATCHED = "patched"
    #: The surface cannot edit in place, so a message saying the card is out of date goes
    #: in its stead. One request either way, from the same window.
    TEXT_FALLBACK = "text_fallback"


@dataclass(frozen=True)
class PatchPlan:
    """What to send, and the card as it now stands.

    `card` is always the disarmed one, whichever outcome this is. A caller that sends
    nothing still holds a card that refuses a press, which is the property that makes the
    fallback safe rather than merely present.
    """

    outcome: PatchOutcome
    card: ApprovalCard
    #: The message to post when the surface cannot be edited. Empty when patched.
    text: str
    #: What to record once the call succeeds. See
    #: `limits.REFUSED_REQUESTS_DO_NOT_EXTEND_THE_WINDOW` for why a refusal records nothing.
    record_against: tuple[Limit, ...]


#: What the fallback message says. It names the decision and the card, and no value from
#: either: the fallback goes to the same place the card went, but a message is forwardable
#: in a way a card in a thread is not, so it carries identifiers only.
FALLBACK_TEMPLATE: Final = "Approval {suspension} is now {state}. The card above is out of date."


def close_card(
    card: ApprovalCard,
    *,
    state: ApprovalState,
    now: datetime,
    capabilities: ChannelCapabilities,
    limiter: LimiterState,
) -> PatchPlan:
    """Take the decision off the surface, or say plainly that it could not be taken off.

    The order is the design. The card is disarmed first, in memory, before anything is
    attempted on the wire: a patch that fails after the disarming leaves a card this process
    refuses to honour, and a patch that fails before it would leave one it does. Then the
    budget, then the surface's ability to edit at all.

    Raises `CardStaleError` when the close window is full. That is the case the text
    fallback cannot cover, because the fallback is a request from the same window: falling
    back there would be the same refusal one call later, dressed as a plan. The exception
    carries the disarmed card and the exact wait, so a caller can retry the same close.

    Retrying the same close is admitted. A *different* close on an already closed card is
    refused: two decisions on one action is not a retry, and picking one of them silently is
    the guess this module has no evidence to make.
    """
    if state is ApprovalState.PENDING:
        msg = "closing a card to pending is not a close; there would be nothing to say"
        raise CardRefusedError(msg)
    if not card.armed and card.state is not state:
        msg = (
            f"card {card.card_id} was already closed as {card.state.value} and this would "
            f"close it as {state.value}; two decisions on one action is not a retry"
        )
        raise CardRefusedError(msg)

    closed = replace(card, state=state, armed=False)
    text = FALLBACK_TEMPLATE.format(suspension=card.suspension_id, state=state.value)

    limit = card_limit(CardCall.CLOSE)
    decision = check(now=now, limits=(limit,), state=limiter)
    if not decision.allowed:
        msg = (
            f"the close window is full, so card {card.card_id} still shows an open decision. "
            f"{A_STALE_CARD_IS_ONE_SOMEBODY_ACTS_ON}"
        )
        raise CardStaleError(msg, card=closed, retry_after_seconds=decision.retry_after_seconds)

    if capabilities.supports(Feature.EDIT_IN_PLACE):
        return PatchPlan(
            outcome=PatchOutcome.PATCHED, card=closed, text="", record_against=(limit,)
        )
    return PatchPlan(
        outcome=PatchOutcome.TEXT_FALLBACK, card=closed, text=text, record_against=(limit,)
    )
