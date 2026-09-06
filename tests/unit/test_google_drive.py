"""The Google Drive connector, driven by documentation because there is nothing else.

**There is no Google Drive recording and there cannot be one here.**
`tests/fixtures/cassettes.py` declares no `Source.GOOGLE_DRIVE`, that file is shared with
every other connector, and `tests/invariants/test_cassettes.py` asserts over it, so adding a
member would be editing a fixture this leaf does not own. Every Drive fact these tests pin
therefore comes from Google's published documentation rather than from a recorded exchange,
which is a weaker footing than `test_freshdesk.py` and `test_xero.py` stand on and is stated
rather than left to be discovered. `test_no_recording_exists_for_this_source_and_the_reply_
shape_is_ready_for_one` is where that is written down, and it proves the shape by taking an
existing cassette from another source and reading it through this connector's own `Reply`
without a translation step.

What the absence actually costs is worth naming. The reason strings on a throttling 403, the
exact error envelope, and whether a shared-drive listing returns permissions to a
non-manager service account are all decisions this module makes from documentation. A
recording would settle them; the tests below pin the *shape* that survives either answer, so
the day a cassette exists it drops in and these tests keep meaning what they mean.

Five properties are being pinned, and each is a way this connector's subject goes wrong
without anything failing.

**The folder pin is checked before anything is read.** `place` is the only thing that issues
an `InScopeFile` and the knowledge path takes nothing else, so "read a file nobody placed"
is not an expression that exists. The tests here are about the third answer: a file whose
folder could not be determined is neither inside nor outside, and both defaults are silent.

**A shortcut leaves the folder while looking entirely ordinary.** It has a name, a parent
and a modified time; it is the escape, and it is refused as content.

**Drive's sharing decides nothing here, in either direction.** A link-shared file does not
become company-wide, a private file does not become unreadable, and a file whose sharing was
never read is refused rather than stored at either default.

**A file id is the identity.** Nothing stores a path, the projected folder is the pin rather
than the parent, and the revision is kept beside the timestamp.

**A silent 200 is the shared-drive failure.** A listing without the all-drives parameters
comes back empty and correct-looking, so the refusal has to happen where the request is
built.

Task ids: M11.6.7
"""

from __future__ import annotations

import dataclasses
import inspect
from dataclasses import dataclass, field, is_dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from brain.connectors.change_signal import DeletionCheck
from brain.connectors.contract import (
    AccessMode,
    ConnectorContractError,
    FetchRequest,
    assert_fetches_only,
    assert_holds_no_credential,
)
from brain.connectors.google_drive import (
    DEFAULT_PAGE_SIZE,
    FILE,
    FILE_FIELDS,
    FILE_MAPPING,
    FILE_SELECTOR_ROOTS,
    FOLDER_MIME,
    GOOGLE_DRIVE,
    INCLUDE_ALL_DRIVES,
    LABEL_FIELD,
    MAX_PAGE_SIZE,
    NEXT_PAGE_FIELD,
    NOT_CARRIED_FROM_DRIVE,
    RETRY_AFTER_WHEN_UNSTATED,
    SHARING_SELECTOR,
    SHARING_TEXT,
    SHORTCUT_MIME,
    SUPPORTS_ALL_DRIVES,
    Ancestry,
    DriveConnection,
    DriveError,
    DriveFile,
    DriveNotFoundError,
    DriveRefusedError,
    DriveUnreachableError,
    Endpoint,
    InScopeFile,
    ListingRequest,
    PermissionFact,
    PermissionKind,
    Placement,
    Reply,
    SharingState,
    admit_from_drive,
    assert_answered,
    assert_indexable,
    assert_selector_covers_the_mapping,
    call_outcome,
    classify_sharing,
    error_reason,
    field_selector,
    file_from_row,
    file_projection,
    first_page,
    folder_fetch,
    folder_query,
    list_folder,
    manifest,
    media_type_for,
    operation_for,
    place,
    placement,
    projected_field_names,
    projected_fields,
    read_page,
    retry_hint,
    shape_for,
    shortcut_target,
    subscription,
)
from brain.connectors.manifest import (
    ChangeSignal,
    ManifestError,
    ProjectedEntity,
    failed_clauses,
)
from brain.connectors.manifest import projectability as clauses_for
from brain.connectors.projection import ProjectedRecord
from brain.connectors.rest import RestSpecError
from brain.connectors.throttle import CallOutcome, UnmeasuredSourceError, is_retryable, limits_for
from brain.core.envelope import IdentityMode, SideEffect
from brain.core.errors import Degraded
from brain.core.projection import (
    MAX_LABEL_CHARS,
    MAX_PROJECTED_FIELDS,
    check_projection,
    is_forbidden,
)
from brain.core.scope import Clause, Op, Scope
from brain.gate.provenance import Freshness
from brain.knowledge.ingest import IngestRefused, MediaType, ScanVerdict
from brain.knowledge.scanning import ScanReport
from brain.knowledge.visibility import Visibility
from brain.ops.limits import MAX_BACKOFF_SECONDS
from brain.ops.secrets import SecretRef, VaultRole
from tests.fixtures.cassettes import CASSETTES, Cassette, Source

FOLDER = "fld0447AbC-_x"
OUTSIDE_FOLDER = "fld0447AbC-_xy"
SUBFOLDER = "sub0447Zz"
SHARED_DRIVE = "drv0447Qq"
DOMAIN = "verz.com"
DEPARTMENT = "web"
STEWARD = "p_rupash"
FETCHED_AT = "2026-09-06T09:00:00+00:00"
NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)

READ_REF = SecretRef(path="connectors/google_drive/service_account", role=VaultRole.APPLICATION)

#: A small PDF. The bytes matter: `brain.knowledge.ingest.sniff` reads the signature, so a
#: body that merely claims to be a PDF is refused by the door and that is a test below.
A_PDF = b"%PDF-1.7\n" + b"x" * 200

#: A zip, which is what a `.docx` is and what a renamed PDF is not.
A_ZIP = b"PK\x03\x04" + b"y" * 200


def a_connection(**overrides: Any) -> DriveConnection:
    settings: dict[str, Any] = {
        "folder_id": FOLDER,
        "domain": DOMAIN,
        "department": DEPARTMENT,
        "steward_id": STEWARD,
    }
    settings.update(overrides)
    return DriveConnection(**settings)


def a_drive_row(number: int, **overrides: Any) -> dict[str, Any]:
    """One file as Drive's own API returns it, canaries included.

    `owners` and `permissions` carry canaries because they are the two fields a Drive
    response holds that must never survive into a record: both are lists of principals in
    Google's directory, and one of them carries an email address on every entry.
    """
    row: dict[str, Any] = {
        "id": f"fileA{number}Bc-_",
        "name": f"SNM proposal {number}.pdf",
        "mimeType": "application/pdf",
        "modifiedTime": "2026-09-05T10:00:00+00:00",
        "headRevisionId": f"rev{number}",
        "trashed": False,
        "parents": [FOLDER],
        "driveId": "",
        "shortcutDetails": {"targetId": ""},
        "owners": [{"emailAddress": "CANARY-DRIVE-OWNER@verz.com", "displayName": "Wei Ling"}],
        "permissions": [
            {"type": "user", "domain": DOMAIN, "emailAddress": "CANARY-GRANTEE@verz.com"}
        ],
        "webViewLink": "https://drive.google.com/file/d/CANARY-LINK/view",
        "md5Checksum": "CANARY-DIGEST",
    }
    row.update(overrides)
    return row


def a_listing(count: int, *, cursor: str = "", start: int = 0) -> dict[str, Any]:
    body: dict[str, Any] = {"files": [a_drive_row(n) for n in range(start, start + count)]}
    if cursor:
        body[NEXT_PAGE_FIELD] = cursor
    return body


