"""Google Drive: one folder, pinned at connect, and a file id that is the only thing fixed.

**Why Drive rather than Microsoft 365.** The leaf asks for one of the two and one is what
gets built. Drive is the harder of the pair in every way that shapes a connector, so a
module written against it is a module the easier one also fits: its change feed is
drive-wide rather than per-folder, its throttling arrives behind a status the platform reads
as "our request was wrong", its native documents have no bytes to fetch at all, and it has
shortcuts, which are a first-class way out of whatever folder you scoped. None of those
differences touch the *shape* below. What would differ for Microsoft 365 is listed at the
end of this docstring, and it is a table and an error map rather than a redesign.

**There are no recordings for this source, and there is no way to add one here.**
`tests/fixtures/cassettes.py` has no `Source.GOOGLE_DRIVE`, that file is shared, and
`tests/invariants/test_cassettes.py` asserts over it. So **every vendor fact in this module
comes from Google's published documentation and none of it from a recorded exchange**, which
is a weaker footing than Freshdesk and Xero stand on and is said here rather than implied.
The facts a recording would settle and currently does not are named in
`WHAT_A_RECORDING_WOULD_SETTLE`. The shape is arranged so that a recording can be added
later without touching this module: `Reply` carries the same three fields a `Cassette`
records, `error_reason` reads both shapes Google's error envelope takes, and every threshold
is a named constant rather than a literal at a call site.

**Scope at connect is the whole of this connector.** A Drive credential usually reaches
everything the company has ever written, so a connector scoped to "whatever the token
reaches" has that blast radius. `ConnectorScope` pins one folder, and the pin is checked
*before* a call is built rather than after a reply arrives: `place` is the only thing that
issues an `InScopeFile`, and `admit_from_drive` takes nothing else. That is the shape
`brain.knowledge.scanning.ScannedContent` uses for the scan ordering, borrowed deliberately,
because "check the scope first" is a convention and a parameter type is not. See
`SCOPE_AT_CONNECT_IS_THE_WHOLE_POINT`.

**A shortcut is the way out of the folder, so it is refused as content.** A shortcut sits
inside the pinned folder and resolves to a file that may be anywhere in the company's Drive,
which makes it the obvious escape and the one that looks entirely ordinary in a listing.
`place` refuses a shortcut outright; `shortcut_target` hands back the target id, and that
target has to be placed on its own ancestry before anything reads it. See
`A_SHORTCUT_IS_THE_WAY_OUT_OF_THE_FOLDER`.

**A Drive permission is not a permission here, in both directions.** A file shared with
"anyone with the link" is not a file everybody in the company may read: it is very often a
client deliverable, and honouring the link as a widening would publish it. A file nobody
shared is not a file nobody here may read either: an administrator pinned the folder, and
the folder is the grant. So the Drive sharing state **never appears on the left of the
visibility decision**. What decides the level is the folder somebody pinned, through
`brain.knowledge.visibility.admit_upload`, which is the existing gate rather than a second
one; the sharing state travels beside it as a classification so a reviewer can see it. See
`A_DRIVE_PERMISSION_IS_NOT_A_PERMISSION_HERE`.

**The permission list is reduced at the boundary and there is nowhere here to keep one.**
`PermissionFact` carries a kind and a domain: the local part of an address is dropped before
the value is constructed, so `a@verz.com` and `b@verz.com` are the same fact. On top of
that, `assert_stores_no_acl` runs over every declaration in this module using
`brain.connectors.manifest`'s own patterns, imported rather than restated, so an attribute
called `shared_with` or `permission_ids` fails the first time anybody builds one. That is
not the same check as `ProjectedEntity`'s: that one guards projected *field names* and never
sees a dataclass here, and this one guards the dataclasses and never sees a manifest.

**A file whose sharing could not be determined is refused, not defaulted.** Drive returns
`permissions` only when a caller asks for it and only when the credential may see it, so
"the field was not there" is a routine outcome and both defaults are wrong: defaulting to
the folder's level publishes a file whose sharing nobody read, and defaulting to personal
hides a document while nothing anywhere reports it. See
`UNDETERMINED_SHARING_IS_REFUSED_RATHER_THAN_DEFAULTED`.

**A file id is the identity; a path is not.** Files are renamed, moved between folders and
edited, and only the id survives all three. So nothing here stores a path, `folder_id`
records the pin the file was reached through rather than whichever parent it happens to sit
under, and `revision_id` is projected beside `modified_at` because a timestamp says
something changed while the revision says the *content* did. See
`A_FILE_ID_IS_THE_IDENTITY_AND_A_PATH_IS_NOT`.

**Shared drives and My Drive are two sources behind one API, and the difference is silent.**
`supportsAllDrives` and `includeItemsFromAllDrives` default to false and `corpora` defaults
to `user`, so a listing of a shared-drive folder made without them comes back **200 with an
empty array**: a folder full of documents reported as an empty folder, with nothing in the
response saying so. `ListingRequest` refuses to be built for a shared-drive connection
without them. See `THE_SHARED_DRIVE_FLAGS_DEFAULT_TO_SILENCE`.

**Drive throttles behind a 403, which the platform classifier reads as a rejection.**
`brain.connectors.throttle.classify` maps 403 to `REJECTED`, which is right for every source
that uses 403 to mean "we will not do that" and wrong for this one, where a rate limit
arrives as 403 with a reason of `userRateLimitExceeded` or `rateLimitExceeded`. Both
misreadings are expensive and opposite: a throttle read as a rejection is never retried
though it would succeed in a minute, and an authorisation failure read as a throttle is
retried for ever. So `call_outcome` narrows the platform's verdict using the vendor's own
reason code, in one direction only, and `classify` is still what decides first. See
`A_DRIVE_THROTTLE_ARRIVES_AS_A_403`.

**Drive answers 404 for a file that is not there and for one this credential may not see,
and it does not separate them.** That is the source doing to us what this platform
deliberately does to a person, and it means a by-id read has one honest answer for two
facts. `DriveNotFoundError` is a sibling of the refusal and of the outage rather than a
subclass of either, because filing it under one of them would be exactly the collapse this
system exists to avoid. What a person is told is `Degraded`'s sentence either way; the trace
keeps the ambiguity, because the remedies differ and only an operator can act on either. See
`A_NOT_FOUND_DOES_NOT_SEPARATE_ABSENT_FROM_REFUSED`.

**Absent, refused and unreachable stay three answers everywhere they can be told apart.** An
empty folder listing is a value with no records in it; a 401 or a non-throttling 403 is
raised as a refusal; a 429, a throttling 403 and a 5xx are raised as unreachable. There is
no parameter anywhere here that could carry a previous answer, for the reason Freshdesk
gives at length about its own.

**A Drive file is untrusted content that a model will eventually read, and this module does
not claim to have solved that.** What it does is remove the shortest path: **no tool
declared here returns the bytes of a file.** The two tools return metadata, and content
reaches a model only through the knowledge plane, which means `admit_from_drive` composes
`brain.knowledge.uploads.receive_upload` with `brain.knowledge.scanning.scan_for_parsing`,
so a scanner clears the bytes before a parser is handed them and the ordering is a parameter
type rather than a rule somebody remembers. What is **not** solved is the semantic problem
`brain.tools.sop_import` was written for on the skill-import path: a document whose text
addresses the model rather than the reader is still a document, this module does not read
the text and has no findings to raise, and nothing downstream of a parser here flags one.
Saying so is the point. See `NOTHING_HERE_MAKES_A_DOCUMENT_SAFE_TO_READ`.

Rejected, and each looks tidier:

*Subscribing to Drive's `changes` feed.* It is the obvious cursor and it is drive-wide:
there is no per-folder changes feed, its `removed` flag fires for a file that left our reach
as well as for one that was deleted, and consuming it means reading every change in the
client's entire Drive in order to discard almost all of them. A connector pinned to one
folder that reads the whole Drive's change stream is scoped to the whole Drive by the back
door. So the cursor is `modifiedTime` inside the folder query, which is folder-scoped by
construction, and deletions are learned by `DeletionCheck.ID_SWEEP`. That is the same answer
`brain.connectors.change_signal.A_CURSOR_CANNOT_SEE_A_DELETION` reaches for Lark Base, and
here it is stronger: a file *moved out of the pinned folder* is gone from our reach and is
not deleted anywhere, so absence is the only sound test either way.

*Declaring a webhook.* Drive can push, through a watch channel that expires and has to be
renewed against an HTTPS endpoint Google must be able to reach. In a client-hosted
single-tenant deployment that endpoint frequently does not exist, so declaring WEBHOOK would
declare somebody else's network as our guarantee.

*Exporting Google-native documents.* A Doc, a Sheet and a Slide deck have no bytes:
`files.get?alt=media` does not serve them and `files.export` has to be told a format. PDF
loses the structure a parser wants and DOCX is a second parser path, the export endpoint
carries its own size ceiling, and choosing silently would decide what the company's own
procedures look like to a model. So `media_type_for` refuses them by name. **This is a real
gap rather than a design: most of what sits in a company's Drive is native, and this
connector indexes none of it.** The remedy is a reviewed export table, one row per native
type, and it is not written here.

*Projecting nothing at all, on the grounds that Drive is files.* The content is knowledge
and goes to the knowledge layer, which is why there is no `document` projection here. The
*pointer* is a different thing and is what `proj.record` is for: which files are in this
folder, of what type, changed when, at which revision, shared how. Retrieval filters and
counts on those without touching Drive, and the knowledge item does not carry the
source-side facts.

*Escaping the folder id into the search query.* `q` is a string with its own quoting, and an
escape function is a second opinion about somebody else's parser. Nothing that reaches the
expression can close a quote, and the credit for that belongs where it is due:
`ConnectorScope`'s selector grammar admits neither a quote nor a space and runs at connect,
`_TIMESTAMP_RE` does the same for the change cursor, and `_FILE_ID_RE` adds only the narrower
question of whether the value is a Drive identifier at all. That is the shape
`transports.NO_SQL_CROSSES_THIS_SEAM` argues for, reached by three grammars rather than by
one escape function.

**What would differ for Microsoft 365**, stated so the second connector is a fill-in rather
than a rediscovery. Graph throttles with 429 and `Retry-After`, so `call_outcome`'s
narrowing is unnecessary and `classify` alone is right. Graph has a per-item `delta` query,
so the change signal is folder-scoped and carries a deleted facet, which makes
`DeletionCheck.DELETED_FEED` available and retires the id sweep. Office documents in
OneDrive and SharePoint are real files with bytes, so the native-export gap above does not
exist and `media_type_for` would refuse almost nothing. Graph addresses an item by id *or*
by path, which makes scoping by path tempting and no more correct. Its link sharing has one
state Drive lacks, an organisation-wide link, which is a fourth `SharingState` rather than a
different mechanism. And application permissions are tenant-wide (`Files.Read.All`), with
`Sites.Selected` narrowing to a site rather than to a folder, so the connect-time pin is
carrying even more weight there than it is here.

Scope: domain logic. Nothing here opens a socket, resolves a name, reads a clock or holds a
credential. The page reader, the scanner, the fetched-at stamp and every interval are
parameters, for the reason `brain.models.routing.CircuitBreaker` gives about `now`.

Task ids: M11.6.7
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Final, Protocol, final

from brain.connectors.change_signal import ChangeSubscription, DeletionCheck
from brain.connectors.contract import (
    AccessMode,
    ConnectorContractError,
    ConnectorScope,
    CredentialBinding,
    FetchRequest,
    TransportKind,
    assert_fetches_only,
    assert_holds_no_credential,
)
from brain.connectors.manifest import (
    PRINCIPAL_FIELD_RE,
    RESOLVED_ACL_RE,
    ChangeSignal,
    ConnectorManifest,
    FieldShape,
    HotUse,
    ProjectedEntity,
    ProjectedField,
    ToolDeclaration,
)
from brain.connectors.projection import ProjectedValue
from brain.connectors.rest import ID_TARGET, OperationSpec, ParameterSpec, RestOperation
from brain.connectors.throttle import CallOutcome, classify
from brain.connectors.transports import FieldMapping, RestTransport, SourceRecord, normalise
from brain.core.envelope import IdentityMode, SideEffect, TypedResult
from brain.core.errors import Degraded
from brain.core.projection import MAX_LABEL_CHARS
from brain.core.scope import Clause, Op, Scope
from brain.gate.provenance import FRESHNESS_TEXT, Freshness
from brain.knowledge.ingest import MediaType
from brain.knowledge.scanning import ScannedContent, Scanner, scan_for_parsing
from brain.knowledge.uploads import receive_upload
from brain.knowledge.visibility import KnowledgeVisibility, Visibility
from brain.knowledge.visibility import admit_upload as admit_visibility_level
from brain.ops.limits import MAX_BACKOFF_SECONDS
from brain.ops.secrets import SecretRef

# ------------------------------------------------------------------ written-down reasons
#: Why the pin is a parameter type rather than a call anybody remembers to make first.
SCOPE_AT_CONNECT_IS_THE_WHOLE_POINT = (
    "A Drive credential reaches whatever the account reaches, and for a company that is "
    "usually everything it has ever written. So the folder is pinned at connect and a fetch "
    "outside it is refused before the address is built, because after a reply has arrived "
    "the file has already been read and narrowing the scope afterwards un-fetches nothing. "
    "Checked as a type rather than as an order: `place` is the only thing that issues an "
    "InScopeFile and `admit_from_drive` accepts nothing else, so 'read this file nobody "
    "placed' is not an expression that type-checks. Enforced by a rule about which function "
    "to call first, it would hold until the fourth caller."
)

#: Why a shortcut is never content and never inherits the folder it sits in.
A_SHORTCUT_IS_THE_WAY_OUT_OF_THE_FOLDER = (
    "A shortcut is a file inside the pinned folder whose target is a file id anywhere in the "
    "company's Drive, so it is the obvious escape and it is invisible in a listing: it has a "
    "name, a modified time and a parent like anything else. Treating a shortcut as content "
    "means the target's bytes are read on the strength of the shortcut's placement, which is "
    "a check that passed for a different file. So a shortcut is refused as content, the "
    "target id is handed back on its own, and the target is placed against its own ancestry "
    "before anything reads it."
)

#: Why Drive's own sharing never decides who here may read a file.
A_DRIVE_PERMISSION_IS_NOT_A_PERMISSION_HERE = (
    "'Anyone with the link' is how a client deliverable is sent to a client, so treating it "
    "as a widening publishes exactly the documents that must not be published. And a file "
    "nobody shared is not one nobody here may read: an administrator pinned the folder, and "
    "the folder is the grant. Both readings are wrong in the direction that looks like "
    "respecting the source. So the level comes from the pinned folder through the existing "
    "upload gate, which cannot widen, and the sharing state is carried beside it as a "
    "classification a reviewer can filter on rather than as an input to the decision."
)

#: Why sharing nobody could read is a refusal rather than either default.
UNDETERMINED_SHARING_IS_REFUSED_RATHER_THAN_DEFAULTED = (
    "Drive returns a file's permissions only when the caller asks for them and only when the "
    "credential may see them, so 'the field was not there' is an ordinary outcome rather "
    "than an error. Both defaults fail quietly. Assuming the folder's level stores a file "
    "whose sharing nobody read, which is how a document shared outside the company lands in "
    "the knowledge layer looking ordinary. Assuming the narrowest level hides a document "
    "from the people who need it, and nothing anywhere reports a document that is merely "
    "not found. So the file is refused, by name, and somebody widens the field selection or "
    "the credential."
)

#: Why nothing here stores a path and why the revision is projected beside the timestamp.
A_FILE_ID_IS_THE_IDENTITY_AND_A_PATH_IS_NOT = (
    "A file is renamed, moved and edited, and only the id survives all three. A stored path "
    "is wrong the moment somebody drags the file, and it is wrong silently: the row still "
    "reads, still filters and still cites, and it points at a place the file no longer is. "
    "The projected folder is therefore the pin the file was reached through rather than "
    "whichever parent it currently sits under, and the revision id is projected beside the "
    "modified time because a timestamp says something about this file changed while the "
    "revision says the content did, and only one of those is a reason to index it again."
)

#: Why a listing request refuses to be built without the shared-drive parameters.
THE_SHARED_DRIVE_FLAGS_DEFAULT_TO_SILENCE = (
    "supportsAllDrives and includeItemsFromAllDrives default to false and corpora defaults "
    "to the user's own drive, so a listing of a shared-drive folder made without them "
    "answers 200 with an empty array. A folder holding four hundred documents is reported as "
    "an empty folder, there is no error, no header and nothing in the body that differs from "
    "a genuinely empty folder, and the symptom is a knowledge layer that is merely thin. It "
    "is refused where the request is built, because once the reply has arrived the two cases "
    "are identical."
)

#: Why the platform's classification is narrowed here, and only in one direction.
A_DRIVE_THROTTLE_ARRIVES_AS_A_403 = (
    "Drive signals rate limiting with 403 and a reason code in the body, which "
    "throttle.classify reads as REJECTED because for every other source a 403 is 'we will "
    "not do that'. The two misreadings are opposite and both are expensive: a throttle read "
    "as a rejection is never retried although it would succeed shortly, and an authorisation "
    "failure read as a throttle is retried until the budget is gone and still fails. So the "
    "vendor's reason narrows the platform's verdict from REJECTED to QUOTA and never the "
    "other way, which keeps classify the thing that decides and leaves this the thing that "
    "reads one field it owns."
)

#: Why a 404 gets an answer of its own rather than being filed under one of the others.
A_NOT_FOUND_DOES_NOT_SEPARATE_ABSENT_FROM_REFUSED = (
    "Drive answers 404 for a file that does not exist and for one this credential may not "
    "see, which is the same conflation this platform performs deliberately for a person, "
    "arriving from the other side. Reported as absent it says a document is gone when it has "
    "only been unshared, and the file quietly stops being indexed with nothing to trace it "
    "to. Reported as a refusal it sends somebody to fix a credential for a file that was "
    "deleted last week. It is therefore its own answer, a sibling of both, carrying the "
    "ambiguity into the trace where an operator can act on it, and the sentence a person "
    "reads is the same one every unreachable source produces."
)

#: What this module removes and what it does not claim to have removed.
NOTHING_HERE_MAKES_A_DOCUMENT_SAFE_TO_READ = (
    "A document is untrusted input written by whoever had edit rights on a folder, and it is "
    "going to be read by a model. Two things are actually done about it. No tool declared "
    "here returns a file's bytes, so a model cannot ask for a document and be handed one; "
    "and the bytes reach a parser only through admit_from_drive, which composes the "
    "knowledge layer's own door and scan gate, so the type is proved from the bytes and a "
    "scanner clears them first. What is not done is the semantic half that "
    "brain.tools.sop_import performs for skill import: nothing here reads the text, so a "
    "paragraph addressed to the model rather than to the reader is not noticed, not flagged "
    "and not shown to anybody. That is a gap, not a defence, and it belongs to whatever "
    "reviews a document before it answers questions."
)

#: Why this connector runs against no measured ceiling, said rather than implied.
THERE_IS_NO_MEASURED_CEILING_HERE = (
    "brain.ops.limits records verified figures for Xero, Freshdesk and Lark Base and none "
    "for this source, and there is no recording to derive one from either. So the manifest "
    "declares no ceiling and throttle.limits_for refuses rather than inventing a number, "
    "which is the correct refusal and is also a real gap: nothing paces this connector, so "
    "twenty concurrent agent runs are twenty concurrent listings. The remedy is a measured "
    "figure in brain.ops.limits, which the manifest then names; nothing in this module needs "
    "to change when somebody records one."
)

#: The vendor facts here that a recording would settle and documentation cannot.
WHAT_A_RECORDING_WOULD_SETTLE = (
    "Four things are taken from Google's documentation and would be facts if a cassette "
    "existed. Which reason strings actually arrive on a throttling 403, and whether a "
    "deployment ever sees one this module does not list. Whether the error envelope carries "
    "error.errors[0].reason, error.status, or both, which is why error_reason reads either. "
    "Whether a shared-drive listing returns a permissions array to a service account that is "
    "not a manager of the drive, which decides how often UNDETERMINED is the ordinary answer "
    "rather than the exception. And whether a 404 on a shortcut's target is shaped like a "
    "404 on a missing file, which is the one case where the ambiguity above is reached by "
    "two different routes."
)


# ---------------------------------------------------------------------------- the names
#: The connector's name, and the string every projected row, subscription and trace carries.
GOOGLE_DRIVE: Final = "google_drive"

#: The one entity kind. A file's metadata, never its contents.
FILE: Final = "file"

#: This connector's own version. Moves when anything in the manifest moves, because an
#: upgrade is recognised by a version change and a pinned digest disagreeing with a connector
#: nobody upgraded is the failure `brain.connectors.registry` fails closed on.
VERSION: Final = "1.0.0"

#: What the field mapping names its specification. A reference and not a document, for the
#: reason `RestTransport.spec_ref` gives.
SPEC_REF: Final = "google_drive.v3"

BASE_URL: Final = "https://www.googleapis.com"

#: Drive's own types for the things that are not files with bytes. A folder is structure, a
#: shortcut is a pointer, and everything under the native prefix is a document that exists
#: only inside Google and has to be exported to have bytes at all.
FOLDER_MIME: Final = "application/vnd.google-apps.folder"
SHORTCUT_MIME: Final = "application/vnd.google-apps.shortcut"
GOOGLE_NATIVE_PREFIX: Final = "application/vnd.google-apps."

#: What a Drive file id, folder id or shared drive id may look like. Deliberately narrower
#: than `ConnectorScope`'s own selector grammar, which admits a dot, a colon, an at sign and
#: both slashes because it has to describe every source's identifiers.
#:
#: **The quote is closed by `ConnectorScope`, not by this**, and the distinction is worth
#: being accurate about: that grammar admits neither a quote nor a space, so a folder id that
#: could break out of the search expression is refused at connect before this pattern is
#: reached, and it was tested rather than assumed. What this adds is the narrower question of
#: whether a value that passes that grammar is a *Drive* identifier. A path, an address or a
#: URL fragment passes there and is not one here, and sending it produces a listing of a
#: folder that does not exist, which Drive answers with an empty array rather than an error.
_FILE_ID_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,512}$")

#: What a domain may look like. Used to decide whether a permission reaches outside the
#: company, so a value that is not a domain would classify every file as external or none.
_DOMAIN_RE: Final = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9-]{1,63})+$")

#: An RFC 3339 instant, which is what Drive's own query language accepts on `modifiedTime`.
#: Matched and refused rather than escaped, for the reason Freshdesk gives about quotes in a
#: search term: an escape is a second opinion about the source's parser, and the parser that
#: decides is the source's.
_TIMESTAMP_RE: Final = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$"
)

#: Drive's reason codes for "you are asking too often". A throttling 403 and an
#: authorisation 403 are the same status and opposite problems: see
#: `A_DRIVE_THROTTLE_ARRIVES_AS_A_403`. Listed rather than pattern-matched on the word
#: "rate", because `sharingRateLimitExceeded` and `dailyLimitExceeded` are both throttles and
#: only one of them says rate.
DRIVE_THROTTLE_REASONS: Final[frozenset[str]] = frozenset(
    {
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "sharingRateLimitExceeded",
        "dailyLimitExceeded",
        "quotaExceeded",
        "RESOURCE_EXHAUSTED",
    }
)

#: The status Drive answers for a file that is absent and for one this credential may not
#: see. Named because it is the only status in this module with two meanings.
HTTP_NOT_FOUND: Final = 404

#: What an unreachable source's data is worth, in `brain.gate.provenance`'s vocabulary. Not
#: STALE, which is a claim about an age we would have to have measured: nothing was read, so
#: there is no read time and nothing may be rendered as current.
UNREACHABLE_FRESHNESS: Final = Freshness.UNSTATED

#: The wait used when a source refuses on volume and says nothing about when. The platform's
#: own ceiling rather than a number invented here, and deliberately the long end: guessing
#: low spends what is left of an allowance nobody has measured for this source.
RETRY_AFTER_WHEN_UNSTATED: Final = MAX_BACKOFF_SECONDS

#: The largest page `files.list` accepts, and the size a request defaults to. Drive is
#: documented to refuse a larger page rather than to clamp it, which is the opposite of
#: Freshdesk: refusing here therefore buys a clear failure where the request is built instead
#: of a 400 in the middle of somebody's question, and it is not defending against a silent
#: truncation the way `freshdesk.PageRequest` is.
MAX_PAGE_SIZE: Final = 1000
DEFAULT_PAGE_SIZE: Final = 100


class DriveError(ConnectorContractError):
    """A Drive connector was declared, or asked, for something it cannot hold.

    A `ConnectorContractError` for the reason that class gives: every refusal of this kind is
    a mistake by whoever wrote or called the connector, it should stop the connector rather
    than degrade somebody's answer, and nobody asking a question should ever see it. A
    request for a file outside the pinned folder is that kind of mistake and not an outcome:
    there is no answer to give, so there is no reply shape for one.
    """


# ------------------------------------------------- no resolved ACL ever lands in this module
#: Drive's own spellings for the fields that carry principals, none of which
#: `manifest.RESOLVED_ACL_RE` or `manifest.PRINCIPAL_FIELD_RE` happens to match. `owners`,
#: `sharingUser` and `lastModifyingUser` are in the default `files` resource and each carries
#: a display name, an email address and a photo link; `permissionIds` is a list of principals
#: with a singular spelling the platform pattern does not cover.
_DRIVE_PRINCIPAL_RE: Final = re.compile(
    r"(^|_)(permission_id|permission_ids|owners|sharing_user|last_modifying_user"
    r"|email_address|display_name)(_|$)"
)


def assert_stores_no_acl(declaration: type | object) -> None:
    """Refuse a declaration in this module that would keep a resolved permission list.

    Checked over annotations rather than over values, in the same form and for the same
    reason as `contract.assert_holds_no_credential`: it runs on a class before anything has
    been constructed, so it cannot be defeated by a field that happens to be empty at
    inspection time.

    The two platform patterns are **imported** from `brain.connectors.manifest` rather than
    restated, so this cannot come to a more generous conclusion than the rule that governs a
    projected field name. What is added is Drive's own vocabulary, which those patterns do
    not cover: `permissionIds` is singular, and `owners`, `sharingUser` and
    `lastModifyingUser` are ordinary-looking names for objects full of email addresses.

    This is not a second copy of `ProjectedEntity._assert_predicate_is_not_an_acl`. That one
    runs over projected field names and over a visibility predicate, and it never sees a
    dataclass in this module; this one runs over the dataclasses and never sees a manifest.
    Two checks that overlapped would be one check with a spare, and the spare is the one the
    next person deletes.
    """
    target = declaration if isinstance(declaration, type) else type(declaration)
    annotations: dict[str, object] = {}
    for base in reversed(target.__mro__):
        annotations.update(getattr(base, "__annotations__", {}) or {})

    offenders = sorted(
        attribute
        for attribute in annotations
        if RESOLVED_ACL_RE.search(attribute.casefold())
        or PRINCIPAL_FIELD_RE.search(attribute.casefold())
        or _DRIVE_PRINCIPAL_RE.search(attribute.casefold())
    )
    if offenders:
        msg = (
            f"{target.__name__} carries {offenders}, which is a resolved permission list or "
            f"a principal read out of somebody else's directory. "
            f"{A_DRIVE_PERMISSION_IS_NOT_A_PERMISSION_HERE}"
        )
        raise DriveError(msg)


# ---------------------------------------------------- what Drive says about who can see it
class PermissionKind(enum.StrEnum):
    """The kinds of grantee a Drive permission names. Closed, and the closure is the point.

    Drive's own set, and it is small because the classification below only needs to know how
    *wide* a grant is, never who holds it. `ANYONE` is the link grant and is the only member
    that reaches outside every directory there is.
    """

    USER = "user"
    GROUP = "group"
    DOMAIN = "domain"
    ANYONE = "anyone"


@dataclass(frozen=True)
class PermissionFact:
    """One Drive permission, reduced to the two things a classification needs.

    **The absent fields are the design.** There is no id, no address and no display name, so
    the reduction happens where the value is built rather than being promised further down.
    A caller turning Drive's permission objects into these has to drop the local part of an
    address to construct one at all, which means `a@verz.com` and `b@verz.com` are the same
    fact and a hundred colleagues are one. That is the same shape
    `brain.knowledge.scanning.ScanReport` uses by carrying no digest: a component that could
    report the thing it is being checked against can report it wrongly.

    `domain` is empty for an `anyone` grant, which has no domain, and holds the domain part
    for every other kind.
    """

    kind: PermissionKind
    domain: str = ""

    def __post_init__(self) -> None:
        assert_stores_no_acl(type(self))
        if self.kind is PermissionKind.ANYONE:
            if self.domain:
                msg = (
                    f"an {PermissionKind.ANYONE} permission names domain {self.domain!r}; a "
                    "link grant reaches past every domain there is, and recording one would "
                    "read as though it had been confined to that domain"
                )
                raise DriveError(msg)
            return
        if not _DOMAIN_RE.match(self.domain.casefold()):
            msg = (
                f"permission domain {self.domain!r} is not a domain; the whole of the "
                "external test is a comparison against the company's own domain, and a value "
                "that is not one classifies every file the same way whichever way it is wrong"
            )
            raise DriveError(msg)


class SharingState(enum.StrEnum):
    """How widely Drive says one file is shared. A classification, never a list.

    Four, and `UNDETERMINED` is the member that earns this being an enum rather than a pair
    of booleans, exactly as `brain.knowledge.ingest.ScanVerdict.UNSCANNABLE` does: "the
    source did not tell us" is a third answer, and folding it into either of the others is
    the failure. See `UNDETERMINED_SHARING_IS_REFUSED_RATHER_THAN_DEFAULTED`.
    """

    #: Every grant is inside the company's own domain and none of them is a link.
    RESTRICTED = "restricted"
    #: Anyone with the link can open it. Wider than any domain, and not a widening here.
    LINK = "link"
    #: At least one grant names a domain that is not the company's.
    EXTERNAL = "external"
    #: Drive did not say. Refused rather than defaulted.
    UNDETERMINED = "undetermined"

    @property
    def is_known(self) -> bool:
        return self is not SharingState.UNDETERMINED


#: What each state means, for a reviewer reading a row. Total over `SharingState`, and a test
#: asserts it, for the reason `brain.knowledge.ingest.CAUSE_TEXT` gives about its own table: a
#: member added without wording renders as a blank explanation.
SHARING_TEXT: Final[MappingProxyType[SharingState, str]] = MappingProxyType(
    {
        SharingState.RESTRICTED: (
            "shared only inside this company's own domain, which is the ordinary case and "
            "still says nothing about who here may read it"
        ),
        SharingState.LINK: (
            "anyone holding the link can open it in Drive, which is how a deliverable is "
            "sent to a client and is not a reason to widen anything here"
        ),
        SharingState.EXTERNAL: (
            "at least one grant reaches a domain that is not this company's, so the file is "
            "already outside the building whatever this system does with it"
        ),
        SharingState.UNDETERMINED: (
            "Drive returned no permissions for this file, so how it is shared was never "
            "read; the file is refused rather than stored at a level nobody checked"
        ),
    }
)


def classify_sharing(entries: Sequence[PermissionFact] | None, *, domain: str) -> SharingState:
    """Reduce a file's permissions to how wide they are, and to nothing else (M11.6.7).

    `None` is the case that matters and is why the parameter is optional: Drive returns
    `permissions` only when a caller selects it and only when the credential may see it, so
    an absent array is routine rather than exceptional. An empty array takes the same path,
    because no real Drive file has nobody on it: a file always has an owner, so an empty list
    is a response we do not understand and understanding it optimistically is the failure.

    The order of the tests is the rule. A link grant is checked before a foreign domain,
    because a file that is both is wider than either and the wider fact is the safe one to
    report; a caller acting on LINK has already been told the more alarming half.

    The company's own domain is required and is compared case-insensitively, because Drive
    returns whatever case the administrator typed and a case-sensitive comparison would
    classify `Verz.com` as external.
    """
    own = domain.strip().casefold()
    if not _DOMAIN_RE.match(own):
        msg = (
            f"the company domain {domain!r} is not a domain; every external test in this "
            "module is a comparison against it, so a value that is not one either classifies "
            "every file as external or none of them, and both read as a working connector"
        )
        raise DriveError(msg)
    if not entries:
        return SharingState.UNDETERMINED
    if any(entry.kind is PermissionKind.ANYONE for entry in entries):
        return SharingState.LINK
    if any(entry.domain.casefold() != own for entry in entries):
        return SharingState.EXTERNAL
    return SharingState.RESTRICTED


# ------------------------------------------------------------ the connection (M11.2.3)
@dataclass(frozen=True)
class DriveConnection:
    """One folder, one domain, one steward, decided at connect and nothing else.

    No client, no session and no credential: `assert_holds_no_credential` runs on the class
    at construction rather than being promised in a comment, and `assert_stores_no_acl` runs
    beside it, so a later attribute called `api_key` or `shared_with` fails the first time
    anybody builds one.

    The scope is built rather than stored, so `ConnectorScope`'s refusals are this class's
    refusals: a selector of `*`, of `all`, or of anything that narrows nothing is refused at
    connect, in front of whoever is installing the connector.

    **`steward_id` is ours and every principal Drive knows about is not.** A Drive owner is
    an identity in Google's directory, and storing one would create an identifier that looks
    joinable to a principal here and is not, that no revocation of ours reaches, and that
    somebody will eventually compare against a Keycloak subject. So the person answerable for
    what this folder puts into the knowledge layer is chosen at connect by a human being, and
    `assert_stores_no_acl` refuses the attribute name that would let Drive's answer in.
    """

    #: The pinned folder. Everything this connector may read sits at or under it.
    folder_id: str
    #: The company's own domain, which is the whole of the external test.
    domain: str
    #: Which department's knowledge this folder is. The ceiling on the level a file lands at.
    department: str
    #: Who is answerable for what this folder contributes. Ours, never Drive's.
    steward_id: str
    #: The shared drive the folder lives in, or empty for My Drive. Not decoration: it
    #: decides `corpora` and the two all-drives flags, and getting it wrong is silent.
    drive_id: str = ""

    def __post_init__(self) -> None:
        assert_holds_no_credential(type(self))
        assert_stores_no_acl(type(self))
        # Constructing the scope is the check. Repeating ConnectorScope's rules here would be
        # a second opinion about what "narrows nothing" means.
        self.scope()
        if not _FILE_ID_RE.match(self.folder_id):
            msg = (
                f"folder id {self.folder_id!r} is not a Drive identifier; a path, an address "
                "or a URL fragment passes the connector scope's own grammar and is not a "
                "folder Drive can match, and a listing of a folder that does not exist comes "
                "back as an empty array rather than as an error"
            )
            raise DriveError(msg)
        if self.drive_id and not _FILE_ID_RE.match(self.drive_id):
            msg = f"shared drive id {self.drive_id!r} is not a Drive identifier"
            raise DriveError(msg)
        if not _DOMAIN_RE.match(self.domain.casefold()):
            msg = (
                f"connection domain {self.domain!r} is not a domain; it is the only thing "
                "separating a grant inside this company from one outside it"
            )
            raise DriveError(msg)
        if not self.steward_id.strip():
            msg = (
                "this connection names no steward; a folder feeding the knowledge layer with "
                "nobody answerable for it produces items no one is ever asked to re-verify, "
                "and Drive's own owner is not a substitute. "
                f"{A_DRIVE_PERMISSION_IS_NOT_A_PERMISSION_HERE}"
            )
            raise DriveError(msg)

    def scope(self) -> ConnectorScope:
        """What this connector was connected to. One folder, named."""
        return ConnectorScope(resource_kind="folder", selectors=(self.folder_id,))

    @property
    def is_shared_drive(self) -> bool:
        """Whether the pinned folder lives in a shared drive rather than in My Drive."""
        return bool(self.drive_id)

    def admits(self, folder_id: str) -> bool:
        """Whether this connection covers that folder. Exact membership, never a prefix.

        `ConnectorScope.admits` is what decides, imported rather than imitated, and its own
        docstring gives the reason: a prefix match would let a pin of `folder_17` admit
        `folder_170`, which is a different folder belonging to somebody else.
        """
        return self.scope().admits(folder_id)

    def visibility_predicate(self) -> Scope:
        """Drive's own permission model, reduced to a predicate that can be stored.

        The folder, and deliberately nothing else. In Drive the folder is the unit anybody
        actually shares, and the per-file part of the model is a resolved ACL by
        construction, so the folder is the only half of it that can be carried as a predicate
        at all. A row that is not in the pinned folder is visible to nobody, and the
        narrowing by department comes from the caller's own entitlement at the gate rather
        than from the row, which is what lets a mover get a different row set with no writes.
        """
        return Scope(clauses=(Clause(field="folder_id", op=Op.EQ, value=self.folder_id),))

    def knowledge_visibility(self, sharing: SharingState) -> KnowledgeVisibility:
        """The level a file from this folder is stored at (M11.6.7).

        **The sharing state is on the right of this decision and never on the left.** It can
        refuse, and it cannot choose. `brain.knowledge.visibility.admit_upload` is what
        decides the level, which is the existing gate rather than a second one: it returns
        the uploader's default and refuses anything wider, so a link-shared file cannot
        become company-wide by way of a connector, and widening stays a proposal with an
        approver and a review date. See `A_DRIVE_PERMISSION_IS_NOT_A_PERMISSION_HERE`.

        A file whose sharing was never read is refused here rather than stored at either
        default. See `UNDETERMINED_SHARING_IS_REFUSED_RATHER_THAN_DEFAULTED`.
        """
        if not sharing.is_known:
            msg = (
                f"this file's sharing is {sharing} and it will not be stored: "
                f"{SHARING_TEXT[sharing]}. "
                f"{UNDETERMINED_SHARING_IS_REFUSED_RATHER_THAN_DEFAULTED}"
            )
            raise DriveError(msg)
        level = admit_visibility_level(None, uploader_department=self.department)
        if level is Visibility.PERSONAL:
            return KnowledgeVisibility.personal(self.steward_id)
        return KnowledgeVisibility.of_department(self.department, owner_id=self.steward_id)


# ------------------------------------------------------ where a file is, before it is read
class Placement(enum.StrEnum):
    """Whether a file is inside the pinned folder, outside it, or unplaceable.

    Three, and the third is load-bearing. `OUTSIDE` is a claim that needs proof, because
    "the pin is not this file's direct parent" is true of every file two folders down.
    Collapsing `UNDETERMINED` into `OUTSIDE` silently stops indexing a folder tree;
    collapsing it into `INSIDE` reads files nobody scoped. Both are invisible.
    """

    INSIDE = "inside"
    OUTSIDE = "outside"
    UNDETERMINED = "undetermined"


@dataclass(frozen=True)
class Ancestry:
    """The folders above a file, as far as the caller resolved them.

    Drive returns one level of `parents` per file, so anything deeper is a walk the caller
    performs and this module cannot: it opens no connection. `reaches_a_root` is the caller
    saying the walk finished, and it is the only thing that can turn "the pin is not in this
    chain" into `OUTSIDE`. Defaulting it to true would make a half-walked chain say a file is
    outside the folder, which is the answer that quietly drops documents.
    """

    folder_ids: tuple[str, ...] = ()
    reaches_a_root: bool = False


#: Nothing resolved. The default `place` uses, named rather than constructed at the call
#: site: an `Ancestry()` written into a signature is a mutable-looking default a linter
#: refuses, and a caller reading `NO_ANCESTRY` is told what the absence means.
NO_ANCESTRY: Final = Ancestry()


@dataclass(frozen=True)
class DriveFile:
    """One file's metadata, as this connector holds it.

    Deliberately without an owner, a sharing user, a permission list or a path: see
    `assert_stores_no_acl` for the first three and `A_FILE_ID_IS_THE_IDENTITY_AND_A_PATH_IS_NOT`
    for the last. `parents` is here because placing the file is the one thing that needs it,
    and it is not projected for the same reason a path is not.

    `sharing` is a classification the caller reached with `classify_sharing`, carried on the
    value so that nothing downstream has to ask Drive a second time and so that a file whose
    sharing was never read cannot be quietly treated as one whose sharing was.
    """

    file_id: str
    name: str
    mime_type: str
    sharing: SharingState
    parents: tuple[str, ...] = ()
    modified_at: str = ""
    revision_id: str = ""
    trashed: bool = False
    drive_id: str = ""
    shortcut_target_id: str = ""

    def __post_init__(self) -> None:
        assert_stores_no_acl(type(self))
        if not _FILE_ID_RE.match(self.file_id):
            msg = (
                f"file id {self.file_id!r} is not a Drive identifier; the id is this record's "
                "identity and is put into an address, so a value outside the grammar is "
                "either a different endpoint or a record that cannot be fetched again"
            )
            raise DriveError(msg)

    @property
    def is_shortcut(self) -> bool:
        return self.mime_type == SHORTCUT_MIME

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME


def file_from_row(row: Mapping[str, Any], *, sharing: SharingState) -> DriveFile | None:
    """One projected row as the typed value the placement and knowledge paths take.

    The one conversion between the mapping's output and this module's own value, so that no
    caller writes a second one. A second conversion is where a field quietly stops being
    carried, and the field it stops carrying is `parents`, at which point every file becomes
    unplaceable and the connector reports an empty folder.

    `sharing` is supplied rather than read off the row, and that is the whole point of the
    parameter being required: the permission list never reaches a mapped record, so the only
    way a row acquires a sharing state is somebody having classified one, and a caller who
    did not is refused by `classify_sharing`'s own `UNDETERMINED` rather than defaulting.

    Returns None for a row with no usable id, mirroring `transports.normalise`: a file that
    cannot be named cannot be fetched again, cannot be cited, and cannot be matched to itself
    on the next pass.
    """
    raw_id = row.get(ID_TARGET)
    if not isinstance(raw_id, str) or not raw_id.strip():
        return None
    parents = row.get("parents")
    return DriveFile(
        file_id=raw_id,
        name=str(row.get("name", "")),
        mime_type=str(row.get("mime_type", "")),
        sharing=sharing,
        parents=tuple(str(parent) for parent in parents) if isinstance(parents, list) else (),
        modified_at=str(row.get("modified_at", "")),
        revision_id=str(row.get("revision_id", "")),
        trashed=bool(row.get("trashed", False)),
        drive_id=str(row.get("drive_id", "")),
        shortcut_target_id=str(row.get("shortcut_target_id", "")),
    )


#: The token `InScopeFile` demands. Module-private, so building one anywhere else means
#: reaching for a name whose leading underscore says what it is for, and being refused anyway.
_ISSUED_BY_THE_SCOPE_CHECK: Final = object()


@final
@dataclass(frozen=True)
class InScopeFile:
    """A file proved to sit inside the pinned folder, and the only shape content is read from.

    The seal is `brain.knowledge.scanning.ScannedContent`'s, borrowed with its argument
    intact. `@final` closes the four-line bypass, which is a subclass with its own
    `__post_init__` that skips the check and then satisfies every signature because it is
    still an `InScopeFile`; that closure is static rather than dynamic, since the interpreter
    subclasses this happily and what refuses it is mypy running strict over `src` as a
    pre-push hook and again in CI.

    `folder_id` is the pin the file was placed against rather than its parent, which is what
    the projection stores and why: see `A_FILE_ID_IS_THE_IDENTITY_AND_A_PATH_IS_NOT`.
    """

    #: `_ISSUED_BY_THE_SCOPE_CHECK`, and nothing else will do.
    issued_by: object
    file: DriveFile
    folder_id: str

    def __post_init__(self) -> None:
        if self.issued_by is not _ISSUED_BY_THE_SCOPE_CHECK:
            msg = (
                "an in-scope file is issued by `place` and by nothing else. Building one "
                "directly asserts that this file was proved to sit inside the pinned folder, "
                f"which is the one claim here nobody may make on the check's behalf. "
                f"{SCOPE_AT_CONNECT_IS_THE_WHOLE_POINT}"
            )
            raise DriveError(msg)


def placement(connection: DriveConnection, file: DriveFile, ancestry: Ancestry) -> Placement:
    """Whether this file is inside the pinned folder, and the honest answer when nobody knows.

    The chain considered is the file's own parents followed by whatever the caller resolved
    above them. `INSIDE` needs the pin to appear in it. `OUTSIDE` needs the caller to say the
    walk reached a root, because without that the absence of the pin means only that it is
    not one of the folders anybody looked at. Everything else is `UNDETERMINED`, including
    the ordinary case of a file whose `parents` were never selected, which is what a listing
    that forgot the field selector produces for every row.
    """
    chain = (*file.parents, *ancestry.folder_ids)
    if any(connection.admits(folder_id) for folder_id in chain):
        return Placement.INSIDE
    if not chain:
        return Placement.UNDETERMINED
    if ancestry.reaches_a_root:
        return Placement.OUTSIDE
    return Placement.UNDETERMINED


def place(
    connection: DriveConnection, file: DriveFile, ancestry: Ancestry = NO_ANCESTRY
) -> InScopeFile:
    """Prove this file is inside the pinned folder, or refuse it (M11.6.7).

    The only thing that issues an `InScopeFile`, which is what makes the pin a type rather
    than a habit. Four refusals, and each one is a different way the folder stops meaning
    anything.

    **A shortcut is refused as content.** It sits in the folder and points anywhere, so its
    placement is a fact about the shortcut and never about what it resolves to. Call
    `shortcut_target` and place the target on its own ancestry. See
    `A_SHORTCUT_IS_THE_WAY_OUT_OF_THE_FOLDER`.

    **A folder is refused**, because a folder has no bytes and placing one reads as having
    placed what is inside it, which is a different set of files with different sharing.

    **Outside is refused**, which needs no argument.

    **Undetermined is refused, and it is the one that would otherwise be waved through.**
    A file whose parents were never selected is not a file in the folder; it is a file we
    know nothing about, and the two are only distinguishable here.
    """
    if file.is_shortcut:
        msg = (
            f"{file.file_id!r} is a shortcut and cannot be placed as content; resolve it and "
            f"place its target against the target's own ancestry. "
            f"{A_SHORTCUT_IS_THE_WAY_OUT_OF_THE_FOLDER}"
        )
        raise DriveError(msg)
    if file.is_folder:
        msg = (
            f"{file.file_id!r} is a folder; placing one reads as having placed everything "
            "inside it, which is a different set of files that were never checked"
        )
        raise DriveError(msg)
    found = placement(connection, file, ancestry)
    if found is Placement.OUTSIDE:
        msg = (
            f"{file.file_id!r} sits outside the folder this connector is pinned to; a Drive "
            f"credential reaches the whole account and the pin is the only thing narrowing "
            f"it. {SCOPE_AT_CONNECT_IS_THE_WHOLE_POINT}"
        )
        raise DriveError(msg)
    if found is not Placement.INSIDE:
        msg = (
            f"{file.file_id!r} could not be placed: nothing says which folders it is under, "
            "so it is neither inside the pin nor outside it. Select `parents` on the listing "
            "or resolve the chain to a root before reading anything"
        )
        raise DriveError(msg)
    return InScopeFile(
        issued_by=_ISSUED_BY_THE_SCOPE_CHECK, file=file, folder_id=connection.folder_id
    )


def shortcut_target(file: DriveFile) -> str:
    """The file id a shortcut points at, or a refusal.

    Separate from `place` on purpose. Returning the target from the placement call would let
    a caller treat the shortcut's own placement as the target's, which is the whole mistake;
    making it a second call means the target arrives with no placement at all and has to be
    placed like anything else.
    """
    if not file.is_shortcut:
        msg = f"{file.file_id!r} is not a shortcut, so it points at nothing to resolve"
        raise DriveError(msg)
    if not _FILE_ID_RE.match(file.shortcut_target_id):
        msg = (
            f"{file.file_id!r} is a shortcut whose target was not selected or is not a Drive "
            "identifier; a shortcut with no readable target is a dangling pointer, and "
            "guessing that it means the shortcut itself would read the wrong file"
        )
        raise DriveError(msg)
    return file.shortcut_target_id


# ------------------------------------------------------------------------ the endpoints
class Endpoint(enum.StrEnum):
    """The two Drive operations this connector reads. Closed, and small on purpose.

    Both return metadata. There is deliberately no member for `files.get?alt=media` and none
    for `files.export`: content does not travel through the tool path at all, and the export
    decision is not made here. See `NOTHING_HERE_MAKES_A_DOCUMENT_SAFE_TO_READ` and the
    native-export paragraph in the module docstring.
    """

    #: `/drive/v3/files`, filtered to the pinned folder. Pages by an opaque token.
    LIST_FILES = "list_files"
    #: `/drive/v3/files/{fileId}`. One file's metadata, and the endpoint that answers 404 for
    #: two different facts.
    GET_FILE = "get_file"


@dataclass(frozen=True)
class EndpointShape:
    """How one endpoint pages and where its records live.

    The specification is carried whole rather than summarised, so `records_at` and the
    parameter list have one home: `brain.connectors.rest.RestOperation` reads them to build
    the address and find the rows, and this module reads the same object to decide how to
    walk. A second copy of "where the records live" is a second thing to keep in step with
    the vendor, and the copy that drifts silently reads an empty array.
    """

    endpoint: Endpoint
    spec: OperationSpec
    #: Whether this endpoint pages at all. The by-id read does not.
    paged: bool = False


def _query(name: str, *, required: bool = False) -> ParameterSpec:
    return ParameterSpec(name=name, location="query", required=required)


#: Drive's own parameter names. Held as constants because three of them are the difference
#: between listing a shared drive and listing nothing at all, and a literal at a call site is
#: the one that gets left out. The page cursor is called `pageToken` at the source and
#: `cursor` here, which keeps the platform's own word for an opaque resumption point.
QUERY_PARAMETER: Final = "q"
FIELDS_PARAMETER: Final = "fields"
PAGE_SIZE_PARAMETER: Final = "pageSize"
PAGE_CURSOR_PARAMETER: Final = "pageToken"
CORPORA_PARAMETER: Final = "corpora"
DRIVE_ID_PARAMETER: Final = "driveId"
SUPPORTS_ALL_DRIVES: Final = "supportsAllDrives"
INCLUDE_ALL_DRIVES: Final = "includeItemsFromAllDrives"

#: Where the next page's cursor arrives, and the only end-of-data signal Drive offers. Unlike
#: Freshdesk, this is an actual statement by the source rather than arithmetic on a page
#: length, so "is this all of them" is answerable here and is not there.
NEXT_PAGE_FIELD: Final = "nextPageToken"

#: What `corpora` has to say for a shared drive. The default is the user's own drive, which
#: is why omitting it is silent rather than loud.
SHARED_DRIVE_CORPUS: Final = "drive"


ENDPOINTS: Final[MappingProxyType[Endpoint, EndpointShape]] = MappingProxyType(
    {
        Endpoint.LIST_FILES: EndpointShape(
            endpoint=Endpoint.LIST_FILES,
            spec=OperationSpec(
                operation_id="listFiles",
                method="get",
                path="/drive/v3/files",
                parameters=(
                    _query(QUERY_PARAMETER, required=True),
                    _query(FIELDS_PARAMETER, required=True),
                    _query(PAGE_SIZE_PARAMETER),
                    _query(PAGE_CURSOR_PARAMETER),
                    _query(CORPORA_PARAMETER),
                    _query(DRIVE_ID_PARAMETER),
                    _query(SUPPORTS_ALL_DRIVES),
                    _query(INCLUDE_ALL_DRIVES),
                ),
                records_at="files",
                returns_list=True,
            ),
            paged=True,
        ),
        Endpoint.GET_FILE: EndpointShape(
            endpoint=Endpoint.GET_FILE,
            spec=OperationSpec(
                operation_id="getFile",
                method="get",
                path="/drive/v3/files/{fileId}",
                parameters=(
                    ParameterSpec(name="fileId", location="path", required=True),
                    _query(FIELDS_PARAMETER, required=True),
                    _query(SUPPORTS_ALL_DRIVES),
                ),
                records_at="",
                returns_list=False,
            ),
        ),
    }
)


def shape_for(endpoint: Endpoint) -> EndpointShape:
    """How this endpoint pages and where its records live.

    A function rather than a bare subscript, so the totality of `ENDPOINTS` is asserted in
    one place and no caller invents a fallback when a lookup misses. The same argument
    `brain.connectors.change_signal.facts_for` makes about its own table.
    """
    try:
        return ENDPOINTS[endpoint]
    except KeyError as exc:  # pragma: no cover - the totality test keeps this unreached
        msg = (
            f"{endpoint!r} has no endpoint shape, so nothing knows how it pages or where its "
            "records are; declare it before anything reads it"
        )
        raise DriveError(msg) from exc


# ------------------------------------------------------------------- the field mapping
#: What arrives from Drive, and the one place a source path is written down.
#:
#: `parents`, `driveId` and `shortcutDetails.targetId` are mapped and deliberately not
#: projected. They are placement facts, used at fetch time to decide whether a file may be
#: read at all and then dropped; a stored parent list is a path by another name. This is the
#: same split `xero.INVOICE_FIELDS` makes for `amount_due`: mapped, used, never kept.
FILE_MAPPING: Final[tuple[FieldMapping, ...]] = (
    FieldMapping(target=ID_TARGET, source_path="id"),
    FieldMapping(target="name", source_path="name"),
    FieldMapping(target="mime_type", source_path="mimeType"),
    FieldMapping(target="modified_at", source_path="modifiedTime"),
    FieldMapping(target="revision_id", source_path="headRevisionId"),
    FieldMapping(target="trashed", source_path="trashed"),
    FieldMapping(target="parents", source_path="parents"),
    FieldMapping(target="drive_id", source_path="driveId"),
    FieldMapping(target="shortcut_target_id", source_path="shortcutDetails.targetId"),
)

#: What the `fields` parameter asks Drive for, declared rather than derived from the mapping.
#:
#: Declared, because the selector has to name one thing the mapping never will: the
#: permissions the sharing classification is built from, which are read and reduced and never
#: mapped into a record. Deriving the list would make the relation below trivially true and
#: therefore worth nothing, and the relation is the thing that matters: **Drive returns a
#: small default field set, so a mapped field that the selector does not ask for simply never
#: arrives.** For `parents` that means every file is unplaceable and nothing is indexed,
#: which is loud; for `headRevisionId` it means the projection quietly loses the one value
#: that says the content changed.
FILE_SELECTOR_ROOTS: Final[tuple[str, ...]] = (
    "driveId",
    "headRevisionId",
    "id",
    "mimeType",
    "modifiedTime",
    "name",
    "parents",
    "shortcutDetails",
    "trashed",
)

#: The sub-selection that carries the sharing classification's input, and the only thing in
#: the selector that is not a mapped field. Sub-selected to two properties on purpose: asking
#: for `permissions` whole returns email addresses and display names for every grantee, and
#: the reduction in `PermissionFact` is worth much more if the extra fields never arrive.
SHARING_SELECTOR: Final = "permissions(type,domain)"


def _source_roots() -> frozenset[str]:
    """The first segment of every mapped source path. What Drive has to be asked for."""
    return frozenset(mapping.source_path.split(".")[0] for mapping in FILE_MAPPING)


def assert_selector_covers_the_mapping() -> None:
    """Every mapped field is one the field selector actually asks Drive for (M11.6.7).

    A relation between two declarations that are edited at different times by different
    people, checked rather than reviewed, in the same shape as
    `xero.assert_declarations_agree`. A mapping naming a field the selector omits is a column
    that never arrives, and Drive's default field set is small enough that this is the
    ordinary mistake rather than an exotic one.
    """
    missing = sorted(_source_roots() - set(FILE_SELECTOR_ROOTS))
    if missing:
        msg = (
            f"the field mapping reads {missing} and the selector does not ask Drive for "
            "them; Drive returns a small default set, so those fields never arrive and the "
            "column is silently empty rather than absent"
        )
        raise DriveError(msg)


def field_selector(endpoint: Endpoint) -> str:
    """Drive's `fields` value for one endpoint, built from the declared selection.

    The list endpoint's selection is nested under `files(...)` and carries the next-page
    cursor beside it, because Drive drops every field that is not named and `nextPageToken`
    is a field like any other: a selector that forgets it produces a walk that always thinks
    it has reached the last page.
    """
    inner = ",".join([*sorted(FILE_SELECTOR_ROOTS), SHARING_SELECTOR])
    if shape_for(endpoint).spec.returns_list:
        return f"{NEXT_PAGE_FIELD},files({inner})"
    return inner


def transport_for(endpoint: Endpoint) -> RestTransport:
    """The declaration `brain.connectors.rest` binds to a parsed operation.

    The operation id comes off the endpoint's own specification rather than being written
    again here, so a mapping cannot name an operation the shape does not describe.
    """
    return RestTransport(
        spec_ref=SPEC_REF,
        operation=shape_for(endpoint).spec.operation_id,
        entity=FILE,
        fields=FILE_MAPPING,
    )


def operation_for(endpoint: Endpoint) -> RestOperation:
    """One endpoint, bound to its mapping and to the one address it is reached at.

    `RestOperation.__post_init__` runs `assert_maps_only` over every declaration this is
    built from, so a mapping that grew a permission clause is refused here rather than at the
    first request. Nothing is fetched: the address is built and checked by `prepare`, which
    the transport calls with a resolver this module does not have.
    """
    return RestOperation(
        base_url=BASE_URL, operation=shape_for(endpoint).spec, transport=transport_for(endpoint)
    )


# --------------------------------------------------------------- one request, as a value
def folder_query(connection: DriveConnection, *, modified_after: str = "") -> str:
    """Drive's own search expression for the pinned folder's direct children.

    Two clauses are always present and the second is the one people leave out. `trashed =
    false` is required because `files.list` returns files in the bin unless the query
    excludes them, so a folder somebody emptied last month keeps answering questions with
    documents that are gone. The first clause is the pin, and it is what makes this a
    folder-scoped read rather than a search of the account.

    `modified_after` is the change cursor. It goes in the query rather than into a
    drive-wide changes feed, which is the decision argued in the module docstring: a
    folder-scoped cursor is the only kind that does not require reading every change in the
    client's Drive to discard almost all of them.

    Nothing here is escaped and nothing needs to be: the folder id passed `ConnectorScope`'s
    grammar and then `_FILE_ID_RE` at connect, and the timestamp passes `_TIMESTAMP_RE` here.
    None of the three admits a quote or a space. See `transports.NO_SQL_CROSSES_THIS_SEAM`
    for the same argument about the same class of mistake.
    """
    clauses = [f"'{connection.folder_id}' in parents", "trashed = false"]
    if modified_after:
        if not _TIMESTAMP_RE.match(modified_after):
            msg = (
                f"{modified_after!r} is not an RFC 3339 instant; it is put into Drive's own "
                "search expression, and a value outside the grammar is either refused by the "
                "source mid-question or is a quote that changes which files are listed"
            )
            raise DriveError(msg)
        clauses.append(f"modifiedTime > '{modified_after}'")
    return " and ".join(clauses)


@dataclass(frozen=True)
class ListingRequest:
    """One page of the pinned folder, checked at the point it is built.

    Frozen and validated in `__post_init__` rather than checked by whoever sends it, because
    the failure it prevents is invisible in the reply: a shared-drive listing made without
    the all-drives parameters is a 200 carrying an empty array, which is byte-for-byte a
    genuinely empty folder. See `THE_SHARED_DRIVE_FLAGS_DEFAULT_TO_SILENCE`.
    """

    connection: DriveConnection
    #: Drive's opaque resumption point, called `pageToken` at the source. Empty is the first
    #: page, which is the only page that has no cursor.
    cursor: str = ""
    page_size: int = DEFAULT_PAGE_SIZE
    modified_after: str = ""
    #: Whether the request carries the two parameters a shared drive needs. Required to be
    #: true for a shared-drive connection and permitted either way for My Drive, because they
    #: are harmless there and a deployment that turns them on everywhere is not wrong.
    all_drives: bool = False

    def __post_init__(self) -> None:
        if self.page_size < 1:
            msg = f"a page of {self.page_size} files is a call that costs a call and returns none"
            raise DriveError(msg)
        if self.page_size > MAX_PAGE_SIZE:
            msg = (
                f"Drive accepts at most {MAX_PAGE_SIZE} files a page and this asks for "
                f"{self.page_size}; the source refuses rather than clamping, so sending it "
                "spends a call to be told 400 in the middle of somebody's question"
            )
            raise DriveError(msg)
        if self.connection.is_shared_drive and not self.all_drives:
            msg = (
                f"this connection is pinned to a folder in shared drive "
                f"{self.connection.drive_id!r} and the request does not carry "
                f"{SUPPORTS_ALL_DRIVES} and {INCLUDE_ALL_DRIVES}. "
                f"{THE_SHARED_DRIVE_FLAGS_DEFAULT_TO_SILENCE}"
            )
            raise DriveError(msg)

    def as_arguments(self) -> dict[str, str]:
        """The arguments `brain.connectors.rest.RestOperation.url_for` builds the address from.

        `corpora` and `driveId` are added only for a shared drive, because Drive refuses
        `driveId` without the matching corpus and the pair is meaningless for My Drive.
        `url_for` refuses an argument the operation does not declare, which is the other half
        of the same rule and the reason this cannot quietly send a parameter nobody reads.
        """
        built: dict[str, str] = {
            QUERY_PARAMETER: folder_query(self.connection, modified_after=self.modified_after),
            FIELDS_PARAMETER: field_selector(Endpoint.LIST_FILES),
            PAGE_SIZE_PARAMETER: str(self.page_size),
        }
        if self.cursor:
            built[PAGE_CURSOR_PARAMETER] = self.cursor
        if self.all_drives:
            built[SUPPORTS_ALL_DRIVES] = "true"
            built[INCLUDE_ALL_DRIVES] = "true"
        if self.connection.is_shared_drive:
            built[CORPORA_PARAMETER] = SHARED_DRIVE_CORPUS
            built[DRIVE_ID_PARAMETER] = self.connection.drive_id
        return built


def first_page(connection: DriveConnection, *, modified_after: str = "") -> ListingRequest:
    """The first page of the pinned folder, with the parameters that folder actually needs.

    A named constructor rather than defaults on `ListingRequest`, because whether the
    all-drives parameters are required is a property of the connection and looking it up is
    the step somebody skips. It is set for My Drive as well, which is harmless there and
    means a folder that moves into a shared drive does not silently start returning nothing.
    """
    return ListingRequest(connection=connection, modified_after=modified_after, all_drives=True)


# ------------------------------------------------------------------ what came back
@dataclass(frozen=True)
class Reply:
    """What came back, as a value. The same three fields a cassette records.

    Deliberately identical in shape to `tests.fixtures.cassettes.Cassette`, so that the day a
    Google Drive recording exists it becomes one of these with no translation step that could
    disagree with the recording. This module never constructs one: it is what a transport
    hands over, which is what keeps every rule here testable without a socket.
    """

    status: int
    headers: Mapping[str, str] = MappingProxyType({})
    body: Any = None

    def header(self, name: str) -> str:
        """One header, matched without regard to case.

        HTTP header names are case-insensitive and vendors change their casing between
        releases. A connector matching `Retry-After` exactly, handed `retry-after`, finds
        nothing and falls back to a guess while believing it read the source's own hint.
        """
        wanted = name.casefold()
        for key, value in self.headers.items():
            if key.casefold() == wanted:
                return value
        return ""


class PageReader(Protocol):
    """Whatever performs one exchange and hands back the reply.

    A protocol rather than a client, so this module holds no connection, and for the reason
    `brain.knowledge.scanning.Scanner` gives about its own: the cases that decide whether
    this is right are a throttling 403, a 404 that means either of two things, and a
    shared-drive listing that came back empty because of a missing parameter. None of the
    three can be arranged reliably against a real Drive, and none of them is recorded.
    """

    def read(self, request: ListingRequest) -> Reply: ...


def error_reason(reply: Reply) -> str:
    """Google's own reason code for a failure, or empty when it did not give one.

    Both envelope shapes are read, and that is deliberate rather than defensive: the classic
    Drive error carries `error.errors[0].reason` and the newer one carries `error.status`,
    the documentation shows both, and no recording exists to say which a deployment will
    meet. Reading either is the shape that does not have to change when one turns out to be
    the truth. See `WHAT_A_RECORDING_WOULD_SETTLE`.

    An unreadable body produces an empty reason rather than an exception. The reason narrows
    a classification the platform has already made, so failing to read it leaves the
    platform's verdict standing, which is the conservative direction.
    """
    body = reply.body
    if not isinstance(body, Mapping):
        return ""
    error = body.get("error")
    if not isinstance(error, Mapping):
        return ""
    listed = error.get("errors")
    if isinstance(listed, Sequence) and not isinstance(listed, str | bytes):
        for entry in listed:
            if isinstance(entry, Mapping) and isinstance(entry.get("reason"), str):
                return str(entry["reason"])
    status = error.get("status")
    return status if isinstance(status, str) else ""


def call_outcome(reply: Reply) -> CallOutcome:
    """What this reply did, in the platform's vocabulary, narrowed by Drive's reason code.

    `brain.connectors.throttle.classify` decides first and is not restated, so this cannot
    come to a different conclusion from the module that owns the rule that a 429 is a quota
    refusal rather than ill health. What is added is one narrowing, in one direction: a
    `REJECTED` verdict whose reason code names a rate limit becomes `QUOTA`. See
    `A_DRIVE_THROTTLE_ARRIVES_AS_A_403`.

    The narrowing never runs the other way. A 429 stays `QUOTA` whatever the body says,
    because a source that has told us to slow down in the status line has told us, and a
    reason code is not a reason to disbelieve it.
    """
    outcome = classify(status=reply.status)
    if outcome is CallOutcome.REJECTED and error_reason(reply) in DRIVE_THROTTLE_REASONS:
        return CallOutcome.QUOTA
    return outcome


def retry_hint(reply: Reply) -> float:
    """How long the source asked us to wait, or the long end when it did not say.

    Seconds only. The other form `Retry-After` may take in HTTP is a date, which cannot be
    turned into a wait without reading a clock this module deliberately does not have, so an
    unparseable value takes the same path as an absent one rather than being half-understood.
    Drive frequently sends no hint at all with a throttling 403, which is exactly why the
    fallback is the long end: see `RETRY_AFTER_WHEN_UNSTATED`.
    """
    stated = reply.header("Retry-After").strip()
    if not stated:
        return RETRY_AFTER_WHEN_UNSTATED
    try:
        seconds = float(stated)
    except ValueError:
        return RETRY_AFTER_WHEN_UNSTATED
    if seconds <= 0:
        return RETRY_AFTER_WHEN_UNSTATED
    return seconds


class DriveUnreachableError(Degraded):
    """Drive could not answer: a quota refusal, a timeout, or a server failure.

    A `Degraded` and therefore carrying the platform's one sentence for this, which does not
    name the system. The call outcome and the retry hint are for whoever is on call; the
    person who asked the question is told the same thing whatever failed, because which of
    the company's systems is unwell is not a fact obtainable by typing a question.

    `call_outcome` is spelled out rather than reusing `BrainError.outcome`, which is the
    user-facing taxonomy and is DEGRADED for every class here. Two different questions
    sharing one attribute is how the operator's one gets rendered to somebody.
    """

    def __init__(
        self,
        detail: str = "",
        *,
        call_outcome: CallOutcome = CallOutcome.UNAVAILABLE,
        retry_after: float = 0.0,
    ) -> None:
        super().__init__(detail)
        self.call_outcome = call_outcome
        self.retry_after = retry_after

    @property
    def freshness(self) -> Freshness:
        """What anything a caller might substitute would be worth: nothing datable."""
        return UNREACHABLE_FRESHNESS

    def trace_line(self) -> str:
        """The full statement, for an operator rather than for the asker.

        Names the source and the outcome unconditionally, which is safe for the reason
        `brain.connectors.federation.PartialAnswer.trace_lines` is safe: a trace is read by
        somebody already entitled to know what the system connects to, and nothing here can
        put this string into a channel payload.
        """
        return (
            f"{GOOGLE_DRIVE}: {self.call_outcome}, retry after {self.retry_after:.0f}s, "
            f"data {FRESHNESS_TEXT[self.freshness]}"
        )


class DriveRefusedError(Degraded):
    """Drive understood the request and would not answer it.

    Its own type rather than a flag on the one above, because the two go to different people
    and have opposite remedies: a credential the client revoked or a folder somebody
    unshared is a person changing a grant, and waiting makes it no better.
    `throttle.is_retryable` says the same thing about `REJECTED`, and this is the shape that
    stops a retry loop being written against it in the first place.

    Also a `Degraded`, so the asker is told the same sentence as for an outage. A refusal
    that read differently would say which of our credentials is wrong to somebody who asked
    about a document.
    """

    def __init__(
        self, detail: str = "", *, call_outcome: CallOutcome = CallOutcome.REJECTED
    ) -> None:
        super().__init__(detail)
        self.call_outcome = call_outcome


class DriveNotFoundError(Degraded):
    """Drive answered 404, which means the file is absent or is one we may not see.

    **A sibling of the two above and a subclass of neither**, which is the whole point of it
    existing. Filed under the refusal it would send somebody to fix a credential for a file
    that was deleted; filed under an absence it would report a document as gone when it has
    only been unshared, and the file would quietly stop being indexed. See
    `A_NOT_FOUND_DOES_NOT_SEPARATE_ABSENT_FROM_REFUSED`.

    `call_outcome` is `REJECTED`, because whichever of the two facts it is, retrying
    reproduces it exactly at full cost. That is the one thing about a 404 that is not
    ambiguous.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail)
        self.call_outcome = CallOutcome.REJECTED

    def trace_line(self) -> str:
        """What an operator is told: the ambiguity, kept rather than resolved."""
        return (
            f"{GOOGLE_DRIVE}: {HTTP_NOT_FOUND}, absent or not visible to this credential, "
            "and the source does not separate the two"
        )


