"""The install flow: six steps, one code path, and an agent that starts at the floor.

Every test here is a way an install could hand somebody more than the template said they
could have, or could look finished when it is not.

**The floor tests come in pairs and the positive half is the load-bearing one.** A pin that
holds every agent at SHADOW passes every refusal test in this file and is useless, so
`test_an_install_keeps_the_rung_its_template_declared_once_the_connector_serves` sits beside
the pin test and fails for exactly that implementation.

**The seal is tested by delegation rather than by outcome.** Refusing a sealed path proves
nothing about where the refusal came from, so
`test_the_wizard_keeps_no_second_list_of_sealed_paths` walks the module's own namespace for
a collection holding one, which is what a third copy would have to be.

**Reach is computed by the real gate.** `rehearse` is asserted through
`brain.gate.invoke.invoke`, the real `brain.tools.registry.ToolRegistry` and the real
`EntitlementSet.intersect`, for the reason `tests/unit/test_agent_model.py` gives: a stand-in
would leave these asserting that this file's idea of a run agrees with itself.

Task ids: M13.3.1, M13.3.2, M13.3.3, M13.3.4, M13.3.5, M13.3.6, M13.3.7
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import JsonValue, ValidationError

import brain.agents.install as install_module
from brain.agents.install import (
    REQUIRED_FIELDS,
    STEP_FIELDS,
    STEP_ORDER,
    ConnectorReadiness,
    Installation,
    InstallBadge,
    InstallDraft,
    Missing,
    MissingKind,
    NoSuchTemplateError,
    Offer,
    Step,
    TemplateCatalogue,
    answer,
    begin,
    begin_hand_built,
    blank_offer,
    complete,
    completeness,
    connector_readiness,
    plan,
    provide,
    rehearse,
    rehearse_golden_set,
)
from brain.agents.model import (
    DISPLAY_NAME_CHARS,
    PERSONA_CHARS,
    AgentAudience,
    AgentState,
    AgentViewer,
    runnable_agent_ids,
    visible_agent_ids,
)
from brain.agents.template import (
    BLANK_TEMPLATE_ID,
    SEALED_PATHS,
    SETTABLE_PATHS,
    GoldenCase,
    LeashRung,
    ManifestAuthority,
    ManifestGuardrails,
    ManifestIdentity,
    Placeholder,
    SealedPathError,
    SignedManifest,
    TemplateError,
    TemplateManifest,
    publish,
)
from brain.connectors.contract import ConnectorScope, CredentialBinding, TransportKind
from brain.connectors.manifest import (
    ChangeSignal,
    ConnectorManifest,
    FieldShape,
    HotUse,
    ProjectedEntity,
    ProjectedField,
    ToolDeclaration,
)
from brain.connectors.registry import ConnectorRegistry, ConnectorState
from brain.core.entitlement import Capability, EntitlementSet, Grant
from brain.core.envelope import Entity, IdentityMode, SideEffect, ToolDefinition, TypedResult
from brain.core.scope import Scope
from brain.gate.injection import AutonomyTier, RiskAssessment
from brain.knowledge.visibility import Visibility
from brain.models.routing import Tier
from brain.ops.secrets import SecretRef, VaultRole
from brain.tools.registry import ToolRegistry

NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)

KEY = "a-signing-key"
OTHER_KEY = "somebody-elses-key"

PUBLISHER = "u_wei_ling"
INSTALLER = "u_aaron"
OUTSIDER = "u_jason"

AGENT_ID = "support_desk"
CONNECTOR = "freshdesk"

CLEAN = RiskAssessment(score=0, matched=())

# The two tools the rehearsals project over. Named here because three tests assert on which
# of them a person reaches, and a name typed twice is a name that can be typed differently.
CLIENT_TOOL = "client.read_summary"
TICKET_TOOL = "ticket.read_status"


# ------------------------------------------------------------------------------- fixtures
class ClientRow(Entity):
    name: str = ""


def a_handler() -> TypedResult[ClientRow]:
    return TypedResult[ClientRow]()


def _tool(name: str, capability: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="does a thing",
        entity=name.split(".")[0],
        required_capability=capability,
        side_effect=SideEffect.NONE,
        identity_mode=IdentityMode.DELEGATED,
    )


def _tools() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_tool(CLIENT_TOOL, "read:client.name"), a_handler)
    registry.register(_tool(TICKET_TOOL, "read:ticket.status"), a_handler)
    return registry


def _reach(*capabilities: str, principal_id: str) -> EntitlementSet:
    return EntitlementSet(
        principal_id=principal_id,
        grants=tuple(
            Grant(capability=Capability(value=v), scope=Scope.unrestricted()) for v in capabilities
        ),
    )


def _connector_manifest() -> ConnectorManifest:
    return ConnectorManifest(
        name=CONNECTOR,
        version="1.0.0",
        transport=TransportKind.REST,
        scope=ConnectorScope(resource_kind="view", selectors=("tickets",)),
        credential=CredentialBinding(
            ref=SecretRef(path="kv/freshdesk", role=VaultRole.APPLICATION)
        ),
        tools=(
            ToolDeclaration(
                name="freshdesk.read_ticket",
                description="One ticket from the helpdesk.",
                entity="ticket",
            ),
        ),
        projections=(
            ProjectedEntity(
                entity="ticket",
                fields=(
                    ProjectedField(name="id", shape=FieldShape.IDENTIFIER, uses=(HotUse.IDENTIFY,)),
                    ProjectedField(name="status", shape=FieldShape.STATUS, uses=(HotUse.FILTER,)),
                    ProjectedField(
                        name="display_name", shape=FieldShape.LABEL, uses=(HotUse.SORT,)
                    ),
                ),
                change_signal=ChangeSignal.WEBHOOK,
                visibility=Scope.department("maintenance"),
            ),
        ),
    )


def _connectors(*, serving: bool | None) -> ConnectorRegistry:
    """A registry in one of the three states an indicator distinguishes.

    `None` means nothing by that name is installed at all, which is a different problem for
    an installer from one that is installed and switched off.
    """
    registry = ConnectorRegistry()
    if serving is None:
        return registry
    registry.register(_connector_manifest(), now=NOW)
    if serving:
        registry.enable(CONNECTOR, now=NOW)
    return registry


def _manifest(**overrides: Any) -> TemplateManifest:
    defaults: dict[str, Any] = {
        "identity": ManifestIdentity(
            template_id="support_template",
            version=1,
            published_by=PUBLISHER,
            display_name="Support Desk",
            summary="Answers helpdesk questions.",
        ),
        "persona": "Answer helpdesk questions from the ticket record and nothing else.",
        "tier": Tier.MAIN,
        "authority": ManifestAuthority(
            capabilities=(Capability(value="read:client.name"),),
            allowed_tools=(CLIENT_TOOL, TICKET_TOOL),
        ),
        "connectors": (CONNECTOR,),
        "guardrails": ManifestGuardrails(
            max_side_effect=SideEffect.SEND,
            leash=(
                LeashRung(target="ticket.update_status", rung=AutonomyTier.AUTONOMOUS),
                LeashRung(target=CLIENT_TOOL, rung=AutonomyTier.AUTONOMOUS),
                LeashRung(target=TICKET_TOOL, rung=AutonomyTier.AUTONOMOUS),
            ),
        ),
        "golden_set": (
            GoldenCase(
                question="How many hours has Tomato Glasses left?",
                expectation="A number of hours, with the client named and a read time.",
            ),
        ),
        "placeholders": (
            Placeholder(key="price_list", prompt="Which price list applies?"),
            Placeholder(key="team_notes", prompt="Anything else?", required=False),
        ),
    }
    defaults.update(overrides)
    return TemplateManifest(**defaults)


def _signed(manifest: TemplateManifest | None = None, *, key: str = KEY) -> SignedManifest:
    return publish(manifest or _manifest(), key=key, signed_by=PUBLISHER, at=NOW)


def _offer(
    manifest: TemplateManifest | None = None,
    *,
    key: str = KEY,
    audience: AgentAudience | None = None,
) -> Offer:
    return Offer(
        signed=_signed(manifest, key=key),
        audience=audience or AgentAudience(level=Visibility.PERSONAL, owner_id=INSTALLER),
    )


def _audience() -> AgentAudience:
    return AgentAudience(level=Visibility.PERSONAL, owner_id=INSTALLER)


def _filled(draft: InstallDraft) -> InstallDraft:
    """Every required placeholder answered, so only the property under test is missing."""
    return provide(draft, "price_list", "the 2026 maintenance rate card")


def _draft(manifest: TemplateManifest | None = None, **offer_kwargs: Any) -> InstallDraft:
    return begin(_offer(manifest, **offer_kwargs), instance_id=AGENT_ID, installer=INSTALLER)


def _install(
    draft: InstallDraft | None = None,
    *,
    serving: bool | None = True,
    audience: AgentAudience | None = None,
) -> Installation:
    return complete(
        draft if draft is not None else _filled(_draft()),
        key=KEY,
        audience=audience or _audience(),
        registry=_connectors(serving=serving),
        at=NOW,
    )


# ------------------------------------------------------- the six steps (M13.3.1)
def test_the_wizard_has_six_steps_in_the_order_it_was_argued_for() -> None:
    """Deleting this lets a seventh screen arrive, one be dropped, or the six be reordered
    with nothing failing. The order is an argument rather than a preference: the ceiling is
    chosen before the connectors are bound, because a connector nothing may reach is a
    binding nobody needed.

    The six are written out here rather than compared against `STEP_ORDER`. Comparing the
    plan against the constant it was built from passes for every order the constant could
    hold, which is the same self-comparison that let a repointed ceiling name pass its own
    test on 2026-09-06. A swap in the constant fails this line."""
    assert len(STEP_ORDER) == 6
    assert len(set(STEP_ORDER)) == 6
    assert set(STEP_ORDER) == set(Step)
    built = plan()
    assert tuple(step.step for step in built.steps) == (
        Step.IDENTITY,
        Step.PERSONA,
        Step.AUTHORITY,
        Step.CONNECTORS,
        Step.PLACEHOLDERS,
        Step.GOLDEN_SET,
    )
    assert tuple(step.position for step in built.steps) == (1, 2, 3, 4, 5, 6)


def test_the_wizard_collects_every_settable_path_and_nothing_else() -> None:
    """Deleting this lets a manifest field arrive that the form never shows, so nobody can
    set it and no failure says so. It is asserted against `SETTABLE_PATHS`, which is derived
    from the manifest's own path list, rather than against a list written here."""
    assert sorted(plan().paths) == sorted(SETTABLE_PATHS)