def a_file(**overrides: Any) -> DriveFile:
    settings: dict[str, Any] = {
        "file_id": "fileA1Bc-_",
        "name": "SNM proposal 1.pdf",
        "mime_type": "application/pdf",
        "sharing": SharingState.RESTRICTED,
        "parents": (FOLDER,),
        "modified_at": "2026-09-05T10:00:00+00:00",
        "revision_id": "rev1",
    }
    settings.update(overrides)
    return DriveFile(**settings)


def a_manifest(**overrides: Any) -> Any:
    connection = overrides.pop("connection", a_connection())
    return manifest(connection, ref=READ_REF, **overrides)


def listing_operation() -> Any:
    return operation_for(Endpoint.LIST_FILES)


@dataclass
class Reader:
    """A page reader scripted one reply per call, recording what it was asked for.

    A fake rather than a mock: the assertions that matter are about which requests were
    built, and a reader that records is the only way to see the parameters that were left
    out. Strict about an unscripted call, deliberately, because a reader that answered a
    third request with the second reply would turn a walk that failed to stop into a test
    that hangs, and a test that hangs is a test nobody keeps.
    """

    replies: list[Reply]
    seen: list[ListingRequest]

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.seen = []

    def read(self, request: ListingRequest) -> Reply:
        self.seen.append(request)
        index = len(self.seen) - 1
        if index >= len(self.replies):
            raise AssertionError(
                f"call {index + 1} was made and this reader holds {len(self.replies)} "
                "replies; the walk did not stop where it should have"
            )
        return self.replies[index]


@dataclass
class FakeScanner:
    """A scanner with a verdict chosen by the test, recording the bytes it was given.

    A protocol implementation rather than a real scanner, for the reason
    `brain.knowledge.scanning.Scanner` gives about its own: the two cases that decide whether
    the ordering is right are an infected verdict and a scanner that reaches no conclusion,
    and neither is reachable against a real one.
    """

    verdict: ScanVerdict = ScanVerdict.CLEAN
    seen: list[bytes] = field(default_factory=list)

    def scan(self, content: bytes) -> ScanReport:
        self.seen.append(content)
        return ScanReport(verdict=self.verdict, scanner="scanner-stub")


def module_dataclasses() -> list[type]:
    from brain.connectors import google_drive

    return [
        member
        for _, member in inspect.getmembers(google_drive, inspect.isclass)
        if member.__module__ == google_drive.__name__ and is_dataclass(member)
    ]


# ------------------------------------------------------------------ scope at connect
def test_the_scope_at_connect_names_one_folder_and_not_the_whole_drive() -> None:
    """A Drive credential reaches whatever the account reaches, which for a company is
    usually everything it has ever written. Delete this and a connector installed against a
    folder is indistinguishable from one installed against the account, and narrowing it
    afterwards un-fetches nothing that was already read."""
    scope = a_connection().scope()

    assert scope.resource_kind == "folder"
    assert scope.admits(FOLDER)
    assert not scope.admits(OUTSIDE_FOLDER)


def test_a_scope_that_narrows_nothing_is_refused() -> None:
    """The refusal every test above depends on. Delete it and `folder: *` installs cleanly,
    which is a connector connected to the account wearing a folder's name."""
    with pytest.raises(ConnectorContractError):
        a_connection(folder_id="*")


def test_a_folder_id_that_is_not_a_drive_identifier_is_refused_at_connect() -> None:
    """Two different refusals, and this pins which one does what, because the first draft of
    the module claimed the wrong one and the test caught it.

    A value carrying a quote never reaches this module's grammar at all: `ConnectorScope`
    admits neither a quote nor a space, so it is refused at connect by the platform rule.
    What this check adds is the narrower question, which is whether a value that *passes*
    that grammar is a Drive identifier: a path or an address does, and asking Drive to list a
    folder that cannot exist is answered with an empty array rather than an error, so it
    arrives as an empty folder and reads as one."""
    with pytest.raises(ConnectorContractError, match="not an identifier"):
        a_connection(folder_id="fld' or '1'='1")

    with pytest.raises(DriveError, match="not a Drive identifier"):
        a_connection(folder_id="shared/web/proposals")


def test_a_file_inside_the_pinned_folder_is_placed() -> None:
    """The positive case, and it is not decoration: a check that only ever refuses is
    satisfied by a `place` that refuses everything, and nothing would then be indexed while
    every refusal test still passed."""
    placed = place(a_connection(), a_file())

    assert isinstance(placed, InScopeFile)
    assert placed.folder_id == FOLDER
    assert placed.file.file_id == "fileA1Bc-_"


def test_a_file_outside_the_pinned_folder_is_refused_before_anything_reads_it() -> None:
    """The whole reason the pin exists. Without this a file id from anywhere in the account
    is fetched on the strength of the connector having been connected at all, and the folder
    in the console describes a restriction that was never applied."""
    outside = a_file(parents=(OUTSIDE_FOLDER,))

    with pytest.raises(DriveError, match="outside the folder"):
        place(a_connection(), outside, Ancestry(reaches_a_root=True))


def test_a_file_whose_folder_is_unknown_is_neither_admitted_nor_excluded() -> None:
    """The third answer, and the one both defaults get wrong silently. Admitting it reads
    every file the credential reaches; excluding it stops indexing a folder tree and nothing
    reports a document that is merely never found. A listing that forgot to select `parents`
    produces exactly this row for every file in the folder."""
    unplaceable = a_file(parents=())

    assert placement(a_connection(), unplaceable, Ancestry()) is Placement.UNDETERMINED

    with pytest.raises(DriveError, match="could not be placed"):
        place(a_connection(), unplaceable)


def test_only_a_completed_ancestry_walk_may_call_a_file_outside_the_folder() -> None:
    """ "The pin is not this file's parent" is true of every file two folders down, so
    `OUTSIDE` is a claim that needs the walk to have finished. Delete this and a half-walked
    chain reports a file in a subfolder as outside the connector's scope, which drops
    documents quietly and looks like a correctly working guard."""
    nested = a_file(parents=(SUBFOLDER,))
    half_walked = Ancestry(folder_ids=(SUBFOLDER,), reaches_a_root=False)
    complete = Ancestry(folder_ids=(SUBFOLDER, FOLDER), reaches_a_root=True)

    assert placement(a_connection(), nested, half_walked) is Placement.UNDETERMINED
    assert placement(a_connection(), nested, complete) is Placement.INSIDE
    assert place(a_connection(), nested, complete).folder_id == FOLDER


def test_an_in_scope_file_cannot_be_built_without_the_scope_check() -> None:
    """The seal, borrowed from `brain.knowledge.scanning.ScannedContent` with its argument
    intact. Delete it and the knowledge path's parameter type stops being a guarantee: any
    caller can assert that a file was placed by constructing the value that says so."""
    with pytest.raises(DriveError, match="issued by"):
        InScopeFile(issued_by=object(), file=a_file(), folder_id=FOLDER)


def test_the_knowledge_path_takes_only_a_file_that_was_placed() -> None:
    """The half of the seal that makes it worth having. `admit_from_drive` declares
    `InScopeFile`, so handing it a `DriveFile` is a type error rather than a runtime check
    somebody can be talked out of; asserting on the signature is what keeps that true when
    the body is rewritten."""
    signature = inspect.signature(admit_from_drive)

    assert signature.parameters["placed"].annotation == "InScopeFile"
    assert "DriveFile" not in str(signature)


