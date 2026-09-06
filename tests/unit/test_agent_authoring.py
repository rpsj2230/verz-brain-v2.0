"""Client-authored templates: the leak report, the dispositions, and what cannot travel.

Every test here is a way a company's own data could leave with a template it published, or a
way the review that was supposed to stop it could be skipped.

**The memory tests are structural and they are the point.** There is no flag to assert
against, so they assert against shapes instead: `author`'s resolved annotations, so an
`Installation` cannot be handed to it and `placeholder_answers` is not an attribute away;
`CARRIED_FIELDS` against a tuple written out in this file, so adding a field to the carried
set fails here rather than passing quietly; and a full `Installation` built with a secret in
its answers, published, and searched for that secret in the signed body.

**The constants are asserted against something outside themselves.** `MANIFEST_PATHS` and
`CARRIED_FIELDS` are compared with tuples written out in this file rather than with
themselves, so a path or a field added to either moves one side of the comparison and not the
other. A test importing a constant and comparing it with itself is green for every value it
could hold, which happened three times in this repository in one afternoon.

**The redaction tests are two-sided.** A redaction that replaced nothing passes any test that
only checks a word survived, and one that replaced everything passes any test that only
checks a value is gone. `test_a_redaction_replaces_a_word_and_not_the_word_it_sits_inside`
asserts both halves of the same substitution.

**The install half is driven through the real wizard.** `offer_for` hands a real
`brain.agents.install.Offer` to a real `TemplateCatalogue`, and the end-to-end test runs
`open_for`, `begin`, `answer`, `provide` and `complete`, so what is asserted is what somebody
installing an authored template actually receives rather than this file's idea of one.

Task ids: M13.6.1, M13.6.2, M13.6.3, M13.6.4, M13.6.5, M13.6.6
"""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, get_type_hints

import pytest
from pydantic import ValidationError

import brain.agents.authoring as authoring_module
from brain.agents.authoring import (
    CARRIED_FIELDS,
    REDACTION_MARK,
    SAVED_VISIBILITY,
    VISIBILITY_ORDER,
    AuthoredDraft,
    AuthoredTemplate,
    AuthoringError,
    Classification,
    Decision,
    Disposition,
    LeakItem,
    LiteralKind,
    TemplateVisibility,
    UndispositionedError,
    author,
    carried,
    decide,
    may_leave_this_installation,
    offer_audience,
    offer_for,
    publish_authored,
    scan,
    set_visibility,
)
from brain.agents.install import (
    Installation,
    InstallBadge,
    InstallDraft,
    NoSuchTemplateError,
    TemplateCatalogue,
    begin,
    complete,
    provide,
)
from brain.agents.model import (
    AgentAudience,
    AgentViewer,
    entitlement_ceiling,
    tool_ceiling,
    visible_agent_ids,
)
from brain.agents.template import (
    BLANK_TEMPLATE_ID,
    MANIFEST_PATHS,
    EffectiveAgent,
    GoldenCase,
    LeashRung,
    ManifestAuthority,
    ManifestGuardrails,
    ManifestIdentity,
    Placeholder,
    SkillRef,
    TemplateManifest,
    hand_built,
    install,
    materialise,
    publish,
)
from brain.connectors.registry import ConnectorRegistry
from brain.core.entitlement import Capability
from brain.core.envelope import SideEffect
from brain.core.scope import Clause, Op, Scope
from brain.gate.injection import AutonomyTier
from brain.knowledge.visibility import Visibility
from brain.models.routing import Tier

NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)

KEY = "a-signing-key"

PUBLISHER = "u_wei_ling"
AUTHOR = "u_priya"
OUTSIDER = "u_jason"

SOURCE_TEMPLATE = "support_template"
SOURCE_AGENT = "support_east"
AUTHORED = "support_pattern"
INSTALLED = "support_west"

TOOL = "client.read_summary"
SKILL_DIGEST = "b" * 64

#: The literals the fixture agent is configured with, named so a test asserting on one of
#: them cannot be satisfied by a different string that happens to be nearby.
CLIENT = "Tomato Glasses"
CONTACT = "bob@acme.example"
TICKET = "TG-4471"
RATE = "$180"
DEPARTMENT = "web"

#: Two words apart by one suffix. The redaction test asserts on both.
PERSONA = (
    f"Answer helpdesk questions for {CLIENT}. Escalate anything urgent to {CONTACT}. "
    f"The {DEPARTMENT} team keeps the website. Quote {RATE} per hour against {TICKET}."
)