def test_a_step_table_leaving_a_settable_path_uncollected_is_refused() -> None:
    """Deleting this leaves the rule above provable only by editing the module's constant.
    A path in no step is a field nobody can fill and the wizard reports itself complete."""
    table = {**STEP_FIELDS, Step.GOLDEN_SET: ()}
    with pytest.raises(TemplateError, match="golden_set"):
        plan(table)


def test_a_step_table_collecting_one_path_twice_is_refused() -> None:
    """Deleting this lets one field appear on two screens, where its value is decided by
    whichever screen the person saved last and neither screen says so."""
    table = {**STEP_FIELDS, Step.IDENTITY: ("identity.display_name", "identity.summary", "persona")}
    with pytest.raises(TemplateError, match="persona"):
        plan(table)


def test_no_wizard_step_may_collect_a_sealed_path() -> None:
    """Deleting this lets the form offer a sealed field, and a form is exactly where a
    sealed path would be offered: it looks like every other field on the screen. The refusal
    is `check_overlay`'s, which is what proves the wizard has no opinion of its own."""
    table = {**STEP_FIELDS, Step.AUTHORITY: (*STEP_FIELDS[Step.AUTHORITY], "guardrails.leash")}
    with pytest.raises(SealedPathError):
        plan(table)


def test_a_wizard_field_carries_the_manifests_own_schema_for_its_own_path() -> None:
    """Deleting this lets the generator resolve every path to the same fragment, which
    renders six screens of identical controls that all look plausible. The four paths below
    have four different shapes, so a resolver that stops at the section, or ignores the
    section, disagrees with at least one of them."""
    built = plan()
    persona = built.field_for("persona").schema
    display_name = built.field_for("identity.display_name").schema
    capabilities = built.field_for("authority.capabilities").schema
    assert persona["type"] == "string"
    assert persona["maxLength"] == PERSONA_CHARS
    assert display_name["type"] == "string"
    assert display_name["maxLength"] == DISPLAY_NAME_CHARS
    assert capabilities["type"] == "array"
    # The two string fields are told apart by their bounds, so a resolver returning the
    # first string field it finds for both would fail here rather than look correct.
    assert persona["maxLength"] != display_name["maxLength"]