def test_a_listing_row_outside_the_pinned_folder_refuses_the_whole_page() -> None:
    """A folder query cannot return a file outside the folder, so a row that is outside it
    means the query was not the one this code believes it sent. Dropping the row would hide
    that and keep returning the rest, which is a connector under-reporting for a reason
    nobody goes looking for."""
    body = a_listing(1)
    body["files"][0]["parents"] = [OUTSIDE_FOLDER]

    with pytest.raises(DriveError, match="pinned to"):
        list_folder(
            listing_operation(),
            Reader(Reply(status=200, body=body)),
            a_connection(),
            fetched_at=FETCHED_AT,
        )


def test_a_listing_row_with_no_parents_is_refused_rather_than_treated_as_inside() -> None:
    """The shape a listing produces when the field selector forgot `parents`. Treating it as
    inside the folder places every file in the account, and there is nothing in the response
    that distinguishes it from a correct listing."""
    body = a_listing(1)
    del body["files"][0]["parents"]

    with pytest.raises(DriveError, match="pinned to"):
        list_folder(
            listing_operation(),
            Reader(Reply(status=200, body=body)),
            a_connection(),
            fetched_at=FETCHED_AT,
        )


# ---------------------------------------------------------------------------- shortcuts
def test_a_shortcut_is_refused_as_content_because_its_target_may_be_anywhere() -> None:
    """The obvious way out of a pinned folder, and the one that looks entirely ordinary in a
    listing: a shortcut has a name, a parent and a modified time like anything else. Delete
    this and the target's bytes are read on the strength of a check that passed for a
    different file."""
    shortcut = a_file(mime_type=SHORTCUT_MIME, shortcut_target_id="targetOutside1")

    with pytest.raises(DriveError, match="shortcut"):
        place(a_connection(), shortcut)


def test_a_shortcut_target_is_placed_on_its_own_ancestry_and_not_the_shortcuts() -> None:
    """The positive and negative halves of the same rule. Resolving the target is allowed;
    inheriting the shortcut's placement is not, which is why the target arrives with no
    placement at all and has to be placed like any other file."""
    shortcut = a_file(mime_type=SHORTCUT_MIME, shortcut_target_id="targetOutside1")

    assert shortcut_target(shortcut) == "targetOutside1"

    target = a_file(file_id="targetOutside1", parents=(OUTSIDE_FOLDER,))
    with pytest.raises(DriveError, match="outside the folder"):
        place(a_connection(), target, Ancestry(reaches_a_root=True))

    inside = a_file(file_id="targetInside1", parents=(FOLDER,))
    assert place(a_connection(), inside).file.file_id == "targetInside1"


def test_a_shortcut_with_no_readable_target_is_refused_rather_than_read_as_itself() -> None:
    """A dangling shortcut is what `shortcutDetails` looks like when it was not selected.
    Falling back to the shortcut's own id would read a file that is not the one anybody
    asked for, and it would succeed."""
    with pytest.raises(DriveError, match="target"):
        shortcut_target(a_file(mime_type=SHORTCUT_MIME, shortcut_target_id=""))

    with pytest.raises(DriveError, match="not a shortcut"):
        shortcut_target(a_file())


def test_a_folder_is_refused_as_content() -> None:
    """Placing a folder reads as having placed what is inside it, which is a different set of
    files with different sharing that nothing checked. It is also the row a folder listing
    returns for every subfolder, so this is the ordinary case rather than an exotic one."""
    with pytest.raises(DriveError, match="folder"):
        place(a_connection(), a_file(mime_type=FOLDER_MIME))


# ------------------------------------------- a Drive permission is not a permission here
def test_sharing_that_could_not_be_determined_is_refused_rather_than_defaulted() -> None:
    """Drive returns permissions only when they are asked for and only when the credential
    may see them, so an absent array is routine. Both defaults fail quietly: the folder's
    level stores a file whose sharing nobody read, and the narrowest level hides a document
    while nothing anywhere reports one that is merely not found."""
    assert classify_sharing(None, domain=DOMAIN) is SharingState.UNDETERMINED
    assert classify_sharing([], domain=DOMAIN) is SharingState.UNDETERMINED

    with pytest.raises(DriveError, match="undetermined"):
        a_connection().knowledge_visibility(SharingState.UNDETERMINED)


def test_a_link_shared_file_does_not_become_readable_by_the_whole_company() -> None:
    """The failure this connector exists to avoid. "Anyone with the link" is how a
    deliverable is sent to a client, so honouring it as a widening publishes exactly the
    documents that must not be published. The level comes from the pinned folder and the
    sharing state has no way to raise it."""
    connection = a_connection()

    levels = {
        state: connection.knowledge_visibility(state) for state in SharingState if state.is_known
    }

    assert {stored.level for stored in levels.values()} == {Visibility.DEPARTMENT}
    assert len(set(levels.values())) == 1, "the sharing state changed something it may not"
    assert Visibility.COMPANY not in {stored.level for stored in levels.values()}


def test_a_file_nobody_shared_is_not_a_file_nobody_here_may_read() -> None:
    """The other direction, and the one a careful reader gets wrong by being careful. An
    administrator pinned the folder and the folder is the grant, so a file with no sharing
    beyond its owner still lands at the folder's level rather than at the narrowest one."""
    stored = a_connection().knowledge_visibility(SharingState.RESTRICTED)

    assert stored.level is Visibility.DEPARTMENT
    assert stored.department == DEPARTMENT
    assert stored.owner_id == STEWARD


def test_a_folder_with_no_department_stores_a_file_at_the_narrowest_level() -> None:
    """`brain.knowledge.visibility.admit_upload` is what decides, imported rather than
    restated, so a connection with no department gets the personal default that module
    already gives an uploader with none. Delete this and the two answers can drift, with the
    connector being the generous one."""
    stored = a_connection(department="").knowledge_visibility(SharingState.RESTRICTED)

    assert stored.level is Visibility.PERSONAL
    assert stored.scope() == Scope(clauses=(Clause(field="owner_id", op=Op.EQ, value=STEWARD),))


def test_a_grant_outside_the_company_domain_is_classified_external() -> None:
    """The classification a reviewer actually acts on: a file already outside the building
    whatever this system does with it. It changes no level here, which is the point, and it
    has to stay visible or nobody ever learns which documents left."""
    entries = [
        PermissionFact(kind=PermissionKind.USER, domain=DOMAIN),
        PermissionFact(kind=PermissionKind.GROUP, domain="snm-construction.example"),
    ]

    assert classify_sharing(entries, domain=DOMAIN) is SharingState.EXTERNAL


def test_a_link_grant_is_reported_ahead_of_an_external_one() -> None:
    """A file that is both is wider than either, and reporting the wider fact is the safe
    direction: a reviewer told LINK has already been told the more alarming half, while one
    told EXTERNAL about a link-shared file has been told the smaller of two true things."""
    entries = [
        PermissionFact(kind=PermissionKind.GROUP, domain="snm-construction.example"),
        PermissionFact(kind=PermissionKind.ANYONE),
    ]

    assert classify_sharing(entries, domain=DOMAIN) is SharingState.LINK


def test_the_company_domain_is_compared_without_regard_to_case() -> None:
    """Drive returns whatever case the administrator typed. A case-sensitive comparison
    classifies `Verz.com` as external, which marks every file in the company as shared
    outside it and teaches whoever reads the classification to ignore it."""
    entries = [PermissionFact(kind=PermissionKind.DOMAIN, domain="Verz.com")]

    assert classify_sharing(entries, domain="verz.com") is SharingState.RESTRICTED
    assert classify_sharing(entries, domain="VERZ.COM") is SharingState.RESTRICTED


def test_a_domain_that_is_not_a_domain_is_refused_rather_than_compared() -> None:
    """The external test is one comparison, so a value that is not a domain classifies every
    file the same way and the direction depends on how the comparison was written. Both
    outcomes read as a working connector, which is why this is refused at the point the value
    arrives rather than being allowed to decide anything."""
    with pytest.raises(DriveError, match="domain"):
        classify_sharing([PermissionFact(kind=PermissionKind.ANYONE)], domain="verz")

    with pytest.raises(DriveError, match="domain"):
        a_connection(domain="not a domain")


