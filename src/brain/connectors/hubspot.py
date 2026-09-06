"""HubSpot: a CRM, which is mostly the denylist, and the honest answer about what is left.

This connector is the one where the projection tier nearly runs out of things to keep, and
that is the finding rather than a failure to try. A CRM record is a person's email, a
person's telephone number, a person's name and job title, the company they work for, and a
number saying how much money the deal is worth. `brain.core.projection` refuses email, phone,
mobile and address by shape, refuses `contract_value` and `margin` by name, and caps whatever
survives at twelve fields. Work through a contact field by field and the projection that
comes out the other side has **no name on it**: a lifecycle stage, two join keys and a
timestamp. See `A_CRM_IS_MOSTLY_THE_DENYLIST`, which states that plainly, and note what
follows from it: any answer that names a person at a client is a live fetch, every time, and
the fast lane can count contacts and can never list them.

Four things are decided here and each has a wrong version that reviews cleanly.

**A person's name is not a pointer, and the platform's own pointer rule proves it.** HubSpot
splits a name into `firstname` and `lastname`. `manifest.projectability` admits at most one
label per entity kind, so projecting a contact's name means choosing which half of somebody's
name to store, and the rule that stops it is a rule already in the platform rather than a
preference of this module. Both halves are mapped, classified and fetched live; neither is
projected. The wrong version projects `lastname` alone as *the* label, passes all five
clauses, and builds a local copy of the client's contact list one row at a time.

**A deal amount is a contract value in another vocabulary, so it is never stored, and it is
CONFIDENTIAL rather than RESTRICTED.** Those are two separate decisions and both are argued:
`A_DEAL_AMOUNT_IS_A_CONTRACT_VALUE_IN_ANOTHER_VOCABULARY` for the storage refusal, and
`A_PIPELINE_FIGURE_IS_NOT_A_PAYROLL_FIGURE` for the classification. The short form of the
second is that RESTRICTED is where a salary sits, a salary is one identified person's private
financial position, and a deal amount is an organisation's commercial figure whose legitimate
audience is most of the commercial side of the business. Classifying it RESTRICTED would make
RESTRICTED mean "money" instead of "a person's private data", and the level stops
discriminating the moment it has to be granted to everybody in sales.

**An association is a second question, and the transport is what enforces it.** Contacts link
to companies link to deals, and a traversal that inlines the far record hands the caller
fields they never asked for, tagged as the record they did ask for. That is worse than it
sounds: `brain.core.redaction` withholds an unclassified field, but an inlined company `name`
arriving on a contact row is not unclassified, it is *misattributed*, and default-deny cannot
see the difference. So associations come from their own endpoint, which makes a traversal a
separate fetch that the gate entitles again and the redactor masks again. That is not a
promise in a comment: declaring `associations` inside a list response would give the operation
two arrays and `brain.connectors.rest.load_spec` refuses such a document outright, which is the
same refusal `xero.spec_document` earns by leaving `LineItems` out. See
`AN_ASSOCIATION_IS_A_SECOND_QUESTION`, and note what this module deliberately does **not**
do: it does not check reach. A connector that could would be a connector holding the caller's
grants, which `contract.assert_fetches_only` refuses structurally.

**One hop, and the number is derived rather than chosen.** `federation.FANOUT_BUDGET_MS` is two
federated timeouts, and an object fetch followed by an association fetch is exactly two
dependent calls. A second hop is 2,400ms against a 1,600ms budget and `FanOutPlan.assert_within`
refuses it. So `MAX_ASSOCIATION_HOPS` is what the answer lane can already afford, not a
guess about how deep a graph walk should go.

Three traps the recordings and the vendor's own shape put here, and all three fail silently:

**HubSpot returns almost nothing unless you ask for it.** A list call without a `properties`
argument returns ids and system timestamps, so every mapped source path resolves to absent,
every projected field is dropped, and the connector returns rows that are correct, empty and
completely useless. `requested_properties` derives the list from the mapping itself rather
than from a second constant, and `assert_declarations_agree` checks the derivation, because a
hand-maintained property list is a list that goes one field out of date and reports the
missing field as a missing value.

**`HUBSPOT-200-empty` is a genuine absence and it is the only HubSpot response anybody has
recorded.** `total: 0, results: []` is a fact about the CRM. It is not a refusal and it is not
an outage, and `HubSpotReply` refuses at construction to carry rows or a read time on a
failure, so answering a 429 from the last good response is not something a caller can express
rather than something they are asked not to do. What a *person* is told is the same sentence
for a refusal and for an unreachable source, because that distinction is ours to act on and
not theirs; the trace keeps it.

**A wait of nothing is a retry loop with a constant in front of it.**
`brain.ops.limits.backoff_seconds` multiplies the figure the source asked for, so a 429 that
carried no `Retry-After` header multiplies zero and returns zero however many refusals came
before it: read as a policy that says retry at once, then retry at once again. `retry_after`
already refuses to invent a figure and answers None, so `hubspot_retry_delay` takes the None
rather than a number a call site converted it into, and substitutes the platform's own
ceiling. See `A_SOURCE_THAT_SAID_NOTHING_DID_NOT_SAY_ZERO`, and
`brain.connectors.freshdesk.RETRY_AFTER_WHEN_UNSTATED`, which reaches the same figure from the
same argument.

Rejected, and worth stating because each looks tidier:

*Modelling a daily budget the way `brain.connectors.xero` does.* Xero's 5,000 a day is in
`brain.ops.limits`, dated, with the consequence written beside it. HubSpot's is not:
`connector_ceiling("hubspot")` returns None today, so there is no verified figure to build a
budget arithmetic on and building one would produce numbers that look measured. This connector
names the ceiling it would run against and refuses to invent one, which means
`throttle.limits_for` refuses it until somebody adds the row. See
`A_CEILING_NOBODY_VERIFIED_IS_NOT_A_CEILING`.

*Subscribing by webhook.* HubSpot offers webhook subscriptions and they are faster. They also
need a publicly reachable inbound endpoint, which a single-tenant client-hosted install may
not have, and `change_signal.A_WEBHOOK_MISS_IS_SILENT` says what a delivery that never
arrives leaves behind, which is nothing. A cursor's failure is visible from the cursor. So
this declares `UPDATED_SINCE`, pays the price named in `A_CURSOR_CANNOT_SEE_A_DELETION` by
declaring an id sweep, and says so rather than claiming a push it may not receive.

*Calling this connector's person entity something of its own.* `brain.core.field_policy` is
keyed by entity name, so `contact` here and `contact` in `brain.connectors.xero` are one key
and `FieldPolicy` raises rather than choosing between two opinions about it. Renaming would
have made the two unjoinable in the entity registry, which is the thing that makes federation
work at all. Instead every rule this module declares for `contact` is either a field Xero does
not declare or is declared identically to Xero's, and `assert_policy_merges_with` is the check
rather than the intention. See `TWO_SOURCES_ONE_ENTITY_NAME`.

*Naming the company entity `company`.* The house noun for a customer organisation is `client`,
and the canaries, the redaction invariants and the access route all classify `client.name`
already. A CRM company and a ledger client are the same real organisation, and giving them two
names would mean no answer could ever join them.

Scope: domain logic. Nothing here opens a connection, resolves a name, reads a clock or holds
a credential. The resolver, the fetcher and `fetched_at` are all parameters, and
`assert_holds_no_credential` runs on the connection at construction.

Task ids: M11.6.6
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Final

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
from brain.connectors.federation import (
    FEDERATION_TIMEOUT_MS,
    FailureReason,
    FanOutPlan,
    PartialAnswer,
    SourceCall,
    SourceFailure,
)
from brain.connectors.manifest import (
    ChangeSignal,
    ConnectorManifest,
    FieldShape,
    HotUse,
    ProjectedEntity,
    ProjectedField,
    ToolDeclaration,
)
from brain.connectors.projection import ProjectedRecord, ProjectedValue, RefreshPromise
from brain.connectors.rest import ID_TARGET, RestOperation, RestSpec, load_spec
from brain.connectors.throttle import CallOutcome, ceiling_for, classify, retry_delay
from brain.connectors.transports import FieldMapping, RestTransport, SourceRecord
from brain.core.envelope import IdentityMode, SideEffect, TypedResult
from brain.core.field_policy import Classification, FieldPolicy, FieldRule
from brain.core.projection import ProjectionRefusedError
from brain.core.scope import Clause, Op, Scope
from brain.gate.provenance import Freshness, StalenessHorizon, assess_freshness
from brain.ops.limits import MAX_BACKOFF_SECONDS, ConnectorLimit, connector_ceiling
from brain.ops.secrets import SecretRef
from brain.tools.fetch import Fetcher, Resolver

# ------------------------------------------------------------------ written-down reasons
#: What is actually left of a CRM record once the platform's storage rules have run.
A_CRM_IS_MOSTLY_THE_DENYLIST = (
    "A CRM contact is an email address, a telephone number, a name, a job title and the "
    "company somebody works for. brain.core.projection refuses email, phone, mobile and "
    "address by shape, and this module refuses the name and the job title because a person "
    "is not a pointer to a record, they are the record. What survives into the projection is "
    "a lifecycle stage, two join keys and a timestamp: enough to count contacts per client "
    "and per stage, enough to join them to a ledger row, and not enough to say who anybody "
    "is. That is the honest outcome of federating a CRM rather than mirroring one, and it "
    "should be stated in the design instead of discovered by whoever asks the fast lane for "
    "a list of names and gets rows with no names in them."
)

#: Why a deal amount may never be projected, when it is not on the platform's own list.
A_DEAL_AMOUNT_IS_A_CONTRACT_VALUE_IN_ANOTHER_VOCABULARY = (
    "brain.core.projection.NEVER_PROJECT already refuses contract_value and margin. A deal "
    "amount is a client's contract value spelled the way HubSpot spells it, so the platform "
    "has already made this decision and the only thing missing is the vendor's word for it. "
    "Nothing outside this module refuses it: declared as a status enum with a filter use it "
    "passes all five clauses of manifest.projectability and a reviewer sees nothing wrong. "
    "Stored, it is filtered and quoted as current long after the deal closed at a different "
    "number, which is the one figure a salesperson will not forgive being wrong."
)

#: Why the deal amount is CONFIDENTIAL and a salary is RESTRICTED.
A_PIPELINE_FIGURE_IS_NOT_A_PAYROLL_FIGURE = (
    "RESTRICTED is where hr.salary sits, and a salary is one identified person's private "
    "financial position: disclosing it inside the company is a personal-data incident with a "
    "named victim. A deal amount is an organisation's commercial figure. Its legitimate "
    "audience is most of the commercial side of the business, because the people who write "
    "the proposal, deliver the work and invoice it all need it, and the harm from "
    "over-disclosure is commercial rather than personal. Classifying it RESTRICTED would do "
    "two bad things at once: it would make RESTRICTED mean 'money' instead of 'a person's "
    "private data', and it would force a grant so wide that the level stops discriminating "
    "between the deal and the payroll run. CONFIDENTIAL is a statement about who may read "
    "it; the projection refusal is a separate statement about what may be kept, and both "
    "apply. brain.core.field_policy names that combination as the ordinary case."
)

#: Why an association is fetched separately rather than attached to the record it belongs to.
AN_ASSOCIATION_IS_A_SECOND_QUESTION = (
    "Inlining an associated record answers a question nobody asked, and the fields arrive "
    "tagged as the record that was asked for. An unclassified field is withheld by "
    "default-deny, so the naive reading is that the platform already handles this; it does "
    "not, because a company's name inlined onto a contact row is not unclassified, it is "
    "misattributed, and the redactor has no way to tell that the value under 'name' belongs "
    "to a different record with a different visibility. So a traversal is a separate fetch "
    "with its own entity tag, entitled again by the gate and masked again by the redactor. "
    "This module does not and must not check reach itself: a connector handed the caller's "
    "grants is exactly what contract.assert_fetches_only refuses, and a second permission "
    "model expressed in a connector would be the permissive copy."
)

#: Why one hop, and why the number was not chosen.
ONE_HOP_IS_WHAT_THE_ANSWER_LANE_CAN_AFFORD = (
    "federation.FANOUT_BUDGET_MS is two federated timeouts, and an association traversal is "
    "an object fetch followed by a dependent association fetch, which is exactly two. A "
    "second hop is three dependent calls, which FanOutPlan.assert_within already refuses "
    "against the same budget. So the cap is arithmetic the platform had already done rather "
    "than a judgement about how far a graph walk should go, and a reviewer can check it "
    "without having an opinion about CRMs."
)

#: Why this connector runs against no ceiling at all rather than the recorded figure.
A_CEILING_NOBODY_VERIFIED_IS_NOT_A_CEILING = (
    "tests/fixtures/cassettes.py records 10,000 calls a day per app per account, and "
    "brain.ops.limits does not carry that figure. The console, the admission ladder and "
    "throttle.limits_for all read brain.ops.limits, so a number restated here would be a "
    "second answer sitting beside three verified ones and looking exactly like them. This "
    "connector therefore names the ceiling it would run against and reads it rather than "
    "declaring it, which means limits_for refuses this connector until somebody adds the "
    "row. Refusing is the intended behaviour: brain.ops.limits returns nothing for an "
    "unknown source precisely so that nobody runs one against no limit at all. Naming the "
    "ceiling rather than leaving it empty is deliberate too, because it makes adding the "
    "verified row the only edit anybody has to make."
)

#: Why an unreachable CRM is never reported as an empty one.
AN_UNREACHABLE_CRM_IS_NOT_AN_EMPTY_ONE = (
    "'There are no open deals with that client' and 'I could not read the CRM' are opposite "
    "answers and one of them gets acted on. HUBSPOT-200-empty is the recording that exists "
    "precisely to keep them apart: a genuine absence, as distinct from a refusal and from an "
    "outage. A connector that returned an empty list for a 429 would produce the first "
    "sentence out of the second fact, nobody would file a bug because the answer looked like "
    "data, and the mistake would be invisible in exactly the questions a CRM connector "
    "exists for. So a failed reply has nowhere to put rows and nowhere to put a read time, "
    "which makes answering from memory unexpressible rather than merely discouraged."
)

#: Why a refusal that named no wait is given the longest one rather than none.
A_SOURCE_THAT_SAID_NOTHING_DID_NOT_SAY_ZERO = (
    "brain.ops.limits.backoff_seconds multiplies the figure the source asked for, so a "
    "refusal carrying no Retry-After header multiplies zero and returns zero however many "
    "refusals came before it. Taken as a wait, that is an instruction to come back "
    "immediately, and the client that comes back immediately is the one that turns a burst "
    "into a rate limit and then keeps it there, which is the failure this whole function "
    "exists to prevent. The substituted figure is the platform's own MAX_BACKOFF_SECONDS "
    "rather than a number chosen here, and it is deliberately the long end for the reason "
    "brain.connectors.freshdesk.RETRY_AFTER_WHEN_UNSTATED gives: guessing low spends what is "
    "left of a daily allowance faster, while a wait that is too long costs one question its "
    "freshness. retry_after answers None rather than zero for exactly this reason, so this "
    "function accepts the None and does the substitution itself. A call site writing "
    "retry_after(headers) or 0.0 is where the zero comes back, and there is no such call "
    "site to write if the parameter takes what retry_after returns."
)

#: Why the portal is pinned at connect even though the token already implies one.
A_PORTAL_IS_PINNED_AT_CONNECT = (
    "A private-app token is issued for one HubSpot account, so unlike a Xero tenant the "
    "portal is not routed by anything this module sends. The pin is therefore not what keeps "
    "the call in the right account; it is what catches the case nothing else would notice, "
    "which is a connector row naming portal A while the vault path holds a token for portal "
    "B. Every row that came back would be real, every test would pass, and the answers would "
    "be about somebody else's company. It also makes the scope inspectable in a console row, "
    "and ConnectorScope matches by exact membership so 24681357 never admits 246813570."
)

#: What HubSpot's own permission model is, and what may honestly be stored from it.
HUBSPOT_VISIBILITY_IS_THE_PORTAL_UNTIL_OWNERS_ARE_MAPPED = (
    "HubSpot narrows by portal and, where a client configures it, by record owner and team. "
    "The predicate stored with a projection has to be evaluated against our own entitlement "
    "set, and a HubSpot owner id is not one of our principals: storing 'owner_id = "
    "12345678' would be storing a predicate nothing can evaluate, which silently admits "
    "every row while reading as a restriction. So the predicate is the portal, which is the "
    "true statement, and owner_id is projected as a join key so that the narrowing becomes "
    "available the day the directory maps HubSpot owners to people. That mapping does not "
    "exist yet, and saying so is better than a predicate that implies it does."
)

#: Why two connectors may write rules for the same entity name, and what has to hold.
TWO_SOURCES_ONE_ENTITY_NAME = (
    "brain.core.field_policy is keyed by (entity, field), and FieldPolicy raises "
    "PolicyConflictError rather than resolving two different opinions about one key by merge "
    "order. Two connectors contributing to one entity kind is the ordinary case here, "
    "because the entity registry exists to join a CRM company to a ledger client. So the "
    "requirement is not separate names, it is agreement: every rule declared here for a "
    "field another source also declares is spelled identically to the house rule, and "
    "assert_policy_merges_with is how that is checked rather than remembered."
)

#: Why the mapping, the projection, the property request and the policy are compared.
FOUR_DECLARATIONS_THAT_DRIFT_APART_QUIETLY = (
    "A mapping says what arrives, a projection says what is kept, a property request says "
    "what the vendor is asked to send, and a policy says who may read it. All four are "
    "edited by different people at different times and every disagreement between them is "
    "silent. A mapped field with no rule is withheld from everybody by default-deny, which "
    "is safe and pointless. A projected field with no mapping is a column that never "
    "arrives, so a fast-lane filter on it matches nothing and returns an empty list nobody "
    "questions. And a mapped source path missing from the property request is the worst of "
    "the three, because HubSpot answers it with a well-formed record that simply does not "
    "contain the field, so the connector looks correct and returns rows with holes in them."
)


# ------------------------------------------------------------------------------- names
CONNECTOR_NAME: Final = "hubspot"

#: The name this connector's ceiling would be registered under in `brain.ops.limits`. Read
#: rather than restated: see `A_CEILING_NOBODY_VERIFIED_IS_NOT_A_CEILING`.
CEILING_NAME: Final = "hubspot"

#: What `TypedResult.source` and `ProjectedRecord.source` carry. The specification is named
#: rather than embedded, for the reason `RestTransport.spec_ref` gives.
SPEC_REF: Final = "hubspot"

MANIFEST_VERSION: Final = "1.0.0"

BASE_URL: Final = "https://api.hubapi.com"

#: The house noun for a customer organisation, which is what a HubSpot company is. See the
#: module docstring on why this is not called `company`.
ENTITY_CLIENT: Final = "client"
ENTITY_CONTACT: Final = "contact"
ENTITY_DEAL: Final = "deal"
#: An edge, and nothing on the far end of it. See `AN_ASSOCIATION_IS_A_SECOND_QUESTION`.
ENTITY_ASSOCIATION: Final = "association"

#: A filter key naming the account. Accepted so a caller may state which portal they believe
#: they are addressing, and checked against the pin; never used to choose one.
PORTAL_FILTER: Final = "portal"

#: HubSpot's own names for the two paging parameters. Held here rather than left to
#: `brain.connectors.rest`, which refuses a limit or a cursor precisely because paging is a
#: parameter name only the vendor's spec knows. This module knows it, so it can honour one.
PAGE_SIZE_PARAMETER: Final = "limit"
CURSOR_PARAMETER: Final = "after"

#: The parameter that decides whether a record comes back with anything on it at all.
PROPERTIES_PARAMETER: Final = "properties"

#: HubSpot's list endpoints refuse a page larger than this. Refused rather than clamped: a
#: silently clamped page is an under-count that reads as a complete answer, which is the same
#: mistake `brain.ops.limits.SEARCH_CAP_IS_NOT_A_PAGE_SIZE` describes from the other side.
MAX_PAGE_SIZE: Final = 100

#: How deep a traversal may go. Derived, not chosen: see
#: `ONE_HOP_IS_WHAT_THE_ANSWER_LANE_CAN_AFFORD`.
MAX_ASSOCIATION_HOPS: Final = 1

#: How often the cursor is polled, which is how quickly a change is expected to reach us.
CURSOR_POLL_INTERVAL: Final = timedelta(minutes=15)

#: How often every record is re-read regardless, which is the interval staleness is measured
#: against and the pass that performs the id sweep. Six hours, and the arithmetic is worth
#: writing down because a full pass is the expensive half: at 100 records a page, roughly
#: 2,000 companies, 5,000 contacts and 1,000 deals is 80 calls a pass and 320 a day, and the
#: quarter-hourly cursor poll over three entity kinds is another 288. Around 600 calls a day
#: against the 10,000 the vendor documents, which leaves the ceiling for questions.
RECONCILIATION_INTERVAL: Final = timedelta(hours=6)

RETRY_AFTER_HEADER: Final = "Retry-After"

#: What a refusal that stated no wait, or stated a zero, is treated as having asked for. The
#: platform's own ceiling rather than a figure invented here, and the same one
#: `brain.connectors.freshdesk.RETRY_AFTER_WHEN_UNSTATED` reaches from the same argument. See
#: `A_SOURCE_THAT_SAID_NOTHING_DID_NOT_SAY_ZERO`.
RETRY_AFTER_WHEN_UNSTATED: Final = MAX_BACKOFF_SECONDS

#: What a person is told, and what an operator is told, and they are different lengths on
#: purpose. Every one is a constant: a detail assembled from a response body would put a
#: filter value, and therefore a client's name, into a health row and a trace that have a
#: different audience and retention from the answer they describe.
DETAIL_ANSWERED: Final = "answering"
DETAIL_RATE_LIMITED: Final = "the account's call allowance refused this call"
DETAIL_UNAUTHORISED: Final = "the source declined this connector's authorisation"
DETAIL_UNAVAILABLE: Final = "the source did not answer"
DETAIL_TIMED_OUT: Final = "the source did not answer in time"
DETAIL_NEVER_PROBED: Final = "nothing has probed this connector since it was installed"


class HubSpotError(ConnectorContractError):
    """A HubSpot connector was declared, or asked, for something it cannot hold.

    A `ConnectorContractError` for the reason that class gives: every refusal in this package
    is a mistake by whoever wrote or called the connector, it should stop the connector rather
    than degrade an answer, and nobody asking a question should ever see it. A request for a
    portal this connection is not pinned to is that kind of mistake and not an outcome, so
    there is no reply shape for one.
    """


# ----------------------------------------------------------- the connection (M11.2.3)
@dataclass(frozen=True)
class HubSpotConnection:
    """One HubSpot account, decided at connect, and nothing else.

    No client, no session and no credential: `assert_holds_no_credential` runs on the class at
    construction rather than being promised in a comment, so a later attribute called
    `private_app_token` fails the first time anybody builds one. See
    `contract.ROTATION_NEEDS_NO_REDEPLOY` for what that buys.

    There is deliberately no `call_headers` here, unlike `brain.connectors.xero`. The only
    header a HubSpot call carries is `Authorization`, which is minted from a lease by whoever
    borrowed it, and nothing in this module may see it or would have anywhere to keep it. A
    connector with nothing to contribute to a call should have no method that looks as though
    it might.
    """

    portal_id: str

    def __post_init__(self) -> None:
        assert_holds_no_credential(type(self))
        # Constructing the scope is the check: ConnectorScope refuses an unbounded selector
        # and a selector the source would not recognise. Repeating either rule here would be
        # a second opinion about what "narrows nothing" means.
        self.scope()

    def scope(self) -> ConnectorScope:
        """What this connector was connected to. One account, named."""
        return ConnectorScope(resource_kind="portal", selectors=(self.portal_id,))

    def admits(self, portal_id: str) -> bool:
        """Whether this connection covers that account. Exact membership, never a prefix."""
        return self.scope().admits(portal_id)

    def assert_admits(self, portal_id: str) -> None:
        """Refuse a call addressed to an account this connection is not pinned to.

        Refused rather than answered from the pinned portal, which is the tempting version and
        is worse: it returns another company's pipeline under the name of the one that was
        asked for, and every test passes because the rows are real.
        """
        if not self.admits(portal_id):
            msg = (
                f"this connection is pinned to one HubSpot account and was asked for "
                f"{portal_id!r}. {A_PORTAL_IS_PINNED_AT_CONNECT}"
            )
            raise HubSpotError(msg)

    def visibility(self) -> Scope:
        """HubSpot's model as a predicate we can actually evaluate.

        See `HUBSPOT_VISIBILITY_IS_THE_PORTAL_UNTIL_OWNERS_ARE_MAPPED`. `Op.EQ` and never
        `Op.IN` over a principal field, which `ProjectedEntity` refuses as a resolved ACL
        wearing a predicate's shape.
        """
        return Scope(clauses=(Clause(field="portal_id", op=Op.EQ, value=self.portal_id),))


# ------------------------------------------------------------- the ceiling (M11.3.5)
def ceiling_is_verified() -> bool:
    """Whether `brain.ops.limits` carries a measured ceiling for this connector yet.

    A question rather than an assumption, because the answer changes the day somebody
    verifies HubSpot's published figures and adds the row. Nothing in this module holds a
    number of its own for it to disagree with.
    """
    return connector_ceiling(CEILING_NAME) is not None


def day_ceiling(manifest: ConnectorManifest) -> ConnectorLimit:
    """The verified ceiling this connector runs against, from where it was verified.

    Delegated to `brain.connectors.throttle.ceiling_for`, which raises
    `UnmeasuredSourceError` when there is nothing verified to return. That refusal is the
    intended behaviour today rather than a gap to work around: see
    `A_CEILING_NOBODY_VERIFIED_IS_NOT_A_CEILING`. The manifest is a parameter rather than
    built here, so the number an operator reads is the number the connector in front of them
    actually runs against.
    """
    return ceiling_for(manifest)


def retry_after(headers: Mapping[str, str]) -> float | None:
    """What the source asked us to wait, in seconds, or None when it did not say.

    None rather than a default, for the reason `brain.ops.limits` refuses to invent a
    ceiling: a missing header means we learned nothing, and a zero substituted for it would
    send the next call straight back into a refusal we were told about.

    Matched without regard to case. Header names are case-insensitive on the wire and
    case-sensitive in a dictionary, and a connector that only recognised the vendor's own
    capitalisation would silently stop reading the instruction the day something normalised
    the headers on the way through.
    """
    wanted = RETRY_AFTER_HEADER.casefold()
    for key, value in headers.items():
        if key.casefold() != wanted:
            continue
        try:
            return float(str(value).strip())
        except ValueError:
            return None
    return None


def hubspot_retry_delay(
    *, retry_after_seconds: float | None, consecutive_refusals: int, jitter: float = 0.0
) -> float:
    """How long to wait after a refusal. The longer of two honest numbers.

    The platform's arithmetic is `brain.connectors.throttle.retry_delay` and is not restated.
    What is added is the one wait it cannot know about, which is what the source actually
    asked for: `brain.ops.limits.backoff_seconds` caps a wait at 300 seconds, correct for a
    window that refills in sixty and wrong for a daily allowance, and HubSpot publishes a
    daily one. Coming back before the source's own stated wait spends a call on a refusal we
    were already told about.

    **A source that said nothing is not a source that said zero.** The platform's arithmetic
    multiplies the figure it is handed, so a refusal carrying no header would produce a wait
    of zero however many refusals preceded it, and this function would be a retry loop with a
    `max` in front of it. `None` is accepted rather than a converted number, so
    `retry_after`'s refusal to invent a figure survives the journey to here instead of being
    undone by a call site. See `A_SOURCE_THAT_SAID_NOTHING_DID_NOT_SAY_ZERO`.

    Deliberately shorter than `brain.connectors.xero.xero_retry_delay`, which takes a third
    number from the time left until the tenant's day resets. That number cannot be computed
    here without a verified ceiling and a reset instant, and computing it from the recorded
    figure would be exactly the invention `A_CEILING_NOBODY_VERIFIED_IS_NOT_A_CEILING`
    refuses.
    """
    stated = (
        RETRY_AFTER_WHEN_UNSTATED
        if retry_after_seconds is None or retry_after_seconds <= 0.0
        else retry_after_seconds
    )
    platform = retry_delay(
        retry_after_seconds=stated,
        consecutive_refusals=consecutive_refusals,
        jitter=jitter,
    )
    return max(platform, stated)


# -------------------------------------------------------------- the spec (M11.1.3)
#: HubSpot's own property names, per entity kind. Declared before the specification because
#: the specification is built from them: the schema, the property request and the field
#: mapping all have to name the same strings, and three copies of a list is three chances for
#: one of them to be a field behind.
COMPANY_PROPERTIES: Final[tuple[str, ...]] = (
    "name",
    "domain",
    "lifecyclestage",
    "hubspot_owner_id",
    "hs_lastmodifieddate",
)
CONTACT_PROPERTIES: Final[tuple[str, ...]] = (
    "firstname",
    "lastname",
    "jobtitle",
    "lifecyclestage",
    "associatedcompanyid",
    "hubspot_owner_id",
    "hs_lastmodifieddate",
)
DEAL_PROPERTIES: Final[tuple[str, ...]] = (
    "dealname",
    "amount",
    "dealstage",
    "pipeline",
    "closedate",
    "hubspot_owner_id",
)


def _object_schema(*properties: str) -> Mapping[str, Any]:
    """One CRM object as HubSpot returns it: an id, some system fields, a properties bag.

    Nothing here declares an array, and that is the point rather than an omission. Adding
    `associations` would give the response two arrays, `load_spec` would refuse the document,
    and the refusal would be correct twice over: which array held the records would be decided
    by key order, and an inlined association is the misattribution
    `AN_ASSOCIATION_IS_A_SECOND_QUESTION` describes.
    """
    return {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "createdAt": {"type": "string"},
            "updatedAt": {"type": "string"},
            "archived": {"type": "boolean"},
            "properties": {
                "type": "object",
                "properties": {name: {"type": "string"} for name in properties},
            },
        },
    }


def _list_response(item: Mapping[str, Any]) -> Mapping[str, Any]:
    """The `results` envelope both the list and the search endpoints answer with."""
    return {
        "200": {
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"results": {"type": "array", "items": item}},
                    }
                }
            }
        }
    }


_LIST_PARAMETERS: Final[tuple[Mapping[str, Any], ...]] = (
    {"name": PROPERTIES_PARAMETER, "in": "query", "required": False},
    {"name": PAGE_SIZE_PARAMETER, "in": "query", "required": False},
    {"name": CURSOR_PARAMETER, "in": "query", "required": False},
    {"name": "archived", "in": "query", "required": False},
)


def spec_document() -> Mapping[str, Any]:
    """The minimum OpenAPI this connector needs, as data.

    Four operations and one server, because `load_spec` refuses a document listing several
    and the reason carries: a document naming production and a sandbox leaves which host is
    called to list order, and only one of them was checked.

    The three object operations are the list endpoints and are GETs. `HUBSPOT-200-empty` was
    recorded against `/crm/v3/objects/companies/search`, which HubSpot serves as a POST, and
    `brain.connectors.rest.READ_METHOD` admits GET alone on purpose: a transport that could
    send a body is a write path and needs the read-back rule in `brain.connectors.throttle`
    rather than that module's silence. The two endpoints answer with the same `results`
    envelope, so the recorded absence is projected through exactly the shape declared here,
    and the difference is the method rather than the body.

    The association operation is separate, takes the object in its path, and answers with
    edges. That separation is the enforcement described in
    `AN_ASSOCIATION_IS_A_SECOND_QUESTION`.
    """
    company = _object_schema(*COMPANY_PROPERTIES)
    contact = _object_schema(*CONTACT_PROPERTIES)
    deal = _object_schema(*DEAL_PROPERTIES)
    edge = {
        "type": "object",
        "properties": {"id": {"type": "string"}, "type": {"type": "string"}},
    }
    return {
        "openapi": "3.0.3",
        "servers": [{"url": BASE_URL}],
        "paths": {
            "/crm/v3/objects/companies": {
                "get": {
                    "operationId": "getCompanies",
                    "parameters": list(_LIST_PARAMETERS),
                    "responses": _list_response(company),
                }
            },
            "/crm/v3/objects/contacts": {
                "get": {
                    "operationId": "getContacts",
                    "parameters": list(_LIST_PARAMETERS),
                    "responses": _list_response(contact),
                }
            },
            "/crm/v3/objects/deals": {
                "get": {
                    "operationId": "getDeals",
                    "parameters": list(_LIST_PARAMETERS),
                    "responses": _list_response(deal),
                }
            },
            "/crm/v3/objects/{objectType}/{objectId}/associations/{toObjectType}": {
                "get": {
                    "operationId": "getAssociations",
                    "parameters": [
                        {"name": "objectType", "in": "path", "required": True},
                        {"name": "objectId", "in": "path", "required": True},
                        {"name": "toObjectType", "in": "path", "required": True},
                        {"name": PAGE_SIZE_PARAMETER, "in": "query", "required": False},
                        {"name": CURSOR_PARAMETER, "in": "query", "required": False},
                    ],
                    "responses": _list_response(edge),
                }
            },
        },
    }


def load_hubspot_spec(*, resolver: Resolver) -> RestSpec:
    """Parse the document and refuse its address before anything is built.

    The resolver is a parameter for the reason `brain.connectors.rest` gives: the address
    check is the same one the skill importer applies, imported rather than restated, and a
    module that resolved names itself could not be tested against the case that matters,
    which is a name answering publicly and then privately.
    """
    return load_spec(spec_document(), resolver=resolver)


# ------------------------------------------------------------------- the mappings
#: What arrives from the companies endpoint. `name` is a company's name and is a label; no
#: telephone number, no address and no annual revenue is mapped, because a field nothing
#: classifies is withheld from everybody and mapping one would move it through this process
#: and into a trace in exchange for nothing at all.
COMPANY_FIELDS: Final[tuple[FieldMapping, ...]] = (
    FieldMapping(target=ID_TARGET, source_path="id"),
    FieldMapping(target="name", source_path="properties.name"),
    FieldMapping(target="domain", source_path="properties.domain"),
    FieldMapping(target="lifecycle_stage", source_path="properties.lifecyclestage"),
    FieldMapping(target="owner_id", source_path="properties.hubspot_owner_id"),
    FieldMapping(target="updated_at", source_path="properties.hs_lastmodifieddate"),
)

#: What arrives from the contacts endpoint, and the entity where the denylist does most of
#: its work. No email, no phone, no mobile, no address: every one of them is refused by
#: `brain.core.projection` by shape, and none is mapped or classified either, so default-deny
#: withholds them from everybody. Both halves of the name and the job title are mapped and
#: classified, and none of the three is projected. See `A_CRM_IS_MOSTLY_THE_DENYLIST`.
CONTACT_FIELDS: Final[tuple[FieldMapping, ...]] = (
    FieldMapping(target=ID_TARGET, source_path="id"),
    FieldMapping(target="first_name", source_path="properties.firstname"),
    FieldMapping(target="last_name", source_path="properties.lastname"),
    FieldMapping(target="job_title", source_path="properties.jobtitle"),
    FieldMapping(target="lifecycle_stage", source_path="properties.lifecyclestage"),
    FieldMapping(target="company_id", source_path="properties.associatedcompanyid"),
    FieldMapping(target="owner_id", source_path="properties.hubspot_owner_id"),
    FieldMapping(target="updated_at", source_path="properties.hs_lastmodifieddate"),
)

#: What arrives from the deals endpoint. `amount` is here and is deliberately absent from the
#: projection below: it is the answer people ask for, it is CONFIDENTIAL, and it is fetched
#: live every time. See `A_DEAL_AMOUNT_IS_A_CONTRACT_VALUE_IN_ANOTHER_VOCABULARY`.
DEAL_FIELDS: Final[tuple[FieldMapping, ...]] = (
    FieldMapping(target=ID_TARGET, source_path="id"),
    FieldMapping(target="deal_name", source_path="properties.dealname"),
    FieldMapping(target="amount", source_path="properties.amount"),
    FieldMapping(target="stage", source_path="properties.dealstage"),
    FieldMapping(target="pipeline", source_path="properties.pipeline"),
    FieldMapping(target="close_date", source_path="properties.closedate"),
    FieldMapping(target="owner_id", source_path="properties.hubspot_owner_id"),
)

#: An edge and nothing else. The id on the far end and what kind of link it is, and there is
#: no source path here that could reach a property of the associated record.
ASSOCIATION_FIELDS: Final[tuple[FieldMapping, ...]] = (
    FieldMapping(target=ID_TARGET, source_path="id"),
    FieldMapping(target="kind", source_path="type"),
)

_MAPPINGS: Final[Mapping[str, tuple[FieldMapping, ...]]] = MappingProxyType(
    {
        ENTITY_CLIENT: COMPANY_FIELDS,
        ENTITY_CONTACT: CONTACT_FIELDS,
        ENTITY_DEAL: DEAL_FIELDS,
        ENTITY_ASSOCIATION: ASSOCIATION_FIELDS,
    }
)

_OPERATIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        ENTITY_CLIENT: "getCompanies",
        ENTITY_CONTACT: "getContacts",
        ENTITY_DEAL: "getDeals",
        ENTITY_ASSOCIATION: "getAssociations",
    }
)

#: The prefix every property-bag source path carries. Named so `requested_properties` reads
#: the vendor's property name off the mapping rather than being told it twice.
_PROPERTY_PREFIX: Final = "properties."


def mapped_targets(entity: str) -> tuple[str, ...]:
    """Every field name this connector's mapping produces for one entity, except the id.

    The id is excluded because it is structural rather than classified: `SourceRecord`
    carries it as the record's name, and no field policy rule governs it anywhere in this
    system.
    """
    fields = _MAPPINGS.get(entity)
    if fields is None:
        msg = f"this connector maps {sorted(_MAPPINGS)} and was asked for {entity!r}"
        raise HubSpotError(msg)
    return tuple(f.target for f in fields if f.target != ID_TARGET)


def requested_properties(entity: str) -> tuple[str, ...]:
    """The vendor property names this entity's mapping needs, derived from the mapping.

    Derived rather than declared, and that is the whole of the guard. HubSpot's list
    endpoints return ids and system timestamps and nothing else unless the call names the
    properties it wants, so a property missing from this list is a field that arrives absent
    from a perfectly well-formed record. `RestOperation.project` then contributes nothing for
    it, `projected_record` drops it, and the connector returns rows with holes in them and no
    error anywhere. A hand-maintained list is a list that goes one field out of date.
    """
    fields = _MAPPINGS.get(entity)
    if fields is None:
        msg = f"this connector maps {sorted(_MAPPINGS)} and was asked for {entity!r}"
        raise HubSpotError(msg)
    return tuple(
        sorted(
            f.source_path[len(_PROPERTY_PREFIX) :]
            for f in fields
            if f.source_path.startswith(_PROPERTY_PREFIX)
        )
    )


def default_arguments(entity: str) -> Mapping[str, str]:
    """The arguments every call to this entity carries unless the caller overrides them.

    Two, and the first is the one that matters: without `properties` the vendor answers with
    a record that has none. The page size is here so that a caller who says nothing still
    gets a deterministic page rather than whatever the vendor's default happens to be this
    quarter.

    The association operation declares no `properties` parameter and gets neither, because
    `RestOperation.url_for` refuses an argument the operation does not declare: a spec-driven
    adapter cannot invent one, and nothing would say whether the source reads it.
    """
    wanted = requested_properties(entity)
    if not wanted:
        return MappingProxyType({})
    return MappingProxyType(
        {
            PROPERTIES_PARAMETER: ",".join(wanted),
            PAGE_SIZE_PARAMETER: str(MAX_PAGE_SIZE),
        }
    )


def operation_for(entity: str, *, resolver: Resolver) -> RestOperation:
    """The bound operation for one entity kind, spec and mapping compared."""
    operation_id = _OPERATIONS.get(entity)
    if operation_id is None:
        msg = (
            f"this connector reads {sorted(_OPERATIONS)} and was asked for {entity!r}; an "
            "entity nothing maps would be fetched as an empty result, which reads as an "
            "empty CRM"
        )
        raise HubSpotError(msg)
    transport = RestTransport(
        spec_ref=SPEC_REF,
        operation=operation_id,
        entity=entity,
        fields=_MAPPINGS[entity],
    )
    return load_hubspot_spec(resolver=resolver).bind(transport)


def connector_fetch(
    connection: HubSpotConnection,
    entity: str,
    *,
    fetcher: Fetcher,
    resolver: Resolver,
    fetched_at: str,
) -> Callable[[FetchRequest], TypedResult[SourceRecord]]:
    """This connection's read side, as the one shape a connector's fetch may take (M11.1.1).

    Three things happen here that `brain.connectors.rest.as_fetch` cannot do for itself.

    **The portal is checked before an address is built**, so a request naming another account
    never reaches the transport, never resolves a name and never spends a call.

    **A limit and a cursor are translated into the vendor's own parameter names.** The generic
    adapter refuses both, correctly, because paging is a parameter name only the vendor's spec
    knows and answering a request for fifty records with all of them is a wrong answer that
    reads as a right one. This module knows the names, so it can honour the request instead of
    refusing it. A page size above the vendor's ceiling is refused rather than clamped: a
    clamped page is an under-count that looks complete.

    **The property request is attached.** Without it every mapped field arrives absent. See
    `requested_properties`.

    `assert_fetches_only` runs on the closure rather than on this function, because the
    closure is the object a registry would call and therefore the object whose signature has
    to be shown never to receive the caller's grants.
    """
    inner = operation_for(entity, resolver=resolver).as_fetch(
        fetcher=fetcher, resolver=resolver, fetched_at=fetched_at
    )

    def _fetch(request: FetchRequest) -> TypedResult[SourceRecord]:
        for key, value in request.filters:
            if key == PORTAL_FILTER:
                connection.assert_admits(value)
        arguments = dict(default_arguments(entity))
        arguments.update((k, v) for k, v in request.filters if k != PORTAL_FILTER)
        if request.limit:
            if request.limit > MAX_PAGE_SIZE:
                msg = (
                    f"a page of {request.limit} records is over the vendor's {MAX_PAGE_SIZE}; "
                    "clamping it silently would return a short page that reads as a complete "
                    "one, so the request is refused and the caller pages"
                )
                raise HubSpotError(msg)
            arguments[PAGE_SIZE_PARAMETER] = str(request.limit)
        if request.cursor:
            arguments[CURSOR_PARAMETER] = request.cursor
        passed = replace(request, filters=tuple(sorted(arguments.items())), limit=0, cursor="")
        return inner(passed)

    assert_fetches_only(_fetch)
    return _fetch


# ----------------------------------------------------- the projection (M11.4.2, M11.4.4)
#: Fields this connector fetches live and may never store, in HubSpot's spelling and in ours.
#: Three groups, and none of them is caught by `brain.core.projection.NEVER_PROJECT`:
#:
#: *Money about a client's business.* `contract_value` and `margin` are on the platform list
#: and `amount` is not, which is a difference in vocabulary rather than in kind.
#:
#: *A person rather than a pointer to one.* A name and a job title are what a CRM contact
#: actually is, and a stored list of them is a copy of the client's contact list.
#:
#: *A payload wearing a CRM's clothes.* `crm_note` is on the platform list; the vendor's own
#: spellings for the same thing are not.
NEVER_PROJECTED_FROM_HUBSPOT: Final[frozenset[str]] = frozenset(
    {
        "amount",
        "amount_in_home_currency",
        "hs_acv",
        "hs_arr",
        "hs_mrr",
        "hs_tcv",
        "annualrevenue",
        "annual_revenue",
        "total_revenue",
        "first_name",
        "firstname",
        "last_name",
        "lastname",
        "full_name",
        "job_title",
        "jobtitle",
        "notes",
        "note_body",
        "hs_note_body",
        "engagement_body",
    }
)

_CAMEL_BOUNDARY: Final = re.compile(r"(.)([A-Z][a-z]+)")
_LOWER_UPPER: Final = re.compile(r"([a-z0-9])([A-Z])")


def _snake(name: str) -> str:
    """`firstName` and `first_name` as one string, so the guard reads both spellings.

    A connector's own targets are snake case and a vendor's are not consistently anything,
    and a rule written against one spelling is a rule a mapping evades by using the other.
    """
    stepped = _CAMEL_BOUNDARY.sub(r"\1_\2", name.strip())
    return _LOWER_UPPER.sub(r"\1_\2", stepped).lower()


def assert_federated_only(entity: str, names: Iterable[str]) -> None:
    """Refuse a declaration that would store what this connector answers live (M11.4.4).

    A `ProjectionRefusedError` rather than an error of this module's own, because it is
    exactly that refusal: the platform's denylist and this list are one rule with two
    vocabularies, and a caller catching one should not have to know about the other.

    This is the only enforcement point, deliberately. An identical check at ingest would look
    like a second defence and be an equivalent mutant, because the projection is built from
    the declared fields rather than copied from a row, so nothing undeclared can arrive to be
    caught. `brain.connectors.manifest.ProjectedEntity` records the same lesson about its own
    signal clause.
    """
    refused = sorted({n for n in names if _snake(n) in NEVER_PROJECTED_FROM_HUBSPOT})
    if refused:
        msg = (
            f"{entity} would project {refused}, which this connector fetches live and never "
            f"stores. {A_DEAL_AMOUNT_IS_A_CONTRACT_VALUE_IN_ANOTHER_VOCABULARY} "
            f"{A_CRM_IS_MOSTLY_THE_DENYLIST}"
        )
        raise ProjectionRefusedError(msg)


#: What is kept locally about a client: enough to find it, join it, filter it and sort it.
#: The record id is not listed because it is not one of the fields: `ProjectedRecord.source_id`
#: carries it, and declaring it again would count it twice against the twelve.
CLIENT_PROJECTED: Final[tuple[ProjectedField, ...]] = (
    ProjectedField(name="name", shape=FieldShape.LABEL, uses=(HotUse.IDENTIFY, HotUse.JOIN)),
    ProjectedField(name="domain", shape=FieldShape.JOIN_KEY, uses=(HotUse.JOIN,)),
    ProjectedField(
        name="lifecycle_stage", shape=FieldShape.STATUS, uses=(HotUse.FILTER, HotUse.COUNT)
    ),
    ProjectedField(name="owner_id", shape=FieldShape.JOIN_KEY, uses=(HotUse.JOIN, HotUse.FILTER)),
    ProjectedField(name="updated_at", shape=FieldShape.TIMESTAMP, uses=(HotUse.SORT,)),
)

#: **Four fields and no label.** This is the shape `A_CRM_IS_MOSTLY_THE_DENYLIST` describes,
#: and it is what a projected CRM contact honestly amounts to: it can be counted, filtered and
#: joined, and it cannot be shown to a person, because the only fields that would name the
#: person are the ones that must not be stored.
CONTACT_PROJECTED: Final[tuple[ProjectedField, ...]] = (
    ProjectedField(
        name="lifecycle_stage", shape=FieldShape.STATUS, uses=(HotUse.FILTER, HotUse.COUNT)
    ),
    ProjectedField(name="company_id", shape=FieldShape.JOIN_KEY, uses=(HotUse.JOIN,)),
    ProjectedField(name="owner_id", shape=FieldShape.JOIN_KEY, uses=(HotUse.JOIN, HotUse.FILTER)),
    ProjectedField(name="updated_at", shape=FieldShape.TIMESTAMP, uses=(HotUse.SORT,)),
)

#: A deal without its amount. Everything needed to find the deal and say where it is in the
#: pipeline, and not the number, which is fetched live for whoever holds `read:deal.amount`.
DEAL_PROJECTED: Final[tuple[ProjectedField, ...]] = (
    ProjectedField(name="deal_name", shape=FieldShape.LABEL, uses=(HotUse.IDENTIFY,)),
    ProjectedField(name="stage", shape=FieldShape.STATUS, uses=(HotUse.FILTER, HotUse.COUNT)),
    ProjectedField(name="pipeline", shape=FieldShape.STATUS, uses=(HotUse.FILTER,)),
    ProjectedField(
        name="close_date", shape=FieldShape.TIMESTAMP, uses=(HotUse.FILTER, HotUse.SORT)
    ),
    ProjectedField(name="owner_id", shape=FieldShape.JOIN_KEY, uses=(HotUse.JOIN, HotUse.FILTER)),
)

PROJECTED_FIELDS: Final[Mapping[str, tuple[ProjectedField, ...]]] = MappingProxyType(
    {
        ENTITY_CLIENT: CLIENT_PROJECTED,
        ENTITY_CONTACT: CONTACT_PROJECTED,
        ENTITY_DEAL: DEAL_PROJECTED,
    }
)


def projection_for(entity: str, connection: HubSpotConnection) -> ProjectedEntity:
    """One entity kind's projection, refused here before the manifest ever sees it.

    `assert_federated_only` runs before `ProjectedEntity`'s own five clauses and catches what
    those cannot: the deal amount and the halves of a person's name pass every platform clause
    when declared as a status or a label. The visibility predicate is the portal, which is the
    part of HubSpot's model we can actually evaluate: see
    `HUBSPOT_VISIBILITY_IS_THE_PORTAL_UNTIL_OWNERS_ARE_MAPPED`.
    """
    declared = PROJECTED_FIELDS.get(entity)
    if declared is None:
        msg = (
            f"this connector projects {sorted(PROJECTED_FIELDS)} and was asked for "
            f"{entity!r}; an association is an edge fetched live and is never stored"
        )
        raise HubSpotError(msg)
    assert_federated_only(entity, (f.name for f in declared))
    return ProjectedEntity(
        entity=entity,
        fields=declared,
        change_signal=ChangeSignal.UPDATED_SINCE,
        visibility=connection.visibility(),
    )


def subscription(entity: str) -> ChangeSubscription:
    """How this source tells us one entity kind moved (M11.4.6).

    `UPDATED_SINCE` with an id sweep, and both halves are argued. The cursor is chosen over
    the webhook in the module docstring; `DeletionCheck.ID_SWEEP` is then not a choice at all,
    because `change_signal.A_CURSOR_CANNOT_SEE_A_DELETION` says a removed record is simply one
    the cursor never mentions again, and `ChangeSubscription` refuses a cursor that claims its
    deletions are signalled. The sweep is what the reconciliation pass does: enumerate the ids
    the source still returns and treat everything in the projection that is missing from that
    enumeration as gone.
    """
    if entity not in PROJECTED_FIELDS:
        msg = f"this connector projects {sorted(PROJECTED_FIELDS)} and was asked for {entity!r}"
        raise HubSpotError(msg)
    return ChangeSubscription(
        source=CONNECTOR_NAME,
        entity=entity,
        kind=ChangeSignal.UPDATED_SINCE,
        notify_within=CURSOR_POLL_INTERVAL,
        reconcile_every=RECONCILIATION_INTERVAL,
        deletion_check=DeletionCheck.ID_SWEEP,
    )


def refresh_promise(entity: str) -> RefreshPromise:
    """What the source has undertaken, at the interval that actually re-reads every row.

    Delegated to `ChangeSubscription.promise`, which hands over `reconcile_every` rather than
    `notify_within` for the reason `change_signal.FRESHNESS_IS_MEASURED_AGAINST_RECONCILIATION`
    gives: staleness is measured from when a record was last *seen*, and a quiet record is
    only re-seen by the full pass.
    """
    return subscription(entity).promise()


def projected_record(
    entity: str, row: Mapping[str, Any], *, last_seen_at: datetime
) -> ProjectedRecord | None:
    """One projected row, built from what was declared rather than copied from what arrived.

    A fresh mapping over the declared fields, which is the shape
    `brain.connectors.rest.WHAT_THE_MAPPING_DOES_NOT_NAME_DOES_NOT_ARRIVE` argues for one
    layer up, and the reason `amount` cannot land here even though the mapping fetches it: a
    copy would carry it the day somebody adds a target, and a build cannot. It is also what
    makes an inlined association harmless if one ever arrives: a company `name` sitting on a
    contact row is not a declared contact field, so it is not copied.

    A declared field the row does not hold contributes nothing rather than a null, matching
    `RestOperation.project`. A `TIMESTAMP` field that cannot be dated is dropped for the same
    reason `parse_hubspot_timestamp` refuses to guess.

    Returns None for a row with no id, mirroring `transports.normalise`: a record that cannot
    be named cannot be refreshed, cited or matched to itself on the next fetch.
    """
    declared = PROJECTED_FIELDS.get(entity)
    if declared is None:
        msg = f"this connector projects {sorted(PROJECTED_FIELDS)} and was asked for {entity!r}"
        raise HubSpotError(msg)
    raw_id = row.get(ID_TARGET)
    if not isinstance(raw_id, str | int) or not str(raw_id).strip():
        return None

    fields: dict[str, ProjectedValue] = {}
    for declaration in declared:
        if declaration.name not in row:
            continue
        value = row[declaration.name]
        if declaration.shape is FieldShape.TIMESTAMP:
            dated = parse_hubspot_timestamp(value)
            if dated is not None:
                fields[declaration.name] = dated
            continue
        # Passed through rather than coerced. A value that is not a pointer is refused by
        # `ProjectedRecord` with the argument attached, and stringifying it here would turn a
        # nested object into a short label and defeat `A_NESTED_OBJECT_IS_NOT_ONE_FIELD`.
        fields[declaration.name] = value

    return ProjectedRecord(
        source=CONNECTOR_NAME,
        entity=entity,
        source_id=str(raw_id),
        last_seen_at=last_seen_at,
        fields=fields,
    )


#: HubSpot's timestamps arrive two ways and one of them is a trap. Property values come back
#: as milliseconds since the epoch, rendered as a *string*; the envelope's own `updatedAt` is
#: ISO 8601 with a zone. Read the millisecond string as seconds and 1794700800000 lands in the
#: year 58854, which sorts, filters and renders without complaint.
_EPOCH_MS_RE: Final = re.compile(r"^-?\d{10,15}$")


def parse_hubspot_timestamp(value: object) -> datetime | None:
    """A HubSpot timestamp as an aware instant, or None when it cannot be dated.

    Both accepted forms are unambiguous about their instant, which is the criterion rather
    than tidiness. A bare epoch figure is milliseconds and is always UTC; an ISO string is
    accepted only when it carries a zone, because a naive one read in Singapore is eight hours
    out and that is the whole width of the ageing band in `brain.gate.provenance`.

    None means "not stated", exactly as `brain.gate.provenance.read_time` means it, and the
    caller drops the field rather than inventing a value nobody sent.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000.0, UTC)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _EPOCH_MS_RE.match(text):
        return datetime.fromtimestamp(int(text) / 1000.0, UTC)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