def test_a_field_that_is_a_reference_keeps_the_default_beside_the_definition() -> None:
    """Deleting this lets the resolver return a bare definition, and `tier` is the one field
    with a sensible default: the form would then present the routing pool with nothing
    selected and whoever installs it would pick one at random."""
    tier = plan().field_for("tier").schema
    assert tier["default"] == Tier.MAIN.value
    assert Tier.MAIN.value in tier["enum"]


def test_a_path_no_step_shows_has_no_step() -> None:
    """Deleting this lets a console ask which screen an unknown field belongs on and get a
    plausible answer, which is how a field ends up rendered on whichever screen was first."""
    built = plan()
    with pytest.raises(TemplateError, match=r"guardrails\.leash"):
        built.step_for("guardrails.leash")


# ------------------------------------------------- the seal, and where it is not (M13.3.1)
def test_an_install_may_not_overlay_a_sealed_path() -> None:
    """Deleting this lets an installer relax the supervision the publisher signed, which is
    the whole of what sealing the guardrails buys."""
    draft = _draft()
    with pytest.raises(SealedPathError):
        answer(draft, "guardrails.max_side_effect", "external")


def test_an_install_may_not_reach_a_sealed_path_by_another_spelling() -> None:
    """Deleting this leaves a wizard that an exact-match deny list satisfies. `guardrails`
    equals no sealed path and sets both of them; `guardrails.leash.0.rung` equals none of
    them and raises one target's rung, which is the smallest version of the edit the seal
    exists to refuse. Both are refused by the companion rule rather than by the five names,
    which is why `answer` delegates the whole question rather than checking the five."""
    draft = _draft()
    with pytest.raises(TemplateError):
        answer(draft, "guardrails", {"max_side_effect": "money", "leash": []})
    with pytest.raises(TemplateError):
        answer(draft, "guardrails.leash.0.rung", int(AutonomyTier.AUTONOMOUS))


