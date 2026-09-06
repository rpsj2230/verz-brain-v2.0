"""The catalogue templates, held to the properties they were written to demonstrate.

`tests/unit/test_agent_template.py` proves the template model is correct. These prove it is
*usable*, which is a different claim: six manifests written against real agency work, each
driven through the real `materialise` rather than asserted about as data.

Task ids: M13.5.7, M13.5.14, M13.5.16, M13.5.19, M13.5.21, M13.5.23
"""

from __future__ import annotations

import pytest

from brain.agents.catalogue import (
    CATALOGUE,
    PUBLISHER,
    accountant_agent,
    capacity_and_hours_analyst,
    catalogue_by_id,
    knowledge_gap_curator,
    sem_agent,
    support_ticket_agent,
    wordpress_developer,
)
from brain.agents.template import TemplateManifest, publish
from brain.core.envelope import SideEffect
from brain.gate.injection import AutonomyTier

IDS = [m.identity.template_id for m in CATALOGUE]


@pytest.mark.parametrize("manifest", CATALOGUE, ids=IDS)
def test_every_template_starts_on_the_bottom_rung(manifest: TemplateManifest) -> None:
    """**The property the whole catalogue is written to.** A rung says how far a stranger's
    install should trust an agent on its first day, and the person who wrote the template has
    never met that company.

    Asserted over every target rather than over the leash as a whole, because a leash that is
    SHADOW on two targets and ASSISTED on a third is exactly the shape somebody adds while
    making one case convenient.

    Read-only templates declare it too, and that is deliberate rather than redundant: a
    read-only agent that is wrong is still wrong in somebody's answer, and SHADOW is what
    makes the first few wrong answers visible.

    Delete this and a template ships pre-trusted, which is a decision made by its author
    about a company they have never seen."""
    assert manifest.guardrails.leash, f"{manifest.identity.template_id} names no target"
    for rung in manifest.guardrails.leash:
        assert rung.rung is AutonomyTier.SHADOW, (
            f"{manifest.identity.template_id} ships {rung.target} pre-trusted at {rung.rung}"
        )


@pytest.mark.parametrize("manifest", CATALOGUE, ids=IDS)
def test_every_template_materialises_into_a_real_agent(manifest: TemplateManifest) -> None:
    """**The claim this whole module exists to test.** The template model is known to be
    correct; whether it can express a useful agent is a separate question and the only way to
    answer it is to build some and run them through the real path.

    `materialise` constructs an `AgentRecord`, so every validator in `brain.agents.model`
    runs: the tier must be one the router can route, the persona must not be empty, the
    audience and the authority must be consistent. A manifest that satisfies the schema and
    not those is a manifest that would fail at install time.

    Delete this and the catalogue can drift into a set of documents that parse and cannot be
    installed, which is the state a fixture-only test suite would never notice."""
    from datetime import UTC, datetime

    from brain.agents.model import AgentAudience
    from brain.agents.template import TemplateInstance, content_digest, materialise
    from brain.knowledge.visibility import Visibility

    signed = publish(
        manifest,
        key="a-key-for-this-test",
        signed_by="p_publisher",
        at=datetime(2026, 9, 6, tzinfo=UTC),
    )
    instance = TemplateInstance(
        instance_id=manifest.identity.template_id,
        template_id=manifest.identity.template_id,
        template_version=manifest.identity.version,
        content_digest=content_digest(manifest),
        created_by="p_installer",
    )
    effective = materialise(
        signed,
        instance,
        audience=AgentAudience(level=Visibility.COMPANY, owner_id="p_installer"),
    )

    assert effective.record.persona, "materialised with no persona"


@pytest.mark.parametrize("manifest", CATALOGUE, ids=IDS)
def test_no_template_can_do_more_than_draft(manifest: TemplateManifest) -> None:
    """Not one of these six needs to write, send or move money, and the ceiling says so
    rather than the persona.

    A persona reading "never change anything without asking" is a request to a model. A
    `max_side_effect` of NONE or DRAFT is a refusal `gate.leash.govern` enforces, and the
    difference between those two sentences is the difference between a guardrail and a hope.

    The bound is DRAFT rather than NONE because `sem_agent` legitimately composes a change
    for a person to commit. Anything above it here would be a template shipping the ability
    to act on a company it has never seen.

    Delete this and a template can acquire `SideEffect.WRITE` in one word, and the only
    symptom is an agent that starts doing things."""
    assert manifest.guardrails.max_side_effect in (SideEffect.NONE, SideEffect.DRAFT), (
        f"{manifest.identity.template_id} ships able to {manifest.guardrails.max_side_effect}"
    )


def test_a_human_commits_a_budget_change_and_the_ceiling_is_what_says_so() -> None:
    """M13.5.21 states the constraint in its own title: a human commits budget changes.

    That is `SideEffect.DRAFT` and it is the reason this template is in the catalogue and the
    AR chaser is not. DRAFT is a thing the gate can refuse; "shadow-pinned thirty days" is a
    duration the leash has nowhere to put.

    Asserted as DRAFT exactly rather than as "at most WRITE", because the whole point is that
    the agent prepares and does not apply, and NONE would be a different agent that cannot
    prepare either.

    Delete this and the one template whose title names a constraint stops being held to it."""
    assert sem_agent().guardrails.max_side_effect is SideEffect.DRAFT