# ------------------------------------------------------- associations (M11.5.2)
@dataclass(frozen=True)
class AssociationEdge:
    """One link between two records: four names and two ids, and nowhere to put a record.

    The absence is the design. There is no field on this class that could hold the associated
    record's properties, so "attach the company to the contact while we are here" is not
    something a caller can express rather than something they are asked not to do. See
    `AN_ASSOCIATION_IS_A_SECOND_QUESTION`.

    An edge is a fact about a record the caller already has, and the record on the far end is
    a separate question with its own entitlement check. That is why the edge carries an id:
    an id is a pointer somebody has to come back and ask about, and coming back is where the
    gate gets to decide.
    """

    from_entity: str
    from_id: str
    to_entity: str
    to_id: str
    kind: str = ""

    def __post_init__(self) -> None:
        for name, value in (
            ("from_entity", self.from_entity),
            ("to_entity", self.to_entity),
        ):
            if value not in _MAPPINGS or value == ENTITY_ASSOCIATION:
                msg = (
                    f"an edge's {name} is {value!r}, which this connector does not read; an "
                    f"edge between kinds nothing maps points at a record nothing can fetch"
                )
                raise HubSpotError(msg)
        for name, value in (("from_id", self.from_id), ("to_id", self.to_id)):
            if not value.strip():
                msg = (
                    f"an edge carries no {name}; an edge whose end cannot be named is not a "
                    "pointer to anything, and following it would mean guessing which record "
                    "was meant"
                )
                raise HubSpotError(msg)


