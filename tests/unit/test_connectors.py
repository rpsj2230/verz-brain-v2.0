"""The connector contract, the manifest, the lifecycle and the four transports, rule by rule.

These are the refusals a connector author meets. The rules that must never break whatever
anybody edits live in `tests/invariants/test_projection_invariants.py`, which is where the
storage guarantees are; this file says what each refusal actually does and what it says.

Task ids: M11.1.1, M11.1.2, M11.1.3, M11.1.4, M11.1.5, M11.1.6, M11.1.7, M11.2.1, M11.2.3,
M11.2.4, M11.2.5, M11.2.6, M11.4.2, M11.4.3, M11.4.5, M11.4.6, M11.4.7
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from brain.connectors.contract import (
    AccessMode,
    ConnectorContractError,
    ConnectorHealth,
    ConnectorScope,
    CredentialBinding,
    FetchRequest,
    HealthState,
    TransportKind,
    assert_fetches_only,
    assert_holds_no_credential,
    identity_mode_default,
)
from brain.connectors.manifest import (
    ChangeSignal,
    ConnectorManifest,
    FieldShape,
    HotUse,
    ManifestError,
    ProjectedEntity,
    ProjectedField,
    ToolDeclaration,
    digest_input,
    failed_clauses,
    manifest_digest,
    projectability,
)
from brain.connectors.registry import (
    ConnectorRegistry,
    ConnectorState,
    LifecycleError,
    ManifestPinError,
)
from brain.connectors.transports import (
    CustomTransport,
    DatabaseTransport,
    FieldMapping,
    McpTransport,
    RestTransport,
    SourceRecord,
    TransportError,
    assert_scope_covers,
    normalise,
)
from brain.core.entitlement import Capability, EntitlementSet
from brain.core.envelope import Entity, IdentityMode, SideEffect, TypedResult
from brain.core.scope import Clause, Op, Scope
from brain.ops.secrets import Lease, SecretRef, Vault, VaultRole

NOW = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
READ_REF = SecretRef(path="database/creds/laravel_ro", role=VaultRole.APPLICATION)
WRITE_REF = SecretRef(path="database/creds/laravel_rw", role=VaultRole.APPLICATION)


def a_scope() -> ConnectorScope:
    return ConnectorScope(resource_kind="view", selectors=("portal.v_client",))


def a_projection(**overrides: object) -> ProjectedEntity:
    defaults: dict[str, object] = {
        "entity": "client",
        "fields": (
            ProjectedField(name="id", shape=FieldShape.IDENTIFIER, uses=(HotUse.IDENTIFY,)),
            ProjectedField(name="status", shape=FieldShape.STATUS, uses=(HotUse.FILTER,)),
            ProjectedField(name="display_name", shape=FieldShape.LABEL, uses=(HotUse.SORT,)),
        ),
        "change_signal": ChangeSignal.UPDATED_SINCE,
        "visibility": Scope.department("maintenance"),
    }
    defaults.update(overrides)
    return ProjectedEntity(**defaults)  # type: ignore[arg-type]


def a_manifest(**overrides: object) -> ConnectorManifest:
    defaults: dict[str, object] = {
        "name": "laravel",
        "version": "1.0.0",
        "transport": TransportKind.DATABASE,
        "scope": a_scope(),
        "credential": CredentialBinding(ref=READ_REF),
        "tools": (
            ToolDeclaration(
                name="laravel.read_client",
                description="One client row from the maintenance portal.",
                entity="client",
            ),
        ),
        "projections": (a_projection(),),
        "ceiling": "xero",
    }
    defaults.update(overrides)
    return ConnectorManifest(**defaults)  # type: ignore[arg-type]


# ------------------------------------------------------------- the fetch contract (M11.1.1)
def test_a_fetch_handed_an_entitlement_set_is_refused() -> None:
    """Deleting this lets a connector filter by permission. There would then be two places
    that answer a permission question, the connector's answer would never be audited, and
    the day the two disagree the permissive one wins with nothing reporting it."""

    def fetch(request: FetchRequest, entitlement: EntitlementSet) -> TypedResult[Entity]:
        raise NotImplementedError

    with pytest.raises(ConnectorContractError, match="EntitlementSet"):
        assert_fetches_only(fetch)


def test_a_fetch_handed_a_capability_or_a_field_policy_is_refused() -> None:
    """The entitlement set is not the only input to a permission decision. Without these,
    a connector takes the policy instead and does exactly the same filtering while passing
    a check that only looked for one type name."""

    def by_capability(request: FetchRequest, needed: Capability) -> TypedResult[Entity]:
        raise NotImplementedError

    with pytest.raises(ConnectorContractError, match="Capability"):
        assert_fetches_only(by_capability)


def test_a_fetch_handed_a_vault_is_refused_and_a_lease_is_not() -> None:
    """A vault lets an adapter mint credentials on its own schedule, outside the `finally`
    that revokes them. A lease is the intended shape: it expires and cannot be read after.
    Deleting this makes 'credentials are borrowed, never held' unenforced."""

    def with_vault(request: FetchRequest, vault: Vault) -> TypedResult[Entity]:
        raise NotImplementedError

    def with_lease(request: FetchRequest, lease: Lease) -> TypedResult[Entity]:
        raise NotImplementedError

    with pytest.raises(ConnectorContractError, match="Vault"):
        assert_fetches_only(with_vault)
    assert_fetches_only(with_lease)


def test_a_fetch_taking_kwargs_is_refused() -> None:
    """A signature that accepts anything has declared nothing, so it cannot be shown never
    to receive the caller's grants. Without this the whole check is one `**kwargs` away."""

    def fetch(request: FetchRequest, **extra: object) -> TypedResult[Entity]:
        raise NotImplementedError

    with pytest.raises(ConnectorContractError, match="declared nothing"):
        assert_fetches_only(fetch)