def test_an_install_may_not_mention_a_path_the_manifest_does_not_have() -> None:
    """Deleting this lets a form post a key nothing reads, which sits in the overlay looking
    configured and is refused later by a check constraint as a database fault."""
    draft = _draft()
    with pytest.raises(TemplateError, match="not a path"):
        answer(draft, "authority.everything", True)


def test_a_settable_path_is_settable_through_the_wizard() -> None:
    """The positive half. A wizard tested only by its refusals is satisfied by one that
    refuses every field, and the whole flow would then collect nothing."""
    draft = answer(_draft(), "persona", "Answer only from the ticket record.")
    assert draft.answers["persona"] == "Answer only from the ticket record."
    assert _install(_filled(draft)).record.persona == "Answer only from the ticket record."


def test_the_wizard_keeps_no_second_list_of_sealed_paths() -> None:
    """Deleting this lets a third copy of the seal grow here, and a third copy is a third
    thing to keep in step: the one a person fills a form against is the one that would be
    looser. Walks the module's own namespace rather than its source text, so an imported
    list and a retyped literal are both caught."""
    sealed = set(SEALED_PATHS)
    for name, value in vars(install_module).items():
        if name.startswith("_"):
            continue
        members: set[str] = set()
        if isinstance(value, tuple | list | set | frozenset):
            members = {str(item) for item in value}
        elif isinstance(value, Mapping):
            members = {
                str(item)
                for group in value.values()
                if isinstance(group, tuple | list | set | frozenset)
                for item in group
            }
        assert not (sealed & members), f"{name} holds a sealed path"


# ------------------------------------------- placeholders and what is missing (M13.3.3)
def test_a_placeholder_the_template_asks_for_is_answerable() -> None:
    """The positive half of the refusal below. A `provide` that refused every key would pass
    that test and collect nothing, and every install would then be permanently incomplete."""
    draft = provide(_draft(), "price_list", "the 2026 maintenance rate card")
    assert draft.placeholder_answers["price_list"] == "the 2026 maintenance rate card"


def test_a_placeholder_answer_to_a_question_the_template_never_asked_is_refused() -> None:
    """Deleting this lets an answer be stored under a key nothing reads. The required
    placeholder it was meant for still reads as missing, and the person who typed it has no
    way to tell which of the two happened."""
    with pytest.raises(TemplateError, match="price_list"):
        provide(_draft(), "pricelist", "the 2026 maintenance rate card")


def test_an_install_missing_a_required_placeholder_is_incomplete_and_names_it() -> None:
    """Deleting this lets an agent go live answering from a blank where a price list should
    be, confidently, which is the failure `Placeholder` exists to prevent."""
    report = completeness(_draft(), connector_readiness((), ConnectorRegistry()))
    assert report.badge is InstallBadge.INCOMPLETE
    assert Missing(kind=MissingKind.PLACEHOLDER, name="price_list") in report.missing


def test_an_optional_placeholder_left_blank_does_not_hold_an_install_open() -> None:
    """Deleting this lets `required` stop being read, so every placeholder becomes
    mandatory and an install can never finish while an optional question is unanswered."""
    report = completeness(_filled(_draft()), connector_readiness((), ConnectorRegistry()))
    assert report.badge is InstallBadge.READY
    assert report.missing == ()


