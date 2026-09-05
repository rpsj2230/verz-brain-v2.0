"""How the unseal key is split, who holds a piece, and what happens to the root token.

OpenBao splits its unseal key with Shamir's scheme at `bao operator init`, once, and the two
numbers that decide the whole custody model used to live as two flags on one line of a shell
command in `ops/openbao/UNSEAL.md`. That is the wrong home for them, and the reason is not
tidiness: **the shell accepts every wrong combination silently, and each one looks like the
shape of the right answer.** `-key-shares=5 -key-threshold=1` initialises perfectly and hands
five people a key that each of them can open the vault with alone. `-key-shares=5
-key-threshold=5` also initialises perfectly and produces a vault that one lost laptop
destroys, permanently, along with every credential in it. Neither is a typo a reviewer
catches by reading, because both are five and a small number on the same line.

So the quorum is an object here, constructed once, validated at construction, and rendered
back out as the exact flags. The rule the module exists to keep is that the numbers cannot be
in two places disagreeing.

**What was decided.** `docs/needs-rupash.md` item 17: five pieces, any three open it, so
three people have to agree and the company survives losing two. The five holders are named
here rather than only in prose. The root token is revoked after setup.

**A frozen dataclass, not a pydantic model, and the dependency is not the reason.** Pydantic
is already a dependency of this project. The reason is coercion. Pydantic's value is parsing
untrusted input at a trust boundary, and there is no boundary here: this object is built once
from literals in this file, under code review, and never from a request body, a JSON document
or an environment variable. At that boundary pydantic would helpfully turn `shares="5"` into
`5`, and for a number this consequential a string is a mistake that should stop a type check
rather than be quietly normalised. A frozen dataclass with a `__post_init__` that refuses gets
the validation without the coercion, and mypy strict checks it with no plugin.

**Source, not a database row, and this is the honest answer to "a setting in the backend".**
The request was to be able to select these options rather than have them buried in a shell
script, and this module is where they now live and where any console screen must read them
from. What was deliberately not built is a row an operator can edit at runtime, because the
vault will not honour it. The split is fixed at `bao operator init` and the only way to change
it afterwards is `bao operator rekey`, which itself needs the current threshold met by the
current holders. A save button that changed a number the vault ignores is worse than no
screen: it would report success for a change that did not happen. There is a second reason
and it is the one that bites during an incident: this policy governs the vault that holds the
database credentials, so keeping it in that database makes it unreadable at exactly the moment
somebody needs to know who to phone.

**Rejected: keeping the root token in a sealed envelope.** This is the usual compromise and
it does not survive being looked at. An envelope protects the paper, not the token. The token
itself is still live, still bypasses every policy in `ops/openbao/policies`, and appeared in
whatever terminal scrollback, screen recording or shell history existed when `bao operator
init` printed it, so the envelope's contents were never the only copy. Worse, the vault's own
audit log cannot help: a root token is recorded as an ordinary accessor HMAC with no field
marking it as root, so no query over the log distinguishes "somebody opened the envelope" from
legitimate admin work. And the emergency the envelope exists for is already served without it:
`bao operator generate-root` reconstructs a root token from a quorum of unseal pieces, which
is the same three people, and leaves a record of having been done. The envelope buys nothing
that generate-root does not, and costs a permanent, unattributable bypass. So the token is
revoked, and `revoke_root_command` is the step that does it.

Task ids: M31.3.2.1
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


class QuorumPolicyError(ValueError):
    """A quorum that would not be a quorum, refused before it can reach `bao operator init`.

    A `ValueError` subclass rather than a new base, so that an `except ValueError` around
    configuration loading still catches it while anything that wants to tell this apart from
    an ordinary bad value can.
    """


#: The container the vault runs in, from `ops/openbao/compose.yml`. Named here so the commands
#: this module renders are lines an operator can paste whole, rather than fragments they have
#: to wrap correctly at the moment they are least able to.
VAULT_CONTAINER = "brain-vault"

#: What an unnamed holder slot looks like. A prefix on the id rather than a boolean flag on
#: `Holder`, deliberately: a flag can be flipped to "configured" without a person being named,
#: and the whole point of the check is that naming somebody is the only way to pass it.
PLACEHOLDER_ID_PREFIX = "unassigned-"

#: The other half of the same guard. Somebody who invents a real-looking id and leaves the
#: name alone has still named nobody, and a check on the id alone would let that through.
PLACEHOLDER_NAME = "UNASSIGNED"

#: What `UNSEAL.md` tells an operator to run to see the numbers and whether the holders have
#: been named. Here rather than only in the runbook so a test can hold the two together; if
#: this module is ever renamed or moved, this string moves with it and the test comparing it
#: against `QuorumPolicy.__module__` fails rather than an operator finding out at a prompt.
PRINT_COMMAND = "uv run python -m brain.ops.vault_quorum"


@dataclass(frozen=True)
class Holder:
    """One person who holds one piece of the unseal key.

    Three fields, and only the third needed arguing about.

    `holder_id` is stable and `name` is not. Names change, get spelled two ways, and are
    shared: two people called Wei Ming are two pairs of hands and one string. The id is what
    the duplicate check compares and what any later question about custody refers to.

    `on_call` is the one operational fact stored, and it is here because the runbook already
    demands it: at least one piece has to sit outside the group that would be handling an
    incident, or a bad enough incident takes out the on-call rotation and the quorum together.
    Recording it turns a "should" in prose into a refusal at construction.

    **What is absent is the point.** No email address, no phone number, no department, no
    manager, no location. This file is committed, cloned by everyone who works on the project,
    and copied into every backup. Contact details in it would make it a single document
    answering "who do I have to reach to open this company's vault, and how" for anybody who
    obtains a copy. The five names are already the sensitive half; the way to reach them must
    not sit beside them, and whoever runs the setup knows how to contact their own colleagues.
    """

    holder_id: str
    name: str
    #: Whether this holder is in the incident on-call rotation.
    on_call: bool = True

    @property
    def identity(self) -> str:
        """The id as the duplicate check compares it: stripped and case-folded.

        Compared normalised because the ids in this file are typed by a person editing a
        list, and `r.jones` beside `R.Jones` is one pair of hands and two strings. Comparing
        raw is a guard that exists, is tested, and does not hold: five slots are accepted,
        the declared three-of-five is really two-of-four, and nothing anywhere says so.

        Over-merging is the safe direction. Two genuinely different holders whose ids differ
        only in case would be refused, and a refusal sends somebody back to a five-line list
        to look; under-merging lowers the threshold silently and forever.
        """
        return self.holder_id.strip().casefold()

    @property
    def is_placeholder(self) -> bool:
        """Whether this slot still names nobody.

        Three ways to be unnamed, because there are three ways to half-fill the slots and each
        of them looks configured at a glance: the shipped id left alone, the shipped name left
        alone, or a name blanked out ready to be typed over and then not typed over.
        """
        return (
            self.holder_id.startswith(PLACEHOLDER_ID_PREFIX)
            or not self.name.strip()
            or self.name.strip().upper() == PLACEHOLDER_NAME
        )


@dataclass(frozen=True)
class QuorumPolicy:
    """`shares` pieces of the unseal key, any `threshold` of which open the vault.

    Every combination refused in `__post_init__` is one OpenBao accepts without complaint. The
    vault does not have an opinion about whether a split is a sensible custody arrangement; it
    has an opinion about whether the arithmetic parses. This is where the difference is caught,
    and it has to be caught before the one command that can never be run twice.
    """

    shares: int
    threshold: int
    holders: tuple[Holder, ...]

    def __post_init__(self) -> None:
        if self.shares < 1:
            msg = f"a key split into {self.shares} pieces is no key at all"
            raise QuorumPolicyError(msg)

        if self.threshold < 1:
            msg = (
                f"a threshold of {self.threshold} would mean the vault opens with no piece "
                "presented"
            )
            raise QuorumPolicyError(msg)

        if self.threshold > self.shares:
            msg = (
                f"{self.threshold} of {self.shares} can never be met, so the vault would be "
                "initialised into a state nobody can ever open. OpenBao accepts this at init "
                "and the failure only appears at the first unseal, by which time the pieces "
                "have been distributed and the root token has scrolled off the screen."
            )
            raise QuorumPolicyError(msg)

        if self.threshold == 1:
            msg = (
                f"a threshold of 1 over {self.shares} pieces is not a split: every holder can "
                "open the vault alone, and all the pieces buy is more copies of one key to "
                "lose"
            )
            raise QuorumPolicyError(msg)

        if self.threshold == self.shares:
            msg = (
                f"{self.shares} of {self.shares} means one lost piece destroys the vault. Not "
                "locks it out of reach: destroys it. Nothing can decrypt the storage without "
                "every piece, so recovery means re-issuing every credential by hand from every "
                "provider."
            )
            raise QuorumPolicyError(msg)

        if len(self.holders) != self.shares:
            msg = (
                f"{self.shares} pieces and {len(self.holders)} named holders. A piece nobody "
                "is named against is a piece whose location cannot be stated, and in practice "
                "that is the one still sitting in the terminal it was printed in."
            )
            raise QuorumPolicyError(msg)

        seen: set[str] = set()
        doubled: set[str] = set()
        for holder in self.holders:
            if holder.identity in seen:
                doubled.add(holder.holder_id)
            seen.add(holder.identity)
        if doubled:
            msg = (
                f"{sorted(doubled)} holds more than one piece. Two pieces in one pair of hands "
                f"makes the declared threshold a fiction: {self.threshold}-of-{self.shares} "
                f"with one person holding two is really {self.threshold - 1}-of-"
                f"{self.shares - 1}, and the number everybody reasons with is the one written "
                "here."
            )
            raise QuorumPolicyError(msg)

        if all(holder.on_call for holder in self.holders):
            msg = (
                "every holder is on call, so the incident that takes out the on-call rotation "
                "takes the quorum with it. At least one piece has to sit outside the group "
                "that would be handling the outage."
            )
            raise QuorumPolicyError(msg)

    @property
    def survivable_losses(self) -> int:
        """How many pieces can be lost with the vault still openable."""
        return self.shares - self.threshold

    @property
    def is_configured(self) -> bool:
        """Whether every slot names a real person."""
        return not any(holder.is_placeholder for holder in self.holders)

    def assert_configured(self) -> None:
        """Refuse a policy whose holders are still the shipped placeholders.

        The failure this exists for is not a wrong number, it is a right-looking one. Five
        slots reading `UNASSIGNED` are a filled-in policy at a glance, and a vault initialised
        against them distributes five pieces to nobody in particular. Anything that acts on
        this policy as though the custody question were settled calls this first.
        """
        unnamed = [holder.holder_id for holder in self.holders if holder.is_placeholder]
        if unnamed:
            msg = (
                f"this policy still names nobody yet in {len(unnamed)} of {self.shares} slots "
                f"({', '.join(unnamed)}). Five pieces cannot be handed out until there are "
                "five people to hand them to, and a placeholder that reaches init is a piece "
                "with no owner from the first minute."
            )
            raise QuorumPolicyError(msg)


#: The decision from `docs/needs-rupash.md` item 17: five pieces, any three open it.
#:
#: Shipped with placeholder holders on purpose, and `assert_configured` refuses it in that
#: state. The alternative was to ship no default at all and make the first operator invent
#: both numbers, which moves the decision out of review and into the hour somebody is trying
#: to get a vault running.
DEFAULT_POLICY = QuorumPolicy(
    shares=5,
    threshold=3,
    holders=(
        Holder("unassigned-1", PLACEHOLDER_NAME),
        Holder("unassigned-2", PLACEHOLDER_NAME),
        Holder("unassigned-3", PLACEHOLDER_NAME),
        Holder("unassigned-4", PLACEHOLDER_NAME),
        # The slot the runbook requires to sit outside the on-call rotation, marked here so
        # the requirement survives whoever fills the names in without reading the prose.
        Holder("unassigned-5", PLACEHOLDER_NAME, on_call=False),
    ),
)


def init_args(policy: QuorumPolicy = DEFAULT_POLICY) -> tuple[str, ...]:
    """The `bao operator init` flags this policy means, and nothing else.

    Deliberately pure and deliberately not gated on `assert_configured`. The numbers were
    decided before the people were, and a renderer that refused to state them until five names
    existed would make the runbook unable to explain the shape of the thing it is describing.
    The gate belongs on `main`, which is what an operator actually runs.
    """
    return (f"-key-shares={policy.shares}", f"-key-threshold={policy.threshold}")


def init_command(policy: QuorumPolicy = DEFAULT_POLICY) -> str:
    """The whole line an operator pastes, so `UNSEAL.md` can quote it verbatim.

    A whole line rather than the flags alone, because the runbook is quoted against this
    string by a test. Comparing flags would let the surrounding command drift while the check
    still passed, and the surrounding command is the half with the container name in it.
    """
    return f"docker exec -it {VAULT_CONTAINER} bao operator init " + " ".join(init_args(policy))


def revoke_root_command() -> str:
    """The line that ends the root token's existence. The `revoke_root` step of the runbook.

    Takes no policy: revocation is not parameterised by the split, and giving it an argument
    would suggest there is a configuration in which it is skipped. There is not. See the
    module docstring for why the sealed-envelope alternative was rejected.
    """
    return f"docker exec -it {VAULT_CONTAINER} bao token revoke -self"


def main() -> int:
    """`python -m brain.ops.vault_quorum`. Prints what `UNSEAL.md` tells an operator to run.

    Exits non-zero while any holder slot is unnamed, because "the setup helper ran cleanly" is
    the wrong thing for a vault with no named custodians to look like. The commands are printed
    anyway rather than withheld: an operator who cannot see them just types them from memory,
    and the memory is where the wrong numbers live.
    """
    policy = DEFAULT_POLICY
    print(f"shares:    {policy.shares}")
    print(f"threshold: {policy.threshold}  (survives losing {policy.survivable_losses})")
    print()
    print(f"initialise:  {init_command(policy)}")
    print(f"revoke_root: {revoke_root_command()}")
    print()
    print("holders:")
    for position, holder in enumerate(policy.holders, 1):
        rota = "on call" if holder.on_call else "outside the on-call rotation"
        flag = "   <- nobody named yet" if holder.is_placeholder else ""
        print(f"  {position}. {holder.name} ({holder.holder_id}, {rota}){flag}")

    try:
        policy.assert_configured()
    except QuorumPolicyError as exc:
        print(f"\nnot ready to initialise: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
