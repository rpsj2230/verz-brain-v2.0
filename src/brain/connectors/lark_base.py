"""Lark Base: a hundred calls a minute for the whole company, and a Base is not a spreadsheet.

Two facts shape everything in this module, and both are constraints rather than preferences.

**A hundred requests a minute, and Lark does not raise it.** `tests/fixtures/cassettes.py`
records that as `raisable=False` and `brain.ops.limits` repeats the number with the same
verdict: it is 1.67 calls a second for the entire tenant, permanently, shared by every
question every one of 126 people asks. That makes the budget a first-class value here rather
than a limiter somebody else applies. One question walking a large table to the end would
spend the whole minute and every colleague asking anything in the next sixty seconds would be
refused, with nothing in their answer explaining why. So a walk is handed a `MinuteBudget`,
it stops when the budget is gone, and it says what it did not fetch. See
`ONE_QUESTION_MUST_NOT_SPEND_THE_COMPANYS_MINUTE`.

**A Base is a Base, not a sheet of cells.** It has tables; a table has typed fields and its
own record ids. Reading it as a grid loses both. A connection is therefore scoped to one Base
*and* one table, as a single selector, because a scope naming only the Base reaches every
table in it and `ConnectorScope.admits` is exact membership. A record is identified by its
`record_id` and never by its position, because a row number is reused the moment somebody
sorts a view.

**A field's type decides what it can become, and four types cannot become anything
honestly.** A link field is a list of record ids in another table this connector is not
scoped to; a lookup is somebody else's field seen from here; an attachment is a file token
and an expiring URL; a person is an object carrying an email. `str()` over any of them
produces a value that reads like data and joins to nothing, so `FieldBinding` refuses them at
declaration and names the remedy. A formula is the interesting middle case: it is a scalar on
the wire and it is recomputed by the Base on every read, so an editor rewriting the formula
changes every row's value with no record-level modification event anywhere. It may be read
live and may never be projected. See `WHAT_A_BASE_FIELD_BECOMES`.

**The kind refuses the projection, not the field's name.** `brain.core.projection` matches its
permanent denylist against a field *name*, and the name in a projected record is ours rather
than Lark's: a Base column called `Mobile` bound to a target called `contact_line` walks
straight past `is_forbidden`. So a phone, a person and a location are refused by their Lark
type, which the author does not choose. See `A_DENYLIST_MATCHED_BY_NAME_IS_RENAMED_PAST`.

**The visibility predicate is stored and the Base's own sharing settings are not consulted.**
Who a Base is shared with is a resolved list held by Lark, it is not reachable through
`base:record:read`, and it is stale for us the moment somebody moves department.
`brain.connectors.manifest` says the same thing at greater length and refuses the two shapes a
resolved ACL arrives in, so this module supplies no predicate of its own and takes one from
the deployment.

**Absent, refused and unreachable stay three answers, and Lark makes that harder than most.**
It returns `code: 0` inside a 200 for success and a non-zero code inside a 200 for failure, so
a connector reading only the HTTP status records a permission refusal as an empty table. That
is the recording `LARK-200-code-permission` exists for. `assert_lark_answered` reads the body's
code before anything projects it.

**A page and one record are read by different functions, because they are different
envelopes.** A page arrives under `data.items` with `has_more` beside it; one record arrives
under `data.record` with no continuation of any kind, so the page reader's own end-signal
refusal fires on a perfectly good single read and reports a healthy source as malformed.
`read_page` and `read_record` each refuse the other's cursor. The single read is also the only
call in this module that spends the budget itself: a page is read inside a walk that owns the
budget, and a single read has no loop above it to do the counting. See
`A_SINGLE_READ_AND_A_PAGE_ARE_DIFFERENT_ENVELOPES`.

Rejected, and each looks tidier:

*Spelling the Base identifier `app_token` and the paging cursor `page_token`, which are
Lark's own words for them.* `contract.CREDENTIAL_ATTRIBUTE_RE` matches attribute names ending
in `_token` and would refuse the declarations outright, and it is right to match by name: a
stored credential is nearly always a `str`, so a type-only rule would pass `api_key: str`. One
is a document id that appears in a browser address bar and the other is an opaque marker into
a listing, and neither is a secret. They are spelled `base_id` and `continuation` on every
attribute here, and the vendor's words survive only as a placeholder in a URL template and a
key in a decoded body, neither of which is something this connector holds. See
`A_VENDOR_IDENTIFIER_IS_NOT_A_CREDENTIAL`.

*Naming each Base column in a `transports.FieldMapping` source path, the way the Freshdesk
connector names ticket fields.* It cannot be done: a Base column is a human-authored label
(`Hours Remaining`, `Contract Value`), and `transports._SOURCE_PATH_RE` admits identifiers
only. Renaming the label to fit would mean reading a different column from the one the author
named. So the REST mapping names the envelope's two parts, the record id and the cell
container, and the allowlist over the cells is `FieldBinding`, applied one layer in by
`decode_row`. The property `brain.connectors.rest.WHAT_THE_MAPPING_DOES_NOT_NAME_DOES_NOT_ARRIVE`
protects is kept: a decoded record is a fresh dictionary built from declared bindings, so a
column added to the Base tomorrow arrives nowhere.

*Deciding when to stop from the `total` the page carries.* `total` is a count taken while the
table is being edited, and a walk sized from it reads a page that no longer exists or stops
one short. `has_more` is the source's own end signal and it is the only thing consulted.

*A table of Lark business codes, mapping each to an outcome.* A table needs a default for the
code nobody has met, and the default that reads as safe ("unavailable, try again") turns a
permanent refusal into a retry loop against a ceiling that cannot be raised. An unrecognised
non-zero code is a refusal, which `throttle.is_retryable` will not retry. See
`AN_UNKNOWN_CODE_IS_A_REFUSAL_RATHER_THAN_A_RETRY`.

*Writing a sentence for a partial answer.* `federation.PartialAnswer.notice` already decides
when a source may be named, and a second sentence here would be a second disclosure rule. The
cost is that a budget stop is described as not having reached Lark, which is not literally
what happened; the wording that distinguishes the two would tell the asker about our internal
budget, which is a fact about us rather than an answer to their question.

Scope: domain logic. Nothing here opens a socket, resolves a name or reads a clock. The
reader, the fetched-at stamp, every interval and every budget are parameters, for the reason
`brain.models.routing.CircuitBreaker` gives about `now`.

Task ids: M11.6.3
"""

from __future__ import annotations

import enum
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Final, Protocol

from brain.connectors.change_signal import ChangeSubscription, DeletionCheck
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
)
from brain.connectors.federation import FailureReason, PartialAnswer, SourceFailure
from brain.connectors.manifest import (
    ChangeSignal,
    ConnectorManifest,
    FieldShape,
    HotUse,
    ProjectedEntity,
    ProjectedField,
    ToolDeclaration,
)
from brain.connectors.projection import ProjectedRecord, ProjectedValue
from brain.connectors.rest import OperationSpec, ParameterSpec, RestOperation, assert_maps_only
from brain.connectors.throttle import CallOutcome, UnmeasuredSourceError, classify
from brain.connectors.transports import FieldMapping, RestTransport, SourceRecord, normalise
from brain.core.envelope import OBJECT_NAME_PATTERN, IdentityMode, TypedResult
from brain.core.errors import Degraded
from brain.core.projection import MAX_LABEL_CHARS
from brain.core.scope import Scope
from brain.ops.limits import MINUTE_SECONDS, ConnectorLimit, connector_ceiling, principal_share_of

# ------------------------------------------------------------------ written-down reasons
#: Why the ceiling shapes the connector instead of being something an operator escalates.
THE_CEILING_IS_A_DESIGN_CONSTRAINT = (
    "A hundred requests a minute, and Lark's own documentation says it cannot be raised, so "
    "there is no plan to buy and no ticket to open. That makes it different in kind from a "
    "starting tier: it is an input to the design rather than a temporary inconvenience, and "
    "it is why this connector reads one page at a time against a budget instead of walking a "
    "table because the table is there. `brain.ops.limits` records the number and the verdict "
    "together so that an operator reading a console row does not go looking for an upgrade "
    "button that does not exist."
)

#: Why one question is budgeted well below the ceiling it runs against.
ONE_QUESTION_MUST_NOT_SPEND_THE_COMPANYS_MINUTE = (
    "The ceiling belongs to the tenant, not to the asker. A question that walks a large table "
    "to the end spends the whole minute, and every colleague who asks anything in the "
    "following sixty seconds is refused with nothing in their answer explaining why, which "
    "reads to all of them as the system being broken. So one question is allowed a share of "
    "the minute rather than the minute, the share is `brain.ops.limits.principal_share_of` "
    "rather than a number invented here, and a walk that reaches the end of its share stops "
    "and says what it did not fetch. A partial answer that says so is worth more than a "
    "complete one that cost everybody else theirs."
)

#: Why a Base field's type decides what it may become, and why four types become nothing.
WHAT_A_BASE_FIELD_BECOMES = (
    "A Base cell is typed, and four of those types hold something that is not a value. A link "
    "and a lookup are lists of record ids in another table, which this connector is not "
    "scoped to and cannot resolve; an attachment is a file token and a URL that expires; a "
    "person is an object carrying a name and an email address. Rendering any of them with "
    "str() produces a string that reads like data, sorts wrongly, joins to nothing and cannot "
    "be told apart from a real value by anybody reading the answer. So they are refused at "
    "declaration, where the author can name a different column, rather than flattened at "
    "ingest, where nobody sees it happen. A multi-select is refused for the narrower reason "
    "`projection.A_NESTED_OBJECT_IS_NOT_ONE_FIELD` gives: several values wearing one field's "
    "name is how a twelve-field cap is defeated politely."
)

#: Why a formula is readable live and never projected.
A_FORMULA_CHANGES_EVERY_ROW_WITH_NO_EVENT = (
    "A formula column is not stored by the Base; it is computed when the record is read. So "
    "an editor who rewrites the formula changes the value of every row at once, and no record "
    "is modified by it: there is no per-record change event, and an updated-since cursor never "
    "mentions any of them again. A projected formula is therefore a value that will be "
    "filtered, sorted and counted on as current for ever with nothing anywhere reporting that "
    "it stopped being true, which is exactly what `manifest.NO_SIGNAL_MEANS_NO_PROJECTION` "
    "refuses. Reading one live is fine, because a live read recomputes it at the source."
)

#: Why the field's Lark type refuses a projection that its name would have allowed.
A_DENYLIST_MATCHED_BY_NAME_IS_RENAMED_PAST = (
    "`brain.core.projection.is_forbidden` matches a field name against the permanent "
    "denylist, and the name in a projected record is ours rather than the source's: a "
    "connector binds a Base column called Mobile to a target called contact_line and the "
    "denylist never sees a phone number. That is not evasion, it is what naming a field looks "
    "like. The defence is to refuse by the source's own type, which the author does not "
    "choose: a phone, a person and a location project nothing whatever the binding calls "
    "them. The denylist still runs, at manifest review and again at ingest; this closes the "
    "one gap that a rename opens."
)

#: Why nothing here is spelled with the vendor's own word for an identifier.
#:
#: The constant itself is named around the point rather than after it: an earlier spelling
#: ended in the vendor's word and ruff's own S105 refused the assignment, by name, on a value
#: that is a paragraph of prose. Three separate rules in this build match a credential by the
#: name beside it, which is the strongest available argument that the name is the thing to fix.
A_VENDOR_IDENTIFIER_IS_NOT_A_CREDENTIAL = (
    "Lark calls the identifier of a Base an app token and the cursor into a listing a page "
    "token, and neither is a credential: the first is a document id that appears in the "
    "address bar of anybody who opens the Base, and the second is an opaque marker saying "
    "where a listing left off. `contract.CREDENTIAL_ATTRIBUTE_RE` matches any attribute name "
    "ending that way and would refuse the declarations outright, and it is right to match by "
    "name rather than by type, because a stored credential is nearly always a str and a "
    "type-only rule would pass api_key: str while refusing an honest lease. The two ways out "
    "are exempting this module from the guard or not naming an identifier with a credential "
    "word, and only the second leaves the guard working everywhere. So the attributes are "
    "base_id and continuation, and the vendor's words survive only where they belong, as a "
    "placeholder in a URL template and a key in a decoded body, neither of which is something "
    "this connector holds between calls."
)