def test_a_finished_install_carries_the_answers_it_was_given() -> None:
    """Deleting this lets the answers be collected and dropped on the way through, so the
    install reports itself complete having stored nothing anybody typed."""
    assert _install().placeholder_answers == {"price_list": "the 2026 maintenance rate card"}


# ------------------------------------------- connector readiness and the pin (M13.3.2, M13.3.7)
def test_a_connector_indicator_tells_absent_apart_from_installed_but_not_serving() -> None:
    """Deleting this collapses three problems into one amber light. Nothing installed needs
    somebody to install it, installed and off needs somebody to switch it on, and the
    installer cannot tell which without being told."""
    absent = connector_readiness((CONNECTOR,), _connectors(serving=None))[0]
    idle = connector_readiness((CONNECTOR,), _connectors(serving=False))[0]
    live = connector_readiness((CONNECTOR,), _connectors(serving=True))[0]
    assert absent == ConnectorReadiness(name=CONNECTOR, state=None, ready=False)
    assert idle == ConnectorReadiness(name=CONNECTOR, state=ConnectorState.REGISTERED, ready=False)
    assert live == ConnectorReadiness(name=CONNECTOR, state=ConnectorState.ENABLED, ready=True)


def test_an_install_is_held_at_shadow_while_a_declared_connector_is_not_serving() -> None:
    """Deleting this lets an agent act unsupervised on a partial picture. The template said
    AUTONOMOUS on the assumption its connectors were there; with one unbound the agent acts
    on whatever it can still reach and the missing half is invisible in the result."""
    installed = _install(serving=False)
    assert installed.is_pinned_to_shadow
    assert installed.leash.rung_for(AGENT_ID, "ticket.update_status", {}) is AutonomyTier.SHADOW
    assert Missing(kind=MissingKind.CONNECTOR, name=CONNECTOR) in installed.completeness.missing


def test_an_install_keeps_the_rung_its_template_declared_once_the_connector_serves() -> None:
    """The positive half, and the one that matters most in this file. A pin that returned
    SHADOW unconditionally passes every refusal test here and makes the leash a decoration:
    no template could ever grant autonomy and nobody would see why."""
    installed = _install(serving=True)
    assert not installed.is_pinned_to_shadow
    assert installed.leash.rung_for(AGENT_ID, "ticket.update_status", {}) is AutonomyTier.AUTONOMOUS


def test_the_pin_follows_the_connector_rather_than_remembering_an_outage() -> None:
    """Deleting this lets the pin be stored at install time. It would then hold an agent at
    SHADOW after the connector came back, and say nothing at all when one is disabled a
    month later, which is the direction that matters."""
    draft = _filled(_draft())
    assert (
        _install(draft, serving=True).leash.rung_for(AGENT_ID, TICKET_TOOL, {})
        is AutonomyTier.AUTONOMOUS
    )
    assert (
        _install(draft, serving=False).leash.rung_for(AGENT_ID, TICKET_TOOL, {})
        is AutonomyTier.SHADOW
    )


def test_the_pin_keeps_the_targets_the_template_configured() -> None:
    """Deleting this lets the pin empty the leash instead of lowering it. Both answer SHADOW,
    so no rung test would notice, and the console would lose the list of what this agent will
    be trusted with once the connector is back."""
    pinned = _install(serving=False).leash
    assert {entry.target for entry in pinned.entries} == {
        "ticket.update_status",
        CLIENT_TOOL,
        TICKET_TOOL,
    }
    assert {entry.rung for entry in pinned.entries} == {AutonomyTier.SHADOW}


# ------------------------------------------------- every new agent starts at the floor
def test_a_hand_built_agent_is_shadow_everywhere_and_has_no_side_effect() -> None:
    """Deleting this lets the wizard raise the floor as a convenience, which is the whole
    guardrail undone: a hand-built agent is meant to be the most supervised agent in the
    estate and nothing in an overlay can change that."""
    draft = answer(
        begin_hand_built(key=KEY, at=NOW, instance_id="notes_helper", installer=INSTALLER),
        "persona",
        "Summarise my own notes and nothing else.",
    )
    installed = complete(draft, key=KEY, audience=_audience(), registry=ConnectorRegistry(), at=NOW)
    assert installed.instance.template_id == BLANK_TEMPLATE_ID
    assert installed.record.authority.max_side_effect is SideEffect.NONE
    assert installed.leash.entries == ()
    for target in ("client", "invoice.send", "ticket.update_status"):
        assert installed.leash.rung_for("notes_helper", target, {}) is AutonomyTier.SHADOW