#: The seventeen manifest paths, written out here rather than imported and compared with
#: themselves. A path added to `MANIFEST_PATHS` moves one side of the comparison in
#: `test_the_published_body_carries_the_seventeen_manifest_paths_and_nothing_else` and not
#: the other, which is the only way that test can notice a new field arriving.
SEVENTEEN_PATHS: tuple[str, ...] = (
    "authority.allowed_tools",
    "authority.capabilities",
    "authority.required_tools",
    "authority.scope",
    "connectors",
    "golden_set",
    "guardrails.leash",
    "guardrails.max_side_effect",
    "identity.display_name",
    "identity.published_by",
    "identity.summary",
    "identity.template_id",
    "identity.version",
    "persona",
    "placeholders",
    "skills",
    "tier",
)

#: The nine fields an authored template carries, written out for the same reason. Adding
#: `memory` to `CARRIED_FIELDS` fails
#: `test_nothing_a_template_carries_is_a_store_of_what_the_system_learnt`.
NINE_CARRIED_FIELDS: tuple[str, ...] = (
    "identity",
    "persona",
    "tier",
    "skills",
    "authority",
    "connectors",
    "guardrails",
    "golden_set",
    "placeholders",
)


# ------------------------------------------------------------------------------- fixtures
def _manifest(**overrides: Any) -> TemplateManifest:
    defaults: dict[str, Any] = {
        "identity": ManifestIdentity(
            template_id=SOURCE_TEMPLATE,
            version=1,
            published_by=PUBLISHER,
            display_name="Support Desk",
            summary="Answers helpdesk questions.",
        ),
        "persona": PERSONA,
        "tier": Tier.MAIN,
        "skills": (SkillRef(name="triage", digest=SKILL_DIGEST),),
        "authority": ManifestAuthority(
            scope=Scope(clauses=(Clause(field="department", op=Op.EQ, value=DEPARTMENT),)),
            capabilities=(Capability(value="read:client.name"),),
            allowed_tools=(TOOL,),
        ),
        "guardrails": ManifestGuardrails(
            max_side_effect=SideEffect.SEND,
            leash=(LeashRung(target=TOOL, rung=AutonomyTier.AUTONOMOUS),),
        ),
        "golden_set": (
            GoldenCase(
                question=f"How many hours has {CLIENT} left against {TICKET}?",
                expectation="A number of hours with the client named.",
            ),
        ),
        "placeholders": (Placeholder(key="price_list", prompt="Which price list applies?"),),
    }
    defaults.update(overrides)
    return TemplateManifest(**defaults)


def _audience(owner: str = AUTHOR) -> AgentAudience:
    return AgentAudience(level=Visibility.PERSONAL, owner_id=owner)


def _effective(manifest: TemplateManifest | None = None) -> EffectiveAgent:
    """A configured agent, built through the real publish, install and materialise path."""
    signed = publish(manifest or _manifest(), key=KEY, signed_by=PUBLISHER, at=NOW)
    instance = install(signed, key=KEY, instance_id=SOURCE_AGENT, created_by=AUTHOR, at=NOW)
    return materialise(signed, instance, audience=_audience())


def _hand_built_effective() -> EffectiveAgent:
    """An agent somebody wrote from nothing, which is an install of the blank template."""
    instance = hand_built(
        key=KEY,
        instance_id="hand_made_desk",
        created_by=AUTHOR,
        at=NOW,
        overlay={"identity.display_name": "Hand made", "persona": PERSONA},
    )
    from brain.agents.template import blank_template

    return materialise(blank_template(key=KEY, at=NOW), instance, audience=_audience())


def _draft(source: EffectiveAgent | None = None, **overrides: Any) -> AuthoredDraft:
    arguments: dict[str, Any] = {
        "template_id": AUTHORED,
        "display_name": "Support pattern",
        "author_id": AUTHOR,
        "summary": "A support desk shape.",
    }
    arguments.update(overrides)
    return author(source if source is not None else _effective(), **arguments)


def _accept() -> Decision:
    return Decision(disposition=Disposition.ACCEPT, by=AUTHOR, at=NOW)


def _redact() -> Decision:
    return Decision(disposition=Disposition.REDACT, by=AUTHOR, at=NOW)


def _hoist(key: str, prompt: str) -> Decision:
    return Decision(
        disposition=Disposition.HOIST, by=AUTHOR, at=NOW, placeholder_key=key, prompt=prompt
    )


def _decide_all(
    draft: AuthoredDraft, choices: Mapping[str, Decision] | None = None
) -> AuthoredDraft:
    """Every item decided, accepting anything the caller did not name.

    A helper in this file and deliberately not a function in `brain.agents.authoring`. A
    module offering this would be the accept-all M13.6.4 exists to refuse, and
    `test_nothing_in_the_module_disposes_of_more_than_one_item_at_a_time` asserts it is not
    there.
    """
    named = dict(choices or {})
    out = draft
    for item in draft.report.items:
        out = decide(out, item.item_id, named.get(item.text, _accept()))
    return out


