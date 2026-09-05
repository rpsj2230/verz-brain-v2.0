"""An abstention must not become the third thing that tells DENIED from ABSENT.

`brain.core.errors` collapses DENIED into ABSENT on the message side. `brain.core.redaction`
does the same on the data side, by dropping a record rather than returning an empty husk of
it. This suite guards the third place the distinction could leak, which is the place a
helpful system leaks from: the sentence that explains why it could not answer.

The rest of the file guards the two properties that make an answer worth anything at all: a
claim with no citation is not stated, and a read time is stated rather than inferred. A
failure here blocks deploy.

Task ids: M8.1.3, M8.1.4, M8.2.1, M8.2.2, M8.2.3, M8.2.4, M8.3.3, M8.3.4, M8.3.5
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from brain.core.errors import BrainError, Degraded, Outcome
from brain.core.redaction import (
    OPAQUE_LABEL,
    ChannelPayload,
    LockedField,
    RedactedAnswer,
    RedactionTrace,
)
from brain.gate.abstain import (
    NOT_FOUND_TEXT,
    PUBLIC_TEXT,
    Abstention,
    AbstentionNotice,
    AbstentionReason,
    AutonomyBreaker,
    CitationPolicy,
    EscalationNotice,
    EscalationRoute,
    EscalationTrigger,
    Handoff,
    SearchScope,
    TakeoverSignal,
    abstain_if_uncited,
    abstention_for_search,
    not_entitled,
    nothing_retrieved,
    raise_escalation,
    scope_of_reach,
)
from brain.gate.compose import compose
from brain.gate.context import Channel
from brain.gate.provenance import (
    DEFAULT_HORIZON,
    Anchor,
    DocumentCitation,
    Freshness,
    Provenance,
    RetrievalTrace,
    provenance_for,
)
from brain.models.routing import FALLBACK_TRIGGER_VALUES, may_fall_back

pytestmark = pytest.mark.invariant

NOW = datetime(2026, 9, 5, 14, 31, tzinfo=UTC)
TRACE = RedactionTrace(policy_epoch="epoch-a", ent_hash="ent-a")

#: What a person can reach. The same for both askers in the indistinguishability tests,
#: because the property is about one person asking two questions, not about two people.
REACH = scope_of_reach(["the helpdesk", "the maintenance portal"])


class NullSink:
    def emit(self, reference: str, payload: ChannelPayload, trace: RedactionTrace) -> None:
        return None


def a_payload(records: tuple[dict[str, object], ...]) -> ChannelPayload:
    return ChannelPayload(
        records=records,
        source="laravel" if records else "",
        fetched_at=NOW.isoformat() if records else "",
    )


A_RECORD: dict[str, object] = {
    "@entity": "client",
    "@id": "c_0447",
    "name": "SNM Construction",
}


# ------------------------------------- DENIED and ABSENT stay one event (M8.2.3)
def test_an_abstention_never_says_the_record_exists() -> None:
    """The rule the whole module is built around. A person refused because a record is
    theirs to see and a person told nothing exists must receive the same object, so that
    asking about a client they have no grant for cannot confirm the client is a client."""
    assert not_entitled(REACH).for_asker() == nothing_retrieved(REACH).for_asker()


def test_the_two_indistinguishable_reasons_share_one_string_by_identity() -> None:
    """Two literals that happen to match agree until somebody improves the wording of one,
    and the improvement is a permission leak in a diff that reads as copy editing."""
    assert PUBLIC_TEXT[AbstentionReason.NOT_ENTITLED] is NOT_FOUND_TEXT
    assert PUBLIC_TEXT[AbstentionReason.NOTHING_RETRIEVED] is NOT_FOUND_TEXT


def test_the_asker_facing_half_has_nowhere_to_put_a_reason() -> None:
    """Checked by reading the type rather than by trusting the code that builds it, the same
    way `render_lock` is checked by its signature. A notice with a reason field is one a
    channel adapter renders while trying to be helpful."""
    fields = {f.name for f in dataclasses.fields(AbstentionNotice)}
    assert fields == {"text", "scope"}


def test_every_field_of_a_payload_is_covered_by_the_sweep_below() -> None:
    """The sweep is only worth what its input space covers, and `ChannelPayload` is where a
    future discriminator would appear. A field added there and not enumerated in
    `every_payload` would leave a lever the sweep never pulls, so this fails first and says
    which one."""
    assert set(ChannelPayload.model_fields) == {
        "records",
        "locked",
        "label",
        "source",
        "fetched_at",
        "truncated",
    }


def every_payload() -> list[ChannelPayload]:
    """Every shape of payload that could be used to tell a refusal from an absence.

    `locked` is the one that matters and the one an earlier version of this sweep missed: a
    lock is present exactly when something was withheld from a record the caller may
    otherwise see, so a classifier reading it would have a working DENIED oracle. The rest
    are enumerated because they are the other levers the type offers.
    """
    lock = LockedField(entity="client", record_id="c_0447", field="contract_value")
    return [
        ChannelPayload(
            records=records,
            locked=locked,
            label=label,
            source=source,
            fetched_at=source and NOW.isoformat(),
            truncated=truncated,
        )
        for records in ((), (A_RECORD,))
        for locked in ((), (lock,))
        for label in ("", OPAQUE_LABEL)
        for source in ("", "laravel")
        for truncated in (False, True)
    ]


def test_no_search_outcome_can_ever_be_not_entitled() -> None:
    """The enforcement of M8.2.3 is an absence: there is no branch in the classifier that
    produces NOT_ENTITLED, because a withheld record is simply not in the payload it reads.
    Delete this and somebody adds the branch as a diagnostic improvement, keyed off whichever
    field of the payload happens to differ."""
    for payload in every_payload():
        for connected in (True, False):
            for answers in (True, False):
                outcome = abstention_for_search(
                    payload,
                    scope=REACH,
                    sources_connected=connected,
                    answers_the_question=answers,
                )
                assert outcome is None or outcome.reason is not AbstentionReason.NOT_ENTITLED


def test_an_empty_payload_abstains_identically_whatever_was_withheld() -> None:
    """The sharper half of the same rule. Two people asking the same question, one refused
    and one finding nothing, differ in the payload only by its locks, and the notice must not
    differ at all."""
    notices = {
        abstention_for_search(
            payload, scope=REACH, sources_connected=True, answers_the_question=True
        ).for_asker()  # type: ignore[union-attr]
        for payload in every_payload()
        if not payload.records
    }
    assert len(notices) == 1


def test_a_withheld_record_and_an_absent_one_produce_the_same_notice() -> None:
    """End to end through the classifier. Redaction drops a record nobody may see, so both
    cases arrive here as an empty payload and cannot be told apart afterwards."""
    withheld = abstention_for_search(
        a_payload(()), scope=REACH, sources_connected=True, answers_the_question=True
    )
    absent = abstention_for_search(
        a_payload(()), scope=REACH, sources_connected=True, answers_the_question=True
    )
    assert withheld is not None
    assert absent is not None
    assert withheld.for_asker() == absent.for_asker()


def test_a_scope_statement_does_not_change_with_what_was_found() -> None:
    """The scope is derived from the asker's reach, never from what ran. A statement that
    varied with the outcome would be readable by asking the same question twice, which is
    the oracle every other rule here exists to close."""
    found = scope_of_reach(["the helpdesk", "the maintenance portal"])
    nothing_found = scope_of_reach(["the helpdesk", "the maintenance portal"])
    assert found == nothing_found
    assert nothing_retrieved(found).for_asker() == not_entitled(nothing_found).for_asker()


def test_every_reason_has_a_public_sentence() -> None:
    """Exhaustive, in the spirit of `traffic_class_for`'s `assert_never`. A reason added
    without a sentence would render as whatever a `.get` default happened to be, and the
    default anybody reaches for first is the reason's own name."""
    assert set(PUBLIC_TEXT) == set(AbstentionReason)