def assert_answered(reply: Reply) -> None:
    """Raise unless this reply is an answer, keeping the outcomes apart.

    The order of the branches is the rule.

    **404 first**, before anything classifies it, because `classify` reads it as a plain
    client error and the whole point is that it is not one.

    **Quota and unavailable** are the source not answering. Raised, with the hint. A
    throttling 403 arrives here as `QUOTA` because `call_outcome` narrowed it, which is what
    stops a rate limit being treated as a permanent rejection.

    **Rejected** is the source refusing us. Raised, with no hint, because a retry reproduces
    it exactly and a hint invites one.

    **OK** returns, and an empty listing then travels as a result with no records in it.

    Called before the body is read, deliberately: a Drive error carries a body of its own,
    and projecting that first would report a rate limit as a malformed response, which sends
    whoever reads the error to the wrong module.
    """
    if reply.status == HTTP_NOT_FOUND:
        raise DriveNotFoundError(
            f"{GOOGLE_DRIVE} answered {HTTP_NOT_FOUND}; the file is not there, or it is and "
            "this credential may not see it, and the source does not say which"
        )
    outcome = call_outcome(reply)
    if outcome in (CallOutcome.QUOTA, CallOutcome.UNAVAILABLE):
        raise DriveUnreachableError(
            f"{GOOGLE_DRIVE} answered {reply.status} ({error_reason(reply) or 'no reason'}); "
            "the source could not be reached, and an answer from anywhere else would be "
            "presented as though it had been",
            call_outcome=outcome,
            retry_after=retry_hint(reply),
        )
    if outcome is CallOutcome.REJECTED:
        raise DriveRefusedError(
            f"{GOOGLE_DRIVE} refused the request with {reply.status} "
            f"({error_reason(reply) or 'no reason'}); this is our credential or our request "
            "rather than Drive's health, so waiting does not fix it",
            call_outcome=outcome,
        )