def test_a_hand_built_agent_and_an_installed_template_finish_through_one_function() -> None:
    """Deleting this lets the two starting points drift into two flows, and the hand-built
    one is the one that gets the lighter review because it looks like the simple case."""
    hand_built = answer(
        begin_hand_built(key=KEY, at=NOW, instance_id="notes_helper", installer=INSTALLER),
        "persona",
        "Summarise my own notes.",
    )
    from_template = _filled(_draft())
    both = (
        complete(hand_built, key=KEY, audience=_audience(), registry=ConnectorRegistry(), at=NOW),
        _install(from_template),
    )
    assert all(isinstance(one, Installation) for one in both)
    assert both[0].instance.template_id == BLANK_TEMPLATE_ID
    assert both[1].instance.template_id == "support_template"


def test_there_is_one_function_that_finishes_an_install() -> None:
    """Deleting this lets a second constructor be added beside `complete`, which is how one
    code path becomes two without anybody deciding to. Reads the module's own annotations
    rather than trusting the docstring that claims it."""
    finishers = sorted(
        name
        for name, value in vars(install_module).items()
        if callable(value) and getattr(value, "__annotations__", {}).get("return") == "Installation"
    )
    assert finishers == ["complete"]


# ----------------------------------------------------- what is missing, and the badge (M13.3.6)
def test_an_incomplete_install_is_not_selectable() -> None:
    """Deleting this leaves an amber badge and a live agent. The badge is read by whoever
    opens the console; the agent is chosen by whoever asks a question, and they never see
    it. Asserted through the real `runnable_agent_ids`, and paired with the audience answer
    to show it is the lifecycle doing the work rather than visibility."""
    installed = _install(serving=False)
    viewer = AgentViewer(principal_id=INSTALLER)
    assert installed.completeness.badge is InstallBadge.INCOMPLETE
    assert installed.record.state is AgentState.DISABLED
    assert visible_agent_ids([installed.record], viewer) == frozenset({AGENT_ID})
    assert runnable_agent_ids([installed.record], viewer) == frozenset()


def test_a_finished_install_is_selectable() -> None:
    """The positive half. A `complete` that disabled everything would pass the test above
    and produce an estate in which no agent can ever answer."""
    installed = _install(serving=True)
    assert installed.completeness.badge is InstallBadge.READY
    assert installed.record.state is AgentState.ENABLED
    assert runnable_agent_ids([installed.record], AgentViewer(principal_id=INSTALLER)) == frozenset(
        {AGENT_ID}
    )


def test_every_required_field_is_one_a_constructor_refuses_blank() -> None:
    """Deleting this lets `REQUIRED_FIELDS` name a field nothing actually requires, so the
    report would hold an install open for a value that was never needed. Each path is
    asserted against the constructors rather than against the tuple it came from."""
    for path in REQUIRED_FIELDS:
        draft = answer(_filled(_draft()), path, "")
        with pytest.raises(ValidationError):
            _install(draft)


def test_a_draft_with_no_persona_is_reported_incomplete_before_it_is_refused() -> None:
    """Deleting this leaves the wizard silent about the one field it cannot finish without.
    A blank persona is refused by `AgentRecord`, which is the right place for the refusal and
    the wrong place for the warning: the person hits save and gets a validation error where
    an amber badge should have told them three screens earlier."""
    fresh = begin_hand_built(key=KEY, at=NOW, instance_id="notes_helper", installer=INSTALLER)
    report = completeness(fresh, connector_readiness((), ConnectorRegistry()))
    assert report.badge is InstallBadge.INCOMPLETE
    assert Missing(kind=MissingKind.FIELD, name="persona") in report.missing


def test_a_field_outside_the_required_ones_may_be_left_blank() -> None:
    """The positive half. A required list that grew to cover every path would hold every
    install open for a summary nobody needs to write."""
    draft = answer(_filled(_draft()), "identity.summary", "")
    assert _install(draft).completeness.badge is InstallBadge.READY


def test_the_report_reads_the_values_materialise_produces() -> None:
    """Deleting this lets the report and the materialised agent drift. The report is a flat
    read of the document with the answers laid over it, shown before a manifest can be built
    at all; `materialise` is the authority, and this is what holds the two together on the
    paths the report actually reads."""
    draft = _filled(answer(_draft(), "persona", "Answer only from the ticket record."))
    installed = _install(draft)
    document = installed.effective.document
    assert install_module._text(draft, "persona") == document["persona"]
    assert install_module._text(draft, "identity.display_name") == document["identity.display_name"]
    assert tuple(p.key for p in install_module._effective_placeholders(draft)) == tuple(
        p.key for p in installed.effective.manifest.placeholders
    )


