"""The mechanics of abstaining, of escalating, and of saying how old the evidence is.

These are the behaviours of `brain.gate.abstain` and `brain.gate.provenance`. The rules that
must never break live beside them in `tests/invariants/test_abstain_invariants.py`, because
most of them are really `brain.core.redaction`'s rules meeting a feature that would break
them by being helpful: a refusal and an absence are one event, and a system that explains
why it could not answer is the third place that distinction leaks.

Both modules are covered here rather than in two files, because the interesting behaviour
is the seam between them: provenance decides whether anything stands behind an answer, and
abstention decides what to do when nothing does.

Task ids: M8.1.1, M8.1.2, M8.1.3, M8.1.4, M8.2.1, M8.2.2, M8.2.3, M8.2.4, M8.3.1, M8.3.2,
M8.3.3, M8.3.4, M8.3.5
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.core.redaction import ChannelPayload, RedactedAnswer, RedactionTrace
from brain.gate.abstain import (
    DEFAULT_ESCALATION_TTL,
    NOT_FOUND_TEXT,
    TAKEOVER_WINDOW,
    Abstention,
    AbstentionNotice,
    AbstentionReason,
    AutonomyBreaker,
    CitationPolicy,
    Escalation,
    EscalationNotice,
    EscalationRoute,
    EscalationState,
    EscalationTrigger,
    Handoff,
    SearchScope,
    TakeoverSignal,
    abstain_if_uncited,
    abstention_for_search,
    nothing_retrieved,
    raise_escalation,
    refused,
    scope_of_reach,
)
from brain.gate.compose import ComposedAnswer, compose
from brain.gate.context import Channel
from brain.gate.injection import AutonomyTier
from brain.gate.provenance import (
    DEFAULT_HORIZON,
    Anchor,
    DocumentCitation,
    Freshness,
    ModelAuthoredCitationError,
    Provenance,
    RetrievalTrace,
    StalenessHorizon,
    assert_derived,
    assert_rows_derived,
    provenance_for,
    read_time,
    state_freshness,
)

NOW = datetime(2026, 9, 5, 14, 31, tzinfo=UTC)
TRACE = RedactionTrace(policy_epoch="epoch-a", ent_hash="ent-a")


class NullSink:
    """A sink that discards. Composition emits a trace and these tests are not about that."""

    def emit(self, reference: str, payload: ChannelPayload, trace: RedactionTrace) -> None:
        return None


A_CLIENT: dict[str, object] = {
    "@entity": "client",
    "@id": "c_0447",
    "name": "SNM Construction",
    "status": "active",
}


def a_payload(records: tuple[dict[str, object], ...] = (A_CLIENT,)) -> ChannelPayload:
    """One post-redaction payload. `source` and `fetched_at` follow the redactor's own rule
    of being suppressed when nothing survived, so a fixture cannot assert on a shape the
    redactor never produces."""
    return ChannelPayload(
        records=records,
        source="laravel" if records else "",
        fetched_at=NOW.isoformat() if records else "",
    )


def an_answer(
    text: str = "SNM is active.", records: tuple[dict[str, object], ...] = (A_CLIENT,)
) -> ComposedAnswer:
    return compose(text, RedactedAnswer(payload=a_payload(records), trace=TRACE), sink=NullSink())


def a_passage(chunk: str = "chunk_7", *, fetched_at: str | None = None) -> DocumentCitation:
    return DocumentCitation(
        document_id="doc_18",
        title="Maintenance SOP",
        anchor=Anchor(chunk_id=chunk, page=4, section="3.2", start=120, end=480),
        source="knowledge",
        fetched_at=NOW.isoformat() if fetched_at is None else fetched_at,
    )


def a_handoff() -> Handoff:
    return Handoff(
        asker_id="u_weiling",
        question="How many hours are left on SNM's block?",
        tried=("client.lookup", "ticket.search"),
        needed="Somebody with the maintenance portal open.",
        trace_ref="abc123",
    )


def a_route(queue: str = "maintenance") -> EscalationRoute:
    return EscalationRoute(queue=queue, channel=Channel.LARK, address="oc_maintenance")


# ----------------------------------------------------------------- freshness (M8.1.3)
def test_a_read_time_that_is_not_a_date_is_not_a_read_time() -> None:
    """`fetched_at` is a bare string with no format contract, so connectors put wall clocks
    in it. Without this, "14:31" would be parsed against today and a value fetched last
    Tuesday would carry a timestamp from this afternoon."""
    assert read_time("14:31") is None
    assert read_time("") is None
    assert read_time("yesterday") is None


def test_a_naive_read_time_is_not_stated() -> None:
    """Singapore reads a naive UTC timestamp as eight hours old, which is the difference
    between LIVE and AGEING for every answer in the building. Delete this and the offset
    becomes a silent freshness error rather than a missing state."""
    assert read_time("2026-09-05T14:31:00") is None
    assert read_time("2026-09-05T14:31:00+00:00") == NOW


def test_freshness_is_live_inside_the_live_window() -> None:
    """The base case. Without it nothing proves the horizon is consulted at all, and every
    other freshness test would still pass against a function that always said UNSTATED."""
    ten_minutes_ago = (NOW - timedelta(minutes=10)).isoformat()
    stated = state_freshness(ten_minutes_ago, horizon=DEFAULT_HORIZON, now=NOW)
    assert stated.state is Freshness.LIVE


def test_freshness_is_ageing_between_the_two_thresholds() -> None:
    """The middle band is why freshness is four states and not a boolean. Delete this and
    an inverted horizon, which makes the band unreachable, passes every other test."""
    two_hours_ago = (NOW - timedelta(hours=2)).isoformat()
    stated = state_freshness(two_hours_ago, horizon=DEFAULT_HORIZON, now=NOW)
    assert stated.state is Freshness.AGEING


def test_freshness_is_stale_past_the_stale_threshold() -> None:
    """The state the whole module exists for. A stale number that reads as live gets acted
    on, and this is the assertion that it is labelled instead."""
    last_week = (NOW - timedelta(days=7)).isoformat()
    stated = state_freshness(last_week, horizon=DEFAULT_HORIZON, now=NOW)
    assert stated.state is Freshness.STALE
    assert "out of date" in stated.render()


def test_a_read_time_in_the_future_is_not_evidence_of_freshness() -> None:
    """Clock skew is the one condition under which "definitely current" is exactly the claim
    we cannot make. Without this a misconfigured connector becomes the freshest source in
    the company."""
    tomorrow = (NOW + timedelta(days=1)).isoformat()
    assert state_freshness(tomorrow, horizon=DEFAULT_HORIZON, now=NOW).state is Freshness.UNSTATED


def test_an_unstated_freshness_does_not_echo_the_string_it_could_not_date() -> None:
    """Repeating "14:31" back while admitting we cannot date it is the same inference in
    politer words, and a person reading it takes it for a read time."""
    stated = state_freshness("14:31", horizon=DEFAULT_HORIZON, now=NOW)
    assert stated.state is Freshness.UNSTATED
    assert "14:31" not in stated.render()


def test_a_horizon_whose_bands_are_inverted_is_refused() -> None:
    """An inverted horizon has an empty ageing band, so freshness silently collapses to a
    boolean and nobody notices until somebody asks why nothing is ever ageing."""
    with pytest.raises(ValueError, match="ageing band"):
        StalenessHorizon(live_for=timedelta(hours=2), stale_after=timedelta(minutes=15))


def test_a_naive_now_is_refused_rather_than_absorbed() -> None:
    """Absorbing it would mark every citation UNSTATED, which reads in the console as a
    connector problem and sends somebody to the wrong system."""
    with pytest.raises(ValueError, match="timezone-aware"):
        state_freshness(NOW.isoformat(), horizon=DEFAULT_HORIZON, now=datetime(2026, 9, 5))


# ------------------------------------------------------ document citations (M8.1.2)
def test_a_document_citation_locates_a_passage_by_position() -> None:
    """A citation to a document rather than a passage is one nobody follows, which is the
    same as having none while looking like diligence."""
    fragment = a_passage().anchor.fragment()
    assert fragment == "chunk=chunk_7&page=4&chars=120-480"


def test_a_document_citation_carries_no_passage_text() -> None:
    """A citation holding the content is a second copy of the answer travelling under a
    different name, and it survives into traces and forwarded messages the payload does
    not reach."""
    rendered = a_passage().render()
    assert "Maintenance SOP" in rendered
    assert "page 4" in rendered
    assert ":~:text=" not in a_passage().anchor.fragment()


def test_half_a_character_span_is_refused() -> None:
    """Half a span renders as a location and resolves to nothing, so the citation looks
    followable and is not."""
    with pytest.raises(ValueError, match="both ends or neither"):
        Anchor(chunk_id="chunk_7", start=120)


def test_an_anchor_falls_back_to_the_chunk_when_there_are_no_human_coordinates() -> None:
    """A parsed document without pages still has to cite something followable, and the chunk
    id is what the index can actually resolve."""
    assert Anchor(chunk_id="chunk_7").describe() == "chunk chunk_7"


# ----------------------------------------------------- assembling provenance (M8.1.4)
def test_provenance_carries_one_evidence_per_surviving_field() -> None:
    """Row citations are the composer's, and this is what proves provenance builds on them
    rather than deriving a second, differently-shaped opinion beside them."""
    provenance = provenance_for(an_answer(), horizon=DEFAULT_HORIZON, now=NOW)
    assert len(provenance.rows) == 2
    assert provenance.stalest() is Freshness.LIVE


def test_a_document_citation_the_trace_does_not_hold_is_refused() -> None:
    """The model produces citation-shaped text, which fails in the direction nobody checks:
    a real document with a plausible page number that does not contain the claim."""
    trace = RetrievalTrace(passages=(a_passage(),))
    with pytest.raises(ModelAuthoredCitationError, match="does not hold"):
        assert_derived([a_passage(chunk="chunk_99")], trace=trace)


def test_a_citation_reconstructed_from_the_trace_is_admitted() -> None:
    """Compared by value, not identity. A citation identical to one the trace holds names a
    passage that was genuinely retrieved, which is the property; refusing it would only
    force callers to pass objects around by reference."""
    assert_derived([a_passage()], trace=RetrievalTrace(passages=(a_passage(),)))


def test_a_row_citation_naming_a_field_the_answer_lacks_is_refused() -> None:
    """The row half of the same rule, checked against the composer's own derivation rather
    than against a recomputation that could disagree with it."""
    answer = an_answer()
    other = an_answer(records=({"@entity": "client", "@id": "c_9999", "margin": "31%"},))
    with pytest.raises(ModelAuthoredCitationError, match="does not contain"):
        assert_rows_derived(other.citations, answer=answer)


def test_the_stalest_citation_is_the_one_the_answer_may_claim() -> None:
    """An answer built from a live row and a stale document is a stale answer. Reporting the
    best state present lets one fresh citation launder the rest."""
    old = RetrievalTrace(passages=(a_passage(fetched_at=(NOW - timedelta(days=9)).isoformat()),))
    provenance = provenance_for(an_answer(), horizon=DEFAULT_HORIZON, now=NOW, trace=old)
    assert provenance.stalest() is Freshness.STALE


def test_an_undatable_citation_outranks_a_stale_one() -> None:
    """ "We do not know how old this is" is a weaker position than "we know it is old", and a
    caller comparing states must not be able to treat the unknown as merely dated."""
    unknown = RetrievalTrace(passages=(a_passage(fetched_at="14:31"),))
    provenance = provenance_for(an_answer(), horizon=DEFAULT_HORIZON, now=NOW, trace=unknown)
    assert provenance.stalest() is Freshness.UNSTATED


# ---------------------------------------------------------- the four states (M8.2.1)
def test_an_empty_payload_abstains_as_nothing_retrieved() -> None:
    """The commonest abstention, and the one every other state must be distinguishable from
    internally while being identical externally."""
    outcome = abstention_for_search(
        a_payload(()),
        scope=SearchScope(),
        sources_connected=True,
        answers_the_question=True,
    )
    assert outcome is not None
    assert outcome.reason is AbstentionReason.NOTHING_RETRIEVED


def test_records_that_do_not_bear_on_the_question_abstain_separately() -> None:
    """Without a distinct state, "I found nothing" and "I found things that do not answer
    you" collapse, and the second one is a retrieval problem somebody could fix."""
    outcome = abstention_for_search(
        a_payload(),
        scope=SearchScope(),
        sources_connected=True,
        answers_the_question=False,
    )
    assert outcome is not None
    assert outcome.reason is AbstentionReason.RETRIEVED_BUT_NOT_ANSWERING