def _item(draft: AuthoredDraft, text: str) -> LeakItem:
    for entry in draft.report.items:
        if entry.text == text:
            return entry
    raise AssertionError(
        f"no item for {text!r}; the report holds {[i.text for i in draft.report.items]}"
    )


def _published(
    draft: AuthoredDraft | None = None,
    choices: Mapping[str, Decision] | None = None,
    *,
    visibility: TemplateVisibility = SAVED_VISIBILITY,
) -> AuthoredTemplate:
    prepared = _decide_all(draft if draft is not None else _draft(), choices)
    return publish_authored(prepared, key=KEY, at=NOW, visibility=visibility)


def _install(authored: AuthoredTemplate, answers: Mapping[str, str] | None = None) -> Installation:
    """The authored template installed through the real wizard."""
    started: InstallDraft = begin(offer_for(authored), instance_id=INSTALLED, installer=AUTHOR)
    for key, value in (answers or {}).items():
        started = provide(started, key, value)
    return complete(
        started,
        key=KEY,
        audience=_audience(),
        registry=ConnectorRegistry(),
        at=NOW,
    )


# ------------------------------------------------------- saving an agent (M13.6.1)
def test_an_installed_agent_and_a_hand_built_one_are_both_saved_the_same_way() -> None:
    """Deleting this lets "save *any* agent" quietly become "save an installed one", and the
    hand-built path is the one nobody exercises because it looks like the simple case.

    Both sources go through one function and produce a draft with the same shape, which is
    only possible because `materialise` gives both the same type.
    """
    from_installed = _draft(_effective())
    from_hand_built = _draft(_hand_built_effective(), template_id="hand_made_pattern")

    assert from_installed.manifest.persona == PERSONA
    assert from_hand_built.manifest.persona == PERSONA
    assert from_installed.report.items
    assert from_hand_built.report.items


def test_a_saved_template_is_private_and_has_no_field_that_could_say_otherwise() -> None:
    """Deleting this lets a visibility default arrive on the draft, and a default that reads
    "organisation" would publish somebody's configured agent to the company by omission.

    Asserted on the type as well as on the value: a property returning a constant cannot be
    passed over, and a field could be.
    """
    draft = _draft()
    assert draft.visibility is TemplateVisibility.PRIVATE
    assert SAVED_VISIBILITY is TemplateVisibility.PRIVATE
    assert VISIBILITY_ORDER[0] is SAVED_VISIBILITY

    field_names = {entry.name for entry in dataclasses.fields(AuthoredDraft)}
    assert "visibility" not in field_names
    assert "visibility" not in inspect.signature(author).parameters


def test_an_authored_template_starts_a_lineage_of_its_own() -> None:
    """Deleting this lets an authored template inherit its source's id, version and
    publisher, so it claims to be an install of somebody else's template and M13.4's upgrade
    diff compares it against the wrong lineage.

    The two text fields are asserted to be exactly what was typed, which is a second
    property: `author` writes its arguments into the identity and decorates none of them, so
    nothing about the source agent leaks in through a field somebody thought was a label.
    """
    identity = _draft().manifest.identity
    assert identity.template_id == AUTHORED
    assert identity.version == 1
    assert identity.published_by == AUTHOR
    assert identity.template_id != SOURCE_TEMPLATE
    assert identity.published_by != PUBLISHER
    assert identity.display_name == "Support pattern"
    assert identity.summary == "A support desk shape."


def test_no_authored_template_may_take_the_blank_templates_id() -> None:
    """Deleting this lets somebody publish a template called `blank`, and a hand-built agent
    would then install from their guardrails instead of `SideEffect.NONE` and an empty leash.
    That is a privilege escalation wearing the clothes of a naming collision.
    """
    with pytest.raises(AuthoringError, match="hand-built"):
        _draft(template_id=BLANK_TEMPLATE_ID)

    # And the positive half: any other id is fine, so the refusal is about this one id.
    assert _draft(template_id="another_pattern").manifest.identity.template_id == "another_pattern"


def test_the_supervision_of_the_source_travels_into_the_template_unchanged() -> None:
    """Deleting this lets authoring relax a leash or a side-effect ceiling on the way out,
    producing a template that looks like the agent it was saved from and behaves differently.

    The signature half matters as much as the value half: a parameter that could set either
    is a way to raise them, and there is none.
    """
    source = _effective()
    draft = _draft(source)

    assert draft.manifest.guardrails == source.manifest.guardrails
    assert draft.manifest.guardrails.max_side_effect is SideEffect.SEND
    assert draft.manifest.guardrails.leash[0].rung is AutonomyTier.AUTONOMOUS

    parameters = set(inspect.signature(author).parameters)
    assert parameters == {"source", "template_id", "display_name", "author_id", "summary"}