#: Why the Base's own sharing settings are not this system's permission model.
A_SHARING_SETTING_IS_NOT_A_PERMISSION_MODEL = (
    "A Base carries its own sharing: who the document was shared with, which group has edit "
    "rights, whether the link is open to the organisation. That is a resolved list held by "
    "Lark, it is not reachable through the base:record:read scope this bot holds, and copying "
    "it would freeze one afternoon's membership into our database. The projection stores the "
    "source's visibility *predicate* and evaluates it against the live entitlement set, so "
    "somebody moving department gets a different row set on their next question with zero "
    "writes and zero invalidation. This module supplies no predicate of its own: an "
    "unrestricted one is refused by `manifest.ProjectedEntity`, and a default supplied here "
    "would be one client's ownership rule applied to another client's records."
)

#: Why a non-zero code inside an HTTP 200 is the case this connector is built around.
A_ZERO_CODE_INSIDE_A_TWO_HUNDRED_IS_THE_ONLY_SUCCESS = (
    "Lark answers 200 for a permission failure and puts the failure in the body's code field. "
    "A connector that checks the status and then projects the body finds no items, returns no "
    "records, and has just recorded 'this table is empty' as a fact about the company. The "
    "recorded exchange LARK-200-code-permission is that exchange, and the order here is "
    "load-bearing: the code is read before anything is projected, so a refusal is a refusal "
    "rather than a malformed response, and an empty table stays a thing only an actually "
    "empty table can produce."
)

#: Why an unrecognised business code is treated as a refusal rather than as an outage.
AN_UNKNOWN_CODE_IS_A_REFUSAL_RATHER_THAN_A_RETRY = (
    "Mapping Lark's business codes to outcomes would need a table, and a table needs a "
    "default for the code nobody has met yet. The default that reads as safe is 'the source "
    "is unwell, try again', and against a ceiling that cannot be raised that turns one "
    "permanent refusal into a retry loop which spends the whole tenant's minute achieving "
    "nothing. So an unrecognised non-zero code is a refusal, `throttle.is_retryable` declines "
    "to retry a refusal, and the one code the recordings actually carry is named as a "
    "constant so the message can say what it means rather than guessing."
)

#: Why a caller's own limit contributes no failure to a partial answer.
A_CALLERS_OWN_LIMIT_IS_NOT_A_SOURCE_FAILURE = (
    "A partial answer's failure list is what `federation.PartialAnswer.notice` turns into 'I "
    "could not reach this source', and a caller who asked for ten records out of a table "
    "holding four hundred was answered exactly. Recording that as a truncation says the source "
    "refused to return more, which is a different fact with a different remedy and is not what "
    "happened; it also puts a sentence about an unreachable system in front of somebody whose "
    "question was answered in full. The reading still carries `stopped_at_caller_limit`, a "
    "false `is_all_of_them` and the result's own truncated flag, so a caller that needs to say "
    "'this is the ten you asked for' has all three, and none of the three is a failure."
)

#: Why one record and a page of them are read by different functions.
A_SINGLE_READ_AND_A_PAGE_ARE_DIFFERENT_ENVELOPES = (
    "A page arrives under data.items with has_more beside it; one record arrives under "
    "data.record with no continuation of any kind. Handing a single read to the page reader "
    "therefore fails on the missing has_more, which reports a perfectly good reply as a "
    "malformed one and sends whoever reads the error looking for a source that is not broken. "
    "The two endpoints already carry their own response shapes for that reason, and the "
    "endpoint recorded on the cursor is what decides which function may be handed it."
)

#: Why a walk asks the source whether there is more and never works it out.
THE_END_SIGNAL_IS_HAS_MORE_AND_NEVER_THE_TOTAL = (
    "A page carries both has_more and a total, and only one of them is an end signal. The "
    "total is a count taken while the table is being edited, so a walk sized from it reads a "
    "page that no longer exists or stops one page short and reports the remainder as absent. "
    "A body that omits has_more is refused rather than read as finished: 'there is no more' "
    "and 'the source did not say' are two different facts, and defaulting to the first is how "
    "an incomplete answer comes to read as a complete one."
)


# ---------------------------------------------------------------------------- the names
#: The connector's name, and the key `brain.ops.limits` records the verified ceiling under.
#: The same string in both places deliberately: `throttle.limits_for` looks up
#: `manifest.ceiling` rather than `manifest.name`, so a deployment installed under a client's
#: own name still has to point at this one or it runs against no measured limit at all.
LARK_BASE: Final = "lark_base"

#: This connector's own version, which moves when anything in the manifest moves. An upgrade
#: is recognised by a version change, so editing a binding without touching this leaves a
#: pinned digest disagreeing with a connector nobody upgraded.
VERSION: Final = "1.0.0"

#: What the field mapping names its specification. A reference and not a document, for the
#: reason `transports.RestTransport` gives: a vendor spec moves on the vendor's schedule and
#: embedding it would put every unrelated edit inside the pinned digest.
SPEC_REF: Final = "lark.bitable.v1"

#: The two hosts a Lark tenant is reached at. A closed set, because pointing a Base connector
#: at a host that is not Lark's is the mistake a copied configuration makes, and a refusal at
#: connect is read by the person installing it. The cost if Lark adds a third cloud is a build
#: failure here, edited in one place, which is the direction to be wrong in.
LARK_HOSTS: Final[frozenset[str]] = frozenset({"open.larksuite.com", "open.feishu.cn"})

#: The page size this connector asks for, which is the size the recorded exchange uses. Not
#: assumed larger: the recordings are the only verified evidence available before anybody
#: holds a credential, and a page size the endpoint silently clamps is the failure
#: `brain.connectors.freshdesk.A_CLAMPED_PAGE_SIZE_READS_AS_THE_LAST_PAGE` describes.
PAGE_SIZE: Final = 100

#: The only body code that means the call succeeded. Every other value is the source
#: declining, inside an HTTP 200. See `A_ZERO_CODE_INSIDE_A_TWO_HUNDRED_IS_THE_ONLY_SUCCESS`.
SUCCESS_CODE: Final = 0

#: The one business code the recordings carry, so a message can say what it means. Used to
#: word the refusal, never to decide whether there was one: see
#: `AN_UNKNOWN_CODE_IS_A_REFUSAL_RATHER_THAN_A_RETRY`.
PERMISSION_DENIED_CODE: Final = 91403

#: What a page's `total` says when the source did not state one.
TOTAL_UNSTATED: Final = -1

#: How long to wait when a source refuses on volume and says nothing about when. A minute,
#: which is the window's own length rather than a number invented here: against a fixed
#: per-minute ceiling the earliest the window can have room is the next minute, so this is
#: measured rather than guessed. `brain.ops.limits.backoff_seconds` lengthens it from here
#: when refusals repeat, and this module does not restate that arithmetic.
WAIT_WHEN_UNSTATED: Final = MINUTE_SECONDS

#: How Lark tells us a projected record moved. Fixed rather than configurable: a Base can
#: raise events, but only through an event subscription and a scope this bot has not been
#: granted, so declaring WEBHOOK would be declaring somebody else's configuration as our
#: guarantee. What the API itself offers is a last-modified cursor, and `subscription` below
#: carries the consequence, which is that a cursor cannot see a deletion.
CHANGE_SIGNAL: Final = ChangeSignal.UPDATED_SINCE

#: Targets the record envelope already owns. A binding to one of these would be overwritten by
#: `transports.normalise`, which reads the id and refuses to carry a second `entity`, so the
#: value would be silently discarded rather than stored.
RESERVED_TARGETS: Final[frozenset[str]] = frozenset({"id", "entity"})

#: What a Base, a table, a view and a record are called. The prefixes are the only structure
#: the vendor's identifiers carry, and checking them catches the copied-configuration mistake
#: where a Base id is pasted where a table id belongs. A Base id has no prefix in the newer
#: format, so only its alphabet and length are checked.
_BASE_ID_RE: Final = re.compile(r"^[A-Za-z0-9]{8,64}$")
_TABLE_ID_RE: Final = re.compile(r"^tbl[A-Za-z0-9]{4,32}$")
_VIEW_ID_RE: Final = re.compile(r"^vew[A-Za-z0-9]{4,32}$")
_RECORD_ID_RE: Final = re.compile(r"^rec[A-Za-z0-9]{4,32}$")

_NAME_RE: Final = re.compile(OBJECT_NAME_PATTERN)


def ceiling() -> ConnectorLimit:
    """The verified ceiling this connector runs against. Looked up, never restated.

    `brain.ops.limits` owns the number and the verdict on whether anything moves it, and this
    is the same lookup `throttle.ceiling_for` performs for a caller holding a manifest. A
    module that stated 100 itself would be a second figure to keep true, and the copy that
    drifts is the one a budget is sized from. See `THE_CEILING_IS_A_DESIGN_CONSTRAINT`.
    """
    found = connector_ceiling(LARK_BASE)
    if found is None:  # pragma: no cover - the ceiling is registered beside the name
        msg = (
            f"connector {LARK_BASE!r} names no verified ceiling; inventing one produces a "
            "number that looks measured and is not"
        )
        raise UnmeasuredSourceError(msg)
    return found


def fair_share_per_minute() -> int:
    """How many calls one question may make in a minute, out of the tenant's hundred.

    `brain.ops.limits.principal_share_of` rather than arithmetic here, so this cannot come to
    a different conclusion from the limiter that will refuse the call anyway. The property
    that matters is that it is strictly below the ceiling: at the ceiling one question can
    take all of it, and `ONE_QUESTION_MUST_NOT_SPEND_THE_COMPANYS_MINUTE` stops being true.
    """
    return principal_share_of(ceiling().per_minute)


# --------------------------------------------------------------------- the budget (M11.6.3)
class LarkBaseBudgetError(Exception):
    """A call was made against a budget that had nothing left in it.

    Deliberately not a `Degraded`: nothing is wrong with Lark, and telling somebody a source
    was unreachable when we chose to stop would be a wrong reason attached to a right outcome.
    Deliberately not a `ConnectorContractError` either: the declaration was fine and the
    refusal is about this run.

    It is a backstop rather than the ordinary path. A walk asks the budget before each page
    and stops honestly, producing a `TableReading` that says what it did not fetch. This is
    what a caller gets for spending without asking, and for the one case that must never
    degrade quietly: a budget already exhausted before the first call, where returning an
    empty result would report our own arithmetic as an empty table.
    """


