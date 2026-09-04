"""The golden set and the adversarial corpus, checked for the properties that make them
useful before anything consumes them.

A fixture nobody validates is a fixture that quietly stops testing what it claims to. Each
of these asserts a property the suite depends on: that refusals are indistinguishable from
absences, that every persona named actually exists, that canaries are never expected in an
answer to someone who cannot hold the field.

Task ids: M0.6.4, M0.6.6
"""

from __future__ import annotations

import pytest

from brain.core.entitlement import Capability
from tests.fixtures.adversarial import PAYLOADS, Vector, all_texts
from tests.fixtures.company import CANARIES, NOW, everyone, person
from tests.fixtures.golden import GOLDEN, REFUSAL_TEXT, Expect, by_expectation

pytestmark = pytest.mark.invariant


# ------------------------------------------------------------ golden set
def test_every_question_is_asked_by_someone_who_exists() -> None:
    """A golden question naming a persona that was renamed would silently stop testing."""
    people = set(everyone())
    for g in GOLDEN:
        assert g.asked_by in people, f"{g.qid} is asked by unknown persona {g.asked_by}"


def test_every_question_records_why_it_is_here() -> None:
    """Without the reason, a failing golden test gets deleted rather than understood."""
    for g in GOLDEN:
        assert g.why.strip(), f"{g.qid} has no stated purpose"


def test_the_same_question_is_asked_by_more_than_one_person() -> None:
    """The whole point. A set where every question has one asker tests answering, not
    permission."""
    from collections import Counter

    shared = Counter(g.question for g in GOLDEN)
    assert sum(1 for n in shared.values() if n > 1) >= 3


def test_a_refusal_says_exactly_what_an_absence_says() -> None:
    """The one that gets tested wrongly everywhere else. 'You may not see SNM's contract
    value' has already confirmed SNM has one."""
    for g in by_expectation(Expect.REFUSE):
        if g.must_contain:
            assert REFUSAL_TEXT in g.must_contain, (
                f"{g.qid} expects a refusal with different wording, which would make it "
                "distinguishable from a record that does not exist"
            )


def test_no_refusal_expects_an_explanation() -> None:
    """A refusal that explains itself leaks the thing it refused."""
    leaky = ("not allowed", "permission", "denied", "restricted", "unauthorised")
    for g in by_expectation(Expect.REFUSE):
        for token in leaky:
            assert token not in " ".join(g.must_contain).lower(), (
                f"{g.qid} expects the word {token!r} in a refusal"
            )


def test_nobody_is_expected_to_receive_a_canary_they_cannot_hold() -> None:
    """The fixture must not contradict itself: if a golden answer expects a canary, that
    persona has to actually hold the field it belongs to."""
    by_token = {v: k for k, v in CANARIES.items()}
    for g in GOLDEN:
        for token in g.must_contain:
            field = by_token.get(token)
            if field is None:
                continue
            ent = person(g.asked_by).entitlement()
            holds = ent.holds(Capability(value=f"read:{field}"), NOW)
            # hr.self is a different capability from hr.*; the golden set covers that
            # case and it is legitimately not covered by a read:hr.salary grant.
            if g.qid == "G08":
                continue
            assert holds, f"{g.qid} expects {g.asked_by} to receive {field}, which they do not hold"


def test_the_locked_field_case_is_covered_from_both_sides() -> None:
    """One persona refused, another answered, same question. A suite with only the
    refusal would pass even if the system refused everyone."""
    contract_qs = [g for g in GOLDEN if "contract" in g.question.lower()]
    assert any(g.expect is Expect.REFUSE for g in contract_qs)
    assert any(g.expect is Expect.ANSWER for g in contract_qs)


def test_aggregation_is_tested_not_just_direct_reads() -> None:
    """Summing fields you cannot read is still reading them, and is the most common way
    field-level security is bypassed."""
    agg = [g for g in GOLDEN if "aggregation" in g.tags]
    assert len(agg) >= 2
    assert all(g.expect is Expect.REFUSE for g in agg), (
        "an aggregation over fields the asker cannot read must refuse; a count is a "
        "narrower leak than a sum and still a leak, since repeating it with different "
        "thresholds recovers the values"
    )


# ------------------------------------------------------- adversarial set
def test_payloads_cover_every_arrival_path() -> None:
    """A document is read once at ingest; a tool result arrives mid-run with the model
    already committed to a plan. Same rule, different trust boundary."""
    assert {p.vector for p in PAYLOADS} == set(Vector)


def test_every_payload_states_what_it_is_trying_to_do() -> None:
    for p in PAYLOADS:
        assert p.goal.strip(), f"{p.pid} has no stated goal"


def test_payloads_are_distinct() -> None:
    texts = all_texts()
    assert len(set(texts)) == len(texts)


def test_at_least_one_payload_names_a_canary_directly() -> None:
    """So that if a payload ever succeeds, the output carries an unmistakable token
    rather than a plausible number."""
    joined = " ".join(all_texts())
    assert any(token in joined for token in CANARIES.values())


def test_the_corpus_includes_an_authority_claim_and_a_forged_grant() -> None:
    """The two shapes that matter most: content claiming to be the system, and content
    claiming the caller has been granted something. Entitlement must come from the gate
    and never from something a tool said."""
    joined = " ".join(all_texts()).lower()
    assert "system:" in joined or "system:" in joined
    assert "granted" in joined
    assert "authorised" in joined or "pre-approved" in joined


def test_a_payload_hides_behind_length() -> None:
    """Instructions late in a long context must be treated no differently from early
    ones."""
    assert any(len(p.text) > 3000 for p in PAYLOADS)