def test_a_field_added_to_the_manifest_does_not_travel_until_somebody_says_so() -> None:
    """Deleting this lets a new manifest field be carried into every authored template with
    nobody deciding that it should be, and the direction the accident runs in is the one that
    exports more. This is the structural half of M13.6.5: a `memory` field on the manifest
    stops authoring dead rather than travelling with it.

    Driven through the parameter rather than by editing the constant, for the reason
    `install.plan(step_fields=...)` gives.
    """
    with pytest.raises(AuthoringError, match="placeholders"):
        carried(_manifest(), fields=CARRIED_FIELDS[:-1])

    # The positive half: with the real tuple it carries the eight non-identity fields.
    assert set(carried(_manifest())) == set(CARRIED_FIELDS) - {"identity"}


# ------------------------------------------------- extraction and hoisting (M13.6.2)
def test_a_literal_is_found_in_prose_and_in_a_predicate_and_in_a_list_alike() -> None:
    """Deleting this lets the scanner read the persona and stop, so a department in a scope
    clause, a tool name and a connector name travel unreviewed. The leak that matters is
    rarely in the paragraph somebody was looking at.
    """
    report = _draft(_effective(_manifest(connectors=("freshdesk",)))).report
    by_text = {item.text: item for item in report.items}

    assert by_text[CONTACT].kind is LiteralKind.EMAIL
    assert by_text[CONTACT].locations == ("persona",)
    assert by_text[RATE].kind is LiteralKind.MONEY
    assert by_text[DEPARTMENT].locations == ("authority.scope/clauses/0/value",)
    assert by_text[TOOL].locations == ("authority.allowed_tools/0", "guardrails.leash/0/target")
    assert by_text["freshdesk"].locations == ("connectors/0",)
    assert by_text["read:client.name"].locations == ("authority.capabilities/0/value",)


def test_one_string_is_one_decision_however_many_places_it_sits_in() -> None:
    """Deleting this lets items be keyed by location as well as text, and an author could
    then redact a ticket reference in the persona, accept the same reference in a golden
    question, and ship it anyway from the field they were not looking at.
    """
    item = _item(_draft(), TICKET)
    assert item.locations == ("golden_set/0/question", "persona")

    published = _published(choices={TICKET: _redact()})
    body = published.signed.manifest.model_dump_json()
    assert TICKET not in body
    assert body.count(REDACTION_MARK) == 2


def test_a_hoisted_value_is_gone_and_its_question_is_asked() -> None:
    """Deleting this lets hoisting become a redaction with a nicer name: the value goes and
    nobody is asked for a replacement, so the next company installs an agent with a blank
    where an escalation contact should be, which is the failure `Placeholder` exists to stop.

    The question is asserted through the real install wizard rather than by reading the
    manifest, because "the question is asked" means `provide` accepts it.
    """
    published = _published(choices={CONTACT: _hoist("escalation_contact", "Who is escalated to?")})
    manifest = published.signed.manifest

    assert CONTACT not in manifest.persona
    assert "{{escalation_contact}}" in manifest.persona
    assert "escalation_contact" in {p.key for p in manifest.placeholders}

    started = begin(offer_for(published), instance_id=INSTALLED, installer=AUTHOR)
    answered = provide(started, "escalation_contact", "whoever is on call")
    assert answered.placeholder_answers["escalation_contact"] == "whoever is on call"


def test_a_hoisted_predicate_value_matches_no_row_until_somebody_sets_it() -> None:
    """Deleting this lets a redaction drop a scope clause instead of replacing its value.
    `Scope` composes by conjunction only, so dropping a clause widens the predicate, and a
    redaction that widens a ceiling is the worst available reading of the word.

    Both halves are asserted: the clause is still there, and it now matches nothing.
    """
    published = _published(choices={DEPARTMENT: _hoist("which_department", "Which department?")})
    scope = published.signed.manifest.authority.scope

    assert len(scope.clauses) == 1
    assert scope.clauses[0].field == "department"
    assert scope.clauses[0].value == "{{which_department}}"
    assert not scope.matches({"department": DEPARTMENT})
    assert not scope.matches({"department": "finance"})


def test_a_redaction_replaces_a_word_and_not_the_word_it_sits_inside() -> None:
    """Deleting this lets a redaction run without word boundaries, so redacting the
    department `web` turns every `website` into `[redacted]site` and the author has accepted
    a paragraph that no longer says what they read.

    Two-sided on purpose. A substitution that replaced nothing passes the second assertion
    alone, and one that replaced everything passes the first alone.
    """
    persona = _published(choices={DEPARTMENT: _redact()}).signed.manifest.persona
    assert f"The {REDACTION_MARK} team" in persona
    assert "keeps the website" in persona