def test_a_permission_fact_has_nowhere_to_carry_who_holds_it() -> None:
    """The reduction is where the ACL stops, so it has to happen at the point the value is
    built rather than being promised further down. A caller turning Drive's permission
    objects into these has to drop the local part of an address to construct one at all,
    which is what makes a hundred colleagues one fact."""
    names = {f.name for f in dataclasses.fields(PermissionFact)}

    assert names == {"kind", "domain"}

    with pytest.raises(DriveError, match="link grant"):
        PermissionFact(kind=PermissionKind.ANYONE, domain=DOMAIN)


def test_no_declaration_in_this_module_can_hold_a_resolved_permission_list() -> None:
    """Checked over every declaration rather than the one somebody remembered. A stored ACL
    goes stale on the next joiner, mover or leaver with nothing reporting it, and Drive's own
    field names are the ones that arrive: `owners`, `sharingUser` and `permissionIds` are all
    in the default response and none of them is matched by the platform's own patterns."""
    declarations = module_dataclasses()

    assert len(declarations) >= 6
    for declared in declarations:
        assert_stores_no_acl_holds(declared)

    for attribute in ("shared_with", "permission_ids", "owners", "members", "user_ids"):
        forged = type("Forged", (), {"__annotations__": {attribute: "tuple[str, ...]"}})
        with pytest.raises(DriveError, match=r"resolved permission list|principal"):
            assert_stores_no_acl_holds(forged)


def assert_stores_no_acl_holds(declared: type) -> None:
    from brain.connectors.google_drive import assert_stores_no_acl

    assert_stores_no_acl(declared)


def test_every_sharing_state_says_what_it_means() -> None:
    """The table is documentation that fails. A member added without wording renders as a
    blank explanation in whatever a reviewer is shown, which is the generic message
    `brain.knowledge.ingest.CAUSE_TEXT` exists to remove."""
    assert set(SHARING_TEXT) == set(SharingState)
    for state, text in SHARING_TEXT.items():
        assert text.strip(), f"{state} is classified with no explanation"


def test_the_visibility_predicate_is_the_folder_and_never_an_enumeration_of_people() -> None:
    """A projection stored with a resolved ACL wearing a predicate's shape does not
    re-evaluate against the live entitlement set, so it is wrong from the next leaver
    onwards. The folder is the half of Drive's model that can be carried as a predicate at
    all, and the manifest refuses the other half rather than trusting this module."""
    connection = a_connection()

    assert connection.visibility_predicate() == Scope(
        clauses=(Clause(field="folder_id", op=Op.EQ, value=FOLDER),)
    )
    assert file_projection(connection).visibility == connection.visibility_predicate()

    with pytest.raises(ManifestError, match="enumerates principals"):
        ProjectedEntity(
            entity=FILE,
            fields=FILE_FIELDS,
            change_signal=ChangeSignal.UPDATED_SINCE,
            visibility=Scope(clauses=(Clause(field="user_id", op=Op.IN, value=("p_a", "p_b")),)),
        )


# ------------------------------------------------- the id is the identity, the path is not
def test_the_projection_keeps_a_file_id_and_never_a_path() -> None:
    """Files are renamed, moved and edited, and only the id survives all three. A stored path
    is wrong the moment somebody drags the file and it is wrong silently: the row still
    reads, still filters and still cites, and it points somewhere the file no longer is."""
    projected = projected_field_names()

    assert "path" not in projected
    assert "parents" not in projected
    assert "id" not in projected
    assert any(mapping.target == "id" for mapping in FILE_MAPPING)


def test_the_projected_folder_is_the_pin_rather_than_the_parent_it_sits_under() -> None:
    """A file two folders down has a parent nobody scoped, so storing the parent names a
    folder that appears in no console and in no predicate. Delete this and the visibility
    predicate stops matching its own rows for every file that is not a direct child."""
    row = listing_operation().project({"files": [a_drive_row(1, parents=[SUBFOLDER])]})[0]

    fields = projected_fields(row, connection=a_connection(), sharing=SharingState.RESTRICTED)

    assert fields["folder_id"] == FOLDER
    assert SUBFOLDER not in str(fields)


def test_a_revision_id_is_projected_beside_the_modified_time() -> None:
    """A timestamp says something about this file changed and a revision says the content
    did, and only one of those is a reason to read the bytes again. With only the timestamp,
    a rename schedules a re-index of every file somebody tidied up."""
    row = listing_operation().project(a_listing(1))[0]

    fields = projected_fields(row, connection=a_connection(), sharing=SharingState.RESTRICTED)

    assert fields["revision_id"] == "rev0"
    assert isinstance(fields["modified_at"], datetime)


def test_an_undatable_modified_time_is_dropped_rather_than_stored_as_a_string() -> None:
    """Drive's format is ordinary, which is the trap: a stored string sorts correctly right
    up until a value arrives without a zone, and then it sorts against every other one
    incorrectly with nothing raising. None means not stated, exactly as
    `brain.gate.provenance.read_time` means it."""
    row = listing_operation().project(
        {"files": [a_drive_row(1, modifiedTime="2026-09-05T10:00:00")]}
    )[0]

    fields = projected_fields(row, connection=a_connection(), sharing=SharingState.RESTRICTED)

    assert "modified_at" not in fields


def test_a_long_file_name_is_cut_to_the_label_limit_rather_than_losing_the_record() -> None:
    """A label over the limit is refused by `check_projection`, so an uncut name would drop
    the whole file at ingest and the projection would be missing precisely the documents with
    the most descriptive names. Deleting this makes that failure silent and selective."""
    long_name = "x" * (MAX_LABEL_CHARS + 40)
    row = listing_operation().project({"files": [a_drive_row(1, name=long_name)]})[0]

    fields = projected_fields(row, connection=a_connection(), sharing=SharingState.RESTRICTED)

    assert check_projection(FILE, {LABEL_FIELD: long_name}) != []
    assert fields[LABEL_FIELD] == "x" * MAX_LABEL_CHARS
    assert check_projection(FILE, dict(fields)) == []


def test_a_field_drive_omitted_contributes_nothing_rather_than_a_null() -> None:
    """Drive omitting an optional field has said something different from Drive sending an
    empty one, and a projection that invented a null would put a value nobody sent in front
    of a reader. `headRevisionId` is absent for exactly the files this connector refuses to
    index, so this is the ordinary case rather than a contrived one."""
    row = listing_operation().project(
        {"files": [{k: v for k, v in a_drive_row(1).items() if k != "headRevisionId"}]}
    )[0]

    fields = projected_fields(row, connection=a_connection(), sharing=SharingState.RESTRICTED)

    assert "revision_id" not in fields


def test_a_row_may_not_be_projected_without_a_sharing_state_somebody_reached() -> None:
    """The refusal carried into the projection, so a file whose sharing was never read cannot
    reach `proj.record` by a different door from the knowledge one. Two paths with one
    refusal between them is one path with a bypass."""
    row = listing_operation().project(a_listing(1))[0]

    with pytest.raises(DriveError, match="undetermined"):
        projected_fields(row, connection=a_connection(), sharing=SharingState.UNDETERMINED)


def test_every_field_this_connector_projects_passes_all_five_clauses() -> None:
    """The positive case, and the one that catches a field added later without an argument.
    Each of the five refuses for a different reason and names a different remedy, so a field
    that passes all five has been thought about five times."""
    labels = sum(1 for f in FILE_FIELDS if f.shape.value == "label")
    for declared in FILE_FIELDS:
        verdicts = clauses_for(
            declared,
            signal=ChangeSignal.UPDATED_SINCE,
            label_count=labels,
            field_count=len(FILE_FIELDS),
        )
        assert failed_clauses(verdicts) == (), f"{declared.name} does not survive review"