# --------------------------------------------------- who may install, and who may see (M13.3)
def test_a_template_a_person_may_not_install_answers_as_one_that_does_not_exist() -> None:
    """Deleting this lets a picker tell a reader which templates exist but are not theirs,
    and a catalogue of templates is a list of what a company does: the departments, the
    systems and the problems, readable by trying names."""
    offering = TemplateCatalogue()
    offering.offer(_signed(), audience=_audience())
    empty = TemplateCatalogue()
    outsider = AgentViewer(principal_id=OUTSIDER)

    # The same id is asked of both catalogues, so the two sentences are comparable
    # character for character rather than allowing for the name each one echoed back.
    with pytest.raises(NoSuchTemplateError) as hidden:
        offering.open_for("support_template", outsider)
    with pytest.raises(NoSuchTemplateError) as absent:
        empty.open_for("support_template", outsider)
    assert str(hidden.value) == str(absent.value)
    assert type(hidden.value) is type(absent.value)


def test_a_template_offered_to_somebody_opens_for_them() -> None:
    """The positive half. A catalogue that refused everybody would pass the test above and
    make every template uninstallable, with the refusal reading as absence."""
    catalogue = TemplateCatalogue()
    catalogue.offer(_signed(), audience=_audience())
    opened = catalogue.open_for("support_template", AgentViewer(principal_id=INSTALLER))
    assert opened.template_id == "support_template"
    assert opened.version == 1


def test_a_listing_of_installable_templates_says_nothing_about_what_it_withheld() -> None:
    """Deleting this lets a count of hidden templates be added, directly or by subtraction.
    'Showing 1 of 4' tells the reader there are three things this company does that they
    were not told about. A frozenset has nowhere to put one."""
    catalogue = TemplateCatalogue()
    catalogue.offer(_signed(), audience=_audience())
    catalogue.offer(
        _signed(
            _manifest(
                identity=ManifestIdentity(
                    template_id="debt_chaser",
                    version=1,
                    published_by=PUBLISHER,
                    display_name="Debt Chaser",
                )
            )
        ),
        audience=AgentAudience(level=Visibility.PERSONAL, owner_id=PUBLISHER),
    )
    listed = catalogue.installable_ids(AgentViewer(principal_id=INSTALLER))
    assert isinstance(listed, frozenset)
    assert listed == frozenset({"support_template"})


def test_the_audience_of_a_template_is_not_the_audience_of_the_agent_it_installs() -> None:
    """Deleting this lets installing something everybody could install publish an agent to
    everybody, which is how a finance assistant appears in the picker of all 126 staff with
    nobody having chosen that."""
    company_wide = AgentAudience(level=Visibility.COMPANY, owner_id=PUBLISHER)
    installed = _install(
        _filled(_draft(audience=company_wide)),
        audience=AgentAudience(level=Visibility.PERSONAL, owner_id=INSTALLER),
    )
    assert installed.record.audience.level is Visibility.PERSONAL
    assert visible_agent_ids([installed.record], AgentViewer(principal_id=OUTSIDER)) == frozenset()


def test_a_manifest_this_installation_did_not_sign_cannot_be_installed() -> None:
    """Deleting this lets a template from anywhere become an agent everybody can start. The
    refusal is `verify`'s, called by `install`, and this is the assertion that the flow goes
    through it rather than round it."""
    draft = _filled(_draft(key=OTHER_KEY))
    with pytest.raises(TemplateError, match="signature"):
        _install(draft)


# --------------------------------------------------- the golden set (M13.3.4, M13.3.5)
def test_the_golden_set_is_rehearsed_as_two_people_and_each_answer_is_reported() -> None:
    """Deleting this lets an install be proved by the administrator who did it. The run that
    finds anything is the second one: a template whose tools nobody ordinary can reach
    assembles perfectly for the person setting it up."""
    installed = _install()
    rehearsal = rehearse_golden_set(
        installed,
        registry=_tools(),
        installer=_reach("read:client.name", "read:ticket.status", principal_id=INSTALLER),
        fixture=_reach("invoke:agent", principal_id=OUTSIDER),
        assessment=CLEAN,
        now=NOW,
    )
    assert rehearsal.installer.started
    assert rehearsal.installer.reachable == (CLIENT_TOOL,)
    assert not rehearsal.fixture.started
    assert rehearsal.fixture.reachable == ()
    assert rehearsal.fixture.rung is None
    assert not rehearsal.both_started