# --------------------------------------------------------------- the leak report (M13.6.3)
def test_a_vocabulary_member_and_a_digest_are_classified_and_everything_else_is_not() -> None:
    """Deleting this lets the classifier widen until it is passing whatever it recognises,
    which is the failure mode of every allow list: the thing that matters is what nobody
    classified, and a classifier that grows quietly shrinks the report nobody notices.
    """
    report = _draft().report
    passed = {literal.location: literal for literal in report.passed}

    assert passed["tier"].classification is Classification.VOCABULARY
    assert passed["guardrails.max_side_effect"].classification is Classification.VOCABULARY
    assert passed["authority.scope/clauses/0/op"].classification is Classification.VOCABULARY
    assert passed["skills/0/digest"].classification is Classification.DIGEST

    # The unclassified side: none of those four is an item, and the ordinary strings are.
    item_texts = {item.text for item in report.items}
    assert item_texts.isdisjoint({"main", "send", "eq", SKILL_DIGEST})
    assert "triage" in item_texts
    assert CONTACT in item_texts
    assert any(CLIENT in text for text in item_texts)


def test_a_persona_containing_a_vocabulary_word_is_not_classified_as_that_vocabulary() -> None:
    """Deleting this lets classification be decided by value alone, so a persona reading
    "none of the above" is passed as a `SideEffect` and a whole paragraph leaves unreviewed.
    Classification is keyed by the field as well as by the value for exactly this reason.
    """
    report = _draft(_effective(_manifest(persona="none"))).report
    assert "none" in {item.text for item in report.items}
    assert "none" not in {literal.text for literal in report.passed}


def test_the_report_carries_what_it_passed_and_the_reason_it_passed_it() -> None:
    """Deleting this lets the report show only its questions, which means every other
    decision was taken silently and the author has no way to notice that a whole field was
    waved through. A report that hides what it passed is a default in disguise.
    """
    report = _draft().report
    assert report.passed
    assert all(literal.reason for literal in report.passed)
    assert all(
        literal.classification is not Classification.UNCLASSIFIED for literal in report.passed
    )


def test_a_redacted_literal_is_absent_from_the_published_body_and_an_accepted_one_survives() -> (
    None
):
    """Deleting this leaves the whole disposition mechanism untested against its own output.
    A publish that ignored the decisions passes every refusal test in this file, and so does
    one that redacted everything; only the pair distinguishes them.
    """
    published = _published(choices={CONTACT: _redact()})
    body = published.signed.manifest.model_dump_json()

    assert CONTACT not in body
    assert REDACTION_MARK in published.signed.manifest.persona
    assert CLIENT in published.signed.manifest.persona
    assert RATE in published.signed.manifest.persona


def test_a_decision_against_an_item_that_is_not_on_the_report_is_refused() -> None:
    """Deleting this lets a decision be recorded against nothing, so the item it was meant
    for stays undecided while the author believes they have dealt with it, and the publish
    refusal they then see names an item they think they answered.
    """
    with pytest.raises(AuthoringError, match="not an item on this report"):
        decide(_draft(), "0" * 16, _accept())


def test_a_hoist_needs_a_question_and_an_accept_may_not_carry_one() -> None:
    """Deleting this lets a hoist be recorded with no question, which removes the value and
    asks the next company nothing, and lets a question be attached to an accept, where it
    becomes a question nobody will ever be asked.
    """
    with pytest.raises(ValidationError):
        Decision(disposition=Disposition.HOIST, by=AUTHOR, at=NOW, placeholder_key="contact")
    with pytest.raises(ValidationError):
        Decision(disposition=Disposition.HOIST, by=AUTHOR, at=NOW, prompt="Who?")
    with pytest.raises(ValidationError):
        Decision(disposition=Disposition.ACCEPT, by=AUTHOR, at=NOW, prompt="Who?")

    # The positive half, so the refusals are not satisfied by refusing every hoist.
    assert _hoist("contact", "Who is escalated to?").disposition is Disposition.HOIST


def test_two_hoists_may_not_ask_the_same_question_twice() -> None:
    """Deleting this lets a template declare one placeholder key twice, and `provide` then
    records one answer against whichever of the two the person filling the form in happened
    to see, with the other left looking unanswered.
    """
    draft = _draft()
    contact = _item(draft, CONTACT).item_id
    ticket = _item(draft, TICKET).item_id

    # A key the source manifest already declares.
    with pytest.raises(AuthoringError, match="already a question"):
        decide(draft, contact, _hoist("price_list", "Which one?"))

    # And a key another hoist on the same draft has taken.
    first = decide(draft, contact, _hoist("contact", "Who is escalated to?"))
    with pytest.raises(AuthoringError, match="already a question"):
        decide(first, ticket, _hoist("contact", "Which reference?"))

    # The positive half: a key nobody has taken is accepted.
    assert decide(first, ticket, _hoist("reference", "Which reference?")).decisions.keys() == {
        contact,
        ticket,
    }