def association_edges(
    *,
    from_entity: str,
    from_id: str,
    to_entity: str,
    rows: tuple[Mapping[str, Any], ...],
) -> tuple[AssociationEdge, ...]:
    """The edges in one association response, as ids and nothing else.

    Built from the projected rows rather than from the raw body, so the only values that can
    reach an edge are the two the mapping names. A row with no id is dropped for the reason
    `transports.normalise` drops one: an edge to a record that cannot be named cannot be
    followed, cited or audited.
    """
    edges: list[AssociationEdge] = []
    for row in rows:
        raw_id = row.get(ID_TARGET)
        if not isinstance(raw_id, str | int) or not str(raw_id).strip():
            continue
        kind = row.get("kind")
        edges.append(
            AssociationEdge(
                from_entity=from_entity,
                from_id=from_id,
                to_entity=to_entity,
                to_id=str(raw_id),
                kind=str(kind) if isinstance(kind, str | int) else "",
            )
        )
    return tuple(edges)


def assert_hops_within_cap(hops: int) -> None:
    """Refuse a traversal deeper than the answer lane can pay for.

    See `ONE_HOP_IS_WHAT_THE_ANSWER_LANE_CAN_AFFORD`. Refused here as well as by
    `FanOutPlan.assert_within` because the two catch it at different moments: this catches an
    agent asking for a three-step walk before any plan is built, and the budget catches a plan
    that was assembled some other way. Neither is redundant, because a caller that never
    builds a plan never reaches the second.
    """
    if hops < 0:
        msg = "a traversal of a negative number of hops is not a traversal"
        raise HubSpotError(msg)
    if hops > MAX_ASSOCIATION_HOPS:
        msg = (
            f"a traversal of {hops} hops is deeper than the {MAX_ASSOCIATION_HOPS} this "
            f"connector allows. {ONE_HOP_IS_WHAT_THE_ANSWER_LANE_CAN_AFFORD}"
        )
        raise HubSpotError(msg)