@dataclass(frozen=True)
class MinuteBudget:
    """What one question may still spend of the tenant's minute.

    Immutable, like every state machine in this package: `spend` returns the next budget
    rather than mutating this one, so a caller cannot lose track of what a branch spent.

    `allowance` is required and has no default. The share is available from
    `fair_share_budget()`, and making it a default here would let a caller construct a budget
    without deciding what fraction of everybody else's minute this question is worth.
    """

    allowance: int
    spent: int = 0

    def __post_init__(self) -> None:
        if self.allowance < 1:
            msg = (
                f"a budget of {self.allowance} calls cannot fetch a first page, so every "
                "question against it reports an empty table it never read"
            )
            raise ConnectorContractError(msg)
        if self.spent < 0:
            msg = "a budget cannot have spent a negative number of calls"
            raise ConnectorContractError(msg)
        # Strictly below the tenant's ceiling, which is the bound `principal_share_of` holds
        # itself to and for the same reason: a budget *equal* to the ceiling is one question
        # allowed to spend the whole company's minute, which is the thing the constant beside
        # it says must not happen. The `max(1, ...)` carries that function's own ceiling-of-one
        # case: nothing can be shared out of a ceiling of one, and a bound of zero would refuse
        # every budget including the fair share.
        limit = ceiling().per_minute
        most = max(1, limit - 1)
        if self.allowance > most:
            msg = (
                f"this question is budgeted {self.allowance} calls out of the tenant's {limit} "
                f"a minute, which is the minute rather than a share of it; at most {most}. "
                f"{ONE_QUESTION_MUST_NOT_SPEND_THE_COMPANYS_MINUTE}"
            )
            raise ConnectorContractError(msg)

    @property
    def remaining(self) -> int:
        return max(0, self.allowance - self.spent)

    @property
    def is_exhausted(self) -> bool:
        return self.remaining <= 0

    def spend(self) -> MinuteBudget:
        """Record one call, or refuse it.

        Refusing raises rather than returning a flag, which is the choice
        `federation.BudgetState.spend` makes and for the same reason: a flag is checked by the
        caller that remembered to, and a call made past an exhausted budget has already taken
        somebody else's allowance by the time anybody reads the flag.
        """
        if self.is_exhausted:
            msg = (
                f"this question has already made {self.spent} calls to {LARK_BASE}, which is "
                f"its whole allowance of {self.allowance} out of the tenant's "
                f"{ceiling().per_minute} a minute"
            )
            raise LarkBaseBudgetError(msg)
        return replace(self, spent=self.spent + 1)


def fair_share_budget() -> MinuteBudget:
    """One question's budget: a share of the minute, leaving the rest for everybody else.

    A named constructor rather than a default on `MinuteBudget`, because the fraction is the
    decision and a default is the thing nobody reads. The share itself is
    `brain.ops.limits.principal_share_of`, so a change to the platform's fairness rule reaches
    this connector without anybody editing it.
    """
    return MinuteBudget(allowance=fair_share_per_minute())


# ------------------------------------------------------------------------ the endpoints
class Endpoint(enum.StrEnum):
    """The two Base operations this connector reads.

    Closed, and small on purpose. Every member is an operation somebody decided we need, with
    a mapping reviewed beside it; a third is a manifest edit that moves the pinned digest,
    which is the visibility the architecture asks for. Both are reads: the credential holds
    `base:record:read` and nothing wider, so a write endpoint could be declared and could not
    be called.
    """

    #: A page of records from the scoped table.
    LIST_RECORDS = "list_records"
    #: One record of the scoped table, by its record id.
    GET_RECORD = "get_record"


def _query(name: str, *, required: bool = False) -> ParameterSpec:
    return ParameterSpec(name=name, location="query", required=required)


def _path(name: str) -> ParameterSpec:
    return ParameterSpec(name=name, location="path", required=True)


#: Every endpoint's specification. Total over `Endpoint`, and `spec_for` is the only way in,
#: so a member added without a row fails in front of whoever added it rather than being
#: classified by a default. `MappingProxyType` for the reason `brain.ops.limits` uses it on
#: its own registries: a module-level dict is a table any importer can edit at run time.
#:
#: The path placeholder is `app_token` and one query parameter is `page_token`, which are
#: Lark's own words. Both are strings in a URL here and neither is an attribute of anything:
#: see `A_VENDOR_IDENTIFIER_IS_NOT_A_CREDENTIAL`.
ENDPOINT_SPECS: Final[MappingProxyType[Endpoint, OperationSpec]] = MappingProxyType(
    {
        Endpoint.LIST_RECORDS: OperationSpec(
            operation_id="listBitableRecords",
            method="get",
            path="/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            parameters=(
                _path("app_token"),
                _path("table_id"),
                _query("page_size"),
                _query("page_token"),
                _query("view_id"),
            ),
            records_at="data.items",
            returns_list=True,
        ),
        Endpoint.GET_RECORD: OperationSpec(
            operation_id="getBitableRecord",
            method="get",
            path=("/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"),
            parameters=(_path("app_token"), _path("table_id"), _path("record_id")),
            # One record, under `data.record`, and the envelope carries no has_more. A single
            # read that borrowed the list endpoint's `data.items` would find nothing and
            # report the record as absent, which is why the two carry their own shapes.
            records_at="data.record",
            returns_list=False,
        ),
    }
)


def spec_for(endpoint: Endpoint) -> OperationSpec:
    """The operation specification for one endpoint.

    A function rather than a bare subscript, so the totality of `ENDPOINT_SPECS` is asserted
    in one place and no caller invents a fallback when a lookup misses. A missing row is a
    contract error rather than a default, because the default for "where do the records live"
    is the one that reads an empty array.
    """
    try:
        return ENDPOINT_SPECS[endpoint]
    except KeyError as exc:  # pragma: no cover - the totality test keeps this unreached
        msg = (
            f"{endpoint!r} has no operation specification, so nothing knows where its records "
            "live or how it pages; declare it before anything reads it"
        )
        raise ConnectorContractError(msg) from exc


# ------------------------------------------------------------- what a Base field is (M11.6.3)
class Representation(enum.StrEnum):
    """What a Base cell's value is, once you look at it rather than at its label.

    The distinction that decides whether a field can be read at all. Closed, and every member
    has a different answer and a different remedy, which is why it is an enum rather than a
    pair of booleans.
    """

    #: One value that stands on its own: a string, a number, a boolean, an instant.
    SCALAR = "scalar"
    #: A list or an object. Several values wearing one field's name.
    CONTAINER = "container"
    #: The value names something held somewhere else: a record in another table, a file in
    #: Lark's own store. The name is not the thing, and we are not scoped to the thing.
    ELSEWHERE = "elsewhere"
    #: The Base computes it when the record is read, so it has no per-record change event.
    RECOMPUTED = "recomputed"


class FieldKind(enum.StrEnum):
    """The Base field types this connector knows about.

    Named rather than numbered in our own vocabulary, with Lark's numeric type carried beside
    it in `KIND_FACTS`. A connector declaring `kind=17` would be a manifest nobody can review,
    and the number is the vendor's spelling rather than the meaning.
    """

    TEXT = "text"
    NUMBER = "number"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"
    DATE = "date"
    CHECKBOX = "checkbox"
    PERSON = "person"
    PHONE = "phone"
    URL = "url"
    ATTACHMENT = "attachment"
    LINK = "link"
    LOOKUP = "lookup"
    FORMULA = "formula"
    LOCATION = "location"
    CREATED_TIME = "created_time"
    MODIFIED_TIME = "modified_time"
    AUTO_NUMBER = "auto_number"


@dataclass(frozen=True)
class KindFacts:
    """What one Base field type is, what it may become, and why it may not become more.

    `shapes` is the whole of the projection decision and it is empty for most kinds. A shape
    is one of the five pointer shapes in the tier table, and a kind offering none of them may
    be read live and never stored. The tuple rather than a single member is deliberate: a text
    column is a label in one table and a client code in another, and which it is here is the
    author's declaration rather than something a type table can know.
    """

    kind: FieldKind
    #: Lark's own numeric field type, carried so an operator can match this against the Base's
    #: field list without a translation table living in somebody's head.
    api_type: int
    representation: Representation
    #: The pointer shapes a binding of this kind may declare. Empty means never projected.
    shapes: tuple[FieldShape, ...]
    #: One sentence for a review. Never the whole argument, which is in the reason constants
    #: above; enough that a reviewer knows which one to go and read.
    note: str

    @property
    def may_be_read(self) -> bool:
        """Whether a value of this kind can be turned into one honest value."""
        return self.representation in (Representation.SCALAR, Representation.RECOMPUTED)

    @property
    def may_be_projected(self) -> bool:
        return bool(self.shapes)


#: Every member of `FieldKind`, and the mapping is total on purpose. A `dict.get` with a
#: default would let a type be added and silently classified as whatever the default said,
#: and for a question like "can this be stored" the convenient default is the one that stores
#: an attachment token. A test asserts the table covers the enum, so adding a member fails the
#: build in front of whoever added it.
KIND_FACTS: Final[MappingProxyType[FieldKind, KindFacts]] = MappingProxyType(
    {
        FieldKind.TEXT: KindFacts(
            kind=FieldKind.TEXT,
            api_type=1,
            representation=Representation.SCALAR,
            shapes=(FieldShape.LABEL, FieldShape.JOIN_KEY, FieldShape.IDENTIFIER),
            note=(
                "one string; a rich-text column returns a list of segments instead and is "
                "refused at decode rather than joined, because joining invents a value"
            ),
        ),
        FieldKind.NUMBER: KindFacts(
            kind=FieldKind.NUMBER,
            api_type=2,
            representation=Representation.SCALAR,
            # No shape, and this is the deliberate one. A measure is data rather than a
            # pointer, and none of the five pointer shapes describes one; a connector that
            # projected it would be storing the value the question is about. The cost is that
            # the fast lane cannot count on a Base number, and the remedy is a single-select
            # or a local field rather than a wider projection.
            shapes=(),
            note=(
                "one number, read live; a measure is data rather than a pointer and none of "
                "the five pointer shapes describes one"
            ),
        ),
        FieldKind.SINGLE_SELECT: KindFacts(
            kind=FieldKind.SINGLE_SELECT,
            api_type=3,
            representation=Representation.SCALAR,
            shapes=(FieldShape.STATUS,),
            note="one option from a closed list, which is what a status enum is",
        ),
        FieldKind.MULTI_SELECT: KindFacts(
            kind=FieldKind.MULTI_SELECT,
            api_type=4,
            representation=Representation.CONTAINER,
            shapes=(),
            note=(
                "a list of options; several values wearing one field's name, which is how a "
                "twelve-field cap is defeated politely"
            ),
        ),
        FieldKind.DATE: KindFacts(
            kind=FieldKind.DATE,
            api_type=5,
            representation=Representation.SCALAR,
            shapes=(FieldShape.TIMESTAMP,),
            note="milliseconds since the epoch, not ISO; decoded here rather than passed on",
        ),
        FieldKind.CHECKBOX: KindFacts(
            kind=FieldKind.CHECKBOX,
            api_type=7,
            representation=Representation.SCALAR,
            shapes=(FieldShape.STATUS,),
            note="a boolean, which is a status enum with two members",
        ),
        FieldKind.PERSON: KindFacts(
            kind=FieldKind.PERSON,
            api_type=11,
            representation=Representation.CONTAINER,
            shapes=(),
            note=(
                "a list of objects carrying a name and an email address; a container, and one "
                "whose contents are on the permanent denylist whatever a binding calls them"
            ),
        ),
        FieldKind.PHONE: KindFacts(
            kind=FieldKind.PHONE,
            api_type=13,
            representation=Representation.SCALAR,
            # A scalar that is never projected, and the refusal is by type rather than by
            # name. See `A_DENYLIST_MATCHED_BY_NAME_IS_RENAMED_PAST`.
            shapes=(),
            note="a phone number; read live, never stored, whatever the binding calls it",
        ),
        FieldKind.URL: KindFacts(
            kind=FieldKind.URL,
            api_type=15,
            representation=Representation.CONTAINER,
            shapes=(),
            note="an object of a link and its display text; two values under one name",
        ),
        FieldKind.ATTACHMENT: KindFacts(
            kind=FieldKind.ATTACHMENT,
            api_type=17,
            representation=Representation.ELSEWHERE,
            shapes=(),
            note=(
                "file tokens and URLs that expire; the token is not the file and the URL "
                "stops working, so either one stored is a value that quietly becomes wrong"
            ),
        ),
        FieldKind.LINK: KindFacts(
            kind=FieldKind.LINK,
            api_type=18,
            representation=Representation.ELSEWHERE,
            shapes=(),
            note=(
                "record ids in another table this connector is not scoped to; a pointer into "
                "something nobody connected, which joins to nothing"
            ),
        ),
        FieldKind.LOOKUP: KindFacts(
            kind=FieldKind.LOOKUP,
            api_type=19,
            representation=Representation.ELSEWHERE,
            shapes=(),
            note=(
                "another table's field seen from here; its type is that field's type, so it "
                "can be an attachment or a person without this table saying so"
            ),
        ),
        FieldKind.FORMULA: KindFacts(
            kind=FieldKind.FORMULA,
            api_type=20,
            representation=Representation.RECOMPUTED,
            shapes=(),
            note=(
                "computed when the record is read, so rewriting the formula changes every row "
                "with no per-record event; readable live, never projected"
            ),
        ),
        FieldKind.LOCATION: KindFacts(
            kind=FieldKind.LOCATION,
            api_type=22,
            representation=Representation.CONTAINER,
            shapes=(),
            note="an object carrying an address, which is on the permanent denylist",
        ),
        FieldKind.CREATED_TIME: KindFacts(
            kind=FieldKind.CREATED_TIME,
            api_type=1001,
            representation=Representation.SCALAR,
            shapes=(FieldShape.TIMESTAMP,),
            note="milliseconds since the epoch, set once by the Base and never edited",
        ),
        FieldKind.MODIFIED_TIME: KindFacts(
            kind=FieldKind.MODIFIED_TIME,
            api_type=1002,
            representation=Representation.SCALAR,
            shapes=(FieldShape.TIMESTAMP,),
            note=(
                "milliseconds since the epoch; the column an updated-since cursor is built on, "
                "and the one that never moves for a record somebody deleted"
            ),
        ),
        FieldKind.AUTO_NUMBER: KindFacts(
            kind=FieldKind.AUTO_NUMBER,
            api_type=1005,
            representation=Representation.SCALAR,
            shapes=(FieldShape.IDENTIFIER, FieldShape.JOIN_KEY),
            note="the Base's own sequence for the row; an identifier people quote to each other",
        ),
    }
)


