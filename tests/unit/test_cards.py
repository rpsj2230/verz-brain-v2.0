"""Approval cards: who they are built for, and what happens when one cannot be patched.

The first half is item 14 in `docs/needs-rupash.md`: a card built with the asker's
permissions and read by an approver with narrower ones. The second half is the rate limit:
a card that cannot be patched is a stale card, and a stale approval card is one somebody
acts on.

Task ids: M10.2.3, M10.2.4
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from brain.channels.adapter import ChannelCapabilities, DeliveryRefusedError, Feature
from brain.channels.cards import (
    LARK_CONNECTOR,
    NOTHING_TO_SAY,
    ApprovalCard,
    CardCall,
    CardRefusedError,
    CardStaleError,
    PatchOutcome,
    assert_label_survives,
    assert_press_is_live,
    build_approval_card,
    buttons,
    card_limit,
    card_split,
    close_card,
    may_open,
    press_value,
    render_body,
    render_card,
)
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.field_policy import Classification
from brain.core.redaction import OPAQUE_LABEL, ChannelPayload, LockedField
from brain.core.scope import Scope
from brain.gate.context import Channel
from brain.gate.leash import ApprovalState
from brain.ops.limits import Limit, LimiterState, WindowState, connector_ceiling

NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=4)

READ_NAME = "read:client.name"
READ_MARGIN = "read:client.margin"

DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


def _ents(*capabilities: str, principal_id: str = "u_approver") -> EntitlementSet:
    return EntitlementSet(
        principal_id=principal_id,
        grants=tuple(
            Grant(capability=Capability(value=v), scope=Scope.unrestricted()) for v in capabilities
        ),
    )


def _caps(*features: Feature, **overrides: object) -> ChannelCapabilities:
    base: dict[str, object] = {
        "channel": Channel.LARK,
        "features": frozenset(features or (Feature.CARDS, Feature.EDIT_IN_PLACE)),
        "max_classification": Classification.CONFIDENTIAL,
        "can_carry_label": True,
    }
    base.update(overrides)
    return ChannelCapabilities(**base)  # type: ignore[arg-type]


def _payload(**fields: str) -> ChannelPayload:
    return ChannelPayload(records=({"@entity": "client", "@id": "c_1", **fields},))


def _card(
    *,
    approver: EntitlementSet | None = None,
    capabilities: ChannelCapabilities | None = None,
    payload: ChannelPayload | None = None,
    action_digest: str = DIGEST,
    expires_at: datetime = LATER,
) -> ApprovalCard:
    approver = approver or _ents(READ_NAME)
    return build_approval_card(
        card_id="card_1",
        suspension_id="susp_1",
        action_digest=action_digest,
        payload=payload if payload is not None else _payload(name="SNM"),
        body_ent_hash=approver.ent_hash(),
        approver=approver,
        raised_at=NOW,
        expires_at=expires_at,
        capabilities=capabilities or _caps(),
    )


def _full_window(limit: Limit) -> LimiterState:
    """A window with exactly its allowance already spent, so the next call is refused."""
    hits = tuple(NOW - timedelta(seconds=index) for index in range(limit.limit))
    return LimiterState(windows={limit.key: WindowState(hits=hits)})


# ============================================================ item 14 (M10.2.3)


def test_an_approval_card_built_at_the_askers_reach_is_refused() -> None:
    """Item 14 in docs/needs-rupash.md, closed rather than carried forward.

    A junior asks, and a manager with narrower reach on that client approves. The body was
    computed at the asker's reach, so it can hold a value the approver could not look up
    themselves, and the card would hand it to them while they decide.

    The check is a comparison of the reach the caller says the body was computed at against
    the approver's own, which is the same comparison `gate.context.GateContext` makes about
    its own pair.

    Deleting this reopens the gap the whole approval path was waiting on, and it reopens it
    silently: the card looks right, and only the approver's own entitlements would say
    otherwise."""
    asker = _ents(READ_NAME, READ_MARGIN, principal_id="u_junior")
    approver = _ents(READ_NAME, principal_id="u_manager")

    with pytest.raises(CardRefusedError, match="approver's reach"):
        build_approval_card(
            card_id="card_1",
            suspension_id="susp_1",
            action_digest=DIGEST,
            payload=_payload(name="SNM", margin="0.34"),
            body_ent_hash=asker.ent_hash(),
            approver=approver,
            raised_at=NOW,
            expires_at=LATER,
            capabilities=_caps(),
        )
    assert asker.ent_hash() != approver.ent_hash()


def test_an_approval_card_built_at_the_approvers_reach_is_accepted() -> None:
    """The positive sibling. A guard tested only by refusals refuses everything.

    Without this, item 14 is closed by a function that never builds a card, and approvals
    stop working rather than start being safe.

    Deleting this lets the refusal above widen until nothing can be approved anywhere."""
    approver = _ents(READ_NAME, principal_id="u_manager")
    card = _card(approver=approver)

    assert card.rendered_for == "u_manager"
    assert card.ent_hash == approver.ent_hash()
    assert card.state is ApprovalState.PENDING
    assert card.is_live(NOW) is True


def test_a_card_carries_no_field_that_could_hold_the_askers_artefact() -> None:
    """Item 14 again, pinned on the shape rather than on one code path.

    `gate.leash.SuspendedAction.artefact` is rendered from the asker's action and carries
    values. The obvious card design stores it; that design is the gap. This asserts the
    card's fields are exactly the set that cannot hold one, so adding it back is a failing
    test rather than a plausible convenience.

    Deleting this lets `artefact` reappear as a field beside the payload, and the reach
    check above would pass while the artefact carried the asker's values anyway."""
    assert {f.name for f in dataclasses.fields(ApprovalCard)} == {
        "card_id",
        "suspension_id",
        "action_digest",
        "rendered_for",
        "payload",
        "ent_hash",
        "raised_at",
        "expires_at",
        "state",
        "armed",
    }