def test_an_unannotated_parameter_is_refused() -> None:
    """Default-deny. An unannotated parameter can hold the entitlement set, so accepting it
    would make the rule hold for every connector except the ones somebody was in a hurry
    about."""

    def fetch(request: FetchRequest, extra) -> TypedResult[Entity]:  # type: ignore[no-untyped-def]
        raise NotImplementedError

    with pytest.raises(ConnectorContractError, match="unannotated"):
        assert_fetches_only(fetch)


def test_a_scope_predicate_is_allowed_through_a_fetch() -> None:
    """A scope is a row filter the gate already computed, and pushing it down can only
    narrow. Deleting this test lets somebody add Scope to the forbidden list, which forbids
    predicate push-down and makes a database adapter pull whole tables to discard them."""

    def fetch(request: FetchRequest, predicate: Scope) -> TypedResult[Entity]:
        raise NotImplementedError

    assert_fetches_only(fetch)


def test_a_connector_that_stores_an_api_key_is_refused() -> None:
    """A stored credential is nearly always a plain string, so a rule that only looked at
    types would pass `api_key: str`. Without this, rotation stops being free: there is a
    cached value nothing can invalidate and no revocation can reach."""

    class Holder:
        api_key: str

    with pytest.raises(ConnectorContractError, match="named for a credential"):
        assert_holds_no_credential(Holder)


def test_a_credential_inherited_from_a_base_class_is_refused_too() -> None:
    """The way somebody would actually do it. Nobody writes `api_key: str` on the connector
    the reviewer is reading; they factor out "common connector state" into a base and the
    subclass looks clean.

    The walk over `__mro__` catches it and nothing tested that it does: reading only the
    class's own annotations passed every other test in this file. Found by mutation after
    the module was written.

    Deleting this makes the guard something a single `class X(Base)` walks around, and the
    file that would need reading to notice is the base, not the connector."""

    class CommonState:
        api_key: str

    class Xero(CommonState):
        timeout_seconds: float

    with pytest.raises(ConnectorContractError, match="named for a credential"):
        assert_holds_no_credential(Xero)


def test_a_connector_that_stores_a_vault_reference_is_refused() -> None:
    """Read-by-path with an extra step. Deleting this leaves a connector that can mint a
    credential whenever it likes, which is the standing credential leasing exists to
    prevent."""

    class Holder:
        ref: SecretRef

    with pytest.raises(ConnectorContractError, match="SecretRef"):
        assert_holds_no_credential(Holder)


def test_a_connector_holding_only_a_transport_is_accepted() -> None:
    """The check must not refuse the ordinary shape. A rule that refused every class would
    be switched off within a week, and then neither half of it would run."""

    class Plain:
        endpoint: str
        timeout_ms: int

    assert_holds_no_credential(Plain)


# ------------------------------------------------------------- scope at connect (M11.2.3)
@pytest.mark.parametrize("selector", ["*", "/", "all", "", "ROOT"])
def test_a_scope_that_narrows_nothing_is_refused(selector: str) -> None:
    """One Drive folder, not the whole Drive. Without this a connector reaches everything
    its credential reaches, and narrowing it later does not un-fetch what was already
    read."""
    with pytest.raises(ConnectorContractError, match=r"narrows nothing|names nothing"):
        ConnectorScope(resource_kind="folder", selectors=(selector,))


def test_a_scope_with_no_selectors_is_refused() -> None:
    """An empty list reads as 'not configured yet' and behaves as 'everything'. Deleting
    this makes the unconfigured state the widest one."""
    with pytest.raises(ConnectorContractError, match="reaches everything"):
        ConnectorScope(resource_kind="folder", selectors=())