def traversal_plan(
    *, entity: str, to_entity: str, timeout_ms: int = FEDERATION_TIMEOUT_MS
) -> FanOutPlan:
    """One object fetch and one dependent association fetch, as a plan somebody executes.

    A plan rather than a loop, because the association call cannot start until the first has
    answered and pretending otherwise produces a fan-out whose measured latency is right and
    whose second call cannot be made. `depends_on` is what says so, and it is also what makes
    `FanOutPlan.critical_path_ms` report 1,600ms rather than 800.

    The plan is deliberately not executed here. This module builds no threads and opens no
    connections, and the budget check belongs to whoever assembles the question's whole
    fan-out, because a per-connector budget cannot see the other three sources the same
    question touches.
    """
    if entity not in PROJECTED_FIELDS or to_entity not in PROJECTED_FIELDS:
        msg = (
            f"a traversal runs between {sorted(PROJECTED_FIELDS)} and was asked for "
            f"{entity!r} to {to_entity!r}"
        )
        raise HubSpotError(msg)
    assert_hops_within_cap(1)
    first = SourceCall(
        call_id=entity, connector=CONNECTOR_NAME, entity=entity, timeout_ms=timeout_ms
    )
    second = SourceCall(
        call_id=f"{entity}_to_{to_entity}",
        connector=CONNECTOR_NAME,
        entity=ENTITY_ASSOCIATION,
        timeout_ms=timeout_ms,
        depends_on=(entity,),
    )
    return FanOutPlan(calls=(first, second))