def test_a_card_on_a_surface_that_does_not_do_cards_is_refused() -> None:
    """M10.2.3. Degrading an approval to a link is a decision, not a silent fallback.

    A surface with no cards can still be told something, and what it is told is a matter for
    the caller; building a card that will render as a wall of text is not the answer.

    Deleting this lets an approval go out somewhere it cannot be acted on, which reads to
    the approver as an FYI."""
    with pytest.raises(CardRefusedError, match="does not do cards"):
        _card(capabilities=_caps(Feature.EPHEMERAL))


def test_a_card_whose_surface_cannot_render_a_label_is_refused() -> None:
    """M10.1.5 reaching cards, which is where item 12 said it would be forgotten.

    An approval card is exactly the template with a title, a body and three buttons and
    nowhere obvious to put "nobody checked this". `assert_can_send` is delegated to rather
    than restated, so a card and a plain message cannot disagree.

    Deleting this lets an unredacted payload be approved as though it were an answer."""
    approver = _ents(READ_NAME)
    opaque = ChannelPayload(records=({"@entity": "x", "@id": "1"},), label=OPAQUE_LABEL)

    with pytest.raises(DeliveryRefusedError, match="cannot render a payload label"):
        _card(approver=approver, payload=opaque, capabilities=_caps(can_carry_label=False))

    card = _card(approver=approver, payload=opaque)
    assert OPAQUE_LABEL in render_card(card)


# ============================================================ pressing (M10.2.3)


def test_a_live_press_by_the_reader_naming_this_action_is_admitted() -> None:
    """The positive sibling for the four refusals below.

    A press check tested only by what it refuses is satisfied by refusing every press, and
    an approval nobody can grant is an approval queue that fills up.

    Deleting this lets the refusals widen until the buttons do nothing."""
    card = _card()
    assert_press_is_live(card, presser_id="u_approver", action_digest=DIGEST, now=NOW)

    approve, reject = buttons()
    assert approve.decision is ApprovalState.APPROVED
    assert reject.decision is ApprovalState.REJECTED
    assert press_value(card, approve) == {
        "suspension_id": "susp_1",
        "action_digest": DIGEST,
        "decision": "approved",
    }