# ------------------------------------------------------ publishing is blocked (M13.6.4)
def test_publishing_is_refused_while_any_literal_is_undecided() -> None:
    """Deleting this is the whole leaf: publishing would proceed with literals nobody looked
    at, and the template that leaves is the one whose report was never read.

    Asserted with one item left undecided rather than none decided, because a check written
    as "no decisions at all" passes for a publish that requires only the first.
    """
    draft = _draft()
    all_but_one = draft
    for item in draft.report.items[1:]:
        all_but_one = decide(all_but_one, item.item_id, _accept())

    assert len(all_but_one.undecided) == 1
    with pytest.raises(UndispositionedError, match=draft.report.items[0].item_id):
        publish_authored(all_but_one, key=KEY, at=NOW)

    # The positive half: with the last one decided it publishes.
    finished = decide(all_but_one, draft.report.items[0].item_id, _accept())
    assert finished.undecided == ()
    assert publish_authored(finished, key=KEY, at=NOW).template_id == AUTHORED


def test_publishing_a_private_template_needs_the_same_decisions_as_a_catalogue_one() -> None:
    """Deleting this lets private publishing skip the report, and the catalogue is then two
    steps away with the report skipped in the first one: visibility is a property of the
    offer and the body is a property of the signature, so raising the level never re-scans.
    """
    draft = _draft()
    for visibility in VISIBILITY_ORDER:
        with pytest.raises(UndispositionedError):
            publish_authored(draft, key=KEY, at=NOW, visibility=visibility)


def test_nothing_in_the_module_disposes_of_more_than_one_item_at_a_time() -> None:
    """Deleting this lets an accept-all arrive later, and an accept-all is a decision nobody
    made about data they did not look at, which is the exact failure M13.6.4 names.

    Read off the module's own annotations rather than asserted in prose: anything taking a
    `Decision` has to take the id of the one item it is about.
    """
    taking_a_decision: list[str] = []
    for name, value in vars(authoring_module).items():
        if name.startswith("_") or not inspect.isfunction(value):
            continue
        if value.__module__ != authoring_module.__name__:
            continue
        hints = get_type_hints(value)
        if any(hint is Decision for key, hint in hints.items() if key != "return"):
            taking_a_decision.append(name)
            assert "item_id" in inspect.signature(value).parameters, (
                f"{name} disposes without naming the item it disposes of"
            )
    assert taking_a_decision == ["decide"]


def test_every_decision_records_who_took_it_and_when() -> None:
    """Deleting this lets a decision be written with no person behind it, and the record of
    what was reviewed then says a review happened without saying who did it. A naive
    timestamp is the same failure in the other axis: hours out in whichever direction the
    host sits, with neither direction announcing itself.
    """
    with pytest.raises(ValidationError):
        Decision(disposition=Disposition.ACCEPT, by="", at=NOW)
    with pytest.raises(ValidationError, match="timezone-aware"):
        Decision(disposition=Disposition.ACCEPT, by=AUTHOR, at=datetime(2026, 9, 6, 9, 0))

    published = _published()
    assert {decision.by for decision in published.decisions.values()} == {AUTHOR}
    assert all(decision.at.tzinfo is not None for decision in published.decisions.values())


def test_raising_the_visibility_does_not_re_open_the_published_body() -> None:
    """Deleting this lets `set_visibility` re-sign, and the body a person read and
    dispositioned would stop being the body every visibility of the template has. It is also
    the argument for the test above: the level moves and the manifest cannot.
    """
    private = _published()
    catalogued = set_visibility(private, TemplateVisibility.CATALOGUE)

    assert catalogued.signed is private.signed
    assert catalogued.signed.content_digest == private.signed.content_digest
    assert catalogued.visibility is TemplateVisibility.CATALOGUE
    assert private.visibility is TemplateVisibility.PRIVATE

    # And back down, because withdrawing is the operation somebody needs on the bad day.
    assert set_visibility(catalogued, TemplateVisibility.PRIVATE).signed is private.signed


def test_a_hoisted_question_may_not_contain_the_answer_it_replaces() -> None:
    """Deleting this lets an author remove a value and then type it into the question that
    replaces it, one line further down, in a field nobody thinks of as configuration.
    """
    draft = _draft()
    with pytest.raises(AuthoringError, match="contains the value it replaces"):
        decide(
            draft,
            _item(draft, CONTACT).item_id,
            _hoist("escalation_contact", f"Who replaces {CONTACT}?"),
        )


def test_a_value_no_accepted_item_accounts_for_cannot_reach_the_published_body() -> None:
    """Deleting this lets text typed after the report was read reach the signed manifest.
    The report is the record of what somebody looked at, so a value arriving after it has
    been reviewed by nobody, and the one thing an author types afterwards is a question.
    """
    with pytest.raises(AuthoringError, match="no accepted item accounts for it"):
        _published(choices={CONTACT: _hoist("escalation_contact", "Ask sam@other.example?")})

    # The positive half: a question with no value in it publishes.
    assert _published(choices={CONTACT: _hoist("escalation_contact", "Who is escalated to?")})