def test_scope_membership_is_exact_and_not_a_prefix() -> None:
    """A prefix rule admits `folder_170` for a scope of `folder_17`, which is a different
    folder belonging to somebody else, and it reads as correct in every test where the ids
    happen not to share a prefix."""
    scope = ConnectorScope(resource_kind="folder", selectors=("folder_17",))
    assert scope.admits("folder_17")
    assert not scope.admits("folder_170")


# ------------------------------------------------------- credential binding (M11.2.1, M11.2.4)
def test_a_binding_defaults_to_read_only() -> None:
    """Read-only has to be the default *value*, not a convention applied by whoever fills
    the form in. Deleting this lets the default become whatever the last edit left."""
    assert CredentialBinding(ref=READ_REF).mode is AccessMode.READ_ONLY


def test_a_write_binding_that_names_nobody_is_refused() -> None:
    """The question asked after a connector writes something unexpected is always 'who
    agreed to this', and a boolean cannot answer it."""
    with pytest.raises(ConnectorContractError, match="names nobody"):
        CredentialBinding(ref=WRITE_REF, mode=AccessMode.WRITE)


def test_a_read_only_binding_that_names_a_granter_is_refused() -> None:
    """A granter on a read-only binding reads as an approval that was already given, so the
    next person to widen it believes the decision has been taken."""
    with pytest.raises(ConnectorContractError, match="already given"):
        CredentialBinding(ref=READ_REF, write_granted_by="u_weiling")


def test_a_read_only_binding_does_not_permit_a_draft() -> None:
    """A draft is a row in somebody else's system created by us. Exempting drafts would let
    a read-only connector create records as long as they were called drafts."""
    binding = CredentialBinding(ref=READ_REF)
    assert binding.permits(SideEffect.NONE)
    assert not binding.permits(SideEffect.DRAFT)
    assert not binding.permits(SideEffect.WRITE)


def test_the_default_identity_mode_is_the_requesters_own() -> None:
    """Delegated means the source enforces its own permissions too, which is a second
    independent check for free. If the default flipped to SERVICE, every tool would run on
    a shared credential and ours would be the only permissions there are."""
    assert identity_mode_default() is IdentityMode.DELEGATED


# ------------------------------------------------------------- the five clauses (M11.4.3)
def test_a_field_nothing_filters_or_sorts_on_fails_the_hot_clause() -> None:
    """Wanting to display a value is not a reason to store it, and display is the reason
    every field is wanted. Without this clause the projection is whatever anybody found
    convenient to copy."""
    verdicts = projectability(
        ProjectedField(name="note", shape=FieldShape.LABEL),
        signal=ChangeSignal.WEBHOOK,
        label_count=1,
        field_count=1,
    )
    assert [v.clause for v in failed_clauses(verdicts)] == ["hot"]


def test_a_field_from_a_source_with_no_change_signal_fails_the_signalled_clause() -> None:
    """This is the clause that stops the projection becoming a mirror. Delete it and a
    value nobody can refresh is filtered, sorted and counted on as though it were current,
    indefinitely, with nothing reporting it."""
    verdicts = projectability(
        ProjectedField(name="status", shape=FieldShape.STATUS, uses=(HotUse.FILTER,)),
        signal=ChangeSignal.NONE,
        label_count=0,
        field_count=1,
    )
    assert [v.clause for v in failed_clauses(verdicts)] == ["signalled"]


def test_a_denylisted_field_fails_the_permitted_clause_even_when_it_is_hot() -> None:
    """A field can be genuinely hot and still be one we may never store. Without this
    clause, 'the fast lane needs it' becomes an argument for storing a salary."""
    verdicts = projectability(
        ProjectedField(name="contact_email", shape=FieldShape.LABEL, uses=(HotUse.FILTER,)),
        signal=ChangeSignal.WEBHOOK,
        label_count=1,
        field_count=1,
    )
    assert [v.clause for v in failed_clauses(verdicts)] == ["permitted"]


def test_a_second_label_fails_the_pointer_shaped_clause() -> None:
    """One label identifies a record. Six labels of 120 characters is a ticket body
    arriving in instalments, and the per-field length limit does not notice."""
    verdicts = projectability(
        ProjectedField(name="summary", shape=FieldShape.LABEL, uses=(HotUse.SORT,)),
        signal=ChangeSignal.WEBHOOK,
        label_count=2,
        field_count=2,
    )
    assert [v.clause for v in failed_clauses(verdicts)] == ["pointer-shaped"]


def test_a_thirteenth_field_fails_the_within_the_cap_clause() -> None:
    """The cap is what keeps the projection 40 MB rather than tens of gigabytes. It is a
    per-entity rule, so it is reported against every field of an over-wide entity."""
    verdicts = projectability(
        ProjectedField(name="status", shape=FieldShape.STATUS, uses=(HotUse.FILTER,)),
        signal=ChangeSignal.WEBHOOK,
        label_count=0,
        field_count=13,
    )
    assert [v.clause for v in failed_clauses(verdicts)] == ["within the cap"]