def test_a_press_on_a_card_whose_decision_is_already_taken_is_refused() -> None:
    """M10.2.4. The half that holds when the patch did not go out.

    The card is disarmed in memory before anything is attempted on the wire, so a press
    arriving from a surface still showing an open decision is refused here rather than
    honoured. See `A_STALE_CARD_IS_ONE_SOMEBODY_ACTS_ON`.

    Deleting this makes the surface the authority on whether a decision is open, and the
    surface is the thing that could not be updated."""
    closed = close_card(
        _card(),
        state=ApprovalState.APPROVED,
        now=NOW,
        capabilities=_caps(),
        limiter=LimiterState(),
    ).card

    assert closed.armed is False
    with pytest.raises(CardRefusedError, match="no longer open"):
        assert_press_is_live(closed, presser_id="u_approver", action_digest=DIGEST, now=NOW)


def test_a_press_by_anybody_but_the_person_the_card_was_built_for_is_refused() -> None:
    """M10.2.3, and the other half of item 14.

    The body was computed at one person's reach. Honouring a press from somebody else would
    mean a second person decided on the strength of what the first was shown, which is the
    same mixing of two people's reach in the opposite direction.

    Deleting this lets a card forwarded to a colleague be pressed by them."""
    with pytest.raises(CardRefusedError, match="no longer open"):
        assert_press_is_live(_card(), presser_id="u_someone_else", action_digest=DIGEST, now=NOW)


def test_a_press_naming_a_different_action_is_refused() -> None:
    """M10.2.3. The digest is what binds an approval to the action it was granted for.

    An amended action has a different `gate.leash.Action.digest`, so a press carrying the
    old one is an approval of something nobody is looking at.

    Deleting this lets an action be edited between the card being read and the button being
    pressed, and the approval would still apply."""
    with pytest.raises(CardRefusedError, match="no longer open"):
        assert_press_is_live(_card(), presser_id="u_approver", action_digest=OTHER_DIGEST, now=NOW)


def test_a_press_after_the_window_has_closed_is_refused() -> None:
    """M10.2.3. An approval that stands forever is a standing grant with extra steps.

    `gate.leash.MAX_APPROVAL_WINDOW` makes the same argument about the suspension; the card
    has to agree, because the card is what somebody is looking at hours later.

    Deleting this lets yesterday's card approve today's action."""
    with pytest.raises(CardRefusedError, match="no longer open"):
        assert_press_is_live(
            _card(), presser_id="u_approver", action_digest=DIGEST, now=LATER + timedelta(minutes=1)
        )


def test_a_card_that_expires_before_it_was_raised_is_refused() -> None:
    """M10.2.3. A window that never opens is a card nobody can ever press.

    Deleting this lets a caller build a card that is dead on arrival, which presents to the
    approver as the buttons doing nothing."""
    with pytest.raises(CardRefusedError, match="expires before"):
        _card(expires_at=NOW - timedelta(minutes=1))


# ============================================================ the patch budget (M10.2.4)


def test_the_card_budget_is_the_verified_lark_ceiling_and_not_a_hoped_for_one() -> None:
    """M10.2.4. 100 a minute, fixed, and the vendor says it cannot be raised.

    Read from `ops.limits.SOURCE_CEILINGS` rather than restated here, so raising the entry
    raises this and a second number cannot drift away from the first. The split sums to the
    ceiling exactly: a split that overcommitted would be a budget that admits more calls
    than the vendor accepts, which presents as 429s rather than as a configuration error.

    Deleting this lets somebody size the card budget against a number they hoped for, and
    the failure would arrive as approvals that silently do not update."""
    ceiling = connector_ceiling(LARK_CONNECTOR)
    assert ceiling is not None
    assert ceiling.raisable is False

    opens, closes = card_split()
    assert opens + closes == ceiling.per_minute
    assert opens > 0
    assert closes >= opens
    assert card_limit(CardCall.OPEN).raisable is False


