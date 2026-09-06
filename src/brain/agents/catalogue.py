"""The template catalogue: the agents a web agency actually runs.

`brain.agents.template` can express a template. This is the first evidence that it can
express a *useful* one, which is a different claim and the only way to find out is to write
some against real work rather than against a fixture.

**Six of the twenty-three, and the six were chosen to press on different corners** rather
than to be the easiest six: a developer agent that reads a house standard, a support agent
bound to a real connector, an analyst reading hours out of a Base, an accountant that must
never see a number it is not entitled to, a media buyer that must never spend, and a curator
whose whole job is to notice absence.

**What writing them found, and it is a gap in the model rather than in these six.** The work
breakdown asks for "AR and Renewal Chaser, Shadow-pinned thirty days" and for
"SEM Agent, Shadow-pinned, human commits budget changes". The second is expressible and is
below. The first is not: `LeashRung` is target, scope and rung, `brain.gate.leash.LeashEntry`
is the same plus an agent id, and **neither carries a time bound**. The only duration in the
leash module is `DEFAULT_APPROVAL_WINDOW`, which is how long an approval request stays open
and is a different quantity entirely.

That is not an oversight to patch quietly, which is why the chaser is absent here rather
than present with its thirty days dropped. A rung that raises itself on the thirty-first day
is supervision that ends without anybody deciding it should, and the failure mode is an agent
becoming autonomous over accounts receivable on a date nobody diarised. "Review after thirty
days" and "become autonomous after thirty days" are different products and only one of them
is a guardrail. `docs/needs-rupash.md` carries the question.

**A template reaches nothing.** `authority` is a ceiling, and `E_run = E(caller) ∩ ceiling`
is computed by the one `intersect` in `brain.gate.invoke`. Every capability written below
narrows; none grants. A template published to the catalogue does not thereby give anybody
anything, which is the same argument `AUDIENCE_IS_NOT_AUTHORITY` makes one module over.

**The personas are deliberately thin.** Prompt material is stored and never parsed, so
anything in a persona that looks like a permission is a permission decided by whoever last
edited a text box. What each agent may reach is in `authority`, where a reviewer can read it.

**Nothing publishes these.** `publish` needs a signing key and `install` needs a catalogue,
and neither is wired to an HTTP route, because there is no route behind the gate for agents.
`CATALOGUE` holds unsigned manifests, which is honest: a signature is a claim about who
published, and nobody has.

Task ids: M13.5.7, M13.5.14, M13.5.16, M13.5.19, M13.5.21, M13.5.23
"""

from __future__ import annotations

from brain.agents.template import (
    GoldenCase,
    LeashRung,
    ManifestAuthority,
    ManifestGuardrails,
    ManifestIdentity,
    Placeholder,
    TemplateManifest,
)
from brain.core.entitlement import Capability
from brain.core.envelope import SideEffect
from brain.core.scope import Clause, Op, Scope
from brain.gate.injection import AutonomyTier
from brain.models.routing import Tier

#: Why every agent here starts on the bottom rung, including the ones whose work is harmless.
#:
#: An agent's first week is when nobody yet knows what it does with the awkward cases, and the
#: awkward cases are the whole reason for supervision. Shipping a template at ASSISTED means
#: the first person to install it inherits a decision the template's author made about a
#: company they have never seen.
#:
#: Raising a rung is one edit by somebody who has watched the agent work. Lowering one after
#: an incident is an incident.
EVERY_AGENT_STARTS_SUPERVISED = (
    "Every template in this catalogue declares SHADOW on every target it names. The rung is "
    "a statement about how much a stranger's install should trust an agent on its first day, "
    "and the author of a template has never met that company. Raising it is one edit by "
    "somebody who has watched the work; the reverse is an incident. Templates whose work is "
    "read-only declare it too, because a read-only agent that is wrong is still wrong in "
    "somebody's answer, and SHADOW is what makes the first few wrong answers visible."
)