def test_nothing_connected_is_reported_before_nothing_retrieved() -> None:
    """Ordering is meaning. Reporting an unconfigured connector as nothing-found sends
    somebody hunting for a record in a system nobody has connected."""
    outcome = abstention_for_search(
        a_payload(()),
        scope=SearchScope(),
        sources_connected=False,
        answers_the_question=True,
    )
    assert outcome is not None
    assert outcome.reason is AbstentionReason.NOTHING_CONNECTED


def test_a_search_that_answered_produces_no_abstention() -> None:
    """The negative case. Delete it and a classifier that abstained from everything would
    pass every other test in this section."""
    assert (
        abstention_for_search(
            a_payload(),
            scope=SearchScope(),
            sources_connected=True,
            answers_the_question=True,
        )
        is None
    )


def test_a_content_policy_refusal_is_an_abstention_with_its_own_sentence() -> None:
    """The 5 September decision. A refusal is answered once, honestly, rather than being
    reproduced on the next rung at full cost or shopped until a provider says yes."""
    outcome = refused(SearchScope(), detail="provider finish_reason=content_filter")
    assert outcome.reason is AbstentionReason.REFUSED
    assert outcome.for_asker().text == "I will not answer that."


# --------------------------------------------------------- the scope statement (M8.2.2)
def test_a_scope_statement_names_the_sources_in_reach() -> None:
    """Without a scope statement a refusal is unfalsifiable: the asker cannot tell whether
    the system looked in the place they meant."""
    scope = scope_of_reach(["the helpdesk", "the maintenance portal"])
    assert scope.render() == "This covers the helpdesk and the maintenance portal."