def test_opens_can_never_exhaust_the_budget_a_close_needs() -> None:
    """M10.2.4, and the reason the text fallback means anything at all.

    Opens and closes draw on one vendor ceiling. If they shared one window, a minute of new
    approvals would leave nothing to close the ones already out, and every one of those
    would stay showing an open decision. Two windows is what makes the reserve a reserve.

    Deleting this lets the two collapse into one allowance, and the failure appears only
    under exactly the load that produces the most stale cards."""
    opens = card_limit(CardCall.OPEN)
    closes = card_limit(CardCall.CLOSE)
    assert opens.key != closes.key

    exhausted = _full_window(opens)
    assert may_open(now=NOW, state=exhausted).allowed is False

    plan = close_card(
        _card(), state=ApprovalState.APPROVED, now=NOW, capabilities=_caps(), limiter=exhausted
    )
    assert plan.outcome is PatchOutcome.PATCHED


def test_a_card_that_cannot_be_patched_does_not_silently_keep_showing_stale_state() -> None:
    """M10.2.4, and the failure this leaf exists to prevent.

    When the close window is full there is no text fallback, because the text is a request
    from the same window: falling back there would be the same refusal one call later
    dressed up as a plan. So it raises, and the raise is the point. A returned outcome enum
    can be ignored by not branching on it, and the ignored branch leaves an approval card
    showing an open decision that has been taken.

    The card on the exception is already disarmed, so the press path refuses while the
    surface still shows the old state, and the exact wait comes with it.

    Deleting this is how "leave the old card and hope" gets written: every other test here
    would still pass."""
    limiter = _full_window(card_limit(CardCall.CLOSE))

    with pytest.raises(CardStaleError, match="still shows an open decision") as raised:
        close_card(
            _card(),
            state=ApprovalState.APPROVED,
            now=NOW,
            capabilities=_caps(),
            limiter=limiter,
        )

    assert raised.value.card.armed is False
    assert raised.value.retry_after_seconds > 0
    with pytest.raises(CardRefusedError):
        assert_press_is_live(
            raised.value.card, presser_id="u_approver", action_digest=DIGEST, now=NOW
        )


def test_a_surface_that_cannot_edit_in_place_gets_the_text_fallback() -> None:
    """M10.2.4. The fallback, in the case it is actually for.

    A surface that cannot edit a message it already sent still has to be told. The message
    names the approval and the decision and carries no value from the body: a card sits in a
    thread and a message is forwardable, so the fallback says less rather than the same.

    Deleting this lets a surface without edit-in-place leave its card standing, which is the
    "leave it and hope" outcome arrived at from the other direction."""
    plan = close_card(
        _card(capabilities=_caps(Feature.CARDS)),
        state=ApprovalState.REJECTED,
        now=NOW,
        capabilities=_caps(Feature.CARDS),
        limiter=LimiterState(),
    )

    assert plan.outcome is PatchOutcome.TEXT_FALLBACK
    assert "susp_1" in plan.text
    assert "rejected" in plan.text
    assert "out of date" in plan.text
    assert "SNM" not in plan.text
    assert plan.card.armed is False


def test_a_patch_within_the_budget_edits_the_card_in_place() -> None:
    """The positive sibling for both fallbacks. The ordinary case has to work.

    Without it, every assertion above is satisfied by a `close_card` that never patches
    anything, and the rate limit would have been "solved" by never using the budget.

    Deleting this lets the fallbacks become the only path."""
    plan = close_card(
        _card(), state=ApprovalState.APPROVED, now=NOW, capabilities=_caps(), limiter=LimiterState()
    )

    assert plan.outcome is PatchOutcome.PATCHED
    assert plan.text == ""
    assert plan.card.state is ApprovalState.APPROVED
    assert plan.record_against == (card_limit(CardCall.CLOSE),)


def test_retrying_the_same_close_is_admitted_and_a_different_one_is_not() -> None:
    """M10.2.4. A retry after a stale-card refusal must be possible, and a reversal must not.

    The card handed back on `CardStaleError` is already disarmed, so a retry presents an
    already-closed card. Refusing that would make the exception unrecoverable. Accepting a
    *different* decision on it would let a rejection overwrite an approval that has already
    been acted on, and nothing here can tell which of the two is the real one.

    Deleting this leaves the recovery path untested, and the first stale card in production
    becomes a card nobody can ever close."""
    approved = close_card(
        _card(), state=ApprovalState.APPROVED, now=NOW, capabilities=_caps(), limiter=LimiterState()
    ).card

    again = close_card(
        approved,
        state=ApprovalState.APPROVED,
        now=NOW,
        capabilities=_caps(),
        limiter=LimiterState(),
    )
    assert again.outcome is PatchOutcome.PATCHED

    with pytest.raises(CardRefusedError, match="not a retry"):
        close_card(
            approved,
            state=ApprovalState.REJECTED,
            now=NOW,
            capabilities=_caps(),
            limiter=LimiterState(),
        )