def test_no_public_sentence_names_a_source_a_record_or_a_capability() -> None:
    """A sentence naming where it looked answers a question nobody may ask, and repeated
    with different guesses it enumerates the sources a person cannot reach."""
    for text in PUBLIC_TEXT.values():
        lowered = text.lower()
        assert "laravel" not in lowered
        assert "read:" not in lowered
        assert "capability" not in lowered
        assert "permission" not in lowered


# ------------------------------------ abstention is not an error, degraded is not it
def test_an_abstention_is_not_an_error() -> None:
    """Modelling a refusal as an error puts every honest "I do not know" on the same chart
    as an outage, which is how the metric gets a target, and the target is met by answering
    questions the system should have declined."""
    assert not issubclass(Abstention, BaseException)
    assert not isinstance(nothing_retrieved(REACH), BrainError)


def test_no_abstention_reason_collides_with_an_error_outcome() -> None:
    """Both are string enums, and a dashboard that groups by string would merge them. The
    disjointness is what keeps an abstention out of the outage count."""
    assert {r.value for r in AbstentionReason} & {o.value for o in Outcome} == set()


def test_a_degraded_source_is_not_an_abstention() -> None:
    """A source being unreachable is a partial answer that says what is missing. Conflating
    it with a refusal means an outage reads as a refusal, and then nobody fixes the outage.
    There is deliberately no constructor here that would produce one."""
    assert issubclass(Degraded, BrainError)
    assert Degraded.outcome is Outcome.DEGRADED
    assert "degraded" not in {r.value for r in AbstentionReason}
    assert "unreachable" not in " ".join(PUBLIC_TEXT.values()).lower()