def test_a_single_source_reads_as_a_sentence_rather_than_a_list() -> None:
    """A statement that reads as machine output is one people stop reading, and this one is
    shown at the exact moment somebody is already unhappy."""
    assert scope_of_reach(["the helpdesk"]).render() == "This covers the helpdesk."


def test_an_asker_with_no_reachable_source_is_told_nothing_extra() -> None:
    """ "This covers nothing" is a sentence about the asker's own account that reads, in the
    moment they were refused, as the explanation for the refusal."""
    assert scope_of_reach([]).render() == ""


def test_a_scope_statement_is_sorted_so_configuration_order_cannot_be_read_from_it() -> None:
    """The order sources happen to be configured in is an operational fact, and it would be
    readable from the sentence by anybody who asked twice."""
    assert scope_of_reach(["xero", "the helpdesk", "xero"]).covered == ("the helpdesk", "xero")


# ------------------------------------------------------- the citation rule (M8.2.4)
def test_an_answer_with_nothing_behind_it_abstains() -> None:
    """The rule this module exists to enforce. Without it the model's fluent paragraph about
    a company's data is returned as though somebody had read the data."""
    outcome = abstain_if_uncited(Provenance(), scope=SearchScope())
    assert outcome is not None
    assert outcome.reason is AbstentionReason.NOTHING_RETRIEVED