# ------------------------------------------------------------------ memory (M13.6.5)
def test_the_only_thing_that_can_be_made_into_a_template_is_an_effective_agent() -> None:
    """Deleting this lets `author` grow a parameter that carries more than a manifest: an
    `Installation` with its placeholder answers, a transcript, a mapping of anything at all.
    There is no flag to assert against for M13.6.5, so the shape of the entry point is the
    assertion, and it is read off the resolved annotations rather than described here.
    """
    hints = get_type_hints(author)
    assert hints == {
        "source": EffectiveAgent,
        "template_id": str,
        "display_name": str,
        "author_id": str,
        "summary": str,
        "return": AuthoredDraft,
    }
    assert Installation not in hints.values()


def test_a_placeholder_answer_never_reaches_a_published_template() -> None:
    """Deleting this leaves the most likely memory-shaped leak untested. The answers are
    exactly the company-specific values a template must not carry, they sit one attribute
    away on `Installation`, and the questions they answer do travel, so a reader glancing at
    the manifest sees questions and assumes the answers went with them.
    """
    secret = "the 2026 Acme rate card, kept on the finance drive"
    installed = complete(
        provide(
            begin(
                offer_for(_published()),
                instance_id=INSTALLED,
                installer=AUTHOR,
            ),
            "price_list",
            secret,
        ),
        key=KEY,
        audience=_audience(),
        registry=ConnectorRegistry(),
        at=NOW,
    )
    assert installed.placeholder_answers["price_list"] == secret

    republished = _published(_draft(installed.effective, template_id="second_pattern"))
    body = republished.signed.manifest.model_dump_json()
    assert secret not in body
    assert "finance drive" not in body
    # The question travels and the answer does not, which is the whole distinction. Asserted
    # on the prompt as well as the key, because a prompt is where an answer would be smuggled
    # if anything ever copied one: it is free text sitting beside the question it answers.
    assert {(p.key, p.prompt) for p in republished.signed.manifest.placeholders} == {
        ("price_list", "Which price list applies?")
    }


def test_the_published_body_carries_the_seventeen_manifest_paths_and_nothing_else() -> None:
    """Deleting this lets an eighteenth path arrive in a published template with nobody
    deciding it should travel. The seventeen are written out in this file rather than
    imported and compared with themselves, so a path added to `MANIFEST_PATHS` moves one side
    of this comparison and not the other.
    """
    assert set(MANIFEST_PATHS) == set(SEVENTEEN_PATHS)
    assert len(SEVENTEEN_PATHS) == 17
    assert set(_published().signed.manifest.document()) == set(SEVENTEEN_PATHS)


def test_nothing_a_template_carries_is_a_store_of_what_the_system_learnt() -> None:
    """Deleting this is what lets somebody add `memory` to `CARRIED_FIELDS` because their
    template does not work as well for the next person as it did for them. The nine are
    written out here, so widening the carried set fails in this file, and every one of them
    is a field of the manifest rather than of anything the system retains.
    """
    assert set(CARRIED_FIELDS) == set(NINE_CARRIED_FIELDS)
    assert set(TemplateManifest.model_fields) == set(NINE_CARRIED_FIELDS)
    assert not {field for field in CARRIED_FIELDS if "memor" in field or "recall" in field}


# ------------------------------------------------------------------ visibility (M13.6.6)
def test_the_three_visibilities_change_who_may_install_and_never_what_a_run_reaches() -> None:
    """Deleting this lets the level become an authority axis, which is
    `AUDIENCE_IS_NOT_AUTHORITY` failing one layer out: a template published to the catalogue
    would come to reach more than a private one, and nobody wrote that grant.

    Asserted over all three levels rather than over one, because a test of one level is
    satisfied by code that ties the level to the ceiling at the other two.
    """
    ceilings = set()
    reaches = set()
    for visibility in VISIBILITY_ORDER:
        installed = _install(_published(visibility=visibility), {"price_list": "the rate card"})
        ceilings.add(tool_ceiling(installed.record))
        reaches.add(entitlement_ceiling(installed.record))
    assert len(ceilings) == 1
    assert len(reaches) == 1

    # And the half that does move: who may install it here.
    assert offer_audience(TemplateVisibility.PRIVATE, author_id=AUTHOR).level is Visibility.PERSONAL
    assert (
        offer_audience(TemplateVisibility.ORGANISATION, author_id=AUTHOR).level
        is Visibility.COMPANY
    )
    assert (
        offer_audience(TemplateVisibility.CATALOGUE, author_id=AUTHOR).level is Visibility.COMPANY
    )