def test_a_golden_set_cannot_be_rehearsed_twice_as_the_same_person() -> None:
    """Deleting this lets both halves of the report be the same run under two labels, which
    reads as having checked a low-privilege user and has checked nobody."""
    reach = _reach("read:client.name", principal_id=INSTALLER)
    with pytest.raises(TemplateError, match="twice"):
        rehearse_golden_set(
            _install(),
            registry=_tools(),
            installer=reach,
            fixture=reach,
            assessment=CLEAN,
            now=NOW,
        )


def test_a_rehearsal_applies_the_agents_ceiling_and_not_only_the_callers_reach() -> None:
    """Deleting this lets the rehearsal report a run wider than any real one. `invoke` does
    not narrow the entitlement it is given, so the lens has to be applied before it: the
    caller here holds both capabilities and the template's ceiling admits one."""
    rehearsal = rehearse(
        _install(),
        registry=_tools(),
        entitlement=_reach("read:client.name", "read:ticket.status", principal_id=INSTALLER),
        assessment=CLEAN,
        now=NOW,
    )
    assert rehearsal.started
    assert rehearsal.reachable == (CLIENT_TOOL,)


def test_a_rehearsal_runs_at_the_pinned_rung_rather_than_the_one_the_template_declared() -> None:
    """Deleting this lets the rehearsal be driven with the template's own leash, so it would
    report AUTONOMOUS for a run that the same install would hold at SHADOW. The rehearsal is
    the thing somebody reads before deciding the install is safe."""
    reach = _reach("read:client.name", principal_id=INSTALLER)
    served = rehearse(
        _install(serving=True), registry=_tools(), entitlement=reach, assessment=CLEAN, now=NOW
    )
    unbound = rehearse(
        _install(serving=False), registry=_tools(), entitlement=reach, assessment=CLEAN, now=NOW
    )
    assert served.rung is AutonomyTier.AUTONOMOUS
    assert unbound.rung is AutonomyTier.SHADOW


def test_a_rehearsal_carries_the_questions_the_template_asks() -> None:
    """Deleting this leaves a reach report with no questions beside it. Judging an answer
    needs a model and there is none here, so the questions are what the person doing the
    judging works from."""
    rehearsal = rehearse(
        _install(),
        registry=_tools(),
        entitlement=_reach("read:client.name", principal_id=INSTALLER),
        assessment=CLEAN,
        now=NOW,
    )
    assert rehearsal.questions == ("How many hours has Tomato Glasses left?",)


def test_a_draft_records_who_is_installing_and_the_overlay_is_owned_by_them() -> None:
    """Deleting this lets an install attribute the values somebody typed to nobody, and the
    per-path ownership an upgrade review reads would have a gap exactly where a local edit
    was made."""
    draft = _filled(answer(_draft(), "persona", "Answer only from the ticket record."))
    installed = _install(draft)
    owner = installed.effective.owners["persona"]
    assert owner.set_by == INSTALLER
    assert owner.set_at == NOW
    assert installed.effective.owners["identity.template_id"].set_by == PUBLISHER


def test_an_overlay_assembled_outside_the_wizard_is_still_checked() -> None:
    """Deleting this trusts a draft built by a caller who never called `answer`. `install`
    checks the whole overlay again for exactly that case, and this asserts the flow reaches
    that check rather than relying on the per-field one."""
    # An overlay assembled from a posted form rather than field by field through `answer`.
    smuggled: Mapping[str, JsonValue] = {"identity.version": 99}
    draft = InstallDraft(
        offer=_offer(),
        instance_id=AGENT_ID,
        installer=INSTALLER,
        answers=smuggled,
        placeholder_answers={"price_list": "the 2026 maintenance rate card"},
    )
    with pytest.raises(SealedPathError):
        _install(draft)


def test_the_blank_offer_is_offered_to_the_person_building_the_agent() -> None:
    """Deleting this lets the blank template be offered company-wide, which would put a
    row in everybody's picker for a template that is a starting point rather than a thing."""
    offered = blank_offer(key=KEY, at=NOW, installer=INSTALLER)
    assert offered.template_id == BLANK_TEMPLATE_ID
    assert offered.audience.level is Visibility.PERSONAL
    assert offered.audience.owner_id == INSTALLER