# --------------------------------------------------------- the classifications (M4.2.1)
#: Every field this connector can return, and the capability that reaches it.
#:
#: `deal.amount` is CONFIDENTIAL and it is the point of the table: see
#: `A_PIPELINE_FIGURE_IS_NOT_A_PAYROLL_FIGURE`. It is returnable to somebody holding
#: `read:deal.amount` and it is never storable, which `brain.core.field_policy` names as the
#: ordinary case rather than the exception.
#:
#: `client.name` and `contact.updated_at` are spelled exactly as the house already spells
#: them, because two sources contributing to one entity kind is the ordinary case here and
#: `FieldPolicy` refuses two different opinions about one key. See `TWO_SOURCES_ONE_ENTITY_NAME`.
#:
#: A contact's name and job title are INTERNAL rather than higher, and that is a deliberate
#: reading rather than an oversight. The sensitive part of a CRM contact is the means of
#: reaching them, and this connector maps no email, no telephone number and no address, so
#: nothing classifies them and default-deny withholds them from everybody. Who we deal with at
#: a client is ordinary internal business information, and classifying it CONFIDENTIAL would
#: either be ignored or make the CRM unusable, which are the two ways a classification stops
#: meaning anything.
HUBSPOT_FIELD_RULES: Final[tuple[FieldRule, ...]] = (
    FieldRule.of(ENTITY_CLIENT, "name", "read:client.name", Classification.INTERNAL),
    FieldRule.of(ENTITY_CLIENT, "domain", "read:client.domain", Classification.INTERNAL),
    FieldRule.of(
        ENTITY_CLIENT, "lifecycle_stage", "read:client.lifecycle_stage", Classification.INTERNAL
    ),
    FieldRule.of(ENTITY_CLIENT, "owner_id", "read:client.owner_id", Classification.INTERNAL),
    FieldRule.of(ENTITY_CLIENT, "updated_at", "read:client.updated_at", Classification.INTERNAL),
    FieldRule.of(ENTITY_CONTACT, "first_name", "read:contact.first_name", Classification.INTERNAL),
    FieldRule.of(ENTITY_CONTACT, "last_name", "read:contact.last_name", Classification.INTERNAL),
    FieldRule.of(ENTITY_CONTACT, "job_title", "read:contact.job_title", Classification.INTERNAL),
    FieldRule.of(
        ENTITY_CONTACT, "lifecycle_stage", "read:contact.lifecycle_stage", Classification.INTERNAL
    ),
    FieldRule.of(ENTITY_CONTACT, "company_id", "read:contact.company_id", Classification.INTERNAL),
    FieldRule.of(ENTITY_CONTACT, "owner_id", "read:contact.owner_id", Classification.INTERNAL),
    FieldRule.of(ENTITY_CONTACT, "updated_at", "read:contact.updated_at", Classification.INTERNAL),
    FieldRule.of(ENTITY_DEAL, "deal_name", "read:deal.deal_name", Classification.INTERNAL),
    FieldRule.of(ENTITY_DEAL, "amount", "read:deal.amount", Classification.CONFIDENTIAL),
    FieldRule.of(ENTITY_DEAL, "stage", "read:deal.stage", Classification.INTERNAL),
    FieldRule.of(ENTITY_DEAL, "pipeline", "read:deal.pipeline", Classification.INTERNAL),
    FieldRule.of(ENTITY_DEAL, "close_date", "read:deal.close_date", Classification.INTERNAL),
    FieldRule.of(ENTITY_DEAL, "owner_id", "read:deal.owner_id", Classification.INTERNAL),
    FieldRule.of(ENTITY_ASSOCIATION, "kind", "read:association.kind", Classification.INTERNAL),
)