# --------------------------------------------------------------------------- the walk
def next_cursor(reply: Reply) -> str:
    """Drive's own statement that there is another page, or empty when there is not.

    Read from the body rather than inferred from the page length, which is the one place this
    connector has it easier than Freshdesk: a short page here is just a short page, and the
    source says plainly whether it has more. A connector that guessed from the length would
    stop early on a folder Drive chose to page differently from the size we asked for, which
    it is entitled to do.
    """
    body = reply.body
    if not isinstance(body, Mapping):
        return ""
    cursor = body.get(NEXT_PAGE_FIELD)
    return cursor if isinstance(cursor, str) else ""


def read_page(
    operation: RestOperation, reader: PageReader, request: ListingRequest
) -> tuple[tuple[Mapping[str, Any], ...], str]:
    """One exchange: refuse the failure, then project what the mapping names.

    The order is load-bearing and is argued for in `assert_answered`. What is not re-wrapped
    is the other failure: a body that is not the shape the operation's own specification
    declares raises `brain.connectors.rest.RestSpecError` from `project`, and that is left to
    propagate rather than being renamed here, for the reason `brain.connectors.rest` gives
    about not giving an operator two names for one refusal. The property that matters holds
    either way and it is the one a naive connector loses: an unreadable body is a failure and
    never an empty result, because reporting an outage as an empty folder is how a document
    that exists becomes a document nobody can find.
    """
    reply = reader.read(request)
    assert_answered(reply)
    return operation.project(reply.body), next_cursor(reply)