def test_every_failing_clause_is_reported_at_once() -> None:
    """One at a time turns writing a connector into a guessing game where each fix reveals
    the next objection, and the author ends up renaming the field rather than fixing the
    design."""
    verdicts = projectability(
        ProjectedField(name="salary", shape=FieldShape.LABEL),
        signal=ChangeSignal.NONE,
        label_count=2,
        field_count=13,
    )
    assert len(failed_clauses(verdicts)) == 5


# ------------------------------------------------------- the projected entity (M11.4.2, M11.4.7)
def test_a_projection_with_no_change_signal_and_fields_is_refused() -> None:
    """M11.4.7, stated at the level the author has to change something at. The per-field
    clause explains one field; this explains the entity."""
    with pytest.raises(ManifestError, match="no change signal"):
        a_projection(change_signal=ChangeSignal.NONE)


def test_a_projection_with_no_change_signal_and_no_fields_is_allowed() -> None:
    """A source that tells us nothing is a legitimate federated-only source. Refusing it
    outright would push people to declare a signal that does not exist."""
    entity = ProjectedEntity(
        entity="invoice",
        fields=(),
        change_signal=ChangeSignal.NONE,
        visibility=Scope.department("finance"),
    )
    assert entity.field_names == ()


def test_a_projection_wider_than_twelve_fields_is_refused_at_the_manifest() -> None:
    """The ingest-time check catches a connector returning more than it declared; this
    catches a manifest declaring it. Without the manifest half, thirteen fields are refused
    on the first ingest, in production, rather than at review."""
    fields = tuple(
        ProjectedField(name=f"f{i}", shape=FieldShape.STATUS, uses=(HotUse.FILTER,))
        for i in range(13)
    )
    with pytest.raises(ManifestError, match="mirror"):
        a_projection(fields=fields)


def test_a_field_declared_twice_is_refused() -> None:
    """Deduplicating picks one declaration silently, and the one it picks decides whether
    the field counts as a label against the one-label rule."""
    duplicated = (
        ProjectedField(name="status", shape=FieldShape.STATUS, uses=(HotUse.FILTER,)),
        ProjectedField(name="status", shape=FieldShape.LABEL, uses=(HotUse.SORT,)),
    )
    with pytest.raises(ManifestError, match="more than once"):
        a_projection(fields=duplicated)


# --------------------------------------------------------- visibility predicate (M11.4.5)
def test_a_projected_field_holding_a_permission_list_is_refused() -> None:
    """A resolved ACL is stale the moment somebody moves department, and nothing reports
    it. This is where the whole category fails: Microsoft's incremental crawls do not
    update permissions at all and Glean's full crawls run on 28-day cycles."""
    fields = (
        ProjectedField(name="id", shape=FieldShape.IDENTIFIER, uses=(HotUse.IDENTIFY,)),
        ProjectedField(name="shared_with", shape=FieldShape.STATUS, uses=(HotUse.FILTER,)),
    )
    with pytest.raises(ManifestError, match="resolved permission list"):
        a_projection(fields=fields)


def test_a_predicate_enumerating_principals_is_refused() -> None:
    """The same resolved list wearing a predicate's clothes. Nothing in it depends on the
    caller, so it does not re-evaluate against the live entitlement set and the mover case
    breaks exactly as it would with a stored ACL."""
    acl = Scope(clauses=(Clause(field="user_id", op=Op.IN, value=("u_a", "u_b")),))
    with pytest.raises(ManifestError, match="wearing a predicate"):
        a_projection(visibility=acl)


def test_a_predicate_on_ownership_is_allowed() -> None:
    """`owner_id = u_weiling` is a property of the record, not an enumeration of who may
    read it. Refusing it would refuse the ordinary case of a source whose visibility
    genuinely follows ownership, and the rule would be relaxed wholesale."""
    ownership = Scope(clauses=(Clause(field="owner_id", op=Op.EQ, value="u_weiling"),))
    assert a_projection(visibility=ownership).entity == "client"


def test_a_projection_with_no_visibility_predicate_is_refused() -> None:
    """A projection with no predicate has discarded the source's permission model rather
    than narrowed it, and every row is then visible to anybody holding the entity's
    capability. Nothing downstream can tell that from a source that genuinely has no
    restrictions."""
    with pytest.raises(ManifestError, match="no visibility predicate"):
        a_projection(visibility=Scope.unrestricted())


# ------------------------------------------------------------------ tool declarations
def test_a_tool_with_a_side_effect_and_no_read_back_is_refused() -> None:
    """The protocol removed message redelivery, so connector-side idempotency is mandatory.
    Without a read-back an operation that crashed sits in UNKNOWN with no way out except a
    retry, and a retry either repeats the action or loses it."""
    with pytest.raises(ManifestError, match="read-back"):
        ToolDeclaration(
            name="laravel.set_status",
            description="Set a client's status.",
            entity="client",
            side_effect=SideEffect.WRITE,
        )


