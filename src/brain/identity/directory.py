"""What the corporate directory asserts about roles, and how a sync run reconciles it.

Role grants arrive from two places. Somebody appoints a person, with a name and a reason
attached; or a group in the company's Keycloak directory asserts it at sign-in, with no human
behind it at all. `brain.identity.oidc.role_grants_from_groups` produces the second kind and
says plainly that it is only a statement of what *should* be there - "the reconciliation that
removes belongs to whoever owns the table". This module is that reconciliation.

**The two kinds live in two tables, and this module can only reach one of them.** That is
decision 21 in `docs/needs-rupash.md` and it is the whole design. The sync has to be able to
delete, because leaving a group has to stop conferring the role; and a process that can delete
rows a person created is a worse thing to build than a process that owns its own table. With
one table the sync must decide on every run which rows are its own, and it either gets that
wrong - deleting a hand-made grant, silently, the symptom being a person who quietly holds
less than they should - or gets it right by carrying a `source` column that every delete and
every access review afterwards has to remember to filter on.

So `reconcile` takes `DirectoryAssertion`s and returns `DirectoryAssertion`s, and there is no
argument, field or return position anywhere in it that a `RoleGrant` fits into. A hand-made
grant is not protected by a WHERE clause here. It is out of reach because the function has
nowhere to put one, which is a property a reviewer can check by reading the signature and
which `test_the_reconciler_has_nowhere_to_put_a_hand_made_grant` checks mechanically.

**The hand-made table is not built yet.** `role_grant` is M1.3.2 and only the type exists
(`brain.identity.roles.RoleGrant`); `auth.directory_role_grant` is deliberately not it and
must not be made to serve as it. `roles_held` below already takes both sides as arguments, so
the day `role_grant` is built the union is a query change and nothing else.

**`to_delete` is a subset of what it was handed as currently-held, always.** It is a set
difference and nothing else, so the reconciler cannot propose removing a row it was not shown.
That matters because the caller is about to execute those deletes: a reconciler that could
invent a row to delete would be a reconciler that could delete a row nobody read first.

**The union of the two sources is additive, and can only ever be.** There is no deny list
anywhere in this system - `brain.identity.packs.ADDITIVE_ONLY` is the sentence, and
`subtractive_state` is the sweep that keeps it true - so a second source of grants can add a
role to somebody and can never take one away from them. A directory group that stops being
asserted removes a row *from the directory's own table*; whether the person still holds the
role is then decided by whether some other row says so, never by a rule that subtracts.

**What is deliberately not here.** No capability, anywhere. A group maps to a `Role` and to
nothing else (M1.1.5), because the alternative moves the answer to "who can see the margin on
this client" into a directory nobody in this company reviews. `brain.gate.resolve` resolves
*entitlements* and must never see a role at all - `assert_no_role_in_resolution` refuses a
resolver that can - so nothing in this module is wired into that path and nothing here should
ever be. The union this module performs is over role grants, which govern the platform, and it
is a different question from the one the gate asks.

No SQLAlchemy model and no migration is written here. The table is
`auth.directory_role_grant` in `brain.tables.identity`, built by `migrations/versions/
0006_directory_role_grant.py`.

Task ids: M1.1.5
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from brain.core.scope import Scope
from brain.identity.oidc import GroupRoleRule
from brain.identity.roles import IdentityError, Role, RoleGrant, assert_not_a_role

#: What `granted_by` says on a grant this module produces. The issuer is appended, so the row
#: names which directory asserted it rather than only that some directory did. Kept identical
#: to `brain.identity.oidc.role_grants_from_groups`, which builds the same prefix, so a person
#: scanning the grant table sees one form and not two for the same fact.
ISSUER_PREFIX = "idp:"

#: `RoleGrant.granted_by` is `Field(max_length=128)`.
GRANTED_BY_CHARS = 128


@dataclass(frozen=True)
class DirectoryAssertion:
    """One sentence the directory says: this group gives this person this role.

    Frozen and hashable so reconciliation is set arithmetic. That is not a convenience: the
    whole safety argument for `reconcile` is that it is two set differences and therefore
    cannot produce anything it was not given.

    These three fields are the primary key of `auth.directory_role_grant`, and the type
    carries them and nothing else on purpose. Add `last_seen_at` here and two rows that say
    the same thing stop being equal, at which point a re-sync proposes deleting yesterday's
    row and inserting today's identical one - churn that reads, in the audit ledger, exactly
    like somebody's role being removed and restored.

    `source_group` is part of the identity rather than a detail hanging off it. Two groups may
    both confer `approver`; if only one of them stops being asserted, the person keeps the
    role, and collapsing them into one row would make leaving either group remove it.
    """

    principal_id: str
    role: Role
    source_group: str

    def __post_init__(self) -> None:
        # The same refusal `RoleGrant` makes, for the same reason: `principal_id =
        # "super_admin"` creates a row that reads like a role grant to every human who later
        # looks at the table, and resolves for whoever happens to own that id.
        assert_not_a_role(self.principal_id)
        if not self.principal_id.strip():
            msg = "a directory assertion needs a principal; a blank id names nobody"
            raise IdentityError(msg)
        if not self.source_group.strip():
            msg = (
                "a directory assertion needs the group that made it; without one the row "
                "cannot be reconciled, because nothing can say the group stopped asserting it"
            )
            raise IdentityError(msg)


@dataclass(frozen=True)
class Reconciliation:
    """What one sync run should write, as two sets and no verbs.

    Returned rather than executed. The function that decides is pure and testable without a
    database; the function that writes holds a transaction and no judgement. Folding the two
    together is how a reconciler becomes something nobody can test the dangerous half of,
    because the dangerous half only runs against a real table.

    There is no `to_update`. A row whose triple is unchanged is the same assertion, and the
    only thing a sync would update on it is `last_seen_at` - which is bookkeeping about the
    sync rather than a change to what anybody holds. `unchanged` carries those, so the caller
    can touch their timestamps without asking this module to have an opinion about it.
    """

    to_insert: frozenset[DirectoryAssertion]
    to_delete: frozenset[DirectoryAssertion]
    unchanged: frozenset[DirectoryAssertion]

    @property
    def is_empty(self) -> bool:
        """True when the directory and the table already agree.

        The common case by far, and worth being able to say cheaply: a sync that logs an
        audit entry per run whether or not anything changed buries the runs that did.
        """
        return not self.to_insert and not self.to_delete


def reconcile(
    asserted: Iterable[DirectoryAssertion],
    held: Iterable[DirectoryAssertion],
) -> Reconciliation:
    """What to insert and what to delete so the table matches the directory.

    `asserted` is what the directory says now. `held` is what `auth.directory_role_grant`
    currently contains. Both are sets of the same type, and the answer is two differences.

    **`to_delete` is a subset of `held` by construction.** The caller is about to execute
    these deletes, so a reconciler able to name a row it was not shown would be a reconciler
    able to delete a row nobody read first. Set difference makes that impossible rather than
    unlikely, which is why this is arithmetic and not a loop with conditions in it.

    **Nothing hand-made can appear in either set.** Not because this function is careful with
    them, but because it never receives one: `DirectoryAssertion` carries a `source_group`,
    which a grant somebody made by hand does not have, and there is no parameter here that a
    `RoleGrant` fits into. That is the property the whole two-table design exists to buy, and
    `assert_reconciler_cannot_reach_hand_made_grants` pins it from the outside.

    Rejected: taking the principal as an argument and reconciling one person at a time. It
    reads safer - the blast radius of a bug is one row - and it is worse, because a person who
    has left every mapped group asserts nothing at all, so a per-principal sync never runs for
    them and their rows live forever. Reconciling the whole set is what makes removal happen
    for the people it most needs to happen for.
    """
    now_asserted = frozenset(asserted)
    currently_held = frozenset(held)
    return Reconciliation(
        to_insert=now_asserted - currently_held,
        to_delete=currently_held - now_asserted,
        unchanged=currently_held & now_asserted,
    )


def assert_reconciler_cannot_reach_hand_made_grants(fn: Callable[..., object]) -> None:
    """Refuse a reconciler whose signature admits or returns a hand-made role grant.

    The two-table design is only worth its second table if the sync structurally cannot touch
    the first one. A check inside `reconcile` would be removable by whoever adds the feature
    that needs it; a parameter that does not exist has to be added first, which is a diff with
    a reviewer on it. This is the same argument, and the same mechanism, as
    `brain.identity.packs.assert_no_role_in_resolution`.

    Matched on the annotations rather than on the body, and on the return type as well as the
    parameters: a reconciler that accepted only assertions but handed back `RoleGrant`s would
    be one whose caller then has hand-made grants in a delete list.

    `eval_str=True` so the annotations are real classes rather than the strings that
    `from __future__ import annotations` leaves behind.
    """
    signature = inspect.signature(fn, eval_str=True)
    reachable = [str(p.annotation) for p in signature.parameters.values()]
    reachable.append(str(signature.return_annotation))
    offending = sorted({text for text in reachable if "RoleGrant" in text})
    if offending:
        msg = (
            f"{fn.__name__} can reach {offending}; a directory sync must not be able to name "
            "a grant a person made, or the second table buys nothing that a WHERE clause "
            "somebody can forget was not already buying"
        )
        raise IdentityError(msg)


# ------------------------------------------------- turning held rows into grants
@dataclass(frozen=True)
class DirectoryRoles:
    """The grants a set of held rows currently confers, and the rows that confer nothing.

    The second half is the point, and it is the shape `brain.identity.oidc.SyncedRoles` uses
    for the same reason. A held row whose group no longer has a rule is the normal state for a
    fortnight after somebody retires a mapping, and it is also what a typo in a renamed rule
    looks like. Dropping both silently makes them indistinguishable, and the second is an
    outage that presents as "my permissions disappeared overnight".
    """

    grants: tuple[RoleGrant, ...]
    #: Held rows whose `source_group` matches no current rule. They confer nothing until a
    #: rule exists again, and they are not deleted here: deleting is `reconcile`'s job and it
    #: answers to the directory, not to the rule set.
    unruled: tuple[DirectoryAssertion, ...]


def directory_role_grants(
    held: Iterable[DirectoryAssertion],
    rules: Sequence[GroupRoleRule],
    *,
    issuer: str,
    granted_at: datetime,
) -> DirectoryRoles:
    """The role grants the directory table currently confers (M1.1.5).

    The scope comes from the rule every time, never from the row. `auth.directory_role_grant`
    deliberately has no scope column: reconciliation keys on the triple, so a row whose group
    is still asserted is never rewritten, and a scope copied onto it would go on being served
    at its original width long after somebody narrowed the reviewed rule. Reading it from the
    rule means the reviewed copy is the only copy.

    A held row whose role disagrees with its rule's role is skipped rather than coerced. That
    happens when somebody re-points an existing group at a different role, and the honest
    answer is that the row is stale: the next reconciliation will delete it and insert the
    right one, because the directory asserts the new triple and not the old.

    `granted_at` is passed in rather than defaulted to now, and it should be the row's
    `created_at` - when the directory first asserted this, not when this sync ran. A grant
    stamped with the current time reads, in every review afterwards, as though the appointment
    were made this morning.
    """
    granted_by = f"{ISSUER_PREFIX}{issuer}"
    if len(granted_by) > GRANTED_BY_CHARS:
        # The same refusal `role_grants_from_groups` makes. A grantor string too long for the
        # column fails at the INSERT otherwise, which is after the decision has been taken.
        msg = f"issuer {issuer!r} is too long to record as a grantor"
        raise IdentityError(msg)

    by_group: dict[str, GroupRoleRule] = {rule.group: rule for rule in rules}
    grants: list[RoleGrant] = []
    unruled: list[DirectoryAssertion] = []
    for assertion in sorted(held, key=_ordering):
        rule = by_group.get(assertion.source_group)
        if rule is None or rule.role is not assertion.role:
            unruled.append(assertion)
            continue
        grants.append(
            RoleGrant(
                principal_id=assertion.principal_id,
                role=assertion.role,
                scope=rule.scope,
                granted_by=granted_by,
                reason=f"member of {assertion.source_group} at {issuer}",
                granted_at=granted_at,
            )
        )
    return DirectoryRoles(grants=tuple(grants), unruled=tuple(unruled))


def _ordering(assertion: DirectoryAssertion) -> tuple[str, str, str]:
    """A total order over assertions, so a caller's output does not depend on set iteration.

    Sets are unordered and Python's iteration order over them varies with hash randomisation,
    so without this the grants come back in a different order between runs - which turns a
    diff of two sync runs into noise and makes a golden test flap.
    """
    return (assertion.principal_id, assertion.role.value, assertion.source_group)


# ------------------------------------------------------------------- the union
def _confers(grant: RoleGrant) -> tuple[str, str, Scope | None, str | None]:
    """What a grant actually gives somebody, ignoring who wrote it and why.

    Two grants that agree on all four confer one role. They will differ on `granted_by`,
    `reason` and `granted_at` - one names a person, the other names a directory - and those
    are the fields a reviewer reads, not the fields that decide what anybody holds.
    """
    return (grant.principal_id, grant.role.value, grant.scope, grant.deputy_of)


def roles_held(
    *,
    hand_made: Iterable[RoleGrant],
    directory: Iterable[RoleGrant],
    now: datetime | None = None,
) -> tuple[RoleGrant, ...]:
    """Every platform role a set of people hold, from both sources (M1.1.5).

    **Entitlements in this system are additive only.** There is no deny list, anywhere - no
    negative grant, no revocation flag, nothing that subtracts at resolve time
    (`brain.identity.packs.ADDITIVE_ONLY`, and `subtractive_state` is the sweep that keeps it
    true). So adding the directory as a second source can only ever add: a group can give
    somebody a role they did not have, and there is no arrangement of rows in
    `auth.directory_role_grant` that takes one away from them. Removing a role means removing
    the row that confers it, from whichever table that row is in, and a sync can only remove
    from its own.

    Keyword-only, because the two arguments are the two sources and passing the same sequence
    twice would double every grant in it while type-checking perfectly.

    **Grants that confer the same thing are collapsed, and the hand-made one wins.** Somebody
    appointed a Super Admin *and* a directory group asserting it is one Super Admin, not two,
    and the difference is load-bearing: `standing_super_admins` counts what this returns, and
    `revoke_role` refuses to take the count below `SUPER_ADMIN_FLOOR`. Without the collapse,
    one person holding a role twice would satisfy a floor that exists precisely because one
    person is a single point of lockout. The hand-made grant is the one kept because it is the
    one a reviewer can act on: it names a grantor and a reason, and the directory row names a
    group in somebody else's Keycloak.

    Order is hand-made first, then directory, and stable within each. A caller rendering this
    to a console wants the reviewable grants at the top, and stability is what stops a page
    reordering itself between refreshes.

    `now` filters to grants that are active at that moment - an expired deputy confers
    nothing, and including it would make `standing_super_admins` count cover as ownership.
    """
    out: list[RoleGrant] = []
    seen: set[tuple[str, str, Scope | None, str | None]] = set()
    for grant in (*hand_made, *directory):
        if not grant.is_active(now):
            continue
        key = _confers(grant)
        if key in seen:
            continue
        seen.add(key)
        out.append(grant)
    return tuple(out)