@dataclass(frozen=True)
class FolderReading:
    """One folder listing's records, and everything needed to say what they are not.

    `next_cursor` is carried rather than dropped so that a caller stopped by their own limit
    can resume, and so `is_all_of_them` is answerable from what the source said rather than
    from arithmetic. There is deliberately no count of anything that was not returned.
    """

    result: TypedResult[SourceRecord]
    pages_read: int
    next_cursor: str = ""
    #: Whether the walk stopped because the caller asked for fewer, which is a different
    #: claim from the source running out and has to stay separable: the caller knows they
    #: asked, and only the source knows whether there was more.
    stopped_at_caller_limit: bool = False

    @property
    def is_all_of_them(self) -> bool:
        """Whether this may be spoken about as everything in the folder.

        True only when Drive said there was no further page and nothing stopped the walk
        early. Unlike Freshdesk this is a fact the source stated rather than one inferred
        from a page length, which is why there is no completeness verdict beside it.
        """
        return not self.next_cursor and not self.stopped_at_caller_limit

    def trace_line(self) -> str:
        """What the listing did, for an operator. Names the source, as a trace may."""
        return (
            f"{GOOGLE_DRIVE}.{FILE}: {len(self.result.records)} record(s) over "
            f"{self.pages_read} page(s), "
            f"{'complete' if self.is_all_of_them else 'more remaining'}"
        )