def test_a_tool_with_no_description_is_refused() -> None:
    """The model has one line and the name to choose from. An empty description is also an
    empty slot inside the pinned digest, so a server could fill it later without moving the
    pin from anything to something."""
    with pytest.raises(ManifestError, match="no description"):
        ToolDeclaration(name="laravel.read_client", description="   ", entity="client")


def test_a_write_tool_on_a_read_only_binding_is_refused_at_the_manifest() -> None:
    """Both halves are already in the manifest, so the mismatch is knowable without calling
    anything. Left to fail at the source it arrives as a 403 during somebody's request and
    reads as a permission problem with the caller."""
    write_tool = ToolDeclaration(
        name="laravel.set_status",
        description="Set a client's status.",
        entity="client",
        side_effect=SideEffect.WRITE,
        verifies_write=True,
    )
    with pytest.raises(ManifestError, match="separate deliberate grant"):
        a_manifest(tools=(write_tool,))


def test_the_same_entity_projected_twice_is_refused() -> None:
    """The twelve-field cap is per entity kind, and two declarations make it twenty-four by
    arithmetic while each one passes on its own."""
    with pytest.raises(ManifestError, match="twice"):
        a_manifest(projections=(a_projection(), a_projection()))


# ------------------------------------------------------------------ the digest (M11.1.7)
def test_the_digest_moves_when_a_tool_description_changes() -> None:
    """The failure this exists for. A server that rewrites 'one invoice' to 'every invoice
    for the tenant' has changed what the connector does with no name changing, and a digest
    over tool names alone would pass it."""
    original = a_manifest()
    reworded = a_manifest(
        tools=(
            ToolDeclaration(
                name="laravel.read_client",
                description="Every client row for the tenant.",
                entity="client",
            ),
        )
    )
    assert manifest_digest(original) != manifest_digest(reworded)


def test_the_digest_does_not_move_when_the_credential_is_rebound() -> None:
    """If the binding were pinned, every rotation would fail closed on reconnect and the
    first fix anybody reached for would be to stop pinning."""
    original = a_manifest()
    rebound = a_manifest(credential=CredentialBinding(ref=WRITE_REF, mode=AccessMode.READ_ONLY))
    assert manifest_digest(original) == manifest_digest(rebound)


def test_the_digest_covers_every_manifest_field_except_the_credential() -> None:
    """The include list is derived from the dataclass rather than written out, so a field
    added later is pinned without anybody remembering to pin it. Deleting this lets an
    explicit list drift behind the model it is supposed to cover."""
    import dataclasses

    body = digest_input(a_manifest())
    for field in dataclasses.fields(ConnectorManifest):
        if field.name == "credential":
            assert f'"{field.name}"' not in body
        else:
            assert f'"{field.name}"' in body, f"{field.name} is not inside the pin"


def test_reordering_a_declaration_does_not_move_the_digest() -> None:
    """A digest that moved when somebody sorted a dataclass differently would read as a
    third party redefining the connector, and the alarm would be ignored the second time."""
    assert manifest_digest(a_manifest()) == manifest_digest(a_manifest())


# ------------------------------------------------------------------ the lifecycle (M11.1.6)
def test_a_registered_connector_serves_nothing_until_it_is_enabled() -> None:
    """Installing is not switching on. The install wizard proves a template works before
    anybody uses it, and a connector that served traffic the instant it was declared would
    make that proof retrospective."""
    registry = ConnectorRegistry()
    registry.register(a_manifest(), now=NOW)
    assert registry.serving() == ()
    registry.enable("laravel", now=NOW)
    assert registry.serving() == ("laravel",)


def test_registering_over_an_installed_connector_is_refused() -> None:
    """Replacing silently re-pins the digest against the new manifest, so the pin would
    match by construction and `reconnect` would be checking the new connector against
    itself."""
    registry = ConnectorRegistry()
    registry.register(a_manifest(), now=NOW)
    with pytest.raises(LifecycleError, match="front door"):
        registry.register(a_manifest(version="2.0.0"), now=NOW)


def test_disabling_leaves_the_pin_and_the_manifest_alone() -> None:
    """Disabling is the reversible half. If it dropped the pin, re-enabling would trust
    whatever the far side says it is now."""
    registry = ConnectorRegistry()
    registry.register(a_manifest(), now=NOW)
    pinned = registry.get("laravel").digest
    registry.disable("laravel", now=NOW)
    assert registry.get("laravel").digest == pinned
    assert registry.get("laravel").state is ConnectorState.DISABLED