def hubspot_field_policy() -> FieldPolicy:
    """What this connector's fields require, as a policy fragment somebody merges."""
    return FieldPolicy(rules=HUBSPOT_FIELD_RULES)


def assert_policy_merges_with(other: FieldPolicy) -> None:
    """Refuse a disagreement with another source's opinion about a shared field (M4.2.1).

    Checked rather than remembered. `FieldPolicy` already raises `PolicyConflictError` when
    two different rules arrive for one key, and that refusal fires wherever the fragments are
    merged, which is a long way from whoever edited one of them. This runs against another
    fragment directly, so the disagreement is reported here with both opinions named.

    Reported all at once rather than one at a time, for the reason `check_projection` gives:
    one at a time turns reconciling two policies into a guessing game where each fix reveals
    the next objection. See `TWO_SOURCES_ONE_ENTITY_NAME`.
    """
    ours = hubspot_field_policy()
    clashes: list[str] = []
    for rule in HUBSPOT_FIELD_RULES:
        theirs = other.rule_for(rule.entity, rule.field)
        if theirs is None or theirs == rule:
            continue
        clashes.append(
            f"{rule.dotted}: this connector says "
            f"{rule.required_capability.value}/{rule.classification.value}, the other says "
            f"{theirs.required_capability.value}/{theirs.classification.value}"
        )
    if clashes:
        listed = "\n".join(f"  - {c}" for c in clashes)
        msg = (
            f"{CONNECTOR_NAME} declares {len(ours)} rules and disagrees with another "
            f"fragment about:\n{listed}\n{TWO_SOURCES_ONE_ENTITY_NAME}"
        )
        raise HubSpotError(msg)