def test_the_projection_stays_inside_the_twelve_field_cap_with_room_to_spare() -> None:
    """The cap is per entity kind and is what keeps the projection a pointer rather than a
    mirror. Asserted with headroom on purpose: a connector sitting exactly on the limit means
    the next field anybody needs is an argument about which one to drop."""
    assert len(FILE_FIELDS) < MAX_PROJECTED_FIELDS
    assert check_projection(FILE, dict.fromkeys(projected_field_names(), 1)) == []


def test_a_fetched_file_becomes_a_projected_record() -> None:
    """The end-to-end positive case: what the mapping produces is what the projection
    accepts. Without it the two halves drift, and the symptom is every record being refused
    at ingest with the projection quietly staying empty."""
    row = listing_operation().project(a_listing(1))[0]

    record = ProjectedRecord(
        source=GOOGLE_DRIVE,
        entity=FILE,
        source_id=str(row["id"]),
        last_seen_at=NOW,
        fields=projected_fields(row, connection=a_connection(), sharing=SharingState.RESTRICTED),
    )

    assert record.source_id == "fileA0Bc-_"
    assert set(record.field_names) == set(projected_field_names())


def test_the_canaries_in_a_drive_response_never_survive_the_projection() -> None:
    """A permission canary rather than an ordinary assertion: it does not check that the
    right fields arrive, it checks that fields nobody mapped cannot. `owners`, `permissions`,
    `webViewLink` and `md5Checksum` are all in Drive's ordinary response, two of them carry
    email addresses, and the mapping is an allowlist so none of them arrives."""
    projected = listing_operation().project(a_listing(1))

    assert projected
    assert "CANARY" not in str(projected)
    for absent in ("owners", "permissions", "webViewLink", "md5Checksum"):
        assert absent not in projected[0]


def test_every_field_deliberately_not_carried_is_named_with_its_reason() -> None:
    """The list is documentation that fails. Without it, the next person to want a file's
    owner finds no record of the decision and reads the platform denylist, which does not
    mention one: what refuses `owners` is this module's own rule about principals read out of
    somebody else's directory."""
    assert set(NOT_CARRIED_FROM_DRIVE) >= {"owners", "permissions", "parents", "md5Checksum"}
    for name, reason in NOT_CARRIED_FROM_DRIVE.items():
        assert reason.strip(), f"{name} is refused with no reason given"
    assert not is_forbidden("owners"), "the platform denylist is not what refuses this"


# ------------------------------------------------ shared drives, the bin, and the request
def test_a_shared_drive_listing_without_the_all_drives_parameters_is_refused() -> None:
    """The silent one. Both parameters default to false, so a shared-drive listing made
    without them answers 200 with an empty array: a folder holding four hundred documents
    reported as an empty folder, with nothing in the response that differs from a genuinely
    empty one. After the reply arrives the two cases are identical."""
    shared = a_connection(drive_id=SHARED_DRIVE)

    with pytest.raises(DriveError, match=SUPPORTS_ALL_DRIVES):
        ListingRequest(connection=shared, all_drives=False)


def test_a_shared_drive_request_carries_the_corpus_and_the_drive_id() -> None:
    """The positive half. `corpora` defaults to the user's own drive, so the two flags alone
    are not enough and the omission is silent in the same way. Asserted on the built
    arguments rather than on the request, because the request is where a parameter is easy to
    hold and forget to send."""
    arguments = first_page(a_connection(drive_id=SHARED_DRIVE)).as_arguments()

    assert arguments["corpora"] == "drive"
    assert arguments["driveId"] == SHARED_DRIVE
    assert arguments[SUPPORTS_ALL_DRIVES] == "true"
    assert arguments[INCLUDE_ALL_DRIVES] == "true"


def test_a_my_drive_listing_names_no_shared_drive_and_still_works() -> None:
    """The other positive case. Drive refuses `driveId` without a matching corpus, so
    sending the pair for My Drive would turn every listing into a 400; sending neither is
    what a My Drive connection needs."""
    arguments = first_page(a_connection()).as_arguments()

    assert "driveId" not in arguments
    assert "corpora" not in arguments
    assert arguments[SUPPORTS_ALL_DRIVES] == "true"


def test_the_folder_query_names_the_pin_and_excludes_the_bin() -> None:
    """`files.list` returns files in the bin unless the query excludes them, so a folder
    somebody emptied last month keeps answering questions with documents that are gone.
    Asserted on the expression rather than on a reply, because the reply is identical either
    way and only the request can be wrong."""
    query = folder_query(a_connection())

    assert f"'{FOLDER}' in parents" in query
    assert "trashed = false" in query


def test_a_change_cursor_outside_the_timestamp_grammar_is_refused_not_escaped() -> None:
    """An escape function is a second opinion about somebody else's parser, and the parser
    that decides is Drive's. Refusing at the point the value arrives means no string in this
    module can close the quote that bounds the search expression."""
    connection = a_connection()

    assert "modifiedTime > '2026-09-01T00:00:00Z'" in folder_query(
        connection, modified_after="2026-09-01T00:00:00Z"
    )

    with pytest.raises(DriveError, match="RFC 3339"):
        folder_query(connection, modified_after="yesterday' or trashed = true or '")


def test_a_page_size_the_endpoint_would_refuse_is_refused_before_it_is_sent() -> None:
    """Drive is documented to refuse a larger page rather than to clamp it, which is the
    opposite of Freshdesk: what this buys is a clear failure where the request is built
    instead of a 400 in the middle of somebody's question. A page of zero is refused for the
    ordinary reason, which is that it costs a call and returns nothing."""
    connection = a_connection()

    with pytest.raises(DriveError, match=str(MAX_PAGE_SIZE)):
        ListingRequest(connection=connection, page_size=MAX_PAGE_SIZE + 1, all_drives=True)

    with pytest.raises(DriveError):
        ListingRequest(connection=connection, page_size=0, all_drives=True)

    assert ListingRequest(connection=connection).page_size == DEFAULT_PAGE_SIZE