def test_an_upgrade_that_changes_the_manifest_without_the_version_is_refused() -> None:
    """The version is what a person reads in a console row, so a redefinition that keeps it
    is invisible in the one place it would be noticed."""
    registry = ConnectorRegistry()
    registry.register(a_manifest(), now=NOW)
    changed = a_manifest(
        tools=(
            ToolDeclaration(
                name="laravel.read_client", description="Something else.", entity="client"
            ),
        )
    )
    with pytest.raises(ManifestError, match="without a version bump"):
        registry.upgrade(changed, now=NOW)


def test_an_upgrade_that_changes_the_transport_is_refused() -> None:
    """A read-only database view and a sandboxed custom module are different deployment
    units with different blast radii. Calling the second an upgrade of the first makes it
    inherit the approval the first was given."""
    registry = ConnectorRegistry()
    registry.register(a_manifest(), now=NOW)
    with pytest.raises(LifecycleError, match="different deployment unit"):
        registry.upgrade(a_manifest(version="2.0.0", transport=TransportKind.CUSTOM), now=NOW)


def test_a_version_bump_with_no_manifest_change_is_allowed() -> None:
    """Bumping a version because something outside the manifest changed is ordinary.
    Refusing it teaches people to avoid the version field, which is the field the previous
    rule depends on."""
    registry = ConnectorRegistry()
    registry.register(a_manifest(), now=NOW)
    event = registry.upgrade(a_manifest(version="1.0.1"), now=NOW)
    assert event.action == "upgrade"


# ------------------------------------------------------------------ reconnect (M11.1.7)
def test_a_reconnect_with_a_moved_manifest_quarantines_and_raises() -> None:
    """Fails closed means quarantined and raising, not logged and continued. A pin that
    warns has been overridden by the time anybody reads the warning, and the thing on the
    other side is by then already being described to a model."""
    registry = ConnectorRegistry()
    registry.register(a_manifest(), now=NOW)
    registry.enable("laravel", now=NOW)
    moved = a_manifest(
        tools=(
            ToolDeclaration(
                name="laravel.read_client",
                description="Every client row for the tenant.",
                entity="client",
            ),
        )
    )
    with pytest.raises(ManifestPinError, match="no longer matches"):
        registry.reconnect("laravel", moved, now=NOW)
    assert registry.get("laravel").state is ConnectorState.QUARANTINED
    assert registry.serving() == ()


def test_a_quarantined_connector_cannot_be_enabled() -> None:
    """If enabling cleared it, the remedy would be one click on an amber badge, taken by
    whoever is on call rather than by whoever understands what the descriptions now say."""
    registry = ConnectorRegistry()
    registry.register(a_manifest(), now=NOW)
    with pytest.raises(ManifestPinError):
        registry.reconnect("laravel", a_manifest(version="9.9.9"), now=NOW)
    with pytest.raises(LifecycleError, match="quarantined"):
        registry.enable("laravel", now=NOW)


def test_accepting_the_new_manifest_through_upgrade_releases_the_quarantine() -> None:
    """Quarantine has to have an exit, and the exit has to be the deliberate one: a person
    reads the diff, accepts it, and the version moves. Without this the only remedy is
    deleting the connector, which loses its projection."""
    registry = ConnectorRegistry()
    registry.register(a_manifest(), now=NOW)
    moved = a_manifest(version="2.0.0", ceiling="freshdesk")
    with pytest.raises(ManifestPinError):
        registry.reconnect("laravel", moved, now=NOW)
    registry.upgrade(moved, now=NOW)
    assert registry.get("laravel").state is ConnectorState.REGISTERED
    assert registry.reconnect("laravel", moved, now=NOW).detail == "manifest matches its pin"


def test_a_matching_reconnect_changes_nothing() -> None:
    """The ordinary path has to stay ordinary. A reconnect that disturbed the state would
    make every restart look like an incident."""
    registry = ConnectorRegistry()
    registry.register(a_manifest(), now=NOW)
    registry.enable("laravel", now=NOW)
    event = registry.reconnect("laravel", a_manifest(), now=NOW)
    assert event.state is ConnectorState.ENABLED
    assert registry.serving() == ("laravel",)


# ------------------------------------------------------------------ rebinding (M11.2.6)
def test_a_rebind_moves_the_path_and_leaves_the_pin() -> None:
    """Rotation without redeploy. Nothing holds a credential between runs, so moving the
    path is a configuration edit; if it moved the pin, the next reconnect would quarantine
    a connector nobody had redefined."""
    registry = ConnectorRegistry()
    registry.register(a_manifest(), now=NOW)
    pinned = registry.get("laravel").digest
    event = registry.rebind("laravel", CredentialBinding(ref=WRITE_REF), now=NOW)
    assert registry.get("laravel").manifest.credential.ref.path == WRITE_REF.path
    assert registry.get("laravel").digest == pinned
    assert event.detail == f"{READ_REF.path} to {WRITE_REF.path}"


