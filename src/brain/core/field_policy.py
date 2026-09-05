"""Which capability each field needs, and what happens to a field nobody classified.

The redactor asks exactly one question per field: may this caller see it? This module is
where the answer comes from. It is separate from the walker because the two rot in
different ways and are read by different people. The walker is code that stops changing
once it is right; the policy is a table that grows every time a connector exposes another
column, and the person adding the column is not the person who wrote the walker.

**What breaks without it.** The walker would have to name a capability per field inline,
so adding a column to a client record would mean editing the redactor. Nobody edits the
redactor to add a column. In practice the column would simply be returned to everybody,
and nothing would say so.

**The rule that matters is default-deny.** A field this policy does not mention is
withheld. That is the answer, not a gap for somebody to fill in later. The alternative,
returning anything unclassified, means every new column ships wide open and the failure is
silent: a field that is over-returned looks exactly like a field that was meant to be
public, and the only way to notice is for the wrong person to read it.

Note the direction of this rule against `brain.core.projection`. That module governs what
may be **stored**; this one governs what may be **returned**. They are different rules and
both apply. HR may read a salary, so no field policy forbids it, and the salary still may
never land in our database. A field can be returnable and unstorable at the same time, and
most of the interesting ones are.

Classification never permits. It records how sensitive a field is, for per-channel
sensitivity policy, artifact retention and reporting. A field classified PUBLIC still
needs its capability, because a classification is a description of a field and a
capability is a statement about a person.

Task ids: M4.2.1, M4.2.2, M4.2.4
"""

from __future__ import annotations

import enum
import hashlib
from collections.abc import Iterable
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from brain.core.entitlement import Capability

#: An entity type or a field name. Deliberately the same grammar as the field half of the
#: capability string in `brain.core.entitlement`: the names a policy can be written about
#: are exactly the names a grant can be written about, so a rule can never require a
#: capability that no grant could ever be phrased as.
NAME_PATTERN = r"^[a-z][a-z0-9_]*$"

#: Same width as `EntitlementSet.ent_hash`, and for the same reason: both go into the
#: answer cache key, and a key made of two differently-shaped halves is a key somebody
#: eventually parses wrongly.
POLICY_EPOCH_CHARS = 32


class Classification(enum.StrEnum):
    """How sensitive a field is. A description of the field, never a permission.

    Four levels rather than a free-text label, because the downstream consumers are all
    comparisons: which sensitivity classes a channel may carry, how long an artifact built
    from this field is retained, which fields a redaction report groups together. None of
    those can be answered against free text.
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"

    @property
    def rank(self) -> int:
        """Position in the order below. Higher is more sensitive."""
        return CLASSIFICATION_ORDER.index(self)


#: The order the four levels sit in. Written out rather than relying on declaration order,
#: because enum declaration order is not part of an enum's contract and a reordering during
#: a merge would silently change every comparison that depends on it.
CLASSIFICATION_ORDER: tuple[Classification, ...] = (
    Classification.PUBLIC,
    Classification.INTERNAL,
    Classification.CONFIDENTIAL,
    Classification.RESTRICTED,
)


class PolicyConflictError(Exception):
    """Two rules disagree about the same field.

    Deliberately not part of the user-facing error taxonomy, for the same reason
    `ProjectionRefusedError` is not: nobody asking a question ever sees this. It is a
    build-time failure and it should stop a policy from loading, rather than degrade an
    answer at request time.

    It exists because an ambiguous policy turns "may this person see this field" from a
    lookup into an evaluation-order problem, which is the exact failure
    `brain.core.entitlement` refuses by having no deny clause. A policy that resolves
    conflicts by last-one-wins would make the answer depend on the order rows came back
    from a table.
    """


class FieldRule(BaseModel):
    """One field, and the capability that reaches it (M4.2.1).

    Frozen, because the mask is computed per record and a rule that could be mutated
    between two records in the same answer would produce an answer nobody can reconstruct.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity: str = Field(min_length=1, max_length=60, pattern=NAME_PATTERN)
    field: str = Field(min_length=1, max_length=120, pattern=NAME_PATTERN)
    required_capability: Capability
    classification: Classification

    @field_validator("required_capability", mode="before")
    @classmethod
    def _accept_a_plain_string(cls, v: object) -> object:
        """Let a policy be written as `"read:client.name"`.

        Convenience with a purpose: a policy table is long, and a rule that has to be
        spelled `Capability(value=...)` in every row is a rule people write with a helper
        that acquires a default, and the default is then the thing under test.
        """
        return Capability(value=v) if isinstance(v, str) else v

    @field_validator("required_capability")
    @classmethod
    def _must_be_a_read(cls, v: Capability) -> Capability:
        """A field policy gates returning a value, and returning is reading.

        Without this, a rule could be satisfied by `write:client.contract_value`, so
        somebody granted the ability to change a number would acquire the ability to see
        it as a side effect. The noun is deliberately left free: a field on one entity may
        legitimately be governed by another entity's capability, for instance a margin
        column on a client record answering to a finance capability.
        """
        if v.verb != "read":
            msg = (
                f"field rules must require a read capability; {v.value!r} is a "
                f"{v.verb!r}, and a permission to act is not a permission to see"
            )
            raise ValueError(msg)
        return v

    @classmethod
    def of(
        cls, entity: str, field: str, capability: str | Capability, classification: Classification
    ) -> Self:
        """A rule from four positional values, with the capability as a string if you like.

        The validator above already accepts a string, but only at runtime: the `__init__`
        pydantic generates is typed as `Capability`, so writing a policy out longhand under
        a strict type checker means spelling `Capability(value=...)` on every row. This is
        the typed door onto the same conversion, and the validator stays because a policy
        also arrives by being loaded from a table rather than written in code.
        """
        return cls(
            entity=entity,
            field=field,
            required_capability=(
                capability if isinstance(capability, Capability) else Capability(value=capability)
            ),
            classification=classification,
        )

    @property
    def key(self) -> tuple[str, str]:
        return (self.entity, self.field)

    @property
    def dotted(self) -> str:
        """`client.contract_value`. What a report and a simulation both name a field by."""
        return f"{self.entity}.{self.field}"