def assert_declarations_agree() -> None:
    """The mapping, the projection, the property request and the policy (M11.4.5).

    Four lists edited by different people at different times, and every disagreement between
    them is invisible in review and silent at runtime. See
    `FOUR_DECLARATIONS_THAT_DRIFT_APART_QUIETLY`.
    """
    policy = hubspot_field_policy()
    problems: list[str] = []
    for entity in sorted(_MAPPINGS):
        mapped = set(mapped_targets(entity))
        projected = {f.name for f in PROJECTED_FIELDS.get(entity, ())}
        unclassified = sorted(name for name in mapped if not policy.governs(entity, name))
        if unclassified:
            problems.append(
                f"{entity} maps {unclassified}, which nothing classifies; default-deny "
                "withholds them from everybody, so they travel for nothing"
            )
        unmapped = sorted(projected - mapped)
        if unmapped:
            problems.append(
                f"{entity} projects {unmapped}, which nothing maps; the column never "
                "arrives, so a filter on it silently matches nothing"
            )
        wanted = set(requested_properties(entity))
        asked = set(default_arguments(entity).get(PROPERTIES_PARAMETER, "").split(","))
        missing = sorted(wanted - asked)
        if missing:
            problems.append(
                f"{entity} maps {missing} and does not ask the source for them; HubSpot "
                "answers with a well-formed record that does not contain the field, so the "
                "value arrives absent and nothing reports it"
            )
    if problems:
        listed = "\n".join(f"  - {p}" for p in problems)
        msg = f"this connector's declarations disagree:\n{listed}"
        raise HubSpotError(msg)


# ------------------------------------------------------------------- the manifest (M11.1.7)
HUBSPOT_TOOLS: Final[tuple[ToolDeclaration, ...]] = (
    ToolDeclaration(
        name="hubspot.read_companies",
        description=(
            "Companies in this HubSpot account: name, web domain, lifecycle stage, owner and "
            "when they last changed. Read-only."
        ),
        entity=ENTITY_CLIENT,
        side_effect=SideEffect.NONE,
        identity_mode=IdentityMode.SERVICE,
    ),
    ToolDeclaration(
        name="hubspot.read_contacts",
        description=(
            "People at a client in this HubSpot account: name, job title, lifecycle stage and "
            "which company they belong to. Read-only, and no email address, telephone number "
            "or postal address of any kind."
        ),
        entity=ENTITY_CONTACT,
        side_effect=SideEffect.NONE,
        identity_mode=IdentityMode.SERVICE,
    ),
    ToolDeclaration(
        name="hubspot.read_deals",
        description=(
            "Deals in this HubSpot account: name, pipeline, stage, close date and the amount. "
            "Read-only, and the amount is fetched live rather than stored."
        ),
        entity=ENTITY_DEAL,
        side_effect=SideEffect.NONE,
        identity_mode=IdentityMode.SERVICE,
    ),
    ToolDeclaration(
        name="hubspot.read_associations",
        description=(
            "Which companies, contacts or deals one HubSpot record is linked to. Returns the "
            "ids of the linked records and nothing else, so reading one of them is a separate "
            "request. Read-only."
        ),
        entity=ENTITY_ASSOCIATION,
        side_effect=SideEffect.NONE,
        identity_mode=IdentityMode.SERVICE,
    ),
)


def hubspot_manifest(
    connection: HubSpotConnection, *, ref: SecretRef, version: str = MANIFEST_VERSION
) -> ConnectorManifest:
    """Everything this connector declares, in one value that can be hashed and pinned.

    `IdentityMode.SERVICE` rather than the DELEGATED default, and the override is an honest
    declaration rather than a preference: a HubSpot private app is one credential for one
    account, and nobody in this company has a personal HubSpot login that maps to their
    principal. The consequence is stated in `brain.tools.registry`: a SERVICE tool must be
    registered beside a scope predicate, because the source will not narrow it for us.
    `HubSpotConnection.visibility` is that predicate, and it is the same one the projection
    stores.

    The binding is read-only by not saying otherwise, which is `CredentialBinding`'s default
    and the whole of `A_WRITE_GRANT_NAMES_SOMEBODY`. A connector that could move a deal to
    closed-won is a different connector, approved by somebody named, and this is not it.

    The ceiling is named and is not verified yet, so `throttle.limits_for` refuses this
    manifest today. That is the intended behaviour: see
    `A_CEILING_NOBODY_VERIFIED_IS_NOT_A_CEILING`.
    """
    assert_declarations_agree()
    return ConnectorManifest(
        name=CONNECTOR_NAME,
        version=version,
        transport=TransportKind.REST,
        scope=connection.scope(),
        credential=CredentialBinding(ref=ref, mode=AccessMode.READ_ONLY),
        tools=HUBSPOT_TOOLS,
        projections=tuple(
            projection_for(entity, connection) for entity in sorted(PROJECTED_FIELDS)
        ),
        ceiling=CEILING_NAME,
    )