def test_a_rebind_cannot_widen_read_only_to_write() -> None:
    """Otherwise the audit line for 'somebody moved a path' and the line for 'somebody
    granted write' are the same line, and write arrives without a reviewer."""
    registry = ConnectorRegistry()
    registry.register(a_manifest(), now=NOW)
    write_binding = CredentialBinding(
        ref=WRITE_REF, mode=AccessMode.WRITE, write_granted_by="u_weiling"
    )
    with pytest.raises(LifecycleError, match="widen"):
        registry.rebind("laravel", write_binding, now=NOW)


# ---------------------------------------------------------------------- health (M11.1.1)
def test_a_degraded_connector_is_still_usable_and_a_down_one_is_not() -> None:
    """A connector answering slowly is still answering. If DEGRADED were unusable, a
    latency problem would become an outage and the state would exist for nothing."""
    degraded = ConnectorHealth(connector="xero", state=HealthState.DEGRADED, checked_at=NOW)
    down = ConnectorHealth(connector="xero", state=HealthState.DOWN, checked_at=NOW)
    unconfigured = ConnectorHealth(connector="xero", state=HealthState.UNCONFIGURED, checked_at=NOW)
    assert degraded.is_usable
    assert not down.is_usable
    assert not unconfigured.is_usable


# ------------------------------------------------------------------- MCP (M11.1.2)
def test_an_mcp_tool_the_manifest_did_not_declare_is_not_exposed() -> None:
    """An MCP server can add a tool between connections. Auto-naming it would put it in
    front of a model because the far side invented it, which is exactly the trust the pin
    exists to withhold."""
    transport = McpTransport(
        endpoint="stdio://freshdesk", tool_names={"searchTickets": "freshdesk.search_ticket"}
    )
    remote = ("searchTickets", "deleteEverything")
    assert transport.exposed(remote) == ("freshdesk.search_ticket",)
    assert transport.undeclared(remote) == ("deleteEverything",)


def test_an_mcp_mapping_producing_an_illegal_tool_name_is_refused() -> None:
    """A name that does not read as source.verb_noun is either never selected by the model
    or selected for the wrong reason, and neither failure raises anything anybody sees."""
    with pytest.raises(TransportError, match=r"source\.verb_noun"):
        McpTransport(endpoint="stdio://x", tool_names={"a": "searchTickets"})


def test_two_remote_tools_mapping_to_one_name_are_refused() -> None:
    """Which one runs would be decided by iteration order, which is not a decision anybody
    made."""
    with pytest.raises(TransportError, match="iteration order"):
        McpTransport(
            endpoint="stdio://x",
            tool_names={"a": "freshdesk.read_ticket", "b": "freshdesk.read_ticket"},
        )


# ---------------------------------------------------------------- REST / OpenAPI (M11.1.3)
def test_a_mapping_with_an_expression_source_path_is_refused() -> None:
    """A field mapping that can filter and compute is a program, and a program in a mapping
    file is reviewed by nobody and produces data nobody declared."""
    with pytest.raises(TransportError, match="plain dotted path"):
        FieldMapping(target="status", source_path="data.items[?(@.open)].status")


def test_two_source_paths_writing_one_target_are_refused() -> None:
    """Which value survives would be decided by declaration order, and the wrong one looks
    exactly like the right one."""
    with pytest.raises(TransportError, match="declaration order"):
        RestTransport(
            spec_ref="xero.yaml",
            operation="getInvoices",
            entity="invoice",
            fields=(
                FieldMapping(target="status", source_path="data.status"),
                FieldMapping(target="status", source_path="data.state"),
            ),
        )


def test_a_rest_transport_with_no_field_mapping_is_refused() -> None:
    """A mapping with nothing in it returns records that are a bare entity tag, which the
    redactor drops as having no visible field. The connector then looks like a permission
    problem rather than an empty mapping."""
    with pytest.raises(TransportError, match="maps no fields"):
        RestTransport(spec_ref="xero.yaml", operation="getInvoices", entity="invoice", fields=())


# --------------------------------------------------------------- database views (M11.1.4)
def test_a_view_not_on_the_allowlist_is_refused() -> None:
    """A read-only credential still reaches every view it was granted. The allowlist is the
    half of that restriction we can see and the half a reviewer can check."""
    transport = DatabaseTransport(views=("portal.v_client",))
    with pytest.raises(TransportError, match="not on this connector's allowlist"):
        transport.plan("portal.v_salary")


def test_an_unqualified_view_name_is_refused() -> None:
    """An unqualified name resolves against search_path, which is whatever the connection
    was left set to rather than what the manifest says."""
    with pytest.raises(TransportError, match="search_path"):
        DatabaseTransport(views=("v_client",))