def test_an_agent_may_be_configured_not_to_require_a_citation() -> None:
    """M8.2.4 asks for this per agent: a drafting agent has nothing to cite. Delete it and
    the requirement becomes global, which is how it gets switched off globally."""
    policy = CitationPolicy(require_citation=False)
    assert abstain_if_uncited(Provenance(), scope=SearchScope(), policy=policy) is None


def test_requiring_a_citation_is_what_an_agent_gets_by_saying_nothing() -> None:
    """Default-deny, the same principle as an unclassified field. An agent nobody has
    configured is an agent nobody has thought about."""
    assert CitationPolicy().require_citation is True


def test_a_grounded_answer_is_not_abstained_from() -> None:
    """The negative case, without which a check that abstained from everything would pass."""
    provenance = provenance_for(an_answer(), horizon=DEFAULT_HORIZON, now=NOW)
    assert abstain_if_uncited(provenance, scope=SearchScope()) is None


# ------------------------------------------------------------- escalation (M8.3.x)
def test_an_escalation_names_a_queue_and_the_channel_that_queue_reads() -> None:
    """M8.3.3. Delivery into somebody's own channel is what makes the handoff arrive; a
    notice posted where nobody looks is an escalation that silently expires."""
    escalation = raise_escalation(
        trigger=EscalationTrigger.AUTHORED_STEP,
        route=a_route(),
        handoff=a_handoff(),
        now=NOW,
    )
    assert escalation.route.queue == "maintenance"
    assert escalation.route.channel is Channel.LARK
    assert escalation.for_asker().text == "This needs a person from maintenance."