def list_folder(
    operation: RestOperation,
    reader: PageReader,
    connection: DriveConnection,
    *,
    fetched_at: str,
    limit: int = 0,
    modified_after: str = "",
) -> FolderReading:
    """Walk the pinned folder, checking every row against the pin as it arrives (M11.6.7).

    The placement check runs on every row and refuses the whole page when one fails, rather
    than dropping the row. That direction is deliberate. A listing built from
    `'<pin>' in parents` cannot return a file outside the folder, so a row that is outside it
    means the query is not the query this code believes it sent, and dropping the row would
    hide that while continuing to return the rest. A refusal is loud and the query gets
    fixed; a silent drop is a connector that under-reports for a reason nobody looks for.

    Only direct children are listed, so no ancestry is needed here: every row's own `parents`
    carries the pin. A recursive walk belongs to the caller, which is why `place` takes an
    `Ancestry` at all.

    `limit` is the caller's and is a request rather than a guarantee, exactly as
    `contract.FetchRequest` says. A limit of zero means "as much as the folder holds", and
    the cursor is carried out either way so a caller who stopped early can resume.
    """
    if limit < 0:
        msg = "a negative limit is not a limit"
        raise ValueError(msg)
    request = first_page(connection, modified_after=modified_after)
    rows: list[Mapping[str, Any]] = []
    pages = 0
    stopped_at_caller_limit = False
    cursor = ""
    while True:
        page, cursor = read_page(operation, reader, request)
        pages += 1
        for row in page:
            assert_row_is_in_the_folder(connection, row)
        rows.extend(page)
        if limit and len(rows) >= limit:
            del rows[limit:]
            stopped_at_caller_limit = True
            break
        if not cursor:
            break
        request = replace(request, cursor=cursor)

    return FolderReading(
        result=normalise(
            FILE,
            tuple(rows),
            source=GOOGLE_DRIVE,
            fetched_at=fetched_at,
            id_field=ID_TARGET,
            truncated=stopped_at_caller_limit or bool(cursor),
        ),
        pages_read=pages,
        next_cursor=cursor,
        stopped_at_caller_limit=stopped_at_caller_limit,
    )