# ------------------------------------------- a claim needs a citation (M8.2.4)
def test_an_uncited_claim_is_never_stated_as_an_answer() -> None:
    """Given a question it cannot ground, a model produces a fluent paragraph rather than a
    refusal, and the paragraph is about a company whose data the reader believes it read."""
    outcome = abstain_if_uncited(Provenance(), scope=REACH)
    assert outcome is not None
    assert outcome.for_asker().text == NOT_FOUND_TEXT


def test_an_agent_that_says_nothing_must_cite() -> None:
    """Default-deny. An agent nobody configured is an agent nobody thought about, and the
    honest behaviour there is to decline rather than to assert."""
    assert CitationPolicy().require_citation is True
    assert abstain_if_uncited(Provenance(), scope=REACH, policy=CitationPolicy()) is not None


# -------------------------------------- citations come from the trace (M8.1.4)
def test_a_citation_does_not_change_when_the_model_changes_its_words() -> None:
    """M8.1.4 as a data dependency rather than a rule: `provenance_for` never reads
    `answer.text`, so citation-shaped prose cannot become a citation. Delete this and the
    first "resolve the model's [1] markers" helper passes review."""
    payload = a_payload((A_RECORD,))
    redacted = RedactedAnswer(payload=payload, trace=TRACE)
    first = compose("SNM is active.", redacted, sink=NullSink())
    second = compose("SNM is active [1]. Sources: Finance Ledger p.4.", redacted, sink=NullSink())
    assert provenance_for(first, horizon=DEFAULT_HORIZON, now=NOW) == provenance_for(
        second, horizon=DEFAULT_HORIZON, now=NOW
    )


def test_provenance_has_nowhere_to_put_the_models_words() -> None:
    """Checked by reading the type. A field able to hold prose is a field somebody fills
    with the model's own account of where it got something."""
    assert {f.name for f in dataclasses.fields(Provenance)} == {"rows", "documents"}