def test_a_handoff_carries_who_asked_what_was_tried_and_what_is_needed() -> None:
    """M8.3.2. A handoff stripped to a capability name is one nobody can judge, and the
    person picking it up is deciding whether to spend twenty minutes on it."""
    handoff = a_handoff()
    assert handoff.asker_id == "u_weiling"
    assert handoff.tried == ("client.lookup", "ticket.search")
    assert handoff.needed


def test_what_was_tried_must_be_step_names_and_not_prose() -> None:
    """Free text there becomes "fetched SNM's contract value = 240000" within a month, and a
    handoff crosses an entitlement boundary the gate has just enforced."""
    with pytest.raises(ValueError, match="step names"):
        Handoff(
            asker_id="u_weiling",
            question="How many hours are left?",
            tried=("looked up SNM Construction and found 12 hours",),
        )


def test_an_escalation_that_would_be_born_expired_is_refused() -> None:
    """An escalation with a deadline already past is never picked up and never reported, so
    the asker waits for an answer nothing will produce."""
    with pytest.raises(ValueError, match="born expired"):
        Escalation(
            trigger=EscalationTrigger.ABSTENTION,
            route=a_route(),
            handoff=a_handoff(),
            raised_at=NOW,
            expires_at=NOW,
        )


def test_an_escalation_is_open_until_its_deadline_and_expired_after() -> None:
    """M8.3.4. The timeout is what turns "nobody picked it up" into an event; without it the
    question silently stops existing."""
    escalation = raise_escalation(
        trigger=EscalationTrigger.ABSTENTION, route=a_route(), handoff=a_handoff(), now=NOW
    )
    assert escalation.state_at(NOW + DEFAULT_ESCALATION_TTL - timedelta(minutes=1)) is (
        EscalationState.OPEN
    )
    assert escalation.state_at(NOW + DEFAULT_ESCALATION_TTL) is EscalationState.EXPIRED


def test_an_expired_escalation_tells_the_asker_the_queue_did_not_reply() -> None:
    """Expiry names the route that did not deliver. Manufacturing an answer here, or
    quietly changing the reason, turns "nobody replied" into "there was nothing to find"."""
    escalation = raise_escalation(
        trigger=EscalationTrigger.ABSTENTION, route=a_route(), handoff=a_handoff(), now=NOW
    )
    assert escalation.expiry_notice(NOW) is None
    notice = escalation.expiry_notice(NOW + timedelta(days=1))
    assert notice == EscalationNotice(
        text="Nobody from maintenance has picked this up, so it is unanswered."
    )


def test_a_queue_that_is_not_a_route_name_is_refused() -> None:
    """A queue is an audited route. A free-text one cannot be looked up, cannot be reported
    on, and is where a sentence about a person ends up."""
    with pytest.raises(ValueError, match="not a route name"):
        EscalationRoute(queue="ask whoever is around", channel=Channel.LARK)


# -------------------------------------------------------- the takeover signal (M8.3.5)
def a_breaker() -> AutonomyBreaker:
    return AutonomyBreaker(agent_id="a_maintenance", target="ticket.update_status")