def test_a_database_transport_produces_a_plan_and_never_a_statement() -> None:
    """There is no string here that could carry `; DROP` because there is no string here
    that becomes SQL. Accepting SQL and validating it means writing a parser, and a parser
    that disagrees with the database's own is a bypass."""
    transport = DatabaseTransport(views=("portal.v_client",))
    read = transport.plan("portal.v_client", filters=(("department", "maintenance"),), limit=50)
    assert read.view == "portal.v_client"
    assert not hasattr(read, "sql")


def test_a_view_outside_the_connect_scope_is_refused() -> None:
    """Two lists that disagree mean one of them is not the restriction anybody approved,
    and the wider one is the one that runs."""
    transport = DatabaseTransport(views=("portal.v_client", "portal.v_invoice"))
    with pytest.raises(TransportError, match="outside the connector"):
        assert_scope_covers(transport, a_scope())


# ---------------------------------------------------------------- custom code (M11.1.5)
def test_a_custom_transport_with_no_sandbox_profile_is_refused() -> None:
    """A profile nobody implemented reads in a manifest as though a boundary existed, which
    is worse than no profile at all: the reviewer stops looking."""
    with pytest.raises(TransportError, match="not one of"):
        CustomTransport(module="verz.legacy_soap", sandbox_profile="trusted")


def test_an_empty_egress_allowlist_under_the_allowlist_profile_is_refused() -> None:
    """An empty allowlist permits every host while reading as a restriction, which is the
    exact shape of a control that is on paper only."""
    with pytest.raises(TransportError, match="permits every host"):
        CustomTransport(module="verz.scraper", sandbox_profile="egress_allowlist")


def test_egress_hosts_under_a_profile_that_ignores_them_are_refused() -> None:
    """A list that is not read is a permission somebody believes they have granted, and the
    belief survives until an incident."""
    with pytest.raises(TransportError, match="not read"):
        CustomTransport(
            module="verz.scraper", sandbox_profile="no_network", egress_allowlist=("api.x.com",)
        )


# ---------------------------------------------------------------- normalising to one contract
def test_every_transport_normalises_to_a_tagged_record() -> None:
    """The entity tag is what makes redaction possible at all. A record without one is
    dropped by the walker, so a transport that skipped tagging would return nothing and look
    like a permission failure."""
    result = normalise(
        "client",
        ({"id": "c_0447", "status": "active"},),
        source="laravel",
        fetched_at="2026-09-05T09:00:00+00:00",
    )
    assert isinstance(result.records[0], SourceRecord)
    assert result.records[0].entity == "client"
    assert result.records[0].model_dump()["status"] == "active"


def test_a_row_with_no_id_is_dropped_rather_than_given_one() -> None:
    """A generated id cannot be cited, cannot be pointed at by a request-access route, and
    cannot be matched to the same record on the next fetch. A record carrying one is
    reported twice and audited never."""
    result = normalise(
        "client",
        ({"id": "c_0447"}, {"status": "active"}, {"id": "   "}),
        source="laravel",
        fetched_at="2026-09-05T09:00:00+00:00",
    )
    assert result.record_count() == 1


def test_an_unknown_column_survives_normalisation_and_is_withheld_later() -> None:
    """A model that refused unknown columns would drop one the day the source added it,
    which reads as data loss. Allowing it is safe because it matches no field policy rule
    and is withheld under default-deny, so nothing new becomes visible."""
    result = normalise(
        "client",
        ({"id": "c_0447", "brand_new_column": "surprise"},),
        source="laravel",
        fetched_at="2026-09-05T09:00:00+00:00",
    )
    assert result.records[0].model_dump()["brand_new_column"] == "surprise"


# ------------------------------------------------- declarations that reach everything
def test_a_database_transport_with_an_empty_allowlist_is_refused() -> None:
    """An empty list reads as 'not configured yet' and behaves as 'every view the
    credential was granted'. Without this the unconfigured state is the widest one, which
    is the direction a default must never fail in."""
    with pytest.raises(TransportError, match="reaches whatever the credential reaches"):
        DatabaseTransport(views=())


def test_an_mcp_transport_with_no_endpoint_is_refused() -> None:
    """A connector with nowhere to connect installs cleanly and fails on first use, during
    somebody's request, as an outage rather than as the missing configuration it is."""
    with pytest.raises(TransportError, match="needs an endpoint"):
        McpTransport(endpoint="  ", tool_names={"a": "freshdesk.read_ticket"})


def test_only_the_database_transport_is_checked_against_the_connect_scope() -> None:
    """Only the database transport keeps a second list of resources, so it is the only one
    that can disagree with the connect scope. Inventing a comparison for the others would
    be inventing a rule, and an invented rule refuses correct connectors."""
    assert_scope_covers(
        RestTransport(
            spec_ref="xero.yaml",
            operation="getInvoices",
            entity="invoice",
            fields=(FieldMapping(target="status", source_path="data.status"),),
        ),
        a_scope(),
    )