# ---------------------------------------- freshness is stated, never inferred (M8.1.3)
def test_a_citation_with_no_recorded_read_time_is_never_current() -> None:
    """A stale number that looks live gets acted on. Every path that cannot date a value has
    to arrive at UNSTATED, including the one where the connector wrote a wall clock."""
    payload = ChannelPayload(records=(A_RECORD,), source="laravel", fetched_at="")
    answer = compose(
        "SNM is active.", RedactedAnswer(payload=payload, trace=TRACE), sink=NullSink()
    )
    provenance = provenance_for(answer, horizon=DEFAULT_HORIZON, now=NOW)
    assert provenance.rows
    for evidence in provenance.rows:
        assert evidence.freshness.state is Freshness.UNSTATED
        assert "current" not in evidence.freshness.render()


def test_an_answer_is_only_as_fresh_as_its_weakest_citation() -> None:
    """Reporting the best state present lets one fresh citation launder the rest, which is
    exactly how a stale figure ends up beside a live timestamp."""
    payload = a_payload((A_RECORD,))
    answer = compose(
        "SNM is active.", RedactedAnswer(payload=payload, trace=TRACE), sink=NullSink()
    )
    stale = RetrievalTrace(
        passages=(
            DocumentCitation(
                document_id="doc_18",
                title="Maintenance SOP",
                anchor=Anchor(chunk_id="chunk_7"),
                source="knowledge",
                fetched_at=(NOW - timedelta(days=30)).isoformat(),
            ),
        )
    )
    provenance = provenance_for(answer, horizon=DEFAULT_HORIZON, now=NOW, trace=stale)
    assert provenance.stalest() is Freshness.STALE


# --------------------------------------------- escalation names a route (M8.3.3)
def test_an_escalation_never_names_a_persons_availability() -> None:
    """ "Ask Wei Ling, she is online" leaks a presence signal and a reporting line to whoever
    forwards the message. Presence is protected by no capability in this system, so the only
    defence is that the asker-facing type has nowhere to carry it."""
    assert {f.name for f in dataclasses.fields(EscalationNotice)} == {"text"}
    route_fields = {f.name for f in dataclasses.fields(EscalationRoute)}
    assert route_fields == {"queue", "channel", "address"}
    for banned in ("assignee", "presence", "online", "available", "manager"):
        assert banned not in route_fields


def test_the_asker_is_told_the_queue_and_nothing_from_the_handoff() -> None:
    """The handoff carries the asker's own words and a delivery address. A notice built from
    the escalation rather than from a template would eventually carry both."""
    escalation = raise_escalation(
        trigger=EscalationTrigger.AUTHORED_STEP,
        route=EscalationRoute(queue="maintenance", channel=Channel.LARK, address="oc_secret"),
        handoff=Handoff(
            asker_id="u_weiling",
            question="How many hours are left on SNM's block?",
            tried=("client.lookup",),
        ),
        now=NOW,
    )
    text = escalation.for_asker().text
    assert "maintenance" in text
    assert "oc_secret" not in text
    assert "u_weiling" not in text
    assert "SNM" not in text


# ------------------------------------------ every escalation expires (M8.3.4)
def test_an_escalation_cannot_be_opened_without_an_expiry() -> None:
    """An escalation with no deadline is a question that silently stops existing: the asker
    was told a person would look, nobody did, and nothing turns that into an event."""
    with pytest.raises(ValueError, match="born expired"):
        raise_escalation(
            trigger=EscalationTrigger.ABSTENTION,
            route=EscalationRoute(queue="maintenance", channel=Channel.LARK),
            handoff=Handoff(asker_id="u_weiling", question="Anything?"),
            now=NOW,
            ttl=timedelta(0),
        )