def test_only_a_catalogue_template_may_leave_this_installation() -> None:
    """Deleting this lets the third level collapse into the second, and a template marked
    organisation would be exportable: the distinction between "everybody here" and "beyond
    here" is the only thing the third level exists to carry.
    """
    assert not may_leave_this_installation(TemplateVisibility.PRIVATE)
    assert not may_leave_this_installation(TemplateVisibility.ORGANISATION)
    assert may_leave_this_installation(TemplateVisibility.CATALOGUE)
    assert _published(visibility=TemplateVisibility.CATALOGUE).may_leave_this_installation
    assert not _published().may_leave_this_installation


def test_a_private_template_is_installable_by_its_author_and_absent_for_everybody_else() -> None:
    """Deleting this lets a private template appear in everybody's picker, which is the
    save-as-template flow publishing an agent nobody agreed to share. Driven through the real
    catalogue, so what is asserted is what `installable_ids` and `open_for` actually answer.
    """
    catalogue = TemplateCatalogue()
    private = _published()
    catalogue.offer(private.signed, audience=offer_for(private).audience)

    mine = AgentViewer(principal_id=AUTHOR)
    theirs = AgentViewer(principal_id=OUTSIDER)
    assert catalogue.installable_ids(mine) == frozenset({AUTHORED})
    assert catalogue.installable_ids(theirs) == frozenset()
    with pytest.raises(NoSuchTemplateError):
        catalogue.open_for(AUTHORED, theirs)

    shared = set_visibility(private, TemplateVisibility.ORGANISATION)
    catalogue.offer(shared.signed, audience=offer_for(shared).audience)
    assert catalogue.installable_ids(theirs) == frozenset({AUTHORED})


def test_the_template_visibility_is_not_the_audience_of_the_agent_it_becomes() -> None:
    """Deleting this lets the agent's audience default to the template's level, so installing
    a catalogue template would publish the resulting agent to all 126 staff without anybody
    choosing that. `complete` takes the agent's audience as its own argument and this proves
    it never reads the offer's.
    """
    installed = _install(
        _published(visibility=TemplateVisibility.CATALOGUE), {"price_list": "the rate card"}
    )
    assert installed.record.audience == _audience()
    assert installed.record.audience.level is Visibility.PERSONAL

    mine = AgentViewer(principal_id=AUTHOR)
    theirs = AgentViewer(principal_id=OUTSIDER)
    assert visible_agent_ids([installed.record], mine) == frozenset({INSTALLED})
    assert visible_agent_ids([installed.record], theirs) == frozenset()


# ------------------------------------------------------------------- the whole loop
def test_an_authored_template_installs_through_the_real_wizard_with_its_redaction_intact() -> None:
    """Deleting this leaves every other test in this file asserting about objects nobody
    installs. The redaction is only worth anything if it survives the round trip into an
    `AgentRecord`, and the round trip is the real `Offer`, `TemplateCatalogue`, `begin`,
    `provide` and `complete` rather than this file's idea of them.
    """
    published = _published(
        choices={
            CONTACT: _hoist("escalation_contact", "Who is escalated to?"),
            TICKET: _redact(),
        },
        visibility=TemplateVisibility.ORGANISATION,
    )
    catalogue = TemplateCatalogue()
    catalogue.offer(published.signed, audience=offer_for(published).audience)

    offer = catalogue.open_for(AUTHORED, AgentViewer(principal_id=OUTSIDER))
    draft = begin(offer, instance_id=INSTALLED, installer=OUTSIDER)
    draft = provide(draft, "price_list", "the 2026 rate card")
    draft = provide(draft, "escalation_contact", "whoever is on call")
    installed = complete(
        draft, key=KEY, audience=_audience(OUTSIDER), registry=ConnectorRegistry(), at=LATER
    )

    assert installed.completeness.badge is InstallBadge.READY
    assert installed.record.persona == published.signed.manifest.persona
    assert CONTACT not in installed.record.persona
    assert TICKET not in installed.record.persona
    assert CLIENT in installed.record.persona
    assert installed.record.authority.max_side_effect is SideEffect.SEND


def test_every_location_the_scanner_reports_roots_at_a_manifest_path() -> None:
    """Deleting this lets `scan` grow a second idea of what a path is. The overlay, the seal,
    the ownership map and M13.4's per-path diff are all keyed by the flat document, so a
    location reading `identity/display_name` rather than `identity.display_name` would send
    whoever followed it to a path none of those four has.
    """
    report = scan(_draft().manifest.document())
    reported = {literal.location for literal in report.passed} | {
        location for item in report.items for location in item.locations
    }
    assert reported
    assert all(location.split("/")[0] in MANIFEST_PATHS for location in reported)
    assert "identity.display_name" in reported
    assert "authority.scope/clauses/0/value" in reported
