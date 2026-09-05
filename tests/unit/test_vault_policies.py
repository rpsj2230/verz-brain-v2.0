"""The vault's policy files and its credential slots, which are documents rather than code.

Both of these fail by absence, and absence is invisible in a diff. A role whose policy file
is missing does not get a narrower policy, it gets whatever token the operator happened to
use when they loaded the rest, which in practice during first setup is root. A connector
added to the code without a slot argued about first gets its scopes decided during the hour
somebody is trying to make it work, which is the one hour in which "just give it write" is
the fastest answer.

The third property here is the one worth keeping forever: **every slot is empty**. A
credential committed into this repository is not undone by deleting it in the next commit,
because git keeps the old one. That is the failure this file exists to make loud on the
commit that introduces it rather than on the day somebody reads the history.

Read as files rather than against a running vault, deliberately. There is no OpenBao in a
unit test and standing one up would test that OpenBao works. What can be tested without one
is that the repository declares a policy for every role the code can ask as, a slot for
every connector and provider the code knows about, and nothing in either that looks like a
secret.

Task ids: M31.3.2.2, M38.4.1.3
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain.ops.limits import SOURCE_CEILINGS
from brain.ops.provider_keys import PROVIDER_SLOTS, ProviderSlot
from brain.ops.secrets import VaultRole

REPO = Path(__file__).resolve().parents[2]
POLICIES = REPO / "ops" / "openbao" / "policies"
SLOTS_DOC = REPO / "ops" / "openbao" / "credential-slots.md"

#: Sorted here rather than inside `parametrize`, so the element type survives. Pytest types
#: its argument values as `Iterable[object]`, which pushes `object` into a lambda written
#: there and makes the attribute access mypy's problem rather than the reader's.
PROVIDERS: list[ProviderSlot] = sorted(PROVIDER_SLOTS, key=lambda slot: slot.slug)


def _policy_file(role: VaultRole) -> Path:
    """Where a role's policy lives. `browser_runner` is `browser-runner.hcl`: the enum spells
    a Python identifier and a vault policy name is conventionally hyphenated."""
    return POLICIES / f"{role.value.replace('_', '-')}.hcl"


def _granted_paths(text: str) -> dict[str, list[str]]:
    """Every `path "..." { capabilities = [...] }` block in a policy, as path to capabilities.

    Parsed out of the HCL rather than substring-matched, because the thing that must be
    asserted is what a rule grants, and a rule that has been commented out still contains
    every word it did before.
    """
    live = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    found: dict[str, list[str]] = {}
    for path, caps in re.findall(
        r'path\s+"([^"]+)"\s*\{[^}]*capabilities\s*=\s*\[([^\]]*)\]', live, re.S
    ):
        found[path] = re.findall(r'"([^"]+)"', caps)
    return found


def _slot_paths() -> set[str]:
    """Every vault path named in a table row of the credential-slots document.

    A slot is declared by appearing in the table with its scopes beside it. Prose mentioning
    a path does not declare a slot, which is why this reads table rows rather than the file.
    """
    slots: set[str] = set()
    for line in SLOTS_DOC.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        first = line.split("|")[1].strip()
        match = re.fullmatch(r"`([a-z][a-z0-9_/*+-]*)`", first)
        if match:
            slots.add(match.group(1))
    return slots


# ------------------------------------------------------ a policy per role (M31.3.2.2)
@pytest.mark.parametrize("role", sorted(VaultRole, key=str))
def test_every_role_the_code_can_ask_as_has_a_policy_of_its_own(role: VaultRole) -> None:
    """Parametrised from `VaultRole` rather than from the directory listing, so a fourth role
    added to the enum arrives here already failing.

    Deleting this makes a missing policy silent. The role still exists, callers still pass
    it, and the vault answers using whatever token loaded the others, which during first
    setup is root. The failure is not a refusal, it is a role that is quietly wider than the
    file it was supposed to be narrowed by.
    """
    where = _policy_file(role)
    assert where.exists(), f"{role.value} has no policy at {where.relative_to(REPO)}"
    assert _granted_paths(where.read_text(encoding="utf-8")), (
        f"{where.name} grants nothing; a policy with no live rule is not a narrower policy"
    )


def test_the_three_policies_are_three_different_policies() -> None:
    """One file copied to three names passes the test above and grants every role the widest
    of the three. The point of a policy per role is that they differ, and the differences are
    the whole design: the worker may renew and the browser runner may not, the browser runner
    gets no database credential, the application gets no static secret.

    Compared as parsed rules rather than as file text, so a difference that is only a comment
    does not count as a difference.
    """
    granted = {
        role.value: _granted_paths(_policy_file(role).read_text(encoding="utf-8"))
        for role in VaultRole
    }
    pairs = [(a, b) for a in granted for b in granted if a < b]
    assert pairs
    for left, right in pairs:
        assert granted[left] != granted[right], f"the {left} and {right} policies are identical"


def test_the_browser_runner_is_given_no_database_credential() -> None:
    """The narrowest policy, and the reason it is narrowest: the browser runner executes
    content it did not write, on pages it does not control. A browser process holding a
    database credential is one page-level exploit away from being a database client.

    Deleting this lets `database/creds/...` be added to that policy during a debugging
    session and stay there, because nothing else in the repository would notice.
    """
    granted = _granted_paths(_policy_file(VaultRole.BROWSER_RUNNER).read_text(encoding="utf-8"))
    offenders = [path for path in granted if path.startswith("database/")]
    assert not offenders, f"the browser runner may reach {offenders}"


def test_the_worker_reaches_named_connectors_and_never_a_wildcard() -> None:
    """The worker runs on a schedule with nobody watching. A wildcard over the connector
    credentials would make the least-watched process in the system the one that can borrow
    any source's key, which is a way to read anything with no person in the loop.

    The application has the wildcard deliberately, because a person's question can reach any
    connector they are entitled to. The difference between the two is the property, so this
    asserts both halves: widening the worker to `+` would otherwise read as consistency.
    """
    worker = _granted_paths(_policy_file(VaultRole.WORKER).read_text(encoding="utf-8"))
    application = _granted_paths(_policy_file(VaultRole.APPLICATION).read_text(encoding="utf-8"))
    assert "connectors/creds/+" not in worker, "the worker may borrow any connector's key"
    assert any(p.startswith("connectors/creds/") for p in worker), "the worker reaches none"
    assert "connectors/creds/+" in application


def test_the_loader_reads_the_directory_rather_than_a_list_of_names() -> None:
    """A loader naming its three files loads three files. Adding a fourth policy then needs
    two edits, one of which is in a shell script nobody opens, and the policy that does not
    get loaded is the new one.

    Asserted on the script because there is no vault here to load into.
    """
    loader = (REPO / "ops" / "openbao" / "load-policies.sh").read_text(encoding="utf-8")
    assert "policies/*.hcl" in loader, "the loader no longer globs the policy directory"


# ------------------------------------------- a slot per connector and provider (M38.4.1.3)
@pytest.mark.parametrize("connector", sorted(c.name for c in SOURCE_CEILINGS))
def test_every_connector_the_code_knows_about_has_a_credential_slot(connector: str) -> None:
    """Parametrised from `SOURCE_CEILINGS`, which is the closed list of sources this system
    has measured a ceiling for. A connector in that list with no slot in the document is a
    connector whose scopes have not been argued about.

    That argument is cheap now and expensive later. Deleting this test means the scopes get
    decided during the hour somebody is trying to make the connector work, and "read and
    write, we can narrow it later" is the fastest thing to type in that hour.
    """
    assert f"connectors/creds/{connector}" in _slot_paths(), (
        f"{connector} has a measured rate limit and no credential slot"
    )


@pytest.mark.parametrize("slot", PROVIDERS)
def test_every_model_provider_has_a_slot_at_the_path_the_code_reads(slot: ProviderSlot) -> None:
    """The provider keys are the one category that cannot be leased, so they are read
    straight out of the vault at a path built from the slug. The document and the code have
    to name the same path or the operator fills a slot nothing reads.

    Deleting this lets the two drift, and the symptom is an unauthenticated model call at
    startup rather than an error, because a provider SDK with no key does not always refuse
    at the moment it is configured.
    """
    assert slot.path in _slot_paths(), f"{slot.slug} has no slot in credential-slots.md"
    body = SLOTS_DOC.read_text(encoding="utf-8")
    row = next(line for line in body.splitlines() if f"`{slot.path}`" in line)
    assert slot.env_var in row, (
        f"the document names a different environment variable for {slot.slug} than the code "
        f"declares; the code says {slot.env_var}"
    )


def test_no_declared_slot_holds_a_value_in_this_repository() -> None:
    """The failure worth catching forever. A credential committed here is not undone by the
    next commit deleting it, because git keeps the old one, so the only useful moment to
    catch it is the commit that adds it.

    Looks for the two shapes a filled slot takes: a `kv put` writing into a slot path, and a
    slot path followed by an assignment. Both are what somebody types when they are pasting a
    key in to test something and mean to take it out again.
    """
    slots = _slot_paths()
    assert slots, "no slots are declared at all; the document has stopped being a document"

    offenders: list[str] = []
    for path in sorted((REPO / "ops").rglob("*")):
        if not path.is_file():
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
        ):
            if re.search(r"\b(bao|vault)\s+kv\s+put\b", line):
                offenders.append(f"{path.relative_to(REPO)}:{number} writes a value into a slot")
                continue
            for slot in slots:
                stem = slot.rstrip("*+")
                if stem and stem in line and re.search(rf"{re.escape(stem)}\S*\s+\w+=\S", line):
                    offenders.append(f"{path.relative_to(REPO)}:{number} assigns into {slot}")
    assert not offenders, offenders


def test_the_document_says_which_scopes_were_refused_and_not_only_which_were_asked_for() -> None:
    """A table of granted scopes is a list of decisions with the reasoning thrown away. The
    column that matters six months later is the one saying what was deliberately not asked
    for, because that is the column somebody has to argue against rather than silently widen.

    Deleting this lets the refused column be dropped as redundant, and the next person adds a
    write scope without ever seeing that its absence was a decision.
    """
    header = next(
        line for line in SLOTS_DOC.read_text(encoding="utf-8").splitlines() if "| Slot |" in line
    )
    assert "NOT requested" in header, "the refused-scopes column is gone from the slot table"
