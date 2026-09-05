"""The unseal quorum. Every test here is a way the vault gets initialised wrong once, forever.

`bao operator init` runs a single time in the life of a vault and there is no second attempt:
the pieces are printed, distributed and never shown again. So every refusal in
`brain.ops.vault_quorum` guards a decision that cannot be revisited by editing a file, and
every test here is the last thing standing between a bad combination and a permanent one.

Task ids: M31.3.2.1
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain.ops.vault_quorum import (
    DEFAULT_POLICY,
    PLACEHOLDER_NAME,
    PRINT_COMMAND,
    Holder,
    QuorumPolicy,
    QuorumPolicyError,
    init_args,
    init_command,
    revoke_root_command,
)

REPO = Path(__file__).resolve().parents[2]
UNSEAL = REPO / "ops" / "openbao" / "UNSEAL.md"


def _holders(count: int) -> tuple[Holder, ...]:
    """`count` distinct, named holders, the first of them off the on-call rota.

    Named rather than placeholder, and distinct rather than repeated, so that a test about
    arithmetic fails on the arithmetic rather than on a rule it was not asking about.
    """
    return tuple(
        Holder(f"person-{i}", f"Person {i}", on_call=i != 1) for i in range(1, count + 1)
    )


# ------------------------------------------------- combinations OpenBao would accept
def test_a_threshold_above_the_share_count_is_refused() -> None:
    """`-key-shares=3 -key-threshold=4` initialises without a word of complaint and produces
    a vault nobody can ever open. The failure surfaces at the first unseal, which is after the
    pieces have been handed out and the root token has scrolled off the screen, so there is no
    moment at which somebody could still fix it. Deleting this test lets that line through
    review looking like an ordinary pair of numbers."""
    with pytest.raises(QuorumPolicyError, match="never be met"):
        QuorumPolicy(shares=3, threshold=4, holders=_holders(3))


def test_a_threshold_of_one_is_refused() -> None:
    """Five pieces with a threshold of one is not a split, it is five copies of the key. It
    passes every glance because the numbers look like a quorum, and it means any one of the
    five holders, or anyone who obtains any one piece, opens the vault alone. Deleting this
    test allows the arrangement the whole splitting exercise exists to prevent."""
    with pytest.raises(QuorumPolicyError, match="not a split"):
        QuorumPolicy(shares=5, threshold=1, holders=_holders(5))


def test_a_threshold_equal_to_the_share_count_is_refused() -> None:
    """Five of five reads as the most secure option and is the one that destroys the vault.
    One holder leaving the company, losing a laptop or being unreachable takes every credential
    with them, permanently, because nothing can decrypt the storage without every piece.
    Deleting this test lets somebody harden the configuration into an unrecoverable one."""
    with pytest.raises(QuorumPolicyError, match="one lost piece"):
        QuorumPolicy(shares=5, threshold=5, holders=_holders(5))


def test_a_share_count_below_one_is_refused() -> None:
    """The degenerate case, and it is reachable: a policy assembled from a loop over an empty
    holder list produces exactly this. Without the check it fails later and further away, at
    the threshold rules, with a message about arithmetic rather than about there being no
    key."""
    with pytest.raises(QuorumPolicyError, match="no key at all"):
        QuorumPolicy(shares=0, threshold=0, holders=())


def test_a_threshold_below_one_is_refused() -> None:
    """A threshold of zero says the vault opens with nothing presented. It cannot be honoured
    and it is what an unset or miscomputed value looks like. Deleting this test lets a zero
    reach `bao operator init`, where the rejection is a CLI usage error rather than a sentence
    saying what was wrong."""
    with pytest.raises(QuorumPolicyError, match="no piece presented"):
        QuorumPolicy(shares=5, threshold=0, holders=_holders(5))


def test_a_holder_list_shorter_than_the_share_count_is_refused() -> None:
    """Five pieces and four names means one piece exists whose location nobody can state. In
    practice that is the one still sitting in the terminal it was printed in, or in the message
    that distributed the others. Deleting this test allows a policy that is silent about a live
    key while looking complete."""
    with pytest.raises(QuorumPolicyError, match="named holders"):
        QuorumPolicy(shares=5, threshold=3, holders=_holders(4))


def test_the_same_person_holding_two_pieces_is_refused() -> None:
    """**The check this module exists for, and the only one invisible in the numbers.** A
    three-of-five where one person holds two pieces is really a two-of-four: two people can
    open the vault, while the policy, the runbook and everybody reasoning about custody still
    say three. Every other rule here catches a combination that looks wrong on inspection; this
    one catches a combination that looks exactly right.

    Compared on `holder_id` rather than on the `Holder` object, and that is the load-bearing
    detail: two `Holder`s with the same id and two spellings of the name are unequal as objects
    and are one pair of hands in the world, so a set-of-holders check would pass.

    Deleting this test means the declared threshold can be quietly higher than the real one,
    which is a weakening nothing outside this file could ever detect."""
    people = _holders(5)
    doubled = (*people[:4], Holder(people[0].holder_id, "Person One", on_call=False))
    with pytest.raises(QuorumPolicyError, match="more than one piece"):
        QuorumPolicy(shares=5, threshold=3, holders=doubled)


@pytest.mark.parametrize(
    ("second", "how"),
    [("R.JONES", "a different capitalisation"), (" r.jones ", "surrounding whitespace")],
)
def test_one_person_written_two_ways_is_still_one_person(second: str, how: str) -> None:
    """The guard behind the guard, and it was open when this module was first written.

    The duplicate check compares ids, which is right, and it compared them raw, which is
    not: these ids are typed by somebody editing a five-line list, so `r.jones` beside
    `R.Jones` is one pair of hands and two strings. The policy was accepted, and a declared
    three-of-five was really a two-of-four with nothing anywhere saying so.

    It is the same failure as the test above and it survived that test, which is the point:
    an exact-match check passes every case built from `_holders`, because a generator never
    produces the two spellings a person does. Delete this and the normalisation looks like
    tidiness and is removed as such."""
    people = _holders(5)
    first = Holder("r.jones", "R Jones", on_call=False)
    doubled = (first, *people[1:4], Holder(second, "Rita Jones", on_call=False))
    with pytest.raises(QuorumPolicyError, match="more than one piece"):
        QuorumPolicy(shares=5, threshold=3, holders=doubled)


def test_two_holders_whose_ids_merely_look_alike_are_still_two() -> None:
    """The other direction, so the normalisation cannot be widened into refusing everything.
    A check that refused any five holders would satisfy the test above."""
    people = _holders(5)
    distinct = (
        Holder("r.jones", "R Jones", on_call=False),
        Holder("r.jonas", "R Jonas", on_call=False),
        *people[2:5],
    )
    assert QuorumPolicy(shares=5, threshold=3, holders=distinct).shares == 5


def test_a_quorum_entirely_inside_the_on_call_rota_is_refused() -> None:
    """The runbook says at least one piece should sit outside the group handling an incident,
    and a "should" in prose is followed until the day it is inconvenient. If all five holders
    are on call, the incident bad enough to need the vault opened is the same incident that has
    all five of them occupied or unreachable. Deleting this test turns a structural property of
    the custody design back into a sentence nobody reads at the time it matters."""
    everyone_on_call = tuple(Holder(f"person-{i}", f"Person {i}") for i in range(1, 6))
    with pytest.raises(QuorumPolicyError, match="on call"):
        QuorumPolicy(shares=5, threshold=3, holders=everyone_on_call)


# ------------------------------------------------------------ placeholders
def test_the_shipped_policy_is_five_pieces_and_any_three() -> None:
    """The decision, recorded where changing it fails a test rather than passing as an edit.
    `docs/needs-rupash.md` item 17: five pieces, any three open it, so three people have to
    agree and the company survives losing two. Deleting this test lets somebody tidy the
    numbers into a two-of-three without anybody re-deciding."""
    assert DEFAULT_POLICY.shares == 5
    assert DEFAULT_POLICY.threshold == 3
    assert DEFAULT_POLICY.survivable_losses == 2
    assert len(DEFAULT_POLICY.holders) == 5


def test_the_shipped_policy_is_refused_until_real_people_are_named() -> None:
    """The one that stops a placeholder being mistaken for a configuration. Five slots reading
    `UNASSIGNED` look filled in at a glance, and a vault initialised against them hands five
    pieces to nobody in particular. Deleting this test makes "shipped with placeholders" and
    "custody decided" indistinguishable to every caller."""
    assert not DEFAULT_POLICY.is_configured
    with pytest.raises(QuorumPolicyError, match="names nobody yet"):
        DEFAULT_POLICY.assert_configured()


def test_half_filling_a_holder_slot_still_counts_as_unnamed() -> None:
    """The likely way this gets fudged: somebody replaces the obvious `unassigned-` ids and
    leaves the names, or types the names and leaves the ids, or blanks a name ready to type
    over it and then does not. All three look more configured than they are. Deleting this test
    leaves a check that a single find-and-replace defeats."""
    real = _holders(5)
    name_left_alone = (Holder("kai-tan", PLACEHOLDER_NAME, on_call=False), *real[1:])
    id_left_alone = (Holder("unassigned-9", "Kai Tan", on_call=False), *real[1:])
    name_blanked = (Holder("kai-tan", "   ", on_call=False), *real[1:])
    for holders in (name_left_alone, id_left_alone, name_blanked):
        assert not QuorumPolicy(shares=5, threshold=3, holders=holders).is_configured


def test_a_policy_naming_real_people_passes_the_configuration_check() -> None:
    """The other side of the placeholder guard. Without it, `assert_configured` could be a
    function that always raises and every test above would still pass, while no vault could
    ever be declared ready to initialise."""
    policy = QuorumPolicy(shares=5, threshold=3, holders=_holders(5))
    assert policy.is_configured
    policy.assert_configured()


def test_a_holder_is_not_a_personnel_record() -> None:
    """Structural, and the mechanism rather than a reminder. This file is committed, cloned by
    everyone who works on the project and copied into every backup. A `Holder` able to carry an
    email address or a phone number would turn it into a single document answering "who do I
    have to reach to open this company's vault, and how" for anybody who obtains it. A type
    with nowhere to put a phone number cannot be talked into carrying one at eleven at night.

    Mirrors the same structural check on `VaultAccess` in the vault audit suite."""
    import dataclasses

    names = {field.name for field in dataclasses.fields(Holder)}
    for forbidden in (
        "email",
        "phone",
        "mobile",
        "address",
        "contact",
        "department",
        "manager",
        "location",
        "employee_id",
    ):
        assert forbidden not in names, f"Holder has a {forbidden!r} field"


# ------------------------------------------------------------------ drift
def test_the_init_arguments_are_exactly_the_policy_numbers() -> None:
    """**The anti-drift test.** The two numbers have to exist in two places that agree: this
    object, which is what was reviewed and decided, and the flags an operator actually runs.
    Anything that renders them differently - a swapped pair of flags, an off-by-one, a copy
    left behind from an earlier decision - produces a vault split differently from the one the
    company agreed to, and no later inspection of the running vault can tell you which split
    was intended.

    Asserted against a policy that is not the default as well as against the default, so a
    renderer that ignored its argument and printed 5 and 3 would still fail. Deleting this test
    is what lets the Python object and the shell command drift apart, which is the entire
    reason the module exists."""
    seven_of_four = QuorumPolicy(shares=7, threshold=4, holders=_holders(7))
    assert init_args(seven_of_four) == ("-key-shares=7", "-key-threshold=4")
    assert init_args(DEFAULT_POLICY) == ("-key-shares=5", "-key-threshold=3")


def test_the_runbook_quotes_the_command_this_module_renders() -> None:
    """The other half of the anti-drift guard, and the half that actually decays. `UNSEAL.md`
    is the file somebody follows at eleven at night; the policy object is the file nobody
    opens. When they disagree the runbook wins, because the runbook is what gets typed. This
    makes disagreeing impossible to commit.

    Only the command is checked here. Whether the page says where the numbers came from is a
    separate claim that can be lost on its own, and it has its own test below."""
    text = UNSEAL.read_text(encoding="utf-8")
    assert init_command() in text, (
        f"UNSEAL.md does not contain the command this module renders:\n  {init_command()}"
    )


def test_the_runbook_says_where_the_numbers_come_from_and_how_to_print_them() -> None:
    """Quoting the right command is not the same as saying it is derived. A page showing
    `-key-shares=5` with no indication of where the 5 came from invites the next person to
    edit the 5 in place, which is the exact failure this module exists to stop, and that edit
    passes review because the page still reads correctly afterwards.

    Two claims, asserted separately, because either can be lost while the other survives: the
    sentence naming the module, and the command an operator runs to see the numbers. The
    module name inside the invocation does not count as the sentence, which is why the
    invocation is stripped out before the second check. Written after a mutation that deleted
    the sentence and was not caught, because the invocation still mentioned the module.

    Also holds `PRINT_COMMAND` against the module's real import path, so renaming or moving
    this module fails here rather than at an operator's prompt.

    Deleting this test lets the derivation claim be edited out while the numbers stay right,
    which puts the page back to being the place the numbers live."""
    text = UNSEAL.read_text(encoding="utf-8")
    assert PRINT_COMMAND.rsplit(" ", 1)[-1] == QuorumPolicy.__module__, (
        f"PRINT_COMMAND names {PRINT_COMMAND.rsplit(' ', 1)[-1]!r}, but this module is "
        f"{QuorumPolicy.__module__!r}; the runbook is telling operators to run the wrong thing"
    )
    assert PRINT_COMMAND in text, (
        f"UNSEAL.md never shows how to print the numbers:\n  {PRINT_COMMAND}"
    )
    prose = text.replace(PRINT_COMMAND, "")
    assert "brain.ops.vault_quorum" in prose, (
        "UNSEAL.md shows the command but nowhere says the numbers come from the policy "
        "module, so the next edit will be made to the numbers on this page"
    )


def test_the_runbook_names_no_share_count_other_than_this_one() -> None:
    """A second copy of the numbers further down the page is how the first copy goes stale.
    The command is updated, the sentence three paragraphs below it is not, and the operator
    following the page reads whichever one they reach first. Deleting this test lets a stale
    second copy sit beside a correct first one, which is worse than either alone."""
    text = UNSEAL.read_text(encoding="utf-8")
    assert set(re.findall(r"-key-shares=(\d+)", text)) == {str(DEFAULT_POLICY.shares)}
    assert set(re.findall(r"-key-threshold=(\d+)", text)) == {str(DEFAULT_POLICY.threshold)}


def test_the_runbook_carries_the_root_token_revocation_step() -> None:
    """The root token is the one credential that bypasses every policy in
    `ops/openbao/policies`, and the audit log cannot distinguish its use from legitimate admin
    work because nothing in an entry marks a token as root. A setup that stops before revoking
    it leaves that bypass in place permanently and looks finished.

    Deleting this test lets the `revoke_root` step be dropped from the runbook during an edit,
    which is a change that removes nothing visible and leaves the vault with a permanent,
    unattributable way around itself."""
    text = UNSEAL.read_text(encoding="utf-8")
    assert "revoke_root" in text, "UNSEAL.md has no step named revoke_root"
    assert revoke_root_command() in text, (
        f"UNSEAL.md does not contain the revocation command this module renders:\n"
        f"  {revoke_root_command()}"
    )