#: Why the leash cannot express the AR chaser, and why it is absent rather than approximated.
A_PIN_WITH_NO_END_IS_NOT_A_PIN_FOR_THIRTY_DAYS = (
    "M13.5.18 asks for an AR and Renewal Chaser shadow-pinned for thirty days. LeashRung "
    "carries target, scope and rung, and gate.leash.LeashEntry carries the same plus an "
    "agent id; neither carries a time bound, and the only duration in that module is how "
    "long an approval request stays open. Writing the chaser without its thirty days would "
    "ship a permanent pin under a name promising a temporary one, and adding an expiry to "
    "the rung would ship supervision that ends on a date nobody diarised: an agent becoming "
    "autonomous over accounts receivable because a timer ran out. The two readings, review "
    "after thirty days and autonomy after thirty days, are different products, so the "
    "template is absent and the question is on the Needs Rupash page."
)


#: Who these were published by, and it is the house rather than a person.
#:
#: A template outlives whoever wrote it, and `published_by` on a person who has left is a
#: record pointing at a principal `is_active` refuses. The catalogue is the agency's, so it
#: is named as such and `M13.6` is where a client's own templates get a real author.
PUBLISHER = "verz"


def _department(slug: str) -> Scope:
    """One department, which is the narrowest scope these templates ever need.

    Built rather than written as a literal so a clause cannot be spelled differently in six
    manifests, which is how six templates come to mean five things.
    """
    return Scope(clauses=(Clause(field="department", op=Op.EQ, value=slug),))


def _shadow(*targets: str) -> tuple[LeashRung, ...]:
    """Every named target on the bottom rung. See `EVERY_AGENT_STARTS_SUPERVISED`."""
    return tuple(LeashRung(target=target, rung=AutonomyTier.SHADOW) for target in targets)


def wordpress_developer() -> TemplateManifest:
    """M13.5.7. The agency's core trade, and the one with a written house standard.

    Reads the standard and the ticket that asked for the work. Writes nothing: a developer
    agent that edits a live theme is a deployment nobody reviewed, and the useful half of
    this job is answering "what does our standard say about this" in the ten seconds before
    somebody guesses.

    The placeholder is the standard's own location, because every install has one and no two
    have the same one.
    """
    return TemplateManifest(
        identity=ManifestIdentity(
            template_id="wordpress_developer",
            version=1,
            published_by=PUBLISHER,
            display_name="WordPress Developer",
        ),
        persona=(
            "You help a WordPress developer follow the house standard. Quote the standard "
            "and say which document you read it from. If the standard does not cover the "
            "question, say so plainly rather than inferring a rule from an example."
        ),
        tier=Tier.MAIN,
        authority=ManifestAuthority(
            capabilities=(
                Capability(value="read:knowledge.document"),
                Capability(value="read:ticket.subject"),
                Capability(value="read:ticket.description"),
            ),
        ),
        connectors=("freshdesk",),
        guardrails=ManifestGuardrails(
            max_side_effect=SideEffect.NONE,
            leash=_shadow("knowledge.read", "ticket.read"),
        ),
        golden_set=(
            GoldenCase(
                question="Which plugins may I install on a new client site?",
                expectation=(
                    "Names the house plugin list from the standard document and says which "
                    "document it came from. Does not invent a plugin that is not on it."
                ),
            ),
            GoldenCase(
                question="What is our policy on editing a theme's core files?",
                expectation=(
                    "Answers from the standard if it says, and says the standard does not "
                    "cover it if it does not, rather than reasoning from a similar rule."
                ),
            ),
        ),
        placeholders=(
            Placeholder(
                key="standard_document",
                prompt="Which knowledge document holds your house WordPress standard?",
            ),
        ),
    )