def test_the_analyst_reads_the_hours_and_not_the_money_beside_them() -> None:
    """The fixture case `tests/e2e/test_wave_two_lark_question.py` drives with two real
    people, written as a template instead of a test double.

    A maintenance Base holds hours and contract values in one table, so an analyst that could
    read the row could read the money. The capability list is what separates them, and it is
    the ceiling rather than a filter applied later: `E_run = E(caller) ∩ ceiling`, so a caller
    who *can* see contract values still cannot see them through this agent.

    Delete this and the template can acquire the money capability while its persona still
    says it answers questions about hours."""
    held = {c.value for c in capacity_and_hours_analyst().authority.capabilities}

    assert "read:client.hours_remaining" in held
    assert "read:client.contract_value" not in held
    assert not any("margin" in value for value in held)


def test_the_accountant_may_read_the_ledger_and_may_not_move_anything() -> None:
    """The only template here naming money capabilities, and it still declares
    `SideEffect.NONE`.

    Reading a ledger and moving money are different verbs. A template that could do the
    second because it needed the first is how a reconciliation agent becomes a payment agent,
    and the change would read in review as one word in a field nobody looks at.

    Delete this and the money capabilities stay while the ceiling can rise."""
    manifest = accountant_agent()
    held = {c.value for c in manifest.authority.capabilities}

    assert "read:invoice.amount" in held
    assert manifest.guardrails.max_side_effect is SideEffect.NONE
    assert all(value.startswith("read:") for value in held), (
        "the accountant holds a capability that is not a read"
    )


def test_the_curator_reads_the_questions_and_never_who_asked() -> None:
    """M13.5.23's whole output is a list of things nobody wrote down, drawn from questions
    people asked. That makes it one capability away from a report on who keeps asking about
    pricing, which is a performance review assembled inside a curation tool.

    So it holds no capability naming a principal, a person or a department, and this asserts
    the absence rather than the presence, because the presence is what a future edit adds.

    Delete this and "who asked" becomes available to the one agent whose job makes it look
    useful."""
    held = {c.value for c in knowledge_gap_curator().authority.capabilities}

    for forbidden in ("principal", "person", "asker", "user", "department", "email"):
        assert not any(forbidden in value for value in held), (
            f"the curator can read something naming a {forbidden}"
        )


@pytest.mark.parametrize("manifest", CATALOGUE, ids=IDS)
def test_no_persona_tries_to_decide_a_permission(manifest: TemplateManifest) -> None:
    """Prompt material is stored and never parsed, so a persona saying "only show this to
    managers" is a permission decided by whoever last edited a text box, and enforced by
    nobody.

    The words below are the ones that would appear if somebody tried. It is a blunt check and
    it is meant to be: what it really guards is the habit, because the first persona that
    says "do not reveal salaries" is the one that makes the next reader believe personas are
    load-bearing.

    Delete this and the catalogue teaches, by example, that access control can be written in
    English."""
    lowered = manifest.persona.lower()

    for phrase in ("only show", "do not reveal", "not allowed to see", "only if the user is"):
        assert phrase not in lowered, (
            f"{manifest.identity.template_id}'s persona tries to decide a permission"
        )


def test_the_catalogue_is_six_distinct_templates_and_its_index_is_derived() -> None:
    """Two things that drift apart if either is maintained by hand: the ids inside the
    manifests and any mapping keyed by them.

    `catalogue_by_id` derives the mapping, so a template whose id changes cannot leave a stale
    key behind, and a duplicate id collapses the mapping rather than shipping two templates
    that install over each other.

    Delete this and two manifests can share an id, which the install flow would resolve by
    whichever it happened to read second."""
    index = catalogue_by_id()

    assert len(index) == len(CATALOGUE), "two templates share an id"
    assert set(index) == set(IDS)
    for template_id, manifest in index.items():
        assert manifest.identity.template_id == template_id


@pytest.mark.parametrize("manifest", CATALOGUE, ids=IDS)
def test_every_template_names_the_house_as_its_publisher(manifest: TemplateManifest) -> None:
    """`published_by` on a person is a record pointing at a principal who may have left, and
    `Principal.is_active` refuses one. A template outlives whoever wrote it.

    M13.6 is where a client's own templates get a real author, and that author is a person at
    that company rather than here.

    Delete this and a departed colleague's id is baked into a signed manifest."""
    assert manifest.identity.published_by == PUBLISHER


def test_the_templates_that_declare_a_connector_declare_one_that_exists() -> None:
    """A connector name in a manifest makes the install ask for a binding and reaches
    nothing, which means a typo produces a template that can never be completed: the install
    waits forever for a binding to a connector nobody can bind.

    Checked against the modules under `brain.connectors` rather than a list here, so a
    connector added or renamed moves this on its own.

    Delete this and `freshdsk` is a template that is permanently incomplete for a reason
    nobody can see from the console."""
    import pkgutil

    import brain.connectors as package

    available = {info.name for info in pkgutil.iter_modules(package.__path__)}
    declared = {name for manifest in CATALOGUE for name in manifest.connectors}

    assert declared, "no template declares a connector, so this checks nothing"
    assert declared <= available, f"declared but absent: {sorted(declared - available)}"


def test_the_support_agent_and_the_developer_both_wait_for_freshdesk() -> None:
    """Naming a connector reaches nothing: it makes `install.completeness` hold the agent
    incomplete and disabled until a binding exists.

    That is the property worth having for these two. A support agent that cannot see tickets
    must not answer a question about tickets from whatever else it can reach, and the
    mechanism that stops it is the install being incomplete rather than the agent being
    careful.

    Delete this and either template can lose its declaration and start answering from the
    knowledge base instead."""
    assert "freshdesk" in support_ticket_agent().connectors
    assert "freshdesk" in wordpress_developer().connectors