def test_closing_a_card_to_pending_is_refused() -> None:
    """M10.2.4. There would be nothing to say, and the card would be disarmed anyway.

    Deleting this lets a caller disarm a card while telling the surface the decision is
    still open, which is the stale state produced deliberately."""
    with pytest.raises(CardRefusedError, match="not a close"):
        close_card(
            _card(),
            state=ApprovalState.PENDING,
            now=NOW,
            capabilities=_caps(),
            limiter=LimiterState(),
        )


# ============================================================ rendering (M10.1.5)


def test_a_body_that_dropped_the_payload_label_is_refused() -> None:
    """M10.1.5, item 12's half that lives outside the redaction module.

    `assert_can_send` asks whether the surface *can* render a label. Nothing in it looks at
    the string produced, so a template with nowhere to put the label satisfies it and
    delivers "here is an answer" where the payload said nobody checked this. This asks the
    string.

    Deleting this leaves the label rule enforced only against surfaces that admit they
    cannot carry one, and every surface says it can."""
    opaque = ChannelPayload(records=({"@entity": "x", "@id": "1"},), label=OPAQUE_LABEL)

    with pytest.raises(CardRefusedError, match="drops the payload label"):
        assert_label_survives("here is an answer", opaque)

    assert OPAQUE_LABEL in render_body(opaque)
    assert_label_survives(render_body(opaque), opaque)


def test_a_rendered_body_never_says_how_much_it_withheld() -> None:
    """M4.3.2 reaching the channel, where a count is easiest to add by accident.

    Locks render as `entity.field` and are deduplicated, so a reader cannot count the
    records a field was withheld on: a line per record is a hidden-item count assembled one
    line at a time. `RedactionTrace.withheld_field_names` deduplicates for the same reason.

    Deleting this lets a renderer helpfully add "and 3 more", which is the one fact the
    asker is not entitled to."""
    payload = ChannelPayload(
        records=(
            {"@entity": "client", "@id": "c_1", "name": "SNM"},
            {"@entity": "client", "@id": "c_2", "name": "Tomato"},
        ),
        locked=(
            LockedField(entity="client", record_id="c_1", field="margin"),
            LockedField(entity="client", record_id="c_2", field="margin"),
        ),
    )

    body = render_body(payload)
    # One lock line for two locked records: the lock says the field exists and refuses to
    # say on how many records it was withheld.
    assert body.count("client.margin") == 1
    assert body.count("Restricted") == 1
    assert "hidden" not in body.lower()
    assert "more" not in body.lower()


def test_a_payload_with_nothing_in_it_reads_the_same_whatever_emptied_it() -> None:
    """M4.3.3 reaching the channel. A refusal and an absence are the same event.

    The channel cannot tell the two apart and must not try: `ChannelPayload` has no field
    that says which happened, and the renderer has one sentence for both.

    Deleting this lets a renderer say "nothing you may see" for one and "no such client"
    for the other, which makes the set of records a person cannot reach enumerable by
    asking about each in turn."""
    assert render_body(ChannelPayload()) == NOTHING_TO_SAY
    assert render_body(ChannelPayload(records=())) == NOTHING_TO_SAY


def test_a_card_body_shows_the_record_and_names_the_decisions_in_words() -> None:
    """M10.2.3. The positive case for rendering, and the accessibility half of it.

    A surface that renders the card as a notification, a digest or a screen reader
    announcement shows the text and not the controls, and a decision nobody can see how to
    take reads as broken.

    Deleting this lets the body become buttons only, which is invisible everywhere the
    buttons are not."""
    body = render_card(_card())

    assert "susp_1" in body
    assert "SNM" in body
    assert "Approve" in body
    assert "Reject" in body