def assert_row_is_in_the_folder(connection: DriveConnection, row: Mapping[str, Any]) -> None:
    """Refuse a listed row whose parents do not include the pin.

    Reads `parents` off the projected row rather than building a `DriveFile`, because a row
    that is missing an id or a mime type is `normalise`'s problem and this check has to run
    on every row the source sent, including the malformed ones. A row with no parents is
    refused too: it is the shape a listing produces when the field selector forgot `parents`,
    and treating it as inside the folder would place every file in the account.
    """
    parents = row.get("parents")
    listed = tuple(str(p) for p in parents) if isinstance(parents, list) else ()
    if not any(connection.admits(folder_id) for folder_id in listed):
        msg = (
            f"a listed file names parents {list(listed)} and none of them is the folder this "
            f"connector is pinned to; the query cannot have been the folder query, so the "
            f"page is refused rather than filtered. {SCOPE_AT_CONNECT_IS_THE_WHOLE_POINT}"
        )
        raise DriveError(msg)


# ------------------------------------------------- the fetch, as the contract wants it
def folder_fetch(
    operation: RestOperation,
    reader: PageReader,
    connection: DriveConnection,
    *,
    fetched_at: str,
) -> Callable[[FetchRequest], TypedResult[SourceRecord]]:
    """The folder listing as a connector fetch, checked against the contract before it is used.

    `assert_fetches_only` runs on the closure rather than on this factory, and that is the
    point of building one: the closure is the object a registry would call, so it is the
    object whose signature has to be shown never to receive the caller's grants, a vault, or
    anything else that decides. The reader, the connection and the stamp are wiring supplied
    by whoever builds the connector, and a parameter a caller cannot reach cannot carry any
    of the three.

    **A cursor is honoured here, which is the opposite of Freshdesk.** Drive pages by an
    opaque token and says plainly when there is another page, so resuming from a cursor is
    the source's own mechanism rather than a shape borrowed from another vendor. What is
    refused instead is a filter: the folder query is the scope, and a caller adding terms to
    it would be narrowing or widening the one thing that is pinned. A `modified_after` cursor
    belongs on the subscription's pass, not on an ad-hoc request.
    """

    def _fetch(request: FetchRequest) -> TypedResult[SourceRecord]:
        if request.entity != FILE:
            msg = (
                f"this connector reads {FILE!r} and was asked for {request.entity!r}; a fetch "
                "answering for the wrong entity returns records tagged as something they are "
                "not, and the redactor then looks up the wrong field policy for every one"
            )
            raise DriveError(msg)
        if request.filters:
            msg = (
                f"this fetch takes no filters and was given {[k for k, _ in request.filters]}; "
                f"the folder query is the scope, and a term added to it either widens the pin "
                f"or narrows it somewhere no console shows. "
                f"{SCOPE_AT_CONNECT_IS_THE_WHOLE_POINT}"
            )
            raise DriveError(msg)
        reading = list_folder(
            operation, reader, connection, fetched_at=fetched_at, limit=request.limit
        )
        return reading.result

    assert_fetches_only(_fetch)
    return _fetch


