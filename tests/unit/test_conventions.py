"""The repository conventions, and the files that carry the ones code cannot.

Two halves. The first tests the rules in `brain.ops.conventions`, which decide whether a
commit message and a branch name mean what they say. The second tests three files that
are not code and therefore have nothing else guarding them: CODEOWNERS, the pull request
template, and the hook that calls the first half.

The second half is the unusual one, and it is here because those files fail silently. A
CODEOWNERS line deleted in a merge does not break a build; it just stops requiring a
review, and the first anybody knows is a permission change that went in unread.

Task ids: M38.1.1.1, M38.1.1.2, M38.1.1.4, M38.1.1.5
"""

from __future__ import annotations

from pathlib import Path

import pytest

from brain.ops.conventions import (
    check_branch_name,
    check_commit_message,
    leaf_ids_in,
)

REPO = Path(__file__).resolve().parents[2]


# ------------------------------------------------- the commit message (M38.1.1.2)
def test_a_message_closing_a_leaf_is_accepted() -> None:
    """The happy path. If this fails, every commit in the repository is refused and the
    rule gets turned off within the hour."""
    assert check_commit_message("Add the thing\n\nCloses: M12.1.1") is None


def test_a_message_closing_nothing_is_accepted() -> None:
    """A revert, a formatting pass, a fix to a fix and a merge all legitimately close
    nothing. Demanding an id from them would be satisfied with an invented one, and an
    invented id is worse than no id: it marks work done that nobody did."""
    assert check_commit_message("Fix a typo in a comment") is None


def test_a_parent_id_closes_nothing() -> None:
    """The rule that most needs enforcing, because `Closes: M12` is the natural thing to
    type. A module id is a summary of its children, so closing one would mark ten tasks
    done on the strength of finishing one."""
    refusal = check_commit_message("Work\n\nCloses: M12")
    assert refusal is not None
    assert "module or group id" in refusal.reason


def test_a_group_id_closes_nothing_either() -> None:
    """`M12.1` looks specific enough to be safe and is not. Two parts is a group; three
    is a leaf. Deleting this leaves the boundary undefended in the case that actually
    looks plausible on a Friday."""
    refusal = check_commit_message("Work\n\nCloses: M12.1")
    assert refusal is not None
    assert "module or group id" in refusal.reason


def test_an_id_mentioned_in_prose_closes_nothing() -> None:
    """A message explaining why M12.1.1 was deliberately *not* done must not close it.
    Only the Closes: line closes anything, and the difference matters most in exactly the
    commit that discusses a task at length."""
    message = "Deferred the registry\n\nM12.1.1 needs a decision from Rupash first."
    assert leaf_ids_in(message) == ()
    assert check_commit_message(message) is None


def test_several_ids_on_one_line_are_all_checked() -> None:
    """Deleting this lets a bad id hide behind a good one, which is the shape a real
    mistake takes: two ids typed together, one of them a parent."""
    assert leaf_ids_in("x\n\nCloses: M12.1.1, M12.1.2") == ("M12.1.1", "M12.1.2")
    refusal = check_commit_message("x\n\nCloses: M12.1.1, M12")
    assert refusal is not None


def test_an_empty_message_is_refused() -> None:
    """Git allows it with --allow-empty-message and the history is then unreadable at
    exactly the commit somebody is trying to understand."""
    refusal = check_commit_message("   \n\n  ")
    assert refusal is not None
    assert "subject line" in refusal.reason


def test_a_refusal_quotes_what_was_written() -> None:
    """A hook that prints only "invalid commit message" makes the author guess which of
    three rules they broke. Guessing wrong twice is how a rule gets disabled."""
    refusal = check_commit_message("Add the registry\n\nCloses: M12")
    assert refusal is not None
    assert "M12" in str(refusal)
    assert "Add the registry" in str(refusal)


# -------------------------------------------------- the branch name (M38.1.1.1)
@pytest.mark.parametrize("name", ["M12/tool-registry", "M38.1/staging", "M3/gate"])
def test_a_branch_named_for_its_module_is_accepted(name: str) -> None:
    assert check_branch_name(name) is None


@pytest.mark.parametrize("name", ["fix", "feature/registry", "M12", "M12/Tool_Registry"])
def test_a_branch_not_named_for_its_module_is_refused(name: str) -> None:
    """Twelve tracks run at once, and they collide in one way: two branches that sound
    alike get reviewed as though they were the same work. The module id makes that
    impossible to do by accident."""
    assert check_branch_name(name) is not None


def test_main_is_exempt() -> None:
    """The trunk is not a track. Refusing it would make the rule something people work
    around rather than follow."""
    assert check_branch_name("main") is None


# ------------------------------------- the files that carry the rest (M38.1.1.4, M38.1.1.5)
#: Every file that can widen what an answer contains. Each needs a second reader, and the
#: reason is the same for all of them: a mistake here returns a plausible answer that is
#: simply too wide, so nothing looks broken.
MUST_HAVE_AN_OWNER = (
    "/src/brain/core/entitlement.py",
    "/src/brain/core/scope.py",
    "/src/brain/core/scope_sql.py",
    "/src/brain/core/redaction.py",
    "/src/brain/core/field_policy.py",
    "/src/brain/core/projection.py",
    "/src/brain/gate/",
    "/tests/invariants/",
)


@pytest.mark.parametrize("path", MUST_HAVE_AN_OWNER)
def test_every_file_that_can_widen_an_answer_has_an_owner(path: str) -> None:
    """CODEOWNERS fails silently, which is why it is tested rather than trusted. A line
    dropped in a merge breaks no build; it stops requiring a review, and the first anybody
    knows is a permission change that went in unread.

    `scope_sql.py` is on this list because it has already gone wrong: an unescaped LIKE
    and a string splitting into an IN list both made the SQL admit rows the Python
    evaluator refused. The redactor is on it because reclassifying one field from
    confidential to internal changes every answer touching that field and breaks no test
    about redaction.
    """
    owners = (REPO / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    lines = [
        line for line in owners.splitlines() if line.strip() and not line.strip().startswith("#")
    ]
    covered = {line.split()[0] for line in lines}
    assert path in covered, f"{path} can widen an answer and nobody is required to read it"
    assert any(line.split()[0] == path and "@" in line for line in lines)


def test_the_pull_request_template_asks_for_leaf_ids_only() -> None:
    """The status page is generated from what the template collects. A template that
    accepted a parent id would produce a page claiming ten tasks done for one."""
    template = (REPO / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
    assert "Closes: M" in template
    assert "Leaf ids only" in template
    assert "a parent id closes nothing" in template


def test_the_commit_hook_calls_the_tested_rule_rather_than_repeating_it() -> None:
    """A hook is the worst place to put logic: it does not run in CI, it does not run for
    anybody who has not set core.hooksPath, and nothing tests it. This asserts the shim
    stays a shim, so the rule keeps living where the tests can reach it."""
    hook = (REPO / "ops" / "hooks" / "commit-msg").read_text(encoding="utf-8")
    assert "brain.ops.conventions" in hook
    # No second copy of the rule. A hook that grepped for `Closes:` itself would drift
    # from the module the moment either changed, and the hook is the copy nobody tests.
    assert "Closes:" not in hook.replace("`Closes:` line", "")