def a_takeover(at: datetime) -> TakeoverSignal:
    return TakeoverSignal(agent_id="a_maintenance", target="ticket.update_status", at=at)


def test_three_takeovers_inside_the_window_lower_the_rung() -> None:
    """M8.3.5. One takeover is somebody preferring their own wording; three on one target is
    the leash being set too long, which nothing else in the system would notice."""
    breaker = a_breaker()
    for days in (0, 1, 2):
        breaker = breaker.record(a_takeover(NOW - timedelta(days=days)))
    assert breaker.is_open(NOW) is True
    assert breaker.rung(AutonomyTier.AUTONOMOUS, NOW) is AutonomyTier.ASSISTED


def test_two_takeovers_leave_the_rung_alone() -> None:
    """The threshold has to bite somewhere above one, or every agent ends up in shadow the
    first time a person edits a draft."""
    breaker = a_breaker().record(a_takeover(NOW)).record(a_takeover(NOW - timedelta(days=1)))
    assert breaker.is_open(NOW) is False
    assert breaker.rung(AutonomyTier.AUTONOMOUS, NOW) is AutonomyTier.AUTONOMOUS


def test_takeovers_older_than_the_window_stop_counting() -> None:
    """An agent taken over three times in March and never since is an agent that was fixed.
    A counter with no window keeps punishing it forever, so somebody removes the counter."""
    breaker = a_breaker()
    for days in (0, 1, 2):
        breaker = breaker.record(a_takeover(NOW - timedelta(days=days)))
    assert breaker.is_open(NOW + TAKEOVER_WINDOW) is False


def test_the_rung_never_goes_below_shadow() -> None:
    """SHADOW is already the fail-closed rung for an agent nobody configured. Below it there
    is nothing, and an enum built by arithmetic would raise on the way there."""
    breaker = a_breaker()
    for days in (0, 1, 2):
        breaker = breaker.record(a_takeover(NOW - timedelta(days=days)))
    assert breaker.rung(AutonomyTier.SHADOW, NOW) is AutonomyTier.SHADOW


def test_a_signal_for_another_agent_is_refused() -> None:
    """A breaker that absorbed a foreign signal would demote the wrong agent, and the
    demotion looks in the console exactly like a correct one."""
    stray = TakeoverSignal(agent_id="a_finance", target="ticket.update_status", at=NOW)
    with pytest.raises(ValueError, match="wrong agent"):
        a_breaker().record(stray)


def test_a_naive_takeover_timestamp_is_refused() -> None:
    """A naive timestamp lands in the wrong window, so a takeover from last week counts as
    today's and demotes an agent nobody has taken over."""
    with pytest.raises(ValueError, match="timezone-aware"):
        TakeoverSignal(
            agent_id="a_maintenance", target="ticket.update_status", at=datetime(2026, 9, 5)
        )


# ------------------------------------------------------------- the notice itself
def test_an_abstention_notice_renders_the_scope_beside_the_sentence() -> None:
    """The scope statement is the only part of an abstention that is falsifiable, so it has
    to actually reach the person."""
    notice = nothing_retrieved(scope_of_reach(["the helpdesk"])).for_asker()
    assert notice.render() == f"{NOT_FOUND_TEXT} This covers the helpdesk."


def test_an_abstention_with_no_scope_renders_as_one_sentence() -> None:
    """A trailing space or an empty clause is how a person notices the system is assembling
    sentences out of parts, which is when they stop trusting the parts."""
    assert AbstentionNotice(text=NOT_FOUND_TEXT).render() == NOT_FOUND_TEXT


def test_the_reason_stays_on_the_internal_half() -> None:
    """The reason is for the trace and the ledger. This is the assertion that it is carried
    at all, so the invariant that it is never rendered has something to be about."""
    abstention = Abstention(
        reason=AbstentionReason.NOT_ENTITLED, detail="denied on read:client.contract_value"
    )
    assert abstention.reason is AbstentionReason.NOT_ENTITLED
    assert abstention.detail