def kind_facts(kind: FieldKind) -> KindFacts:
    """What one Base field type may become. Looked up, never restated on a binding."""
    try:
        return KIND_FACTS[kind]
    except KeyError as exc:  # pragma: no cover - the totality test keeps this unreached
        msg = (
            f"{kind!r} has no entry in KIND_FACTS, so nothing knows whether it can be stored; "
            "classify it before any binding may declare it"
        )
        raise ConnectorContractError(msg) from exc


# ------------------------------------------------------------------- one bound field
@dataclass(frozen=True)
class FieldBinding:
    """One Base column, the name we give it, and what the Base says it holds.

    Not a `transports.FieldMapping`, and the reason is the vendor's rather than a preference:
    a Base column is a human-authored label with spaces and punctuation in it (`Hours
    Remaining`), and `transports._SOURCE_PATH_RE` admits identifiers only. A mapping that
    renamed the label to fit would read a different column from the one the author named.
    `base_field` is therefore the label verbatim, matched as a dictionary key rather than
    walked as a path, which is all a Base cell container needs: it is one level deep.

    A binding names fields and never people. `assert_maps_only` runs over the declaration for
    exactly the reason `brain.connectors.rest.A_MAPPING_NAMES_FIELDS_AND_NEVER_PEOPLE` gives,
    and it runs on `type(self)` so a subclass that grew a capability clause is refused too.

    `uses` empty means this column is read live and never stored, which is the ordinary case.
    A non-empty `uses` is a request to project, and then `shape` is required: the Base's type
    says which shapes are available and the author says which one this is.
    """

    target: str
    base_field: str
    kind: FieldKind
    uses: tuple[HotUse, ...] = ()
    shape: FieldShape | None = None

    def __post_init__(self) -> None:
        assert_maps_only(type(self))
        if not _NAME_RE.match(self.target):
            msg = (
                f"binding target {self.target!r} is not a name; the field policy is looked up "
                "by this string, and a name nothing matches is withheld from everybody"
            )
            raise ConnectorContractError(msg)
        if self.target in RESERVED_TARGETS:
            msg = (
                f"binding target {self.target!r} is one of {sorted(RESERVED_TARGETS)}, which "
                "the record envelope already owns; a value written there is discarded by "
                "normalise rather than stored, silently"
            )
            raise ConnectorContractError(msg)
        if not self.base_field.strip():
            msg = (
                f"binding {self.target!r} names no Base column; a binding that matches no "
                "column contributes nothing and reads in a manifest as a field being read"
            )
            raise ConnectorContractError(msg)
        self._assert_the_kind_can_be_read()
        self._assert_the_shape_is_one_the_kind_offers()

    def _assert_the_kind_can_be_read(self) -> None:
        """Refuse a column whose value cannot be turned into one honest value.

        Refused at declaration rather than flattened at ingest, which is the whole of
        `WHAT_A_BASE_FIELD_BECOMES`: at declaration the author can name a different column and
        somebody reviews the choice, while at ingest a `str()` of a list of record ids becomes
        a value that reads like data and nobody sees it happen.
        """
        facts = kind_facts(self.kind)
        if facts.may_be_read:
            return
        msg = (
            f"binding {self.target!r} reads a {self.kind} column, which is a "
            f"{facts.representation}: {facts.note}. Name a column that holds one value, or "
            f"read this one in the Base. {WHAT_A_BASE_FIELD_BECOMES}"
        )
        raise ConnectorContractError(msg)

    def _assert_the_shape_is_one_the_kind_offers(self) -> None:
        """The projection half: a shape is required to store, and the kind decides which.

        Three refusals, and the middle one is the interesting one. A kind offering no shapes
        may never be stored however the binding is named, which is what closes the gap in
        `A_DENYLIST_MATCHED_BY_NAME_IS_RENAMED_PAST`. The other two keep the declaration
        honest in both directions: asking to project without saying what the field is, and
        saying what it is without asking to project.

        The `shape is None` clause is an equivalent mutant and is kept anyway, which is worth
        saying plainly rather than leaving for the next person to rediscover. Deleting it
        refuses exactly the same declarations, because `None` is in no kind's shape tuple and
        the clause below it therefore catches every input this one does, with the same
        exception type. What changes is only the sentence the author reads: this one names the
        shapes the column could have been, and the one below reports `None` as a shape that
        does not fit. No test can tell them apart without asserting on message text, so none
        pretends to. It survives as an explanation, not as an enforcement point.
        """
        facts = kind_facts(self.kind)
        if not self.uses:
            if self.shape is not None:
                msg = (
                    f"binding {self.target!r} declares shape {self.shape} and no use; a shape "
                    "is what a projected field is, and a field nothing in the fast lane "
                    "filters, sorts, counts, joins or identifies on is fetched live"
                )
                raise ConnectorContractError(msg)
            return
        if not facts.may_be_projected:
            reason = (
                A_FORMULA_CHANGES_EVERY_ROW_WITH_NO_EVENT
                if facts.representation is Representation.RECOMPUTED
                else A_DENYLIST_MATCHED_BY_NAME_IS_RENAMED_PAST
            )
            msg = (
                f"binding {self.target!r} would project a {self.kind} column: {facts.note}. "
                f"Fetch it live. {reason}"
            )
            raise ConnectorContractError(msg)
        if self.shape is None:
            msg = (
                f"binding {self.target!r} asks to be projected and declares no shape; the "
                f"{self.kind} column can be {[s.value for s in facts.shapes]} and which it is "
                "here is the author's declaration rather than something a type table knows"
            )
            raise ConnectorContractError(msg)
        if self.shape not in facts.shapes:
            msg = (
                f"binding {self.target!r} declares shape {self.shape} over a {self.kind} "
                f"column, which can only be {[s.value for s in facts.shapes]}; a shape the "
                "value cannot hold is a pointer that points at the wrong kind of thing"
            )
            raise ConnectorContractError(msg)

    @property
    def is_projected(self) -> bool:
        return bool(self.uses)

    def as_projected_field(self) -> ProjectedField:
        """This binding as the manifest's own declaration, for the five-clause test.

        Built rather than declared twice, so a binding and the field it projects cannot
        disagree about the shape or the uses. `projectability` then runs the denylist, the
        pointer clause and the cap over it, which is where a target named `salary` is caught
        however honest its Base type happens to be.
        """
        if self.shape is None:
            msg = (
                f"binding {self.target!r} is not projected, so it has no shape and no "
                "projected field; the constructor refuses that combination"
            )
            raise ConnectorContractError(msg)
        return ProjectedField(name=self.target, shape=self.shape, uses=self.uses)