def support_ticket_agent() -> TemplateManifest:
    """M13.5.14. Bound to a real connector, and the reason the connector is declared.

    Naming `freshdesk` in `connectors` reaches nothing. It makes the install ask for a
    binding, and `install.completeness` holds the agent incomplete and disabled until one
    exists, so a support agent that cannot see tickets never answers a question about them
    with whatever else it can reach.
    """
    return TemplateManifest(
        identity=ManifestIdentity(
            template_id="support_ticket_agent",
            version=1,
            published_by=PUBLISHER,
            display_name="Support Ticket Agent",
        ),
        persona=(
            "You answer questions about support tickets. Quote the ticket you read and its "
            "number. If a ticket is not in what you can see, say you cannot find it rather "
            "than answering from a similar one."
        ),
        tier=Tier.MAIN,
        authority=ManifestAuthority(
            capabilities=(
                Capability(value="read:ticket.subject"),
                Capability(value="read:ticket.description"),
                Capability(value="read:ticket.status"),
                Capability(value="read:client.name"),
            ),
        ),
        connectors=("freshdesk",),
        guardrails=ManifestGuardrails(
            max_side_effect=SideEffect.NONE,
            leash=_shadow("ticket.read", "ticket.search"),
        ),
        golden_set=(
            GoldenCase(
                question="What is the oldest open ticket for this client?",
                expectation=(
                    "Names one ticket with its number and age, from tickets the asker may "
                    "see. Never says how many it could not see."
                ),
            ),
        ),
    )


def capacity_and_hours_analyst() -> TemplateManifest:
    """M13.5.16. Reads hours out of a Base, and must not read what is beside them.

    The interesting constraint is the capability list rather than the persona: a maintenance
    Base holds hours and contract values in one table, and an analyst that could read the
    row could read the money. It holds `read:client.hours_remaining` and not
    `read:client.contract_value`, which is the fixture case
    `tests/e2e/test_wave_two_lark_question.py` drives with two real people.
    """
    return TemplateManifest(
        identity=ManifestIdentity(
            template_id="capacity_and_hours_analyst",
            version=1,
            published_by=PUBLISHER,
            display_name="Capacity and Hours Analyst",
        ),
        persona=(
            "You answer questions about remaining maintenance hours. Give the number and "
            "the date it was last updated. Do not estimate a figure you cannot read."
        ),
        tier=Tier.MAIN,
        authority=ManifestAuthority(
            scope=_department("maintenance"),
            capabilities=(
                Capability(value="read:client.name"),
                Capability(value="read:client.hours_remaining"),
                Capability(value="read:client.hosting_expiry"),
            ),
        ),
        connectors=("lark_base",),
        guardrails=ManifestGuardrails(
            max_side_effect=SideEffect.NONE,
            leash=_shadow("record.read", "record.search"),
        ),
        golden_set=(
            GoldenCase(
                question="How many hours are left on SNM Construction?",
                expectation=(
                    "Gives the hours and when they were last updated. Says nothing about "
                    "the contract value, whether or not the asker could see it elsewhere."
                ),
            ),
        ),
    )


def accountant_agent() -> TemplateManifest:
    """M13.5.19. Shadow-pinned, and the one where the money capabilities are the point.

    It is the only template here that names a money capability at all, and it still declares
    `SideEffect.NONE`: reading a ledger and moving money are different verbs, and a template
    that could do the second because it needed the first is how a reconciliation agent
    becomes a payment agent.
    """
    return TemplateManifest(
        identity=ManifestIdentity(
            template_id="accountant_agent",
            version=1,
            published_by=PUBLISHER,
            display_name="Accountant Agent",
        ),
        persona=(
            "You answer questions about invoices and payments from the ledger. Quote the "
            "invoice number and its date. Never total figures across clients unless the "
            "question asked for a total."
        ),
        tier=Tier.MAIN,
        authority=ManifestAuthority(
            scope=_department("finance"),
            capabilities=(
                Capability(value="read:invoice.number"),
                Capability(value="read:invoice.amount"),
                Capability(value="read:invoice.status"),
                Capability(value="read:client.name"),
            ),
        ),
        connectors=("xero",),
        guardrails=ManifestGuardrails(
            max_side_effect=SideEffect.NONE,
            leash=_shadow("invoice.read", "invoice.search"),
        ),
        golden_set=(
            GoldenCase(
                question="Which invoices are overdue for this client?",
                expectation=(
                    "Lists invoices by number with their dates, from the ledger, for one "
                    "client. Does not offer to chase, remind or write anything."
                ),
            ),
        ),
    )