# ------------------------------------------------------ what one call produced (M11.5.5)
class HubSpotOutcome(enum.StrEnum):
    """The four answers a call to HubSpot can produce, and they stay four.

    `ABSENT` is a fact about the CRM and is the one HubSpot response anybody has recorded.
    `REFUSED` is HubSpot declining to talk to this connector, which is a job for whoever owns
    the connection. `UNREACHABLE` is HubSpot not answering, whether it said 429 or said
    nothing at all. See `AN_UNREACHABLE_CRM_IS_NOT_AN_EMPTY_ONE`.

    A person is told the same thing for the last two, because the difference is ours to act on
    rather than theirs, and the trace keeps it.
    """

    PRESENT = "present"
    ABSENT = "absent"
    REFUSED = "refused"
    UNREACHABLE = "unreachable"

    @property
    def answered(self) -> bool:
        """Whether the source answered at all. The only place the four collapse to two."""
        return self in (HubSpotOutcome.PRESENT, HubSpotOutcome.ABSENT)


@dataclass(frozen=True)
class HubSpotReply:
    """One call's result: what it was, and rows only where there are rows.

    The constructor is the guarantee. A failed reply cannot carry rows and cannot carry a read
    time, so "answer the 429 from the last good response" is not something a caller can
    express here. A read time on a failure would be worse than the rows: `assess_freshness`
    would date it and report the answer as current.
    """

    outcome: HubSpotOutcome
    call: CallOutcome
    rows: TypedResult[SourceRecord] | None = None
    fetched_at: str = ""
    reason: FailureReason | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.outcome.answered:
            if self.rows is None:
                msg = (
                    f"a {self.outcome} reply carries no rows; an answered call produces a "
                    "result even when the result is empty, and None here would make an "
                    "absence indistinguishable from a failure"
                )
                raise HubSpotError(msg)
            has_records = bool(self.rows.records)
            if has_records is not (self.outcome is HubSpotOutcome.PRESENT):
                msg = (
                    f"a {self.outcome} reply holds {len(self.rows.records)} record(s); "
                    "present and absent are decided by what came back, and a mismatch here is "
                    "an empty CRM being reported as a full one or the reverse"
                )
                raise HubSpotError(msg)
            return
        if self.rows is not None or self.fetched_at:
            msg = (
                f"a {self.outcome} reply was given rows or a read time. "
                f"{AN_UNREACHABLE_CRM_IS_NOT_AN_EMPTY_ONE}"
            )
            raise HubSpotError(msg)
        if self.reason is None:
            msg = (
                f"a {self.outcome} reply names no failure reason; the trace is the only place "
                "a refusal and an outage stay distinguishable, and it is assembled from this"
            )
            raise HubSpotError(msg)

    def freshness(self, *, horizon: StalenessHorizon, now: datetime) -> Freshness:
        """How old this is, in `brain.gate.provenance`'s vocabulary and not a second one.

        Unconditional: a failed reply has no read time, so `assess_freshness` returns UNSTATED
        by its own rule about a time it cannot date. A branch here would be a second
        implementation of that rule, and the constructor above is what makes this one
        sufficient.
        """
        return assess_freshness(self.fetched_at, horizon=horizon, now=now)

    def failure(self) -> SourceFailure | None:
        """This reply as the federation layer's failure record, or None when it answered."""
        if self.outcome.answered or self.reason is None:
            return None
        return SourceFailure(connector=CONNECTOR_NAME, reason=self.reason, detail=self.detail)

    def notice(self, *, disclosable: frozenset[str]) -> str:
        """What the asker is told. Named only if their own catalogue already named HubSpot.

        Delegated whole to `federation.PartialAnswer.notice`, so a HubSpot outage produces
        exactly the sentence every other unreachable source produces and the two are
        indistinguishable to somebody probing. Restating it here would be a second disclosure
        rule, and the generous copy is the one that gets read.
        """
        failure = self.failure()
        if failure is None:
            return ""
        return PartialAnswer(failed=(failure,)).notice(disclosable=disclosable)

    def trace_line(self) -> str:
        """The full statement, for an auditor. Keeps what the notice drops.

        Safe here for the reason `federation.PartialAnswer.trace_lines` is safe: a trace is
        read by somebody already entitled to know what this system connects to, and nothing in
        this module can put this string into a channel payload. It carries no value from the
        response body either: every detail is a constant in this module, so a client's name
        cannot arrive in it by way of a filter.
        """
        count = len(self.rows.records) if self.rows is not None else 0
        return f"{CONNECTOR_NAME}: {self.outcome} ({self.call}), {count} record(s), {self.detail}"


#: How a call's outcome becomes the reason the trace records. `REJECTED` maps to `NOT_SERVING`
#: because `FailureReason` has no member for "the source declined our credential", and
#: inventing one in another module's enum is not this connector's decision: this connector is
#: not serving requests until somebody re-authorises it, which is the nearest true statement,
#: and `detail` carries the distinction.
_REASON_FOR: Final[Mapping[CallOutcome, FailureReason]] = MappingProxyType(
    {
        CallOutcome.QUOTA: FailureReason.QUOTA,
        CallOutcome.REJECTED: FailureReason.NOT_SERVING,
        CallOutcome.UNAVAILABLE: FailureReason.TRANSPORT,
        CallOutcome.TRUNCATED: FailureReason.TRUNCATED,
    }
)

_DETAIL_FOR: Final[Mapping[CallOutcome, str]] = MappingProxyType(
    {
        CallOutcome.QUOTA: DETAIL_RATE_LIMITED,
        CallOutcome.REJECTED: DETAIL_UNAUTHORISED,
        CallOutcome.UNAVAILABLE: DETAIL_UNAVAILABLE,
    }
)


def interpret(
    operation: RestOperation,
    *,
    status: int | None,
    body: Any = None,
    fetched_at: str,
    timed_out: bool = False,
    connection_failed: bool = False,
    more_pages: bool = False,
) -> HubSpotReply:
    """One response, as an answer. The classification is `throttle.classify`'s (M11.3.3).

    The branch order is the rule and it is not this module's: `classify` checks a timeout
    before a status, and 429 before the generic client-error branch, and both orderings have
    arguments attached where they live. What this adds is the projection, and it happens only
    on the success branch. Projecting first would run the field mapping over a 429's error
    body, which holds no `results`, so a rate limit would surface as a specification error in
    the middle of somebody's question.

    `more_pages` is the caller's, matching `transports.normalise`: HubSpot pages by cursor and
    publishes no result-set ceiling, so the only thing that knows an answer was cut short is
    whoever stopped asking for pages.
    """
    outcome = classify(status=status, timed_out=timed_out, connection_failed=connection_failed)
    if outcome is CallOutcome.OK:
        rows = operation.records(body, fetched_at=fetched_at, truncated=more_pages)
        answer = HubSpotOutcome.PRESENT if rows.records else HubSpotOutcome.ABSENT
        return HubSpotReply(
            outcome=answer, call=outcome, rows=rows, fetched_at=fetched_at, detail=DETAIL_ANSWERED
        )
    answer = (
        HubSpotOutcome.REFUSED if outcome is CallOutcome.REJECTED else HubSpotOutcome.UNREACHABLE
    )
    detail = DETAIL_TIMED_OUT if timed_out else _DETAIL_FOR.get(outcome, DETAIL_UNAVAILABLE)
    return HubSpotReply(
        outcome=answer,
        call=outcome,
        reason=_REASON_FOR.get(outcome, FailureReason.TRANSPORT),
        detail=detail,
    )


# ------------------------------------------------------------------- health (M11.1.1)
_HEALTH_FOR: Final[Mapping[CallOutcome, HealthState]] = MappingProxyType(
    {
        CallOutcome.OK: HealthState.OK,
        CallOutcome.TRUNCATED: HealthState.DEGRADED,
        CallOutcome.QUOTA: HealthState.DEGRADED,
        CallOutcome.REJECTED: HealthState.DOWN,
        CallOutcome.UNAVAILABLE: HealthState.DOWN,
    }
)


def health(reply: HubSpotReply | None, *, checked_at: datetime) -> ConnectorHealth:
    """What the last probe found, as a fact with a time on it.

    Three judgements, and each sends the row to a different person.

    **A rate limit is DEGRADED, never DOWN.** The source is healthy and we asked for too much,
    which is `throttle.A_QUOTA_REFUSAL_IS_NOT_ILL_HEALTH` stated as a health state. DOWN would
    send somebody to check whether HubSpot is up, which it is, and the only action available
    is to ask for less.

    **A declined authorisation is DOWN.** It was working this morning and it is an incident for
    whoever owns the connection. UNCONFIGURED would file it as somebody's installation task and
    it would sit there.

    **No probe at all is UNCONFIGURED.** A connector nobody has called yet is a job for whoever
    installed it, and reporting DOWN would page somebody about a system that may be perfectly
    healthy.

    The mapping is total on purpose. A `dict.get` with a default would let a sixth outcome be
    classified as whatever the default said, and for a health state the convenient default is
    OK. Every detail is a constant from this module, so a health row cannot carry a filter
    value, and therefore a client's name, into a console with a different audience and a
    different retention from the answer it described.
    """
    if reply is None:
        return ConnectorHealth(
            connector=CONNECTOR_NAME,
            state=HealthState.UNCONFIGURED,
            checked_at=checked_at,
            detail=DETAIL_NEVER_PROBED,
        )
    return ConnectorHealth(
        connector=CONNECTOR_NAME,
        state=_HEALTH_FOR[reply.call],
        checked_at=checked_at,
        detail=reply.detail,
    )
