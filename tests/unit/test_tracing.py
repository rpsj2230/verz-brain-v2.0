"""Span masking, the environment vocabulary, and the two rules about payloads.

Every test here is about a trace store that would end up holding the thing the rest of the
system spent a wave withholding.

Task ids: M32.1.1.3, M32.1.2.1, M32.1.2.2, M32.1.2.3, M32.1.2.4, M32.1.2.5
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from brain.config import REQUIRED
from brain.identity.roles import Role
from brain.ops.tracing import (
    PAYLOAD_ROLE,
    RETENTION,
    SAFE_ATTRIBUTES,
    SAFE_VALUE_MAX_CHARS,
    TRACE_ENVIRONMENTS,
    PayloadRead,
    Retention,
    Span,
    TraceRecord,
    TracingError,
    assert_environment,
    assert_environment_vocabulary,
    mask,
    mask_value,
    may_read_payloads,
    read_payload,
    retention_for,
    retention_gaps,
)

#: One string, planted everywhere, looked for everywhere. Chosen so that no legitimate
#: masking token could contain it by accident.
CANARY = "CANARY-8f3b1d-DO-NOT-EXPORT"

NOW = datetime(2026, 9, 5, 14, 0, tzinfo=UTC)


def _span(**overrides: object) -> Span:
    base: dict[str, object] = {
        "name": "answer",
        "environment": "production",
        "attributes": {"tool": "client.read_summary", "outcome": "denied", "latency_ms": 42},
        "payload_in": "who is the account manager for SNM",
        "payload_out": "Wei Ling",
    }
    base.update(overrides)
    return Span(**base)  # type: ignore[arg-type]


def _attribute(span: Span, key: str) -> str:
    """One masked attribute as text. `Span.attributes` is `Mapping[str, object]` because a
    latency is a number, so every assertion about a mask token would otherwise carry a cast."""
    return str(span.attributes[key])


# --------------------------------------------------- the canary (M32.1.2.2)
def test_no_canary_string_survives_masking_into_the_payload_store() -> None:
    """The assertion the whole module exists for. A canary is planted in the question, the
    answer, an attribute value, an attribute key and the span name, and the masked span is
    serialised exactly as it would be sent. Delete this and every other test here checks a
    rule while nothing checks the outcome the rules are for."""
    span = Span(
        name="answer",
        environment="production",
        attributes={
            "tool": CANARY,
            "principal_id": CANARY,
            f"client_{CANARY}": "x",
            "nested": {"deep": [CANARY]},
        },
        payload_in=CANARY,
        payload_out=f"prefix {CANARY} suffix",
    )
    wire = json.dumps(asdict(mask(span)), default=str)
    assert CANARY not in wire, wire


def test_a_canary_short_enough_to_pass_the_length_rule_still_does_not_survive() -> None:
    """The near miss. `tool` is on the allowlist and a short string under an allowlisted key
    is kept, so a canary planted there is the case where the allowlist itself is the leak.
    It is caught because the *value* is the canary, not because it is long. Delete this and
    shortening a canary in the test above would make that test pass for the wrong reason."""
    span = _span(attributes={"tool": "t"}, payload_in="t", payload_out=CANARY)
    assert CANARY not in json.dumps(asdict(mask(span)))


# --------------------------------------------------- masking (M32.1.2.1)
def test_the_payloads_are_masked_whatever_else_happens() -> None:
    """There is no argument that turns this off, and there must never be one: an option to
    send raw payloads gets set true during one debugging session and left. Delete this and
    a future keyword argument could default to sending them."""
    masked = mask(_span())
    assert masked.payload_in.startswith("[masked:")
    assert masked.payload_out.startswith("[masked:")


def test_an_attribute_that_is_not_on_the_allowlist_is_masked() -> None:
    """Default-deny. The alternative is a denylist, which is a list of the leaks somebody
    has already thought of. Delete this and an unrecognised key passes through, which is
    every key a new feature adds."""
    masked = mask(_span(attributes={"principal_id": "u_weiling"}))
    assert masked.attributes["principal_id"] == "[masked:str/small]"


def test_a_container_under_an_allowlisted_key_is_masked() -> None:
    """A dictionary is where somebody files a record while meaning to file a summary, and
    the key it goes under does not change what it is. Delete this and `outcome` becomes a
    hole wide enough for a result set."""
    masked = mask(_span(attributes={"outcome": {"record": "SNM", "value": 40000}}))
    assert masked.attributes["outcome"] == "[masked:dict/small]"


def test_a_long_string_under_an_allowlisted_key_is_masked() -> None:
    """Length is the only thing separating `denied` from a paragraph explaining who was
    denied what. Delete this and free text reaches the store through any allowlisted key."""
    masked = mask(_span(attributes={"outcome": "x" * (SAFE_VALUE_MAX_CHARS + 1)}))
    assert _attribute(masked, "outcome").startswith("[masked:str/")
    kept = mask(_span(attributes={"outcome": "x" * SAFE_VALUE_MAX_CHARS}))
    assert kept.attributes["outcome"] == "x" * SAFE_VALUE_MAX_CHARS


def test_an_attribute_key_that_is_not_system_shaped_is_dropped_entirely() -> None:
    """Keys are kept unmasked, so a key built by interpolating a client's name is the one
    channel the value mask does not cover. Dropped rather than masked, because a masked
    value under a leaked key still leaks the key. Delete this and `client_SNM_hours: 12`
    goes to the trace store with the number hidden and the client named."""
    masked = mask(_span(attributes={"client_SNM": "x", "tool": "client.read_summary"}))
    assert "client_SNM" not in masked.attributes
    assert masked.attributes["tool"] == "client.read_summary"


def test_free_text_under_an_allowlisted_key_is_masked_however_short_it_is() -> None:
    """The hole the canary test found. With only a length rule, `tool` accepted any short
    string, so the one key a caller is trusted with was the one place nothing was checked.
    Every real value in that set is a lowercase token; a person's name has a capital and a
    sentence has spaces. Delete this and `VALUE_TOKEN_RE` reads as pedantry and is
    removed."""
    assert _attribute(mask(_span(attributes={"outcome": "Wei Ling"})), "outcome").startswith(
        "[masked:"
    )
    assert _attribute(
        mask(_span(attributes={"outcome": "denied because SNM"})), "outcome"
    ).startswith("[masked:")


@pytest.mark.parametrize(
    "value",
    ["client.read_summary", "denied", "human_interactive", "read:client.name", "moonshot/kimi-k2"],
)
def test_real_system_vocabulary_still_reaches_the_trace(value: str) -> None:
    """A mask that kept nothing would pass every leak test in this file and make the trace
    ledger useless, which is how a control gets switched off. Delete this and the value
    grammar can be tightened until traces carry no diagnostic value at all."""
    assert mask(_span(attributes={"tool": value})).attributes["tool"] == value


def test_a_mask_token_reports_a_size_class_and_never_a_length() -> None:
    """An exact length is a small leak that compounds: nine characters is an NRIC and very
    little else, and a length repeated across spans narrows a value considerably. Delete
    this and somebody adds the length back because it is useful, which it is."""
    assert mask_value("x" * 9) == "[masked:str/small]"
    assert mask_value("x" * 63) == "[masked:str/small]"
    assert mask_value("") == "[masked:str/empty]"
    assert mask_value("x" * 2000) == "[masked:str/large]"


def test_masking_is_a_pure_function_and_leaves_the_original_span_alone() -> None:
    """The caller keeps the unmasked span, because it is still answering the request with
    it. A mask that mutated in place would strip the answer on its way to the person who
    asked. Delete this and `mask` could start editing the dictionary it was handed."""
    original = _span()
    mask(original)
    assert original.payload_out == "Wei Ling"
    assert original.attributes["tool"] == "client.read_summary"


def test_no_key_that_identifies_a_person_is_on_the_allowlist() -> None:
    """The allowlist is the whole security boundary, so its contents are asserted rather
    than reviewed. Delete this and `principal_id` can be added to it in a one-line diff that
    reads as an improvement to observability."""
    for forbidden in ("principal_id", "email", "user", "actor", "question", "answer", "prompt"):
        assert forbidden not in SAFE_ATTRIBUTES


# --------------------------------------------------- the environment vocabulary (M32.1.2.3)
def test_the_trace_environments_match_the_vocabulary_the_application_validates_against() -> None:
    """Langfuse fixes this set at first ingest and rejects unseen values afterwards, so a
    mismatch becomes permanent the first time a span arrives. There is no database enum to
    check against - `brain.config.REQUIRED` is the vocabulary that exists. Delete this and
    a fourth environment name is discovered when its traces silently stop arriving."""
    assert_environment_vocabulary()
    assert set(TRACE_ENVIRONMENTS) == set(REQUIRED)


def test_a_drifted_vocabulary_is_refused_and_says_why_it_matters() -> None:
    """The check must fail when it should, not merely pass when things are fine. Delete this
    and `assert_environment_vocabulary` could compare a set with itself."""
    with pytest.raises(TracingError, match="first ingest"):
        assert_environment_vocabulary(["development", "staging", "production", "sandbox"])


def test_an_unknown_environment_is_refused_rather_than_defaulted() -> None:
    """Defaulting to production files a laptop's traces beside a client's; defaulting to
    development hides a production incident in a view nobody opens during one. Delete this
    and a typo picks one of those silently."""
    with pytest.raises(TracingError, match="is not one of"):
        Span(name="answer", environment="prod")
    assert assert_environment("staging") == "staging"


# --------------------------------------------------- retention (M32.1.1.3)
@pytest.mark.parametrize("record", list(TraceRecord))
def test_every_kind_of_trace_record_has_a_window_and_a_reason(record: TraceRecord) -> None:
    """A trace ledger with no expiry becomes the longest-lived copy of the business: it
    outlives the records it describes, the permissions that governed them, and the people
    who could read them. Delete this and a fourth kind of record can be added with no
    retention at all, which in every system means the longest."""
    entry = retention_for(record)
    assert entry.days >= 1
    assert entry.because.strip()


def test_no_trace_record_is_kept_for_ever() -> None:
    """There is no unbounded option and there must not be one, because the argument for this
    whole module is that the ledger must not become the oldest copy of who asked what. Delete
    this and `days` can be given a None branch as an operator convenience."""
    assert all(isinstance(entry.days, int) and entry.days >= 1 for entry in RETENTION)


def test_a_retention_of_zero_days_is_refused() -> None:
    """Zero reads in an incident as the ledger being broken rather than as a policy, because
    the traces somebody came to look at are already gone. Delete this and a half-finished
    configuration deploys as one."""
    with pytest.raises(TracingError, match="not a window"):
        Retention(record=TraceRecord.TRACE, days=0, because="none given")


def test_a_retention_with_no_stated_reason_is_refused() -> None:
    """Same rule as the storage buckets, for the same reason: a window nobody can explain is
    extended the first time an investigation wants an older trace, and the extension is
    permanent because nobody knows what the number was protecting. Delete this and the next
    entry arrives with an empty string."""
    with pytest.raises(TracingError, match="states no reason"):
        Retention(record=TraceRecord.BLOB, days=7, because="  ")


def test_the_three_windows_nest_so_nothing_outlives_its_only_route_to_it() -> None:
    """An observation is reached through its trace and a blob through its observation.
    Outliving the parent is not extra safety, it is a bill for data with no route to it, and
    it is the failure nobody notices because the only symptom is the invoice. Delete this and
    the two nesting rules survive as a comment."""
    assert retention_gaps() == ()
    assert (
        retention_for(TraceRecord.BLOB).days
        <= retention_for(TraceRecord.OBSERVATION).days
        <= retention_for(TraceRecord.TRACE).days
    )


def test_an_observation_outliving_its_trace_is_reported() -> None:
    """Run against the declared set the nesting check passes with its body removed, so it is
    run here against windows that break it. Delete this and both nesting rules can be deleted
    with `retention_gaps()` still returning empty."""
    findings = retention_gaps(
        [
            Retention(record=TraceRecord.TRACE, days=7, because="short"),
            Retention(record=TraceRecord.OBSERVATION, days=30, because="longer than its parent"),
            Retention(record=TraceRecord.BLOB, days=1, because="short"),
        ]
    )
    assert any("outliving its trace" in f for f in findings), findings


def test_a_blob_outliving_its_observation_is_reported() -> None:
    """The second nesting rule, one level down: a blob is reached through the observation
    that points at it. Delete this and only the trace-observation rule is ever exercised, so
    the blob rule can be dropped without a failure."""
    findings = retention_gaps(
        [
            Retention(record=TraceRecord.TRACE, days=30, because="long"),
            Retention(record=TraceRecord.OBSERVATION, days=7, because="shorter"),
            Retention(record=TraceRecord.BLOB, days=14, because="longer than its parent"),
        ]
    )
    assert any("outliving the observation" in f for f in findings), findings


def test_a_kind_of_record_with_no_window_at_all_is_reported() -> None:
    """The closure rule. A kind of record nobody gave a window is a kind of record kept for
    ever by whatever the store's own default is, which is the failure this whole section
    argues against. Delete this and adding a fourth `TraceRecord` is a silent omission."""
    findings = retention_gaps([Retention(record=TraceRecord.TRACE, days=30, because="long")])
    assert any("no retention declared" in f for f in findings), findings


# --------------------------------------------------- the payload role (M32.1.2.4)
def test_payload_access_is_its_own_role_and_not_one_of_the_platform_roles() -> None:
    """Reading raw trace payloads is something an operator is granted for an afternoon, not
    something a person *is*. Adding it to `Role` would widen the compiled-in permission
    model and hand it to whoever already holds the nearest role. Delete this and the two
    vocabularies can collide without anybody noticing which one is being checked."""
    assert PAYLOAD_ROLE not in {r.value for r in Role}
    assert not may_read_payloads([r.value for r in Role])


def test_a_role_that_merely_contains_the_payload_role_does_not_grant_it() -> None:
    """Exact membership, never a prefix or substring. A client's own Keycloak administrator
    names groups on their own schedule, and a substring check hands them the ability to
    grant this by naming a group well. Delete this and `startswith` looks equivalent."""
    assert may_read_payloads([PAYLOAD_ROLE])
    assert not may_read_payloads([f"{PAYLOAD_ROLE}-readonly"])
    assert not may_read_payloads([f"view-{PAYLOAD_ROLE}"])
    assert not may_read_payloads([])


# --------------------------------------------------- the audit row (M32.1.2.5)
def test_the_audit_row_is_written_before_the_payload_is_fetched() -> None:
    """The order is the whole function. Fetching first gives an audit trail complete except
    for the reads that failed partway, which are the ones worth having a trail of. Delete
    this and swapping two lines produces code that passes every other test here."""
    order: list[str] = []
    read_payload(
        PayloadRead(at=NOW, actor="u_rupash", trace_id="t_1", reason="incident 2026-09-05"),
        lambda _e: order.append("recorded"),
        lambda: order.append("fetched") or "payload",  # type: ignore[func-returns-value]
    )
    assert order == ["recorded", "fetched"]


def test_a_payload_is_never_fetched_when_the_audit_row_cannot_be_written() -> None:
    """A payload store nobody can audit is a payload store nobody should be reading from.
    Delete this and a recorder wrapped in a try/except becomes an unaudited read path."""
    fetched: list[str] = []

    def refuse(_event: PayloadRead) -> None:
        msg = "ledger unavailable"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="ledger unavailable"):
        read_payload(
            PayloadRead(at=NOW, actor="u_rupash", trace_id="t_1", reason="incident"),
            refuse,
            lambda: fetched.append("x") or "payload",  # type: ignore[func-returns-value]
        )
    assert fetched == []


def test_a_payload_read_with_no_stated_reason_is_not_an_audit_row() -> None:
    """The question asked of this table six months later is always why. A row proving only
    that somebody looked cannot answer it. Delete this and the field becomes optional in
    practice, because the caller with nothing to say passes an empty string."""
    with pytest.raises(TracingError, match="no reason"):
        PayloadRead(at=NOW, actor="u_rupash", trace_id="t_1", reason="  ")


def test_a_payload_read_with_a_naive_timestamp_is_refused() -> None:
    """Two operators in two timezones produce a sequence that cannot be ordered, and the
    ordering is what the row is for. The same rule the deployment chain applies, for the
    same reason. Delete this and the failure appears only when somebody travels."""
    with pytest.raises(TracingError, match="no timezone"):
        PayloadRead(at=datetime(2026, 9, 5, 14, 0), actor="u", trace_id="t", reason="r")