# ------------------------------------------------------------------------- the projection
#: What is kept locally about a file, and why each earns a place under the twelve-field cap.
#: Seven of twelve, and the headroom is deliberate: the fields most likely to be wanted next
#: are a size and an owning shared drive, and leaving room means adding one is a review
#: rather than an argument about which existing field to drop.
#:
#: `id` is deliberately absent. `brain.connectors.projection.ProjectedRecord` carries the
#: source id as its own field and `proj.record`'s primary key is built from it, so declaring
#: it here would spend one of the twelve on a value the row already has.
FILE_FIELDS: Final[tuple[ProjectedField, ...]] = (
    #: The pin this file was reached through, not the parent it sits under. It is also the
    #: field the visibility predicate tests, which is why it is a join key rather than a
    #: label: it is how a row is attributed to a connector and to a scope.
    ProjectedField(name="folder_id", shape=FieldShape.JOIN_KEY, uses=(HotUse.FILTER, HotUse.JOIN)),
    #: The one label. A second would be a payload arriving in instalments, which is what the
    #: pointer clause in `manifest.projectability` refuses.
    ProjectedField(name="name", shape=FieldShape.LABEL, uses=(HotUse.IDENTIFY,)),
    ProjectedField(name="mime_type", shape=FieldShape.STATUS, uses=(HotUse.FILTER, HotUse.COUNT)),
    ProjectedField(
        name="modified_at", shape=FieldShape.TIMESTAMP, uses=(HotUse.FILTER, HotUse.SORT)
    ),
    #: Which revision was indexed. See `A_FILE_ID_IS_THE_IDENTITY_AND_A_PATH_IS_NOT`: a
    #: timestamp says something changed and this says the content did.
    ProjectedField(name="revision_id", shape=FieldShape.IDENTIFIER, uses=(HotUse.IDENTIFY,)),
    #: The classification, never the list. `manifest.RESOLVED_ACL_RE` watches for `shared`,
    #: and the name it watches for is precisely the name of the thing this refuses to store.
    ProjectedField(
        name="sharing_state", shape=FieldShape.STATUS, uses=(HotUse.FILTER, HotUse.COUNT)
    ),
    #: Drive's bin is not deletion. A trashed file still exists, still answers a by-id read
    #: and is still restorable, so it is a state rather than an absence.
    ProjectedField(name="trashed", shape=FieldShape.STATUS, uses=(HotUse.FILTER,)),
)

#: The projected field that identifies a file to a person, and therefore the one value that
#: has to be cut to fit `brain.core.projection.MAX_LABEL_CHARS`.
LABEL_FIELD: Final = "name"

#: Drive fields this connector deliberately never keeps, with the reason each is out. Written
#: down because the interesting half is that they are refused by *different* rules, and a
#: reader who assumes the platform denylist covers everything would conclude that `owners` is
#: projectable: it is not on that list, and what refuses it is this module's own guard about
#: principals read out of somebody else's directory.
NOT_CARRIED_FROM_DRIVE: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        "owners": "a list of principals in Google's directory, each carrying a display name "
        "and an email address; ours are Keycloak subjects and the two only look joinable",
        "sharingUser": "the same, for whoever shared it, and an address besides",
        "lastModifyingUser": "the same, for whoever touched it last",
        "permissions": "the resolved ACL itself, read once and reduced to a classification "
        "by `classify_sharing`, and never stored in any shape",
        "permissionIds": "the same list with the addresses removed, which is still a list of "
        "principals and is still stale the moment somebody moves department",
        "parents": "mapped and used to place the file, never kept: a stored parent is a path "
        "by another name and is wrong the moment somebody drags the file",
        "webViewLink": "a URL that carries the file id and grants nothing, and a second "
        "identity for a record that already has one",
        "md5Checksum": "a digest the source computed; the digest anything here is bound to "
        "is the one `brain.knowledge.ingest.admit_upload` computes over the bytes in hand",
        "description": "free text somebody typed into Drive, which is content rather than a "
        "pointer and has no hot use in the fast lane",
    }
)


def projected_field_names() -> tuple[str, ...]:
    return tuple(field.name for field in FILE_FIELDS)