def test_an_expired_escalation_produces_a_notice_and_never_an_answer() -> None:
    """Expiry reports that the route did not deliver. Anything else here would turn "nobody
    replied" into "there was nothing to find", which is a different and false statement."""
    escalation = raise_escalation(
        trigger=EscalationTrigger.ABSTENTION,
        route=EscalationRoute(queue="maintenance", channel=Channel.LARK),
        handoff=Handoff(asker_id="u_weiling", question="Anything?"),
        now=NOW,
    )
    notice = escalation.expiry_notice(NOW + timedelta(days=1))
    assert isinstance(notice, EscalationNotice)
    assert "unanswered" in notice.text


def test_escalation_has_no_trigger_for_a_model_changing_its_mind() -> None:
    """M8.3.1. A model-judged escalation fires on the easy question and stays quiet on the
    hard one, and no configuration anywhere would have changed either."""
    values = {t.value for t in EscalationTrigger}
    assert values == {"authored step", "abstention", "approval required"}


# ------------------------------ a takeover is not a provider fault (M8.3.5)
def test_a_takeover_is_never_a_provider_fallback_trigger() -> None:
    """The leaf says "fed to the circuit breaker", and the provider breaker is the wrong
    one: its trigger set is closed against judgements about content. Routing a takeover
    there would remove a healthy model from rotation for everybody because one agent's leash
    was set too long on one target."""
    assert "takeover" not in FALLBACK_TRIGGER_VALUES
    assert may_fall_back("takeover") is False
    breaker = AutonomyBreaker(agent_id="a_maintenance", target="ticket.update_status")
    assert not isinstance(breaker, BaseException)


def test_the_autonomy_breaker_only_ever_tightens() -> None:
    """A run nobody took over is not evidence that a person would have approved it, only
    that nobody was watching. A breaker with a success path would raise a rung on silence."""
    methods = {
        name
        for name in dir(AutonomyBreaker)
        if not name.startswith("_") and callable(getattr(AutonomyBreaker, name))
    }
    assert "record_success" not in methods
    assert methods == {"record", "recent", "is_open", "rung"}


def test_a_takeover_signal_carries_no_principal() -> None:
    """Who took over belongs in the ledger. Here it would be a per-person performance
    counter growing inside a safety mechanism, and the first use of it would not be safety."""
    fields = {f.name for f in dataclasses.fields(TakeoverSignal)}
    assert fields == {"agent_id", "target", "at"}


# ------------------------------------------------------- the scope statement type
def test_a_scope_statement_cannot_be_built_from_what_actually_ran() -> None:
    """Checked by signature, the way `render_lock` is. A second parameter for "what ran"
    would let a caller intersect the two in one line and reintroduce the outcome-dependence
    this type exists to remove."""
    import inspect

    parameters = list(inspect.signature(scope_of_reach).parameters)
    assert parameters == ["admissible"]
    assert {f.name for f in dataclasses.fields(SearchScope)} == {"covered"}


def test_the_shared_string_does_not_itself_disclose_anything() -> None:
    """Indistinguishability and discretion are two properties, and the tests above only
    cover the first.

    `test_an_abstention_never_says_the_record_exists` compares the two reasons against each
    other, and the identity test compares each against the shared constant. All three hold
    no matter what that constant says: editing `NOT_FOUND_TEXT` itself changes both sides at
    once, so "I could not find that" could become "that record exists and you may not see
    it" with every test still green. Found by mutating the constant rather than the mapping.
    """
    text = NOT_FOUND_TEXT.lower()
    for leak in (
        "exist",
        "permission",
        "not allowed",
        "denied",
        "restricted",
        "authoris",
        "authoriz",
        "access",
        "forbidden",
    ):
        assert leak not in text, f"the shared refusal says {leak!r}, which confirms a record"


def test_every_public_sentence_is_checked_for_the_same_words() -> None:
    """The constant is not the only string a person sees. A reason added later with a
    careless sentence would pass the test above by not being that constant."""
    for reason, sentence in PUBLIC_TEXT.items():
        lowered = sentence.lower()
        for leak in ("exist", "permission", "not allowed", "denied", "forbidden"):
            assert leak not in lowered, f"{reason} says {leak!r} to the asker"