class FieldPolicy(BaseModel):
    """Every rule, and the epoch that changes whenever any of them does.

    The epoch is a digest over the rules rather than a number somebody increments
    (M4.2.4). A counter is a counter somebody forgets, and the failure mode of forgetting
    is precise and bad: the architecture notes that without a policy epoch in the cache
    key, tightening a field policy leaves a window where a cached answer keeps disclosing
    a field that was just revoked. A digest cannot be forgotten, because it is not a step.

    A digest also behaves correctly on a revert, which a counter does not. Undo a policy
    change and the epoch returns to its previous value, so answers cached under the
    original policy become valid again, which is right: the policy is byte-for-byte the
    one they were computed under. A counter would keep climbing and throw away a cache
    that was never wrong.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rules: tuple[FieldRule, ...] = ()

    #: Built once at construction. A linear scan per field per record over a policy with a
    #: few hundred rules is the sort of cost that only shows up under load, in the one
    #: component that runs on every single answer.
    _index: dict[tuple[str, str], FieldRule] = PrivateAttr(default_factory=dict)

    def model_post_init(self, _context: object, /) -> None:
        index: dict[tuple[str, str], FieldRule] = {}
        clashes: list[str] = []
        for rule in self.rules:
            existing = index.get(rule.key)
            if existing is not None and existing != rule:
                clashes.append(
                    f"{rule.dotted}: {existing.required_capability.value}"
                    f"/{existing.classification.value} against "
                    f"{rule.required_capability.value}/{rule.classification.value}"
                )
            index[rule.key] = rule
        if clashes:
            listed = "\n".join(f"  - {c}" for c in clashes)
            msg = f"field policy has conflicting rules:\n{listed}"
            raise PolicyConflictError(msg)
        self._index = index

    def rule_for(self, entity: str, field: str) -> FieldRule | None:
        """The rule governing this field, or None when nothing classifies it.

        None is the interesting return. It is not "no restriction"; the caller is required
        to read it as "withhold", and `brain.core.redaction.compute_mask` is where that
        reading is enforced. The lookup deliberately does not decide, so that a caller
        cannot get a permissive answer out of it by accident.
        """
        return self._index.get((entity, field))

    def governs(self, entity: str, field: str) -> bool:
        return (entity, field) in self._index

    def fields_for(self, entity: str) -> tuple[str, ...]:
        """Every field this policy classifies for an entity type, sorted.

        Sorted so that two policies holding the same rules report them identically. An
        order that followed the rule tuple would make a report depend on the order rows
        came out of a table.
        """
        return tuple(sorted(field for (ent, field) in self._index if ent == entity))

    def entities(self) -> tuple[str, ...]:
        return tuple(sorted({ent for (ent, _field) in self._index}))

    def epoch(self) -> str:
        """A stable digest over every rule. Changes when any rule changes (M4.2.4).

        Sorted before hashing, so two policies built in different orders that classify the
        same fields the same way share an epoch. Without that, loading the same policy
        from a table with a different `ORDER BY` would invalidate every cached answer in
        the system and look like a policy change in the trace.

        The separator is safe without length-prefixing because none of the four parts can
        contain a pipe: entity and field match `NAME_PATTERN`, the capability grammar
        admits only letters, digits, underscore, colon, dot and a trailing star, and a
        classification is one of four fixed words.
        """
        parts = sorted(
            "|".join(
                (
                    rule.entity,
                    rule.field,
                    rule.required_capability.value,
                    rule.classification.value,
                )
            )
            for rule in self.rules
        )
        digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
        return digest[:POLICY_EPOCH_CHARS]

    def with_rules(self, *rules: FieldRule) -> Self:
        """A new policy with these rules added, replacing any rule for the same field.

        Replacement rather than conflict, because this is the deliberate-edit path: the
        caller is saying what the rule should now be. The conflict check in
        `model_post_init` guards the other case, where two rules for one field arrive
        together and nothing in the input says which was meant.

        Returns a new policy rather than mutating, so the epoch before and after a change
        can both be held at once. A simulation of a tightening needs exactly that.
        """
        replaced = {rule.key for rule in rules}
        kept = tuple(rule for rule in self.rules if rule.key not in replaced)
        return type(self)(rules=kept + tuple(rules))

    def without(self, entity: str, *fields: str) -> Self:
        """A new policy with these fields unclassified, and therefore withheld.

        Removing a rule is how a field is revoked from everybody at once. It is not a deny
        clause: nothing subtracts from what a person holds, and the field becomes
        invisible because nothing classifies it any more.
        """
        dropped = {(entity, field) for field in fields}
        return type(self)(rules=tuple(r for r in self.rules if r.key not in dropped))

    def __len__(self) -> int:
        return len(self._index)


def policy_from_rows(
    rows: Iterable[tuple[str, str, str, Classification]],
) -> FieldPolicy:
    """Build a policy from `(entity, field, capability, classification)` tuples.

    The shape a policy table hands back, so that whatever eventually persists these rules
    has one obvious way to turn rows into a policy rather than each caller inventing one.
    """
    return FieldPolicy(
        rules=tuple(
            FieldRule(
                entity=entity,
                field=field,
                required_capability=Capability(value=capability),
                classification=classification,
            )
            for entity, field, capability, classification in rows
        )
    )