def parse_drive_timestamp(value: object) -> datetime | None:
    """Drive's RFC 3339 instant as an aware datetime, or None when it cannot be dated.

    The trap here is the opposite of Xero's. Xero's format is exotic enough that parsing it
    as ISO raises, which is at least loud; Drive's is ordinary, so the temptation is to store
    the string and let it sort lexically, which works until a value arrives without a zone
    and sorts against every other one incorrectly. So it is parsed, and an undatable value is
    dropped rather than guessed: None means "not stated", exactly as
    `brain.gate.provenance.read_time` means it.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def projected_fields(
    row: Mapping[str, Any], *, connection: DriveConnection, sharing: SharingState
) -> dict[str, ProjectedValue]:
    """One fetched row as the fields a projected record may hold.

    Built from the declared names rather than by removing what is unwanted from the row, for
    the reason `brain.connectors.rest.WHAT_THE_MAPPING_DOES_NOT_NAME_DOES_NOT_ARRIVE` gives:
    the two read the same today and diverge the first time Google adds a field, because a
    copy carries it and a build does not.

    Two of the seven do not come off the row at all. `folder_id` is the pin, supplied by the
    connection, because the row's own parent may be a subfolder and storing that would name a
    folder nobody scoped. `sharing_state` is the classification the caller reached, because
    the permission list it was reduced from must not reach this far.

    The label is cut to `MAX_LABEL_CHARS`, and the cut is silent by design: a marker would
    make a filename that genuinely ends in an ellipsis indistinguishable from one that was
    cut, and the record's identity is its id rather than its name. The alternative is worse
    in the way that matters, because `check_projection` refuses an over-long string, so a
    file with a long name would be dropped whole at ingest.
    """
    if not sharing.is_known:
        msg = (
            f"this file's sharing is {sharing}, so there is no row to store: "
            f"{UNDETERMINED_SHARING_IS_REFUSED_RATHER_THAN_DEFAULTED}"
        )
        raise DriveError(msg)
    built: dict[str, ProjectedValue] = {
        "folder_id": connection.folder_id,
        "sharing_state": sharing.value,
    }
    for name in projected_field_names():
        if name in built or name not in row:
            # An absent field contributes nothing rather than a null. Drive omitting an
            # optional field has said something different from Drive sending an empty one.
            continue
        value = row[name]
        if name == LABEL_FIELD and isinstance(value, str):
            built[name] = value[:MAX_LABEL_CHARS]
            continue
        if name == "modified_at":
            dated = parse_drive_timestamp(value)
            if dated is not None:
                built[name] = dated
            continue
        built[name] = value
    return built


def file_projection(connection: DriveConnection) -> ProjectedEntity:
    """What is kept about a file, and the predicate that decides who may read a row.

    The predicate is the connection's, which is the folder and deliberately nothing else: see
    `DriveConnection.visibility_predicate`. `ProjectedEntity` refuses an unrestricted one
    because a projection stored with no predicate has discarded the source's permission model
    rather than narrowed it, and it refuses one that enumerates principals because that is
    the resolved ACL wearing a predicate's shape. Both refusals are the manifest's and are
    not repeated here.

    The change signal is `UPDATED_SINCE` and not a webhook, and the cursor is `modifiedTime`
    inside the folder query rather than Drive's own changes feed. The argument is in the
    module docstring and it is a scope argument rather than a convenience one: the changes
    feed is drive-wide, so subscribing to it would read the client's whole Drive to discard
    almost all of it.
    """
    return ProjectedEntity(
        entity=FILE,
        fields=FILE_FIELDS,
        change_signal=ChangeSignal.UPDATED_SINCE,
        visibility=connection.visibility_predicate(),
    )


# ------------------------------------------------------ into the knowledge plane (M7.1.x)
def media_type_for(mime_type: str) -> MediaType:
    """The knowledge layer's type for a Drive mime type, or a refusal naming the reason.

    Drive reports the same IANA strings the knowledge allowlist is keyed by, so there is no
    translation table here and nothing to drift: `MediaType` is the allowlist and this looks
    a value up in it. The interesting half is what it refuses, and the three refusals are
    three different problems.

    **A folder and a shortcut have no bytes.** Reaching this with either means something
    upstream is treating structure as content.

    **A Google-native document has no bytes either**, and that is the gap rather than a
    design: exporting one is a choice of format that decides what the company's own
    procedures look like to a model, `files.export` carries its own ceiling, and choosing
    silently here would make that decision invisible. See the module docstring.

    **Anything else unrecognised is refused rather than attempted**, which is
    `brain.knowledge.ingest`'s own rule: there is no deny list anywhere in this platform, so
    safety comes from what is named rather than from what somebody remembered to exclude.

    Note what this does *not* settle: whether the bytes really are what Drive called them.
    Drive's `mimeType` is frequently whatever the uploading client claimed, so it chooses
    which ceiling applies and `admit_upload` proves the container from the bytes.
    """
    if mime_type == FOLDER_MIME:
        msg = "a folder is structure rather than content and has no bytes to index"
        raise DriveError(msg)
    if mime_type == SHORTCUT_MIME:
        msg = (
            "a shortcut has no bytes of its own; resolve it and place the target. "
            f"{A_SHORTCUT_IS_THE_WAY_OUT_OF_THE_FOLDER}"
        )
        raise DriveError(msg)
    if mime_type.startswith(GOOGLE_NATIVE_PREFIX):
        msg = (
            f"{mime_type!r} is a Google-native document, which has no bytes to fetch: it has "
            "to be exported, and which format it is exported to decides what the document "
            "looks like to a parser and to a model. Nothing here chooses that, so this "
            "connector indexes no native documents at all and the remedy is a reviewed "
            "export table rather than a default"
        )
        raise DriveError(msg)
    try:
        return MediaType(mime_type)
    except ValueError as exc:
        msg = (
            f"{mime_type!r} is not a type the knowledge layer accepts; accepted types are "
            f"{sorted(t.value for t in MediaType)}. An unrecognised type is refused rather "
            "than attempted, because nothing unrecognised should reach code that handles it"
        )
        raise DriveError(msg) from exc


def assert_indexable(file: DriveFile) -> None:
    """Refuse a file that must not be handed to the knowledge layer at all.

    Type first, because it is the refusal that names a remedy; then the bin, because a
    trashed file is a file somebody deleted and indexing it puts a document the company
    removed back in front of a model. A trashed file is deliberately not an error anywhere
    else: it is a state, it is projected, and it is what a reconciliation pass reads to know
    a file went away without having to guess.
    """
    media_type_for(file.mime_type)
    if file.trashed:
        msg = (
            f"{file.file_id!r} is in Drive's bin; it still exists and still answers a by-id "
            "read, which is exactly why indexing it would put a document somebody deleted "
            "back in front of a reader"
        )
        raise DriveError(msg)


def admit_from_drive(placed: InScopeFile, content: bytes, *, scanner: Scanner) -> ScannedContent:
    """The only route a Drive file's bytes take towards a parser (M11.6.7).

    Three gates, none of them written here, and that is the design. The parameter is an
    `InScopeFile`, so a file nobody placed inside the pinned folder cannot be spelled.
    `brain.knowledge.uploads.receive_upload` is the door: it proves the type from the bytes
    rather than believing Drive's `mimeType`, and it enforces the type's own size ceiling.
    `brain.knowledge.scanning.scan_for_parsing` is the scan gate: it recomputes the digest,
    scans, and issues the only value a parser accepts. Reimplementing any of the three would
    put the rule in two places, and the copy in the vendor file is the one nobody re-reads.

    One consequence is worth stating because it is a refusal a person meets. A Drive file
    whose name will not survive being quoted in a message is refused by
    `uploads.assert_safe_filename` rather than renamed, and the remedy is to rename it in
    Drive. That is the door's rule and not a second one; renaming somebody else's file
    silently is how the document they look for later is not there.

    What this does not do is make the document safe to read. See
    `NOTHING_HERE_MAKES_A_DOCUMENT_SAFE_TO_READ`.
    """
    assert_indexable(placed.file)
    received = receive_upload(
        filename=placed.file.name,
        declared_type=media_type_for(placed.file.mime_type).value,
        chunks=(content,),
    )
    return scan_for_parsing(received.upload, received.body, scanner=scanner)


# ---------------------------------------------------------------- the change subscription
def subscription(*, notify_within: timedelta, reconcile_every: timedelta) -> ChangeSubscription:
    """How Drive tells us a file in the pinned folder moved, and how a removal is learned.

    Two of the four fields are facts about the source and are fixed here. The cursor is
    `modifiedTime` inside the folder query, which is `UPDATED_SINCE` in this vocabulary, and
    `ID_SWEEP` is the only deletion check available to a folder-pinned read-only integration.
    That is a stronger statement here than it is for Freshdesk: a file can leave this
    connector's reach by being deleted, by being moved out of the folder, by being unshared,
    or by the credential losing access to the shared drive, and **only the first of those is
    a deletion anywhere**. All four look identical from inside the folder, which is an
    absence, so absence is the only sound test. See
    `brain.connectors.change_signal.A_CURSOR_CANNOT_SEE_A_DELETION`.

    The two intervals have no defaults and are the deployment's, matching `RefreshPromise`'s
    own refusal to hold one: how often a client's folder is polled and fully reconciled is a
    property of that installation, and a module-level number applied on a caller's behalf
    would be an inference presented as a declaration.
    """
    return ChangeSubscription(
        source=GOOGLE_DRIVE,
        entity=FILE,
        kind=ChangeSignal.UPDATED_SINCE,
        notify_within=notify_within,
        reconcile_every=reconcile_every,
        deletion_check=DeletionCheck.ID_SWEEP,
    )


# --------------------------------------------------------------------------- the manifest
#: What the model reads when it decides whether this tool answers the question. Inside the
#: pinned digest, and written to say the two things that are true of this connector and of
#: almost no other: it sees one folder, and it never returns a document's contents.
LIST_TOOL_DESCRIPTION: Final = (
    "List the files in the one Google Drive folder this connector is connected to: name, "
    "type, when each changed, which revision, and how each is shared in Drive. Metadata "
    "only. No tool here returns the contents of a file."
)

READ_TOOL_DESCRIPTION: Final = (
    "Read one Drive file's metadata by its file id, within the connected folder. Metadata "
    "only, and a file id rather than a path, because a path changes when anybody moves or "
    "renames the file."
)


def manifest(
    connection: DriveConnection, *, ref: SecretRef, version: str = VERSION
) -> ConnectorManifest:
    """Everything this connector declares, for one deployment (M11.6.7).

    **The scope names one folder, and that is the load-bearing declaration.** It refuses a
    credential pointed at another folder, and unlike Freshdesk's helpdesk pin it narrows
    something *inside* the account rather than merely identifying which account. What it does
    not do is make Google enforce it: a Drive service account reaches everything it was
    granted, so the pin is our restriction and the checks in `place` and
    `assert_row_is_in_the_folder` are what make it real.
    `brain.connectors.transports.THE_SANDBOX_IS_NOT_IN_THIS_MODULE` draws the same line, and
    the honest statement is the same: somebody chose this, and choosing it is not the same as
    it having been enforced.

    **`ceiling` is empty and that is deliberate rather than forgotten.** See
    `THERE_IS_NO_MEASURED_CEILING_HERE`.

    **Both tools declare SERVICE identity**, which is the honest reading of a shared service
    account: Drive is not enforcing any of our people's permissions on our behalf, so ours
    are the only ones there are. That is exactly the case `brain.tools.registry` refuses to
    register without a scope predicate, and `DriveConnection.visibility_predicate` is it.

    **Neither tool returns a file's bytes**, which is checked here rather than promised: the
    tool list is two metadata reads and there is no third. See
    `NOTHING_HERE_MAKES_A_DOCUMENT_SAFE_TO_READ`.

    The binding is read-only by not saying otherwise, which is `CredentialBinding`'s default
    and the whole of `A_WRITE_GRANT_NAMES_SOMEBODY`. A connector that could write to a
    client's Drive is a different connector, approved by somebody named, and this is not it.
    """
    assert_selector_covers_the_mapping()
    return ConnectorManifest(
        name=GOOGLE_DRIVE,
        version=version,
        transport=TransportKind.REST,
        scope=connection.scope(),
        credential=CredentialBinding(ref=ref, mode=AccessMode.READ_ONLY),
        tools=(
            ToolDeclaration(
                name="google_drive.list_folder",
                description=LIST_TOOL_DESCRIPTION,
                entity=FILE,
                side_effect=SideEffect.NONE,
                identity_mode=IdentityMode.SERVICE,
            ),
            ToolDeclaration(
                name="google_drive.read_file",
                description=READ_TOOL_DESCRIPTION,
                entity=FILE,
                side_effect=SideEffect.NONE,
                identity_mode=IdentityMode.SERVICE,
            ),
        ),
        projections=(file_projection(connection),),
        ceiling="",
    )