# ----------------------------------------------------------------- decoding a cell
def _milliseconds_to_datetime(target: str, raw: object) -> datetime:
    """A Lark instant, which is milliseconds since the epoch and never ISO.

    The same conversion `brain.channels.lark` performs on a message's `create_time`, and it is
    not imported from there: a connector must not depend on a channel adapter, and what the
    two share is the vendor's unit rather than a rule that could be changed in one place and
    not the other. Timezone-aware, because `projection.ProjectedRecord` refuses a naive
    timestamp and Singapore reads a naive UTC instant as eight hours old.
    """
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        msg = (
            f"{target!r} is a date column and the Base sent {type(raw).__name__}; Lark states "
            "an instant as milliseconds since the epoch, and a value that is not a number "
            "would be parsed into whatever a string happens to look like"
        )
        raise ConnectorContractError(msg)
    try:
        return datetime.fromtimestamp(raw / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        msg = f"{target!r} holds {raw!r}, which is not a millisecond timestamp"
        raise ConnectorContractError(msg) from exc


def decode_value(binding: FieldBinding, raw: object) -> ProjectedValue:
    """One Base cell as one value, or a refusal naming what arrived instead.

    Refuses rather than coerces, in both directions. A container arriving where a scalar was
    declared is the rich-text case and the "somebody changed the column type" case, and both
    produce a value that looks right after a `str()`; a number arriving as text is the same
    problem wearing quotes. What a refusal costs is one field of one record; what a coercion
    costs is a value nobody can tell apart from a real one.

    A `None` passes through as `None`. A Base cell that is empty has said something different
    from a Base cell holding a zero, and collapsing the two would invent a figure.
    """
    if raw is None:
        return None
    facts = kind_facts(binding.kind)
    if binding.kind in (FieldKind.DATE, FieldKind.CREATED_TIME, FieldKind.MODIFIED_TIME):
        return _milliseconds_to_datetime(binding.target, raw)
    if binding.kind is FieldKind.CHECKBOX:
        if not isinstance(raw, bool):
            msg = (
                f"{binding.target!r} is a checkbox column and the Base sent "
                f"{type(raw).__name__}; a truthiness test over it would read the string "
                "'false' as true"
            )
            raise ConnectorContractError(msg)
        return raw
    if binding.kind is FieldKind.NUMBER:
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            msg = (
                f"{binding.target!r} is a number column and the Base sent "
                f"{type(raw).__name__}; parsing it here would be this module deciding what a "
                "number written as text means, which is the source's decision"
            )
            raise ConnectorContractError(msg)
        return raw
    if isinstance(raw, str | int | float | bool):
        return raw
    msg = (
        f"{binding.target!r} is declared as {binding.kind} and the Base sent "
        f"{type(raw).__name__}: {facts.note}. A container rendered with str() reads like a "
        f"value and is not one. {WHAT_A_BASE_FIELD_BECOMES}"
    )
    raise ConnectorContractError(msg)


def decode_row(
    bindings: tuple[FieldBinding, ...], row: Mapping[str, Any]
) -> dict[str, ProjectedValue]:
    """One fetched record as the values the bindings name, and nothing else.

    A fresh dictionary built from the declared bindings, never a copy of the Base's cell
    container with unwanted keys removed. The two read the same on the day they are written
    and diverge the first time somebody adds a column to the Base: a copy carries it and a
    build does not. This is the layer that keeps
    `brain.connectors.rest.WHAT_THE_MAPPING_DOES_NOT_NAME_DOES_NOT_ARRIVE` true for this
    connector, because the REST mapping above it names the cell container whole and cannot
    name the cells.

    A column the record does not carry contributes nothing rather than a null. A Base omitting
    an empty cell has said something different from one sending an empty value.
    """
    cells = row.get("fields")
    if not isinstance(cells, Mapping):
        msg = (
            "this record carries no fields object; a Base record is a record id and a "
            "container of typed cells, and a record without one is not a Base record"
        )
        raise ConnectorContractError(msg)
    built: dict[str, ProjectedValue] = {}
    for binding in bindings:
        if binding.base_field not in cells:
            continue
        built[binding.target] = decode_value(binding, cells[binding.base_field])
    return built


# -------------------------------------------------------------------------- the table
@dataclass(frozen=True)
class LarkBaseTable:
    """One Base, one table, and what we read out of it. What a connection is scoped to.

    Holds no credential and cannot: `assert_holds_no_credential` runs over the class in
    `__post_init__`, and the Base identifier is spelled `base_id` rather than the vendor's
    `app_token` for the reason `A_VENDOR_IDENTIFIER_IS_NOT_A_CREDENTIAL` gives at length.

    `entity` is the deployment's rather than a constant, and that is the honest reading of
    what a Base is: one table holds clients, the next holds maintenance hours, and the entity
    tag is what `brain.core.field_policy` looks a rule up by. A connector that named the
    entity for the whole source would give every table in the tenant one field policy.
    """

    base_id: str
    table_id: str
    entity: str
    bindings: tuple[FieldBinding, ...]

    def __post_init__(self) -> None:
        assert_holds_no_credential(type(self))
        if not _BASE_ID_RE.match(self.base_id):
            msg = (
                f"base id {self.base_id!r} is not a Lark Base identifier; a scope built from "
                "one the source would not recognise admits whatever the transport decides it "
                "meant"
            )
            raise ConnectorContractError(msg)
        if not _TABLE_ID_RE.match(self.table_id):
            msg = (
                f"table id {self.table_id!r} does not look like a Base table id; the prefix is "
                "the one piece of structure Lark's own ids carry, and pasting a Base id where "
                "a table id belongs is what a copied configuration does"
            )
            raise ConnectorContractError(msg)
        if not _NAME_RE.match(self.entity):
            msg = (
                f"entity {self.entity!r} is not a name; the field policy is looked up by this "
                "string, and a tag nothing matches is withheld from everybody"
            )
            raise ConnectorContractError(msg)
        self._assert_the_bindings_are_a_set()

    def _assert_the_bindings_are_a_set(self) -> None:
        """No column read twice and no target written twice.

        Refused rather than deduplicated, for the reason `manifest.ProjectedEntity` gives
        about its own duplicates: deduplicating picks one silently, and the one it picks
        decides whether the field is a label, which decides whether the projection is legal.
        """
        if not self.bindings:
            msg = (
                f"{self.entity} binds no columns, so every record it returns is a bare entity "
                "tag and the redactor drops it"
            )
            raise ConnectorContractError(msg)
        targets = [b.target for b in self.bindings]
        repeated = sorted({t for t in targets if targets.count(t) > 1})
        if repeated:
            msg = (
                f"{self.entity} writes {repeated} from more than one Base column; which value "
                "survives would be decided by declaration order"
            )
            raise ConnectorContractError(msg)
        columns = [b.base_field for b in self.bindings]
        twice = sorted({c for c in columns if columns.count(c) > 1})
        if twice:
            msg = (
                f"{self.entity} reads Base column(s) {twice} under more than one name; two "
                "names for one column are two fields to classify and one value to keep fresh"
            )
            raise ConnectorContractError(msg)

    # ------------------------------------------------------------------ scope at connect
    @property
    def selector(self) -> str:
        """The one resource this connector is connected to: a Base and a table within it.

        One selector rather than two, and that is the narrowing. A scope naming the Base alone
        reaches every table in it, and `ConnectorScope.admits` is exact membership rather than
        a prefix, so a joined selector cannot be satisfied by a sibling table the way a Base id
        would be.
        """
        return f"{self.base_id}/{self.table_id}"

    def scope(self) -> ConnectorScope:
        """What this connector was connected to, decided once, at connect (M11.2.3)."""
        return ConnectorScope(resource_kind="base_table", selectors=(self.selector,))

    # ------------------------------------------------------------------ the REST binding
    def path_arguments(self) -> dict[str, str]:
        """The path arguments every operation here takes. The vendor's words, in a URL."""
        return {"app_token": self.base_id, "table_id": self.table_id}

    def transport(self) -> RestTransport:
        """The declaration `brain.connectors.rest` binds to a parsed operation.

        Two mappings and no more: the record id, which `RestOperation` refuses a mapping for
        lacking, and the cell container, which is as far as a dotted path can reach into a
        Base. The allowlist over the cells is `bindings`, applied by `decode_row`.
        """
        return RestTransport(
            spec_ref=SPEC_REF,
            operation=spec_for(Endpoint.LIST_RECORDS).operation_id,
            entity=self.entity,
            fields=(
                FieldMapping(target="id", source_path="record_id"),
                FieldMapping(target="fields", source_path="fields"),
            ),
        )

    def operation(self, endpoint: Endpoint, *, host: str) -> RestOperation:
        """One endpoint, bound to its mapping and to the one address it is reached at.

        The host is checked against `LARK_HOSTS` here rather than left to
        `brain.tools.fetch.assert_fetchable`, and the two are not redundant. That one refuses
        a private address, a credential in the URL and every hop of a redirect; this one
        refuses a public address that is simply not Lark, which is the copied-configuration
        mistake and is invisible to an address checker. Nothing is fetched: the address is
        built and checked by `prepare`, which the transport calls with a resolver this module
        deliberately does not have.
        """
        if host not in LARK_HOSTS:
            msg = (
                f"host {host!r} is not one of {sorted(LARK_HOSTS)}; a Base connector pointed "
                "at something that is not Lark reads as configured and answers from somewhere "
                "nobody approved"
            )
            raise ConnectorContractError(msg)
        return RestOperation(
            base_url=f"https://{host}",
            operation=spec_for(endpoint),
            transport=replace(self.transport(), operation=spec_for(endpoint).operation_id),
        )

    # ------------------------------------------------------------------ the projection
    def projected_bindings(self) -> tuple[FieldBinding, ...]:
        return tuple(b for b in self.bindings if b.is_projected)

    def projection(self, *, visibility: Scope) -> ProjectedEntity:
        """What is kept locally about this table's records, and who may read a row.

        `visibility` has no default and is the one thing this module refuses to decide. See
        `A_SHARING_SETTING_IS_NOT_A_PERMISSION_MODEL`: a Base's own sharing is a resolved list
        held by Lark and unreachable through `base:record:read`, and a predicate supplied here
        would be one client's ownership rule applied to another client's records.
        `manifest.ProjectedEntity` refuses an unrestricted one and refuses an `IN` over
        principal ids, which is the same list wearing a predicate's clothes.
        """
        return ProjectedEntity(
            entity=self.entity,
            fields=tuple(b.as_projected_field() for b in self.projected_bindings()),
            change_signal=CHANGE_SIGNAL,
            visibility=visibility,
        )

    def projected_fields(self, row: Mapping[str, Any]) -> dict[str, ProjectedValue]:
        """One fetched record as the fields a projected record may hold.

        The projected bindings only, so a column read live never reaches the database by way
        of a caller who used the wrong function. A label is cut to
        `brain.core.projection.MAX_LABEL_CHARS`, silently and for the reason the Freshdesk
        connector gives about its own subject line: a marker would make a value that genuinely
        ends in an ellipsis indistinguishable from one that was cut, the record's identity is
        its id rather than its label, and the alternative is `check_projection` dropping whole
        records whose label happens to be long, which would leave the projection missing
        precisely the noisiest rows.
        """
        decoded = decode_row(self.projected_bindings(), row)
        for binding in self.projected_bindings():
            value = decoded.get(binding.target)
            if binding.shape is FieldShape.LABEL and isinstance(value, str):
                decoded[binding.target] = value[:MAX_LABEL_CHARS]
        return decoded

    def projected_record(
        self, row: Mapping[str, Any], *, last_seen_at: datetime
    ) -> ProjectedRecord:
        """One fetched record as the value that goes to `proj.record`.

        `ProjectedRecord` enforces the twelve-field cap, the denylist and the container
        refusal in its own constructor, so an oversized projection never exists as a value in
        this process. Nothing is restated here: this builds the record and that refuses it.
        """
        record_id = row.get("id")
        if not isinstance(record_id, str) or not record_id.strip():
            msg = (
                "this record carries no record id; a record that cannot be named cannot be "
                "refreshed, cited or matched to itself on the next pass, and a row number "
                "would be reused the moment somebody sorts a view"
            )
            raise ConnectorContractError(msg)
        return ProjectedRecord(
            source=LARK_BASE,
            entity=self.entity,
            source_id=record_id,
            last_seen_at=last_seen_at,
            fields=self.projected_fields(row),
        )


# ------------------------------------------------------------------ one page, as a value
@dataclass(frozen=True)
class PageCursor:
    """Where a read is up to. Lark pages by an opaque token, never by a page number.

    Validated on construction rather than by whoever sends it, because both failures below are
    invisible in the reply: a cursor carrying a record id on the list endpoint sends an
    argument the operation does not declare, and a get carrying a page token asks for a page of
    one record. Neither is distinguishable afterwards from the thing it pretends to be.

    `continuation` is opaque, exactly as `contract.FetchRequest.cursor` says: a cursor's shape
    is the source's business, and parsing one here would make this module wrong the day Lark
    changes it. It is Lark's `page_token` and is not spelled that way on an attribute, for the
    reason `A_VENDOR_IDENTIFIER_IS_NOT_A_CREDENTIAL` gives: the guard that refuses a held
    credential matches by name, and a paging marker is not worth an exemption from it.
    """

    endpoint: Endpoint
    page_size: int = PAGE_SIZE
    continuation: str = ""
    view_id: str = ""
    record_id: str = ""

    def __post_init__(self) -> None:
        if self.page_size < 1 or self.page_size > PAGE_SIZE:
            msg = (
                f"a page of {self.page_size} records is not one this connector asks for; the "
                f"recorded exchange uses {PAGE_SIZE}, which is the only size anybody has "
                "verified, and a size the endpoint clamps comes back looking like a last page"
            )
            raise ConnectorContractError(msg)
        if self.view_id and not _VIEW_ID_RE.match(self.view_id):
            msg = (
                f"view {self.view_id!r} is not a Base view id; a view argument the source does "
                "not recognise reads at the call site as a filter being applied"
            )
            raise ConnectorContractError(msg)
        if self.endpoint is Endpoint.GET_RECORD:
            if not _RECORD_ID_RE.match(self.record_id):
                msg = (
                    f"{self.endpoint} reads one record and was given record id "
                    f"{self.record_id!r}; without one the address names the whole table"
                )
                raise ConnectorContractError(msg)
            if self.continuation or self.view_id:
                msg = (
                    f"{self.endpoint} reads one record and was given a page token or a view; "
                    "an argument that is accepted and dropped reads as a filter being applied"
                )
                raise ConnectorContractError(msg)
            return
        if self.record_id:
            msg = (
                f"{self.endpoint} reads a page and was given record id {self.record_id!r}; "
                "the list operation declares no such parameter and would refuse the address"
            )
            raise ConnectorContractError(msg)

    def query_arguments(self) -> dict[str, str]:
        """The arguments `RestOperation.url_for` builds the query from.

        Only what is set, so an unused page token is not sent as an empty string. `url_for`
        refuses an argument the operation does not declare, which is the other half of the
        same rule and the reason this cannot quietly send a parameter nobody reads.
        """
        if self.endpoint is Endpoint.GET_RECORD:
            return {"record_id": self.record_id}
        built = {"page_size": str(self.page_size)}
        if self.continuation:
            # The vendor's key, on the wire, where it belongs: the operation declares
            # `page_token` and `url_for` refuses an argument it does not declare.
            built["page_token"] = self.continuation
        if self.view_id:
            built["view_id"] = self.view_id
        return built


def first_cursor(*, view_id: str = "", continuation: str = "") -> PageCursor:
    """The first page of the scoped table, at the size the recordings verify."""
    return PageCursor(
        endpoint=Endpoint.LIST_RECORDS,
        page_size=PAGE_SIZE,
        continuation=continuation,
        view_id=view_id,
    )


def arguments_for(table: LarkBaseTable, cursor: PageCursor) -> dict[str, str]:
    """Everything one call's address is built from: the table's path and the cursor's query.

    Assembled in one place rather than at each call site, because "which arguments apply" is
    the decision a refactor drops a line from, and a dropped path argument is an address that
    names a different Base.
    """
    return {**table.path_arguments(), **cursor.query_arguments()}


# ------------------------------------------------------------------ what came back
@dataclass(frozen=True)
class LarkReply:
    """What came back, as a value. The same three fields a cassette records.

    Deliberately identical in shape to `tests.fixtures.cassettes.Cassette`, so a recorded
    exchange becomes one of these without a translation step that could disagree with the
    recording. This module never constructs one: it is what a transport hands over, which is
    what keeps every rule here testable without a socket.

    The Freshdesk connector carries a value of the same shape under its own name. Extracting a
    shared one was considered and left alone while three vendor connectors are being written
    at once: the shape is the cassette's, and the extraction belongs in
    `brain.connectors.transports` when a third vendor wants it rather than as a dependency
    between two vendor modules today.
    """

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
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


class RecordReader(Protocol):
    """Whatever performs one exchange and hands back the reply.

    A protocol rather than a client, so this module holds no connection. The cases that matter
    here are a permission code inside a 200, a page that claims more and names no cursor, and
    a budget running out mid-walk, and none of them can be arranged reliably against a real
    tenant. In production the implementation borrows a lease for the duration of the call and
    goes through `brain.connectors.rest.RestOperation.read`; in tests it is the recordings.
    """

    def read(self, cursor: PageCursor) -> LarkReply: ...


class LarkBaseUnreachableError(Degraded):
    """Lark could not answer: a quota refusal, a timeout, or a server failure.

    A `Degraded`, so the asker is told the platform's one sentence, which does not name the
    system. The outcome and the wait are for whoever is on call; which of the company's
    systems is unwell is not a fact obtainable by typing a question.

    `call_outcome` is spelled out rather than reusing `BrainError.outcome`, which is the
    user-facing taxonomy and is DEGRADED for both this and the refusal below. Two different
    questions sharing one attribute is how the operational one ends up rendered to somebody.
    """

    def __init__(
        self,
        detail: str = "",
        *,
        call_outcome: CallOutcome = CallOutcome.UNAVAILABLE,
        wait_for: float = 0.0,
    ) -> None:
        super().__init__(detail)
        self.call_outcome = call_outcome
        self.wait_for = wait_for

    def trace_line(self) -> str:
        """The full statement, for an operator rather than for the asker.

        Names the source and the outcome unconditionally, which is safe for the reason
        `federation.PartialAnswer.trace_lines` is safe: a trace is read by somebody already
        entitled to know what the system connects to, and nothing here can put this string
        into a channel payload.
        """
        return f"{LARK_BASE}: {self.call_outcome}, wait {self.wait_for:.0f}s"


class LarkBaseRefusedError(Degraded):
    """Lark understood the request and declined it.

    Its own type rather than a flag on the one above, because the two go to different people
    and have opposite remedies: a Base the bot was never added to, or a scope nobody granted,
    is somebody changing a configuration, and waiting makes it no better.
    `throttle.is_retryable` says the same about `REJECTED`, and this is the shape that stops a
    retry loop being written against it, which against an unraisable ceiling would spend the
    whole tenant's minute achieving nothing.

    Also a `Degraded`, so the asker is told the same sentence as for an outage. A refusal that
    read differently would tell somebody who asked about a client which of our credentials is
    wrong.
    """

    def __init__(
        self,
        detail: str = "",
        *,
        call_outcome: CallOutcome = CallOutcome.REJECTED,
        code: int = 0,
    ) -> None:
        super().__init__(detail)
        self.call_outcome = call_outcome
        self.code = code


def wait_seconds(reply: LarkReply) -> float:
    """How long the source asked us to wait, or the window's own length when it did not say.

    Seconds only. The other form `Retry-After` may take in HTTP is a date, which cannot be
    turned into a wait without reading a clock this module deliberately does not have, so an
    unparseable value takes the same path as an absent one rather than being half understood.
    The fallback is a measurement rather than a guess: see `WAIT_WHEN_UNSTATED`.
    """
    stated = reply.header("Retry-After").strip()
    if not stated:
        return WAIT_WHEN_UNSTATED
    try:
        seconds = float(stated)
    except ValueError:
        return WAIT_WHEN_UNSTATED
    return seconds if seconds > 0 else WAIT_WHEN_UNSTATED


def business_code(reply: LarkReply) -> int:
    """The code Lark puts inside the body, which is where success and failure actually live.

    A body that is not an object, or that carries no integer code, is refused rather than
    treated as a success. Lark answers every call with a code; something that does not is an
    error page, a proxy, or a login redirect, and reading one as an empty table is how an
    outage is recorded as a fact about the company.
    """
    if not isinstance(reply.body, Mapping):
        msg = (
            f"{LARK_BASE} answered with {type(reply.body).__name__} rather than an object; "
            "every Lark reply carries a code, and something that does not is not Lark"
        )
        raise LarkBaseRefusedError(msg)
    code = reply.body.get("code")
    if not isinstance(code, int) or isinstance(code, bool):
        msg = (
            f"{LARK_BASE} answered with no code in the body; treating a missing code as "
            f"success is how a refusal becomes an empty table. "
            f"{A_ZERO_CODE_INSIDE_A_TWO_HUNDRED_IS_THE_ONLY_SUCCESS}"
        )
        raise LarkBaseRefusedError(msg)
    return code


def assert_lark_answered(reply: LarkReply) -> None:
    """Raise unless this reply is an answer, keeping the three outcomes apart (M11.6.3).

    The status classification is `brain.connectors.throttle.classify`'s and is not restated,
    so this cannot come to a different conclusion from the module that owns the rule that a
    429 is a quota refusal rather than ill health. What this adds is the half that is Lark's:
    a 200 carrying a non-zero code is a refusal, not an empty table.

    - `QUOTA` and `UNAVAILABLE` are the source not answering. Raised, with a wait.
    - `REJECTED` is the source refusing us. Raised, with no wait, because a retry reproduces
      it exactly and a wait invites one.
    - A non-zero body code is also a refusal, for the reason
      `AN_UNKNOWN_CODE_IS_A_REFUSAL_RATHER_THAN_A_RETRY` gives.
    - `code == 0` returns, and a table that matched nothing then travels as a result with no
      records in it.

    Called before the body is projected, deliberately. A refusal carries a body of its own
    (`{"code": 91403, "msg": "Forbidden", "data": {}}`), and projecting that first would
    report a permission failure as a malformed response, sending whoever reads the error to
    the wrong module.
    """
    outcome = classify(status=reply.status)
    if outcome in (CallOutcome.QUOTA, CallOutcome.UNAVAILABLE):
        msg = (
            f"{LARK_BASE} answered {reply.status}; the source could not be reached, and an "
            "answer from anywhere else would be presented as though it had been"
        )
        raise LarkBaseUnreachableError(msg, call_outcome=outcome, wait_for=wait_seconds(reply))
    if outcome is CallOutcome.REJECTED:
        msg = (
            f"{LARK_BASE} refused the request with {reply.status}; this is our credential or "
            "our request rather than the Base's health, so waiting does not fix it"
        )
        raise LarkBaseRefusedError(msg, call_outcome=outcome)
    code = business_code(reply)
    if code == SUCCESS_CODE:
        return
    if code == PERMISSION_DENIED_CODE:
        msg = (
            f"{LARK_BASE} answered {reply.status} carrying code {code}: the bot's token is "
            "scoped to base:record:read and was not added to this Base, or the table is not "
            "the one it was granted. Waiting does not fix it and a retry spends the tenant's "
            "minute"
        )
        raise LarkBaseRefusedError(msg, code=code)
    msg = (
        f"{LARK_BASE} answered {reply.status} carrying code {code}, which is not success. "
        f"{AN_UNKNOWN_CODE_IS_A_REFUSAL_RATHER_THAN_A_RETRY}"
    )
    raise LarkBaseRefusedError(msg, code=code)


# ------------------------------------------------------------------ the page envelope
@dataclass(frozen=True)
class PageEnvelope:
    """What a page says about the pages after it.

    `total` is carried and is never consulted to decide when to stop. See
    `THE_END_SIGNAL_IS_HAS_MORE_AND_NEVER_THE_TOTAL`: it is useful for a trace and for sizing
    a reconciliation sweep, and it is a count taken while somebody is editing the table.
    """

    has_more: bool
    #: Lark's `page_token`, under the name every cursor here carries it by. See
    #: `A_VENDOR_IDENTIFIER_IS_NOT_A_CREDENTIAL`.
    continuation: str = ""
    total: int = TOTAL_UNSTATED

    @property
    def states_a_total(self) -> bool:
        return self.total >= 0


def envelope_of(body: Any) -> PageEnvelope:
    """What the page said about continuation, refusing a body that did not say.

    An absent `has_more` is a refusal rather than a stop. "There is no more" and "the source
    did not say" are two different facts, and defaulting to the first is how an incomplete
    answer comes to read as a complete one, silently, on exactly the table that grew.

    A page claiming more and naming no token is also refused, and that one is the sharp
    refusal in this module: a walk that re-sent an empty token would read page one for ever,
    which against a hundred calls a minute spends the whole tenant's budget in under a minute
    and returns the same records every time.
    """
    if not isinstance(body, Mapping):
        msg = "a Lark page is an object; this reply is not one"
        raise ConnectorContractError(msg)
    data = body.get("data")
    if not isinstance(data, Mapping):
        msg = (
            "this reply carries no data object, so it says nothing about whether there are "
            "more records; reading that as the end of the table under-reports silently"
        )
        raise ConnectorContractError(msg)
    has_more = data.get("has_more")
    if not isinstance(has_more, bool):
        msg = (
            "this page does not say whether there is more. "
            f"{THE_END_SIGNAL_IS_HAS_MORE_AND_NEVER_THE_TOTAL}"
        )
        raise ConnectorContractError(msg)
    token = data.get("page_token", "")
    if not isinstance(token, str):
        msg = "a page token is an opaque string; this page carries something else"
        raise ConnectorContractError(msg)
    if has_more and not token.strip():
        msg = (
            "this page says there are more records and names no page token to continue from; "
            "re-sending an empty token reads the first page again for ever, which against "
            f"{ceiling().per_minute} calls a minute spends the whole tenant's budget and "
            "returns the same records every time"
        )
        raise ConnectorContractError(msg)
    total = data.get("total", TOTAL_UNSTATED)
    if not isinstance(total, int) or isinstance(total, bool):
        total = TOTAL_UNSTATED
    return PageEnvelope(has_more=has_more, continuation=token, total=total)


# -------------------------------------------------------------------------- the walk
@dataclass(frozen=True)
class TableReading:
    """What was read, what was not, and everything needed to say which.

    There is deliberately no value here meaning "withhold", which is the shape
    `brain.connectors.projection` and `brain.ops.limits` both use for their own assessments: a
    future caller cannot start refusing on a partial read without adding somewhere to express
    it and being seen in review.

    `stopped_for_budget` and `more_at_source` are separate because they are separate claims.
    The first is ours and we know it; the second is the source's and it is the only evidence
    that anything was missed. A walk can stop for budget on the last page, in which case
    nothing was missed at all.
    """

    result: TypedResult[SourceRecord]
    pages_read: int
    budget: MinuteBudget
    more_at_source: bool = False
    stopped_for_budget: bool = False
    stopped_at_caller_limit: bool = False
    #: Where a later question resumes. Opaque, and trace-only: a paging marker in an answer
    #: would put the source's internal state in front of somebody who asked about a client.
    resume_from: str = ""
    #: What the last page said it was counting, or `TOTAL_UNSTATED`. Never used to stop.
    total_at_source: int = TOTAL_UNSTATED

    @property
    def is_all_of_them(self) -> bool:
        """Whether this may be spoken about as every record in the table."""
        return not self.more_at_source and not self.stopped_at_caller_limit

    def partial(self) -> PartialAnswer:
        """What was and was not fetched, in the vocabulary that owns the disclosure rule.

        `federation.PartialAnswer` decides when a source may be named and folds everything
        else into the one sentence an unreachable source has always produced. Reusing it is
        the point: a sentence written here would be a second disclosure rule, and the day the
        two disagree the one that names a source the asker could not already see is the one
        that wins. The cost is that a budget stop is described as not having reached Lark,
        which is discussed in the module docstring.

        A `QUOTA` failure rather than `TRUNCATED`, because that is what happened: we stopped
        to protect a shared allowance. Truncation is a source refusing to return more, which
        is a different fact with a different remedy.

        A walk stopped by the caller's own limit contributes no failure at all, and that is
        the correction rather than an omission: it is the one stop reason the asker chose, and
        `TRUNCATED` would have described their own limit as the source declining. See
        `A_CALLERS_OWN_LIMIT_IS_NOT_A_SOURCE_FAILURE`. `TRUNCATED` stays as the reason for a
        source that had more and was not stopped by either of those, which is a stop reason
        nothing here produces today and the honest default for the next one somebody adds.
        """
        if not self.more_at_source or self.stopped_at_caller_limit:
            return PartialAnswer(fetched=(LARK_BASE,))
        reason = FailureReason.QUOTA if self.stopped_for_budget else FailureReason.TRUNCATED
        detail = (
            f"{self.pages_read} page(s) read of a table with more to give; "
            f"{self.budget.spent} of {self.budget.allowance} calls spent"
        )
        return PartialAnswer(
            fetched=(LARK_BASE,),
            failed=(SourceFailure(connector=LARK_BASE, reason=reason, detail=detail),),
        )

    def trace_line(self) -> str:
        """What the walk did, for an operator. Names the source and the token, as a trace may."""
        return (
            f"{LARK_BASE}: {len(self.result.records)} record(s) over {self.pages_read} page(s), "
            f"{self.budget.spent}/{self.budget.allowance} calls, more={self.more_at_source}, "
            f"resume={self.resume_from or 'none'}"
        )


def read_page(
    operation: RestOperation, reader: RecordReader, cursor: PageCursor
) -> tuple[tuple[Mapping[str, Any], ...], PageEnvelope]:
    """One exchange: refuse the failure, then read the rows and what the page said after them.

    The order is load-bearing and is argued for in `assert_lark_answered`. What is not
    re-wrapped is the other failure: a body whose shape the operation's own specification does
    not describe raises `brain.connectors.rest.RestSpecError` from `project`, and that is left
    to propagate rather than renamed here, for the reason `brain.connectors.rest` gives about
    not giving an operator two names for one refusal. The property that matters holds either
    way, and it is the one a naive connector loses: an unreadable body is a failure and never
    an empty table.

    A cursor for the single-record endpoint is refused here rather than read. Its reply is a
    good one that simply carries no `has_more`, so without this the walk's own end-signal
    refusal fires and reports a healthy source as malformed. See
    `A_SINGLE_READ_AND_A_PAGE_ARE_DIFFERENT_ENVELOPES`.
    """
    if cursor.endpoint is not Endpoint.LIST_RECORDS:
        msg = (
            f"{cursor.endpoint} was handed to the page reader, which reads a page and its "
            f"continuation; use read_record. {A_SINGLE_READ_AND_A_PAGE_ARE_DIFFERENT_ENVELOPES}"
        )
        raise ConnectorContractError(msg)
    reply = reader.read(cursor)
    assert_lark_answered(reply)
    return operation.project(reply.body), envelope_of(reply.body)


def read_record(
    operation: RestOperation,
    reader: RecordReader,
    cursor: PageCursor,
    *,
    budget: MinuteBudget,
) -> tuple[Mapping[str, Any], MinuteBudget]:
    """One record by its id, and what is left of the budget after paying for it (M11.6.3).

    The other half of `read_page`, and the endpoint the `read_` tool in the manifest is
    declared for. It exists separately rather than as a flag because the two envelopes differ:
    see `A_SINGLE_READ_AND_A_PAGE_ARE_DIFFERENT_ENVELOPES`.

    **This one spends the budget and `read_page` does not**, which is deliberate and is the
    only asymmetry between them. A page is read inside a walk, and the walk owns the budget
    because it is the thing that can run away with it; a single read has no loop above it, so a
    call that spent nothing would be a call against the tenant's hundred a minute that nothing
    counted. The budget after the spend is returned rather than mutated, matching every other
    state machine here, so a caller reading several records carries it forward.

    A record with no id is refused rather than returned. `normalise` drops such a row, so
    handing one back would turn an unreadable reply into a record that is absent, and absence
    is the one answer that must never be manufactured. Note what this cannot separate: Lark
    reports a record that does not exist as a non-zero body code, so a genuinely absent record
    arrives as a refusal, which is the cost `AN_UNKNOWN_CODE_IS_A_REFUSAL_RATHER_THAN_A_RETRY`
    accepts rather than keeping a table of the vendor's business codes. It is the safe
    direction: a refusal and an absence are indistinguishable to the person asking, and it is
    the reverse mistake, an absence manufactured out of a refusal, that reads as fact.
    """
    if cursor.endpoint is not Endpoint.GET_RECORD:
        msg = (
            f"{cursor.endpoint} was handed to the single-record reader, which reads one record "
            f"by its id and has nowhere to put a page. "
            f"{A_SINGLE_READ_AND_A_PAGE_ARE_DIFFERENT_ENVELOPES}"
        )
        raise ConnectorContractError(msg)
    spent = budget.spend()
    reply = reader.read(cursor)
    assert_lark_answered(reply)
    row = operation.project(reply.body)[0]
    record_id = row.get("id")
    if not isinstance(record_id, str) or not record_id.strip():
        msg = (
            f"{LARK_BASE} answered the read of {cursor.record_id!r} with a record carrying no "
            "record id; a row nothing can name is dropped by normalise, so returning it would "
            "turn a reply nobody can read into a record that is simply absent"
        )
        raise ConnectorContractError(msg)
    return row, spent


def read_records(
    table: LarkBaseTable,
    operation: RestOperation,
    reader: RecordReader,
    *,
    fetched_at: str,
    budget: MinuteBudget,
    view_id: str = "",
    limit: int = 0,
    resume_from: str = "",
) -> TableReading:
    """Walk the scoped table inside a budget, and say plainly where it stopped (M11.6.3).

    Three ways this ends and they are three different claims.

    **The source said there is no more.** The only end signal Lark offers that is worth
    trusting, and the one the result may be spoken about as complete on.

    **The caller asked for fewer.** Their claim, and they already know they made it, so it is
    kept apart from the two below rather than folded into "incomplete".

    **The budget ran out.** Ours. The records already read are real and are returned; what is
    reported is that the table had more and this question was not allowed to take it. See
    `ONE_QUESTION_MUST_NOT_SPEND_THE_COMPANYS_MINUTE`.

    A budget with nothing in it before the first call raises rather than returning an empty
    reading, and that is the case this whole module is arranged around: an empty result would
    be our own arithmetic reported as a fact about the company's data. The refusal is
    `MinuteBudget.spend`'s and there is deliberately not a second one here. An earlier draft
    checked the budget before the loop as well, on the grounds that the first call is the case
    worth naming; mutation testing showed it was an equivalent mutant, because the loop's first
    statement is the spend and it raises the same error before anything is read. Two checks
    that look like two enforcement points and are really one is worse than one check, for the
    reason `manifest.ProjectedEntity` records about its own: the next person to edit this
    deletes whichever they find first, and there is then no way to tell which one was load
    bearing.

    `limit` is the caller's and is a request rather than a guarantee, exactly as
    `contract.FetchRequest` says. `resume_from` is a cursor a previous reading handed back, so
    a question that ran out of budget can be continued rather than restarted, which is the
    only way a large table is ever read at all under this ceiling.
    """
    if limit < 0:
        msg = "a negative limit is not a limit"
        raise ValueError(msg)

    cursor = first_cursor(view_id=view_id, continuation=resume_from)
    rows: list[Mapping[str, Any]] = []
    pages = 0
    spent = budget
    envelope = PageEnvelope(has_more=False)
    stopped_for_budget = False
    stopped_at_caller_limit = False

    while True:
        spent = spent.spend()
        page, envelope = read_page(operation, reader, cursor)
        pages += 1
        rows.extend(page)
        if limit and len(rows) >= limit:
            del rows[limit:]
            stopped_at_caller_limit = True
            break
        if not envelope.has_more:
            break
        if spent.is_exhausted:
            stopped_for_budget = True
            break
        cursor = replace(cursor, continuation=envelope.continuation)

    decoded = tuple(
        {"id": row.get("id"), **decode_row(table.bindings, row)}
        for row in rows
        if isinstance(row, Mapping)
    )
    result = normalise(
        table.entity,
        decoded,
        source=LARK_BASE,
        fetched_at=fetched_at,
        # Both reasons produce a partial answer and they are not the same claim, so both set
        # the flag and `TableReading` keeps them apart for anybody who needs to say which.
        truncated=envelope.has_more or stopped_at_caller_limit,
    )
    return TableReading(
        result=result,
        pages_read=pages,
        budget=spent,
        more_at_source=envelope.has_more,
        stopped_for_budget=stopped_for_budget,
        stopped_at_caller_limit=stopped_at_caller_limit,
        resume_from=envelope.continuation if envelope.has_more else "",
        total_at_source=envelope.total,
    )


# ------------------------------------------------- the fetch, as the contract wants it
#: The one filter this connector understands, which is Lark's own way of narrowing a table.
#: A Base filter expression is the source's `FilterInfo` syntax and this module builds none,
#: so anything else is refused rather than dropped: an argument accepted and discarded reads
#: at the call site as a filter being applied.
VIEW_FILTER: Final = "view_id"


def records_fetch(
    table: LarkBaseTable,
    operation: RestOperation,
    reader: RecordReader,
    *,
    fetched_at: str,
    budget: MinuteBudget,
) -> Callable[[FetchRequest], TypedResult[SourceRecord]]:
    """The table read as a connector fetch, checked against the contract before it is returned.

    `assert_fetches_only` runs on the closure rather than on this factory, and that is the
    point of building one: the closure is the object a registry would call, so it is the
    object whose signature has to be shown never to receive the caller's grants or a vault.
    The reader, the stamp and the budget are wiring supplied by whoever builds the connector,
    and a parameter a caller cannot reach is a parameter that cannot carry any of them.

    A cursor is accepted here, unlike the Freshdesk connector which refuses one. Lark genuinely
    pages by an opaque token, so `FetchRequest.cursor` means exactly what it says and passing
    it through is how a question continued after a budget stop resumes rather than restarts.

    What is dropped is the partial-answer verdict: this returns the `TypedResult` the contract
    asks for, whose `truncated` flag says the answer is incomplete without saying why. A
    caller that needs to say why calls `read_records` and reads the `TableReading`, which is
    the same split `brain.connectors.freshdesk` makes for the same reason.
    """

    def _fetch(request: FetchRequest) -> TypedResult[SourceRecord]:
        if request.entity != table.entity:
            msg = (
                f"this connector reads {table.entity!r} out of {table.selector} and was asked "
                f"for {request.entity!r}"
            )
            raise ConnectorContractError(msg)
        unknown = sorted(name for name, _ in request.filters if name != VIEW_FILTER)
        if unknown:
            msg = (
                f"this connector understands {VIEW_FILTER!r} and was given {unknown}; a Base "
                "filter is Lark's own FilterInfo expression and nothing here builds one, so "
                "these would be accepted and dropped, which reads as a filter being applied"
            )
            raise ConnectorContractError(msg)
        return read_records(
            table,
            operation,
            reader,
            fetched_at=fetched_at,
            budget=budget,
            view_id=dict(request.filters).get(VIEW_FILTER, ""),
            limit=request.limit,
            resume_from=request.cursor,
        ).result

    assert_fetches_only(_fetch)
    return _fetch


# ------------------------------------------------------------------------- health
#: What one call's outcome says about the connector, as a probe result. Total over
#: `CallOutcome`, and the two interesting rows are the ones that are not DOWN.
#:
#: A quota refusal is DEGRADED rather than DOWN, because `ConnectorHealth.is_usable` admits
#: DEGRADED and a connector refused on volume is a working connector being asked too much.
#: Feeding it to the breaker is what `throttle.A_QUOTA_REFUSAL_IS_NOT_ILL_HEALTH` refuses, and
#: this is the same judgement about the health page.
#:
#: A refusal is UNCONFIGURED rather than DOWN. The request shape is fixed by the specification
#: in this module, so a Lark refusal is almost always a Base the bot was never added to or a
#: scope nobody granted, which is a task for whoever installed it rather than an incident for
#: whoever is on call. `contract.HealthState` keeps the two separate precisely so a rollout
#: does not read as an outage.
HEALTH_BY_OUTCOME: Final[MappingProxyType[CallOutcome, HealthState]] = MappingProxyType(
    {
        CallOutcome.OK: HealthState.OK,
        CallOutcome.TRUNCATED: HealthState.DEGRADED,
        CallOutcome.QUOTA: HealthState.DEGRADED,
        CallOutcome.UNAVAILABLE: HealthState.DOWN,
        CallOutcome.REJECTED: HealthState.UNCONFIGURED,
    }
)


def health(*, outcome: CallOutcome, checked_at: datetime, detail: str = "") -> ConnectorHealth:
    """One probe's result, with the time on it (M11.1.1).

    `checked_at` is a parameter rather than a clock read here, for the reason
    `ConnectorHealth` gives about itself: a health page showing OK with no time on it keeps
    showing OK after the prober stops, which is worse than no health page.
    """
    try:
        state = HEALTH_BY_OUTCOME[outcome]
    except KeyError as exc:  # pragma: no cover - the totality test keeps this unreached
        msg = f"{outcome!r} has no health state; classify it before a probe can report it"
        raise ConnectorContractError(msg) from exc
    return ConnectorHealth(connector=LARK_BASE, state=state, checked_at=checked_at, detail=detail)


# ------------------------------------------------- the reconciliation sweep (M11.4.6)
@dataclass(frozen=True)
class SweepCost:
    """What re-enumerating one table costs, in calls and in wall clock.

    A value an operator reads before agreeing a reconciliation interval, because the cost is
    the surprising part: a deletion is learned about here only by absence, so the sweep reads
    every record in the table, and at a hundred records a page against a share of a hundred
    calls a minute a large table is measured in tens of minutes rather than in seconds.
    """

    records: int
    calls: int
    duration: timedelta
    #: The per-minute rate this was costed at: a share of the tenant's ceiling, not the whole
    #: of it, so a sweep leaves most of the minute for the questions people are asking.
    calls_per_minute: int


def sweep_cost(record_count: int, *, page_size: int = PAGE_SIZE) -> SweepCost:
    """How long an id sweep of this table takes at a fair share of the ceiling.

    Costed at `fair_share_per_minute()` rather than at the whole ceiling, and that is the
    decision. A sweep is the largest single consumer this connector has, and running it at the
    ceiling means every question asked while it runs is refused. The arithmetic is deliberately
    optimistic in one place and it is worth saying so: it assumes every page comes back and
    nothing is retried, so a real sweep takes longer than this and never less.
    """
    if record_count < 0:
        msg = "a table cannot hold a negative number of records"
        raise ValueError(msg)
    if page_size < 1:
        msg = "a page of no records reads nothing and costs a call"
        raise ValueError(msg)
    calls = max(1, math.ceil(record_count / page_size))
    per_minute = fair_share_per_minute()
    return SweepCost(
        records=record_count,
        calls=calls,
        duration=timedelta(minutes=calls / per_minute),
        calls_per_minute=per_minute,
    )


def assert_reconciliation_is_affordable(
    subscription_declared: ChangeSubscription, *, record_count: int
) -> None:
    """Refuse a reconciliation interval this table cannot be swept inside (M11.6.3).

    The check the floor in `brain.connectors.change_signal` cannot make, because that module
    knows how often a pass is owed and not how long one takes. A subscription promising an
    hourly full pass over a table that needs ninety minutes to enumerate is a promise that is
    never kept, and the symptom is a projection that reads as reconciled while deletions
    accumulate in it for ever. Refused at review, where the interval can be changed, rather
    than discovered as a sweep that never finishes.
    """
    cost = sweep_cost(record_count)
    if cost.duration > subscription_declared.reconcile_every:
        msg = (
            f"{subscription_declared.source}.{subscription_declared.entity} promises a full "
            f"pass every {subscription_declared.reconcile_every} and enumerating "
            f"{cost.records} records takes {cost.duration} at {cost.calls_per_minute} calls a "
            f"minute ({cost.calls} calls). A pass that cannot finish inside its own interval "
            "never finishes, and the projection reads as reconciled while deletions "
            f"accumulate. {ONE_QUESTION_MUST_NOT_SPEND_THE_COMPANYS_MINUTE}"
        )
        raise ConnectorContractError(msg)


def subscription(
    table: LarkBaseTable, *, notify_within: timedelta, reconcile_every: timedelta
) -> ChangeSubscription:
    """How Lark tells us a projected record moved, and how a deletion is ever learned.

    Two of the four fields are facts about the API and are fixed here. The cursor is what a
    Base offers (`last_modified_time`), and `ID_SWEEP` is the only deletion check a
    `base:record:read` credential has: a deleted record is not "updated", it is simply one the
    cursor never mentions again, so absence has to be checked for by enumerating the ids the
    source still returns. `brain.connectors.change_signal.A_CURSOR_CANNOT_SEE_A_DELETION`
    names this connector as the case it was written from.

    The two intervals have no defaults and are the deployment's, matching `RefreshPromise`'s
    own refusal to hold one. `assert_reconciliation_is_affordable` is the check that belongs
    beside this and is deliberately a separate call: it needs the table's size, which is a
    fact about the client's data rather than about the declaration.
    """
    return ChangeSubscription(
        source=LARK_BASE,
        entity=table.entity,
        kind=CHANGE_SIGNAL,
        notify_within=notify_within,
        reconcile_every=reconcile_every,
        deletion_check=DeletionCheck.ID_SWEEP,
    )


# --------------------------------------------------------------------------- the manifest
#: What the model reads when it decides whether this tool answers the question. Inside the
#: pinned digest, and written to say the thing that is true of this source and of almost no
#: other: the answer can be a page of a table rather than the table, because the budget is
#: shared with everybody else asking anything.
LIST_TOOL_DESCRIPTION: Final = (
    "Read records from one table of one Lark Base. Returns a page at a time against a shared "
    "per-minute allowance, and reports the result as incomplete when the allowance runs out "
    "before the table does, so a full result is never evidence that it is all of them."
)

READ_TOOL_DESCRIPTION: Final = (
    "Read one record of one Lark Base table by its record id. Linked, lookup, attachment and "
    "person columns are not returned: their values name things held elsewhere and cannot be "
    "represented as a value."
)


def manifest(
    table: LarkBaseTable,
    *,
    host: str,
    credential: CredentialBinding,
    visibility: Scope,
    version: str = VERSION,
) -> ConnectorManifest:
    """Everything this connector declares, for one deployment (M11.6.3).

    **The scope names one table of one Base, and that is a real narrowing.** Unlike an
    account-wide API key, a Lark bot reaches exactly the Bases it has been added to, so the
    scope and the credential can genuinely agree. What it still does not narrow is which
    tables inside a Base the bot can reach, which is the token's business rather than ours, and
    a scope claiming otherwise would read in a console as a boundary that had been enforced.
    `transports.THE_SANDBOX_IS_NOT_IN_THIS_MODULE` makes the same distinction, and the honest
    statement is the same: somebody chose this, and choosing is not enforcing.

    **`ceiling` is the verified source name and not this deployment's.** `throttle.limits_for`
    looks the numbers up by that field, so a connector installed as `acme_hours` with no
    ceiling named would run against no measured limit at all rather than against the hundred a
    minute that cannot be raised.

    **A write binding is refused, which is the reverse of the check the platform already
    makes.** `ConnectorManifest` refuses a write tool on a read-only binding. This refuses a
    write binding under a connector that declares only read tools: a grant covering nothing is
    a permission somebody approved, audited and never used, and it reads in a console as a
    connector that writes into the company's Bases. The bot holds `base:record:read`, so the
    grant would also be a claim the token cannot honour.

    **Every tool declares SERVICE identity**, which is the honest reading of a bot token: the
    source is not enforcing anybody's permissions on our behalf, so ours are the only ones
    there are. That is exactly the case `brain.tools.registry` refuses to register without a
    scope predicate, which is what `visibility` supplies.

    **A projection is declared only when something is projected.** A `ProjectedEntity` with no
    fields is a promise to keep nothing fresh, and it reads in a console exactly like a
    projection that is working.
    """
    if credential.mode is not AccessMode.READ_ONLY:
        msg = (
            f"the binding for {LARK_BASE} is {credential.mode} and this connector declares "
            f"only read tools; the bot holds base:record:read, so a write grant covers nothing "
            "it could do and reads in a console as a connector that writes"
        )
        raise ConnectorContractError(msg)
    # Built rather than only declared, so a host that is not Lark's fails here as well as at
    # the first call, in front of whoever is installing the connector.
    table.operation(Endpoint.LIST_RECORDS, host=host)
    projected = table.projected_bindings()
    return ConnectorManifest(
        name=LARK_BASE,
        version=version,
        transport=TransportKind.REST,
        scope=table.scope(),
        credential=credential,
        tools=(
            ToolDeclaration(
                name=f"{LARK_BASE}.list_{table.entity}",
                description=LIST_TOOL_DESCRIPTION,
                entity=table.entity,
                identity_mode=IdentityMode.SERVICE,
            ),
            ToolDeclaration(
                name=f"{LARK_BASE}.read_{table.entity}",
                description=READ_TOOL_DESCRIPTION,
                entity=table.entity,
                identity_mode=IdentityMode.SERVICE,
            ),
        ),
        projections=(table.projection(visibility=visibility),) if projected else (),
        ceiling=LARK_BASE,
    )