def sem_agent() -> TemplateManifest:
    """M13.5.21. Shadow-pinned, and a human commits budget changes.

    **This is the one the work breakdown states as a constraint and the model can express.**
    "A human commits budget changes" is `SideEffect.DRAFT`: the agent may prepare a change
    and may not apply one, which is a property of the ceiling rather than of a sentence in
    the persona. A persona saying "always ask before changing a budget" is a request; a
    ceiling of DRAFT is a refusal, and `gate.leash.govern` is what enforces it.

    Contrast M13.5.18, which asks for a pin with a duration and is absent for the reason
    `A_PIN_WITH_NO_END_IS_NOT_A_PIN_FOR_THIRTY_DAYS` gives.
    """
    return TemplateManifest(
        identity=ManifestIdentity(
            template_id="sem_agent",
            version=1,
            published_by=PUBLISHER,
            display_name="SEM Agent",
        ),
        persona=(
            "You prepare paid search changes for a person to review. Say what you would "
            "change, by how much, and what you expect it to do. You never apply a change."
        ),
        tier=Tier.MAIN,
        authority=ManifestAuthority(
            scope=_department("marketing"),
            capabilities=(
                Capability(value="read:campaign.name"),
                Capability(value="read:campaign.spend"),
                Capability(value="read:campaign.budget"),
            ),
        ),
        guardrails=ManifestGuardrails(
            # DRAFT, never WRITE. The agent composes a change and a person commits it.
            max_side_effect=SideEffect.DRAFT,
            leash=_shadow("campaign.read", "campaign.draft_change"),
        ),
        golden_set=(
            GoldenCase(
                question="This campaign is overspending. What should we do?",
                expectation=(
                    "Proposes a specific budget change with the figures it read, and states "
                    "that a person has to apply it. Does not report having applied it."
                ),
            ),
        ),
        placeholders=(
            Placeholder(
                key="monthly_budget_ceiling",
                prompt="What monthly spend may this agent propose changes within?",
            ),
        ),
    )


def knowledge_gap_curator() -> TemplateManifest:
    """M13.5.23. Its job is to notice absence, which makes it the awkward one here.

    An agent that reports what the corpus does not cover is an agent whose output is a list
    of things nobody wrote down, and that list is drawn from questions people asked. So it
    reads the questions and never the askers: there is no `read:principal` capability below
    and there is not going to be one, because "who keeps asking about pricing" is a
    performance report assembled inside a curation tool.
    """
    return TemplateManifest(
        identity=ManifestIdentity(
            template_id="knowledge_gap_curator",
            version=1,
            published_by=PUBLISHER,
            display_name="Knowledge Gap Curator",
        ),
        persona=(
            "You report subjects people asked about that the knowledge base does not "
            "answer. Give the subject and an example question. Never name who asked."
        ),
        tier=Tier.SMALL,
        authority=ManifestAuthority(
            capabilities=(
                Capability(value="read:knowledge.document"),
                Capability(value="read:knowledge.title"),
            ),
        ),
        guardrails=ManifestGuardrails(
            max_side_effect=SideEffect.NONE,
            leash=_shadow("knowledge.read", "knowledge.search"),
        ),
        golden_set=(
            GoldenCase(
                question="What are people asking about that we have not documented?",
                expectation=(
                    "Names subjects with an example question each. Names no person, no "
                    "department and no count of who asked."
                ),
            ),
        ),
    )


#: Every template this module offers, in one order.
#:
#: A tuple rather than a dict keyed by id, because the id is already inside each manifest and
#: a second copy in a key is a second thing to keep in step. `catalogue_by_id` derives the
#: mapping when a caller wants one.
CATALOGUE: tuple[TemplateManifest, ...] = (
    accountant_agent(),
    capacity_and_hours_analyst(),
    knowledge_gap_curator(),
    sem_agent(),
    support_ticket_agent(),
    wordpress_developer(),
)


def catalogue_by_id() -> dict[str, TemplateManifest]:
    """The catalogue keyed by template id, derived rather than maintained."""
    return {manifest.identity.template_id: manifest for manifest in CATALOGUE}