def test_the_field_selector_asks_drive_for_every_field_the_mapping_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive returns a small default field set, so a mapped field the selector does not name
    simply never arrives. For `parents` that means every file is unplaceable and nothing is
    indexed; for `headRevisionId` the projection quietly loses the one value that says the
    content changed. The selector is declared rather than derived so this relation is a
    check rather than a tautology."""
    from brain.connectors import google_drive

    assert_selector_covers_the_mapping()

    _, _, inner = field_selector(Endpoint.LIST_FILES).partition(",files(")
    tokens = set(inner.rstrip(")").split(","))
    assert {"parents", "headRevisionId", "shortcutDetails"} <= tokens
    assert field_selector(Endpoint.LIST_FILES).startswith(NEXT_PAGE_FIELD)
    assert SHARING_SELECTOR in inner

    monkeypatch.setattr(
        google_drive,
        "FILE_SELECTOR_ROOTS",
        tuple(root for root in FILE_SELECTOR_ROOTS if root != "parents"),
    )
    with pytest.raises(DriveError, match="parents"):
        google_drive.assert_selector_covers_the_mapping()


# ---------------------------------------------- absent, refused, unreachable, and a fourth
def test_a_throttling_403_is_a_quota_refusal_and_not_a_rejection() -> None:
    """Drive signals rate limiting with 403 and a reason code, which `throttle.classify`
    reads as REJECTED because for every other source a 403 means "we will not do that". Read
    as a rejection it is never retried although it would succeed shortly, and
    `is_retryable` is what makes that concrete."""
    throttled = Reply(
        status=403,
        body={"error": {"errors": [{"reason": "userRateLimitExceeded"}], "code": 403}},
    )

    assert call_outcome(throttled) is CallOutcome.QUOTA
    assert is_retryable(CallOutcome.QUOTA)

    with pytest.raises(DriveUnreachableError) as caught:
        assert_answered(throttled)

    assert caught.value.call_outcome is CallOutcome.QUOTA


def test_an_authorisation_403_stays_a_refusal_and_is_never_retried() -> None:
    """The other half, and the reason the narrowing reads a reason code rather than the
    status. An authorisation failure treated as a throttle is retried until the budget is
    gone and still fails, which is exactly what `XERO-401-expired` was recorded to prevent
    for a different source."""
    refused = Reply(
        status=403,
        body={"error": {"errors": [{"reason": "insufficientFilePermissions"}]}},
    )

    assert call_outcome(refused) is CallOutcome.REJECTED
    assert not is_retryable(CallOutcome.REJECTED)

    with pytest.raises(DriveRefusedError):
        assert_answered(refused)


def test_a_429_stays_a_quota_refusal_whatever_the_body_says() -> None:
    """The narrowing runs one way only. A source that has told us to slow down in the status
    line has told us, and a reason code is not a reason to disbelieve it; a two-way rule
    would let a body turn a rate limit into a permanent rejection."""
    reply = Reply(
        status=429,
        headers={"Retry-After": "45"},
        body={"error": {"errors": [{"reason": "insufficientFilePermissions"}]}},
    )

    assert call_outcome(reply) is CallOutcome.QUOTA


def test_a_not_found_is_neither_an_absence_nor_a_refusal() -> None:
    """Drive answers 404 for a file that is gone and for one this credential may not see, and
    does not say which. Reported as an absence it says a document is gone when it has only
    been unshared and the file quietly stops being indexed; reported as a refusal it sends
    somebody to fix a credential for a file that was deleted last week."""
    with pytest.raises(DriveNotFoundError) as caught:
        assert_answered(Reply(status=404, body={"error": {"code": 404}}))

    assert not isinstance(caught.value, DriveRefusedError)
    assert not isinstance(caught.value, DriveUnreachableError)
    assert caught.value.call_outcome is CallOutcome.REJECTED
    assert "does not separate" in caught.value.trace_line()


def test_a_server_failure_is_unreachable_for_the_same_reason_a_quota_refusal_is() -> None:
    """Two very different operational problems with the same answer for the asker: we could
    not reach it. Without this, a 5xx takes the success path, the body is projected, and
    whatever an error page happens to contain becomes records."""
    with pytest.raises(DriveUnreachableError) as caught:
        assert_answered(Reply(status=503, body={"error": {"code": 503}}))

    assert caught.value.call_outcome is CallOutcome.UNAVAILABLE


def test_an_empty_folder_is_an_answer_rather_than_a_failure() -> None:
    """The positive case for every refusal above. A guard that raised on everything would
    pass all of them and make a genuinely empty folder an incident, which is the fastest way
    to have the guard removed."""
    reading = list_folder(
        listing_operation(),
        Reader(Reply(status=200, body={"files": []})),
        a_connection(),
        fetched_at=FETCHED_AT,
    )

    assert reading.result.records == ()
    assert reading.is_all_of_them
    assert not reading.result.truncated


def test_absent_refused_and_unreachable_stay_three_different_answers() -> None:
    """A naive connector collapses all three into an empty list, and the reading a person
    takes from an empty list is "there are none". This asserts the connector keeps them
    apart; `tests/invariants/test_cassettes.py` asserts the recorded corpus does, which is
    the half this source has no recording for."""
    absent = list_folder(
        listing_operation(),
        Reader(Reply(status=200, body={"files": []})),
        a_connection(),
        fetched_at=FETCHED_AT,
    )
    assert absent.result.records == ()

    with pytest.raises(DriveRefusedError):
        assert_answered(Reply(status=401, body={"error": {"code": 401}}))

    with pytest.raises(DriveUnreachableError):
        assert_answered(Reply(status=500, body={"error": {"code": 500}}))


def test_the_sentence_a_person_is_shown_does_not_name_the_system_that_failed() -> None:
    """Naming it says a Drive exists and that we are connected to it, which is a fact
    obtainable by anybody who can type a question. The detail is for the trace, which is read
    by somebody already entitled to know what the system connects to."""
    unreachable = DriveUnreachableError("drive answered 403", call_outcome=CallOutcome.QUOTA)

    assert unreachable.public_message == Degraded.public_message
    assert GOOGLE_DRIVE not in unreachable.public_message
    assert DriveRefusedError().public_message == unreachable.public_message
    assert DriveNotFoundError().public_message == unreachable.public_message
    assert GOOGLE_DRIVE in unreachable.trace_line()


def test_an_unreachable_source_has_no_read_time_to_state() -> None:
    """UNSTATED rather than STALE, and the difference is `brain.gate.provenance`'s: nothing
    was read, so there is no age. A caller able to treat this as merely dated is a caller who
    will substitute a previous answer and describe it as out of date rather than as
    unknown."""
    assert DriveUnreachableError().freshness is Freshness.UNSTATED


def test_the_retry_hint_is_read_from_the_source_and_falls_back_to_the_long_end() -> None:
    """Drive frequently sends no hint at all with a throttling 403, so the fallback is the
    ordinary path rather than the exception. Guessing low spends what is left of an allowance
    nobody has measured for this source, and produces another refusal; a fallback of zero is
    a retry loop against a source that has just asked us to stop.

    **Asserted against `brain.ops.limits.MAX_BACKOFF_SECONDS` rather than against this
    module's own constant, and the difference is the whole test.** The first version compared
    the fallback with `RETRY_AFTER_WHEN_UNSTATED`, which is what the fallback is defined as,
    so it passed for any value including zero: mutation testing set the constant to 0.0 and
    nothing failed. A test satisfied by its own subject is the shape `CLAUDE.md` warns about
    and this is one, found the way that file says to find it. What is pinned now is the
    property: the fallback is the platform's own ceiling and it is longer than any hint a
    source would state, so it can never be the shorter of the two."""
    stated = retry_hint(Reply(status=403, headers={"retry-after": "45"}))
    unstated = retry_hint(Reply(status=403))
    undatable = retry_hint(
        Reply(status=403, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    )
    nonsense = retry_hint(Reply(status=403, headers={"Retry-After": "0"}))

    assert stated == 45.0
    assert unstated == MAX_BACKOFF_SECONDS
    assert undatable == MAX_BACKOFF_SECONDS
    assert nonsense == MAX_BACKOFF_SECONDS
    assert unstated > stated
    assert RETRY_AFTER_WHEN_UNSTATED == MAX_BACKOFF_SECONDS


def test_a_reason_code_is_read_from_either_shape_googles_envelope_takes() -> None:
    """The one place the absence of a recording is visible in the code. The classic Drive
    error carries `error.errors[0].reason` and the newer one carries `error.status`, the
    documentation shows both, and nothing here can say which a deployment meets. Reading
    either is the shape that does not have to change when one turns out to be the truth."""
    assert (
        error_reason(
            Reply(status=403, body={"error": {"errors": [{"reason": "rateLimitExceeded"}]}})
        )
        == "rateLimitExceeded"
    )
    assert (
        error_reason(Reply(status=429, body={"error": {"status": "RESOURCE_EXHAUSTED"}}))
        == "RESOURCE_EXHAUSTED"
    )
    assert error_reason(Reply(status=500, body="<html>Server Error</html>")) == ""


def test_a_failure_is_recognised_before_its_body_is_projected() -> None:
    """A Drive error carries a body of its own. Projecting first turns a rate limit into a
    complaint about the response shape, which sends whoever reads the error to the wrong
    module and hides the fact that the source asked us to wait."""
    throttled = Reply(status=403, body={"error": {"errors": [{"reason": "rateLimitExceeded"}]}})

    with pytest.raises(DriveUnreachableError):
        read_page(listing_operation(), Reader(throttled), first_page(a_connection()))


def test_a_body_read_at_the_wrong_place_is_a_refusal_and_never_an_empty_folder() -> None:
    """The direction of the failure is what matters. A source answering with a shape its own
    specification does not describe has failed, and reporting that as "no files" summarises
    an outage as an absence, which nobody files a bug about."""
    with pytest.raises(RestSpecError):
        listing_operation().project({"items": []})


# ------------------------------------------------------------------- the walk and the fetch
def test_the_walk_follows_the_cursor_the_source_gives_and_stops_when_it_stops() -> None:
    """Drive says plainly whether there is another page, which is the one thing this
    connector has easier than Freshdesk. A walk that guessed from the page length would stop
    early on a folder Drive chose to page differently from the size asked for, which it is
    entitled to do, and would report part of a folder as all of it."""
    reader = Reader(
        Reply(status=200, body=a_listing(2, cursor="page2")),
        Reply(status=200, body=a_listing(1, start=2)),
    )

    reading = list_folder(listing_operation(), reader, a_connection(), fetched_at=FETCHED_AT)

    assert [request.cursor for request in reader.seen] == ["", "page2"]
    assert reading.pages_read == 2
    assert len(reading.result.records) == 3
    assert reading.is_all_of_them
    assert not reading.result.truncated


def test_a_callers_limit_stops_the_walk_and_says_the_answer_is_not_all_of_them() -> None:
    """A limit is a request rather than a guarantee, and stopping early is a different claim
    from the folder running out: the caller knows they asked for two, and only Drive knows
    whether there was more. The cursor is carried out so they can resume rather than start
    again."""
    reader = Reader(Reply(status=200, body=a_listing(3, cursor="page2")))

    reading = list_folder(
        listing_operation(), reader, a_connection(), fetched_at=FETCHED_AT, limit=2
    )

    assert len(reading.result.records) == 2
    assert reading.stopped_at_caller_limit
    assert reading.next_cursor == "page2"
    assert reading.result.truncated
    assert not reading.is_all_of_them


def test_a_negative_limit_is_refused_rather_than_quietly_trimming_the_answer() -> None:
    """The failure it prevents is silent, which is why it is worth a test of its own: a
    negative limit is truthy, so the walk would stop after one page and the trim would remove
    records from the end of it. The caller gets a short answer to a question they asked
    wrongly and nothing says either thing happened."""
    with pytest.raises(ValueError, match="limit"):
        list_folder(
            listing_operation(),
            Reader(Reply(status=200, body=a_listing(1))),
            a_connection(),
            fetched_at=FETCHED_AT,
            limit=-1,
        )


def test_the_fetch_satisfies_the_contract_it_is_built_under() -> None:
    """The positive case for the contract's own guard. A check that only ever refuses is
    satisfied by a module with no fetch in it, and the closure this returns is the object a
    registry would actually call."""
    fetch = folder_fetch(
        listing_operation(),
        Reader(Reply(status=200, body=a_listing(1))),
        a_connection(),
        fetched_at=FETCHED_AT,
    )

    assert_fetches_only(fetch)
    result = fetch(FetchRequest(entity=FILE))
    assert result.source == GOOGLE_DRIVE
    assert len(result.records) == 1


def test_the_fetch_is_refused_an_entity_this_connector_does_not_read() -> None:
    """A fetch answering for the wrong entity returns records tagged as something they are
    not, and the redactor then looks up the wrong field policy for every one of them, which
    is a permission decision made against the wrong table."""
    fetch = folder_fetch(
        listing_operation(),
        Reader(Reply(status=200, body=a_listing(1))),
        a_connection(),
        fetched_at=FETCHED_AT,
    )

    with pytest.raises(DriveError):
        fetch(FetchRequest(entity="ticket"))


def test_the_fetch_refuses_a_filter_because_the_folder_query_is_the_scope() -> None:
    """The folder query is the pin expressed as a search, so a term added to it either widens
    the pin or narrows it somewhere no console shows. Ignoring the filter instead would
    answer a question nobody asked while looking like it had been applied."""
    fetch = folder_fetch(
        listing_operation(),
        Reader(Reply(status=200, body=a_listing(1))),
        a_connection(),
        fetched_at=FETCHED_AT,
    )

    with pytest.raises(DriveError, match="scope"):
        fetch(FetchRequest(entity=FILE, filters=(("q", "'other' in parents"),)))


# ------------------------------------------------------------------- the knowledge plane
def test_no_tool_returns_the_bytes_of_a_file() -> None:
    """The one structural thing this module does about untrusted document content: a model
    cannot ask for a document and be handed one, because there is no tool that returns one.
    Asserted over the declared endpoints as well as the tools, since an endpoint reaching
    `alt=media` or `files.export` is how the third tool would arrive."""
    declared = a_manifest()

    assert declared.tool_names() == ("google_drive.list_folder", "google_drive.read_file")
    for tool in declared.tools:
        assert tool.side_effect is SideEffect.NONE
        assert "Metadata only" in tool.description

    for endpoint in Endpoint:
        spec = shape_for(endpoint).spec
        assert "export" not in spec.path
        assert "alt" not in {parameter.name for parameter in spec.parameters}


def test_a_google_native_document_is_refused_rather_than_exported_by_default() -> None:
    """The gap, stated as a refusal rather than hidden behind a default. A Doc has no bytes,
    exporting it is a choice of format that decides what the document looks like to a parser
    and to a model, and choosing silently here would make that decision invisible. This
    connector indexes no native documents, and that is a fact somebody has to know."""
    with pytest.raises(DriveError, match="exported"):
        media_type_for("application/vnd.google-apps.document")

    with pytest.raises(DriveError, match="exported"):
        media_type_for("application/vnd.google-apps.spreadsheet")


def test_a_folder_and_a_shortcut_have_no_bytes_to_index() -> None:
    """Reaching the knowledge path with either means something upstream is treating structure
    as content, and for the shortcut it is the escape route arriving by a second door."""
    with pytest.raises(DriveError, match="folder"):
        media_type_for(FOLDER_MIME)

    with pytest.raises(DriveError, match="shortcut"):
        media_type_for(SHORTCUT_MIME)


def test_an_unrecognised_type_is_refused_rather_than_attempted() -> None:
    """`brain.knowledge.ingest`'s own rule, met rather than restated: there is no deny list
    anywhere in this platform, so safety comes from what is named. Deleting this lets an
    executable in a Drive folder reach a parser on the strength of Drive having a name for
    its type."""
    with pytest.raises(DriveError, match="not a type the knowledge layer accepts"):
        media_type_for("application/x-msdownload")

    assert media_type_for("application/pdf") is MediaType.PDF


def test_a_trashed_file_is_not_indexed() -> None:
    """Drive's bin is not deletion: a trashed file still exists and still answers a by-id
    read, which is exactly why indexing it would put a document somebody deleted back in
    front of a reader. It is projected as a state rather than treated as an absence, so a
    reconciliation pass can see it went away."""
    with pytest.raises(DriveError, match="bin"):
        assert_indexable(a_file(trashed=True))

    assert_indexable(a_file())


def test_a_files_bytes_reach_a_parser_only_after_a_scanner_has_cleared_them() -> None:
    """The ordering, made a type rather than an order somebody remembers. The value returned
    is the only shape `brain.knowledge.scanning.Parser.parse` accepts, and it is issued by
    the scan gate, so a Drive file that skipped the scanner cannot be handed to a parser at
    all."""
    scanner = FakeScanner()

    cleared = admit_from_drive(place(a_connection(), a_file()), A_PDF, scanner=scanner)

    assert scanner.seen == [A_PDF]
    assert cleared.body == A_PDF
    assert cleared.upload.media_type is MediaType.PDF
    assert cleared.scan.verdict is ScanVerdict.CLEAN


def test_an_unscannable_file_never_becomes_parseable() -> None:
    """ "The scanner could not read it" is not "clean", and treating it as clean means every
    file crafted to defeat a scanner is also a file that skips it. The refusal is
    `brain.knowledge.ingest.assert_clean`'s and is reached rather than reimplemented, which
    is what stops this module having a second, gentler opinion."""
    scanner = FakeScanner(verdict=ScanVerdict.UNSCANNABLE)

    with pytest.raises(IngestRefused, match="unscanned is not clean"):
        admit_from_drive(place(a_connection(), a_file()), A_PDF, scanner=scanner)


def test_drives_declared_type_is_not_believed_and_the_bytes_decide() -> None:
    """A Drive `mimeType` is frequently whatever the uploading client claimed, so it chooses
    which ceiling applies and nothing else. Handing a zip to a PDF parser is the cheapest way
    to spend a parse worker's whole memory budget on something that was never a PDF, and the
    door is what refuses it."""
    scanner = FakeScanner()

    with pytest.raises(IngestRefused, match="the extension is not the type"):
        admit_from_drive(place(a_connection(), a_file()), A_ZIP, scanner=scanner)

    assert scanner.seen == [], "the bytes were scanned before the type was settled"


def test_a_row_becomes_a_file_and_a_row_with_no_id_becomes_nothing() -> None:
    """The one conversion between the mapping's output and this module's value, so nobody
    writes a second one that quietly stops carrying `parents`. A row with no id returns None
    for the reason `transports.normalise` drops one: a file that cannot be named cannot be
    fetched again, cited, or matched to itself on the next pass."""
    row = listing_operation().project(a_listing(1))[0]

    built = file_from_row(row, sharing=SharingState.RESTRICTED)

    assert built is not None
    assert built.file_id == "fileA0Bc-_"
    assert built.parents == (FOLDER,)
    assert built.sharing is SharingState.RESTRICTED
    assert file_from_row({"name": "no id here"}, sharing=SharingState.RESTRICTED) is None


# ------------------------------------------------- the manifest, the subscription, the gaps
def test_the_connector_is_read_only_and_names_nobody_who_granted_write() -> None:
    """Read-only is the default value of the field rather than a convention, so a connector
    installed by somebody in a hurry cannot write to a client's Drive. A connector that could
    is a different connector, approved by somebody named."""
    declared = a_manifest()

    assert declared.credential.mode is AccessMode.READ_ONLY
    assert declared.credential.write_granted_by == ""


def test_every_tool_declares_the_identity_it_actually_runs_under() -> None:
    """A shared service account means Drive enforces none of our people's permissions for us,
    so ours are the only ones there are. Declaring DELEGATED would claim a second independent
    check that does not exist, and `brain.tools.registry` would stop insisting on the scope
    predicate that is the only narrowing there is."""
    for tool in a_manifest().tools:
        assert tool.identity_mode is IdentityMode.SERVICE


def test_the_connector_declares_no_ceiling_because_nobody_has_measured_one() -> None:
    """The honest declaration and a real gap in one assertion. `brain.ops.limits` records no
    figure for this source and there is no recording to derive one from, so `limits_for`
    refuses rather than inventing a number that would look verified. Nothing paces this
    connector today, and naming a ceiling here would hide that rather than fix it."""
    declared = a_manifest()

    assert declared.ceiling == ""
    with pytest.raises(UnmeasuredSourceError, match="declares no ceiling"):
        limits_for(declared, principal_id="p_rupash")


def test_the_subscription_declares_an_id_sweep_because_absence_is_the_only_signal() -> None:
    """A file leaves this connector's reach by being deleted, moved out of the folder,
    unshared, or by the credential losing the shared drive, and only the first is a deletion
    anywhere. All four look identical from inside the folder, so absence is the only sound
    test and a cursor cannot see any of them."""
    subscribed = subscription(
        notify_within=timedelta(minutes=30), reconcile_every=timedelta(hours=12)
    )

    assert subscribed.kind is ChangeSignal.UPDATED_SINCE
    assert subscribed.deletion_check is DeletionCheck.ID_SWEEP
    assert subscribed.needs_an_absence_check
    assert not subscribed.sees_deletions_by_itself
    assert subscribed.promise().interval == timedelta(hours=12)


def test_nothing_this_module_declares_holds_a_credential() -> None:
    """Checked over every declaration rather than the one somebody remembered, so a field
    named `service_account_key` added to any of them is refused. A connector holding a
    credential has a value no rotation can invalidate and no revocation can reach."""
    declarations = module_dataclasses()

    assert len(declarations) >= 6
    for declared in declarations:
        assert_holds_no_credential(declared)


def test_every_endpoint_has_a_shape_and_a_mapping() -> None:
    """The table is total on purpose. A `dict.get` with a default would let a third endpoint
    be classified as whatever the default said, and the default for "where are the records"
    is the answer that reads an empty array."""
    for endpoint in Endpoint:
        shape = shape_for(endpoint)
        assert shape.endpoint is endpoint
        assert operation_for(endpoint).transport.entity == FILE

    assert shape_for(Endpoint.LIST_FILES).spec.records_at == "files"
    assert shape_for(Endpoint.GET_FILE).spec.records_at == ""
    assert shape_for(Endpoint.LIST_FILES).paged
    assert not shape_for(Endpoint.GET_FILE).paged


def test_the_page_cursor_reaches_the_address_and_the_first_page_carries_none() -> None:
    """Asserted on the built address rather than on the request, because the request is where
    a cursor is easy to hold and forget to send: a walk that asks for page two and fetches
    page one loops on the first page for ever and reports a fraction of the folder."""
    operation = operation_for(Endpoint.LIST_FILES)
    connection = a_connection()

    first = operation.url_for(first_page(connection).as_arguments())
    resumed = operation.url_for(
        ListingRequest(connection=connection, cursor="page2", all_drives=True).as_arguments()
    )

    assert "pageToken" not in first
    assert "pageToken=page2" in resumed


def test_no_recording_exists_for_this_source_and_the_reply_shape_is_ready_for_one() -> None:
    """**The honest statement about what this connector was built from.**

    `tests/fixtures/cassettes.py` declares no `Source.GOOGLE_DRIVE` and this leaf may not add
    one: the fixture is shared and `tests/invariants/test_cassettes.py` asserts over it. So
    every Drive fact in the connector comes from Google's documentation, and the four that a
    recording would settle rather than merely support are the throttling reason codes, which
    shape the error envelope takes, whether a shared-drive listing returns permissions to a
    non-manager, and whether a 404 on a shortcut target looks like any other 404.

    What is pinned here is that the absence costs nothing structural. `Reply` carries the
    same three fields a `Cassette` records, so a recording becomes one with no translation
    step that could disagree with it, and this proves it by reading an existing cassette from
    another source straight through this connector's own hint reader.

    Delete this and the next reader has no way to tell that this connector is documented
    rather than recorded, which is the one thing about it they most need to know."""
    assert GOOGLE_DRIVE not in {source.value for source in Source}
    assert not [c for c in CASSETTES if c.source.value == GOOGLE_DRIVE]

    recorded = {f.name for f in dataclasses.fields(Cassette)}
    assert {f.name for f in dataclasses.fields(Reply)} <= recorded

    fresh = next(c for c in CASSETTES if c.cid == "FRESH-429")
    borrowed = Reply(status=fresh.status, headers=fresh.headers, body=fresh.body)

    assert retry_hint(borrowed) == 60.0
    assert call_outcome(borrowed) is CallOutcome.QUOTA
