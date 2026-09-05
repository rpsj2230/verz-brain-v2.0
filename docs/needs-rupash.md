# Needs Rupash

Decisions and access I cannot resolve alone. Served at `/build/needs-rupash`.

**3 items are open: 24, 25 and 26.** None blocks
anything today, and none is urgent this week. 24 is a disclosure trade-off that starts to
matter when people with narrow permissions begin using the system, which is wave 4. 25 is a
measured capacity limit that needs a decision before wave 4 rather than during it. 26 is a
product question about the website widget, and the machinery around it is being built
either way.

Everything else on this page is decided. It is kept as a record: each item states what the
problem was, what was built, and why, so the reasoning outlives the conversation it happened
in.

---

# Open

## 26. The chat widget on a client's marketing site: what may a stranger ask it?

**Nothing is blocked.** The plumbing is being built either way, and it is safe by default
today: an anonymous visitor currently holds nothing, so the widget can mint a session and
that session can ask nothing. Your answer decides what, if anything, that session is allowed
to reach.

**The situation.** The plan has a chat widget embedded on a client's public website. Whoever
loads that page is a stranger: not signed in, not an employee, possibly a competitor, a
bot, or a journalist. The rest of this system answers "what may this person see" by looking
up what they hold. A stranger holds nothing, and the way this platform is built, nothing
means nothing: entitlements are additive only, so an anonymous caller sees exactly what has
been explicitly granted to anonymous callers, and no such grant exists.

**So the widget works and answers nothing, unless you decide otherwise.** That is a
deliberate safe default rather than an oversight, and it is where it will stay until you
choose.

**The three shapes it could take, and what each costs:**

1. **Lead capture only.** The widget collects a question and a contact address and creates
   a task for a human. It answers nothing itself. Cost: it is a contact form with a chat
   interface. Benefit: no exposure of any kind, and it is the only option with no way to be
   wrong.
2. **Public knowledge only.** A specific, small, explicitly published set of content is
   granted to anonymous callers: opening hours, service descriptions, published pricing. The
   agent may answer from that and nothing else. Cost: somebody has to decide, per client,
   what is public, and be right. The risk is not the answer, it is the *retrieval*: a
   question is a probe, and an answer that says "I cannot find that" for one product and
   answers for another has told a competitor which products exist.
3. **Identify first, then answer.** The widget asks who they are and verifies it, typically
   by emailing a link. After that they are an ordinary principal with ordinary entitlements
   and nothing here is special. Cost: friction on a marketing site, which is where friction
   costs the most.

**My recommendation: 1 for the first client, with 2 available per client afterwards.** The
reason is not caution for its own sake. Option 2 needs a person to correctly classify a body
of content as public, on a page where being wrong is visible to everybody including
competitors, and the first client is the worst place to learn what that classification
process needs to be. Option 3 is a real product and belongs in a later wave.

**What I need:** which of the three, and for option 2, who at the client decides what is
public.

**What is being built meanwhile:** the session minting and its abuse guard (M10.5.5,
M23.1.4), which are needed under all three options. A widget on a public site is an
unauthenticated endpoint that mints credentials, so it is rate-limited per origin and capped
on live sessions per origin, and an anonymous session expires much sooner than a signed-in
one.

---

## 24. When a source is down, should the answer name it?

**Nothing is blocked. I have built the safe reading and this is a question about whether to
loosen it.**

The plan says that when the Brain cannot reach one of your systems, the answer should say
which one. That is obviously good service: "I could not reach Xero" is a better answer than
"something went wrong", because you know whether to wait or to ask somebody.

**The problem is who else is asking.** The same sentence, sent to somebody who has no access
to Xero at all, tells them Xero exists and that you connect to it. Ask about invoices and
learn there is an accounting system; ask about tickets and learn there is a helpdesk. A
person with no permissions anywhere could map every system you run, one question at a time,
without ever seeing a single record.

That is the same rule the rest of the system already follows: an answer never says "I looked
in the finance ledger and found nothing", because the sentence gives away the ledger.

**What I have built.** The answer names a source only when that person could already see it
in their own tool list. Everything else becomes "part of this answer is unavailable", and
the full list of what failed goes to the operator's log, where you and whoever is on support
can read it.

| | Names every failed source | Names only what they can already see |
|---|---|---|
| A person with full access | Sees exactly what is down | Sees exactly what is down |
| A person with narrow access | Learns which systems exist | Told part of the answer is unavailable |
| Somebody probing | Can map your whole estate | Learns nothing |
| Your support team | Reads it in the answer | Reads it in the log |

**My recommendation: keep it as built.** The cost is that a narrowly-permissioned person
gets a vaguer message and has to ask, and the person they ask can see the log. The cost the
other way is a map of your systems available to anybody who can type a question.

This only becomes a real difference once there are people using it with narrow permissions,
which is wave 4. Worth deciding before then rather than during.

---

---

## 25. The full feature set does not fit on the current server

**Measured, not estimated. Nothing is blocked today, and this needs deciding before
wave 4 rather than during it.**

Your server has about 6.4 GB usable. Your other production system already uses 3.7 GB of
it. Leaving a small reserve for the machine itself, that gives the Brain about **2.4 GB**.

The Brain today fits comfortably. The full feature set does not: it wants **3.7 GB**, so it
is over by about **1.3 GB**.

**What is using it.** The single biggest item is the database behind the tracing tool
(`langfuse-clickhouse`) at 1 GB. That is the component that records what the AI did, step
by step, so a wrong answer can be explained afterwards. It is genuinely useful and it is
genuinely large.

**Your options, cheapest first.**

| Option | Cost | What you give up |
|---|---|---|
| Use a hosted tracing service instead of running one | A subscription, roughly the price of a small server | Your traces sit on somebody else's infrastructure. They contain no client data by design, but they do show what your staff asked |
| A second small server just for tracing | Another VPS, similar to what you pay now | Nothing functionally; one more machine to keep patched |
| Run without full tracing | Nothing | When an answer is wrong, "why" gets much harder to establish. This is the thing that makes an AI system auditable |
| Move your other project off this box | Depends where it goes | Nothing here, but it is work on the other project |

**My recommendation: a second small server, and not yet.** The tracing database is only
needed once real people are asking real questions of real data, which is wave 4. Deciding
now costs you money for months before it is used. What is worth doing now is knowing the
number, which is why this is written down: `budget_breaches("full")` in the code answers it
at any time, and it is checked by a test so it cannot quietly become wrong.

**The thing I would not do** is quietly shrink the tracing database to make it fit. It would
work, then fail under real load, and it would fail as "the AI is broken" rather than as "we
undersized a component on purpose in September".

---

---

# Answered

## 23. Should the client's audit trail show your deployment history? — DECIDED: leave the two chains separate

**Nothing is blocked. I have built it the safe way and this is a question about whether to
open it up.**

Every deploy is now recorded: the time, the version, whether it worked, and which tasks
went out in it. The record cannot be edited afterwards without that being detectable.

**The question is where it lives.** There are two records in this system:

| | The permission trail | The deployment trail |
|---|---|---|
| What it holds | Who could see what, who was refused, who granted whom access | What version went out and when |
| Who can read it | Your staff, filtered by their own permissions; a client can ask for their own | Nobody yet: it is for you and whoever runs the servers |
| Where it goes | Into the compliance export you would hand an auditor | Nowhere outward |

Today they are separate. A deploy does not appear in anybody's audit view and does not
appear in a compliance export.

**Why I kept them apart.** The permission trail is built around people: every entry is
about a person, an agent or a record, and the whole design assumes you can ask "everything
that ever happened to this person". A deployment is about none of those. Putting it in
would mean anybody who can read the full audit trail also reads your release history, and
that a compliance export handed to a client contains your engineering activity. Neither
is obviously wrong; both should be your choice rather than a side effect.

**What you would gain by merging them.** One timeline. An auditor asking "did the code
change between these two permission decisions" could answer it from one place instead of
lining up two records by time.

**My recommendation: leave them separate, and revisit if a client ever asks.** The cost of
being wrong in this direction is a slightly awkward query for an auditor. The cost the
other way is client-visible information you did not intend to publish, and unpublishing it
is harder than publishing it.

Nothing to do unless you disagree.

---

---

## 22. The plan says production deploys only tested releases. You asked for every push. — DECIDED: every push deploys, no tagging

**These are both reasonable and they cannot both happen. I have kept yours running and am
not changing it without you saying so.**

On 5 September you asked whether deploys should be automatic and answered yes. They are:
every push to the main branch builds an image, and the server picks it up within three
minutes.

The build plan says something different for production: deploy only from a tagged release
that has passed staging. That is the safer arrangement and it is slower by design.

**What each one costs.**

| | Every push (what runs today) | Only tested releases (what the plan says) |
|---|---|---|
| How fast a fix reaches you | Three minutes | When somebody tags a release |
| What reaches production | Whatever passed the automated checks | Only what also ran against a real database with real migrations |
| When it goes wrong | The rollback puts the previous version back automatically | It mostly does not get that far |
| Who has to do something | Nobody | Somebody tags, and somebody looks at staging |

**Why this is worth deciding now rather than later.** Right now nothing is behind the
permission gate and no client data is in the system, so a bad deploy costs three minutes of
a page being down. That stops being true the moment real connector credentials go in.

**My recommendation, and it is a middle option rather than either column.** Keep every push
deploying automatically, and add staging *in front of it* rather than instead: the push
deploys to staging, the full test suite runs there against a real database, and production
follows automatically only if that passes. You keep the three minutes; the difference is
that the three minutes now includes a real migration against a real Postgres, which is the
one thing the current automated checks cannot do.

That is roughly a day of work and it needs no decision from you beyond "yes, do that".

**What exists already:** the staging stack is built and its isolation is tested. It is not
deployed yet, and it uses about 1.4 GB on a server with 6.4 GB free, which is comfortable
alongside your other project on the same box.

---

---

## 21. Where do role grants that came from the directory live? — DECIDED: directory-sourced grants get their own table, owned by the sync

**A smaller decision inside the same area, recorded so it is not made by accident.**

Roles can be granted two ways: a person gives somebody a role, or the role arrives because
of a group they are in, synced from the company directory.

Every role grant currently requires a named grantor and a reason, because the review of those
two fields is the only thing that ever removes a grant that should not have been made. A row
that arrived from a directory has no human grantor.

**What I did:** recorded the grantor as the identity provider itself. It satisfies the field
and it quietly puts unreviewed rows in the same table as reviewed ones, where a person
scanning for mistakes cannot tell them apart.

**The options:** keep them together and add a column saying where each came from, or keep
directory-sourced grants in their own table that the sync owns and can also remove from.

**My recommendation:** the second. The sync needs to be able to take a role away when
somebody leaves a group, and a process that can delete rows a person created is a worse
thing to build than a process that owns its own table.

---

---

## 20. Two designs for what a person's ID is, and they contradict each other — DECIDED: keep the indirection; the architecture line is wrong

**Not urgent, but it gets expensive the moment the identity provider is wired in.**

The architecture says a person's ID in the Brain *is* the ID Keycloak gives them. It also
specifies a separate table mapping identities to people. Those cannot both be load-bearing:
if the ID is the Keycloak one, the mapping table has nothing to do.

**Why it matters.** If a person's ID is the one Keycloak issued, then replacing Keycloak, or
migrating a client onto their own identity provider, rewrites every ID in the system, and
every audit row and every grant that references one.

**What I built:** the indirection. The Brain gives people its own IDs, and a table says which
external identity maps to which person. A token says which record to look up rather than
being the record.

**My recommendation:** keep the indirection and correct the architecture line. The cost is
one join. The alternative's cost is a migration nobody can do safely once there is audit
history.

**No action needed if you agree.**

**BUILT (2026-09-05).** The line is corrected in two places, because the wrong idea was
written twice. `docs/architecture.html` gave a principal's representation as "uuid from
Keycloak"; it now says the id is minted here and the provider's subject maps to it. The
comment on `PrincipalRow.id` claimed the id "arrives from the identity provider" while
giving `c_0447` as the example, which is not a Keycloak subject: that comment now states
the indirection and the migration it protects.

The code was already right and already proved right. `test_a_known_subject_resolves_to_the
_principal_the_directory_holds` uses a Keycloak-shaped uuid for the subject and `u_priya`
for the principal id, so an implementation that shortcut the lookup and returned the
subject fails that test today. Nothing to change beyond the two comments.

---

---

## 19. How long should somebody stay signed in? — CONFIRMED: 10 hours absolute, 30 minutes idle

**A number I picked, and nobody has confirmed.**

A session expires two ways. It ends after a period of no activity, and it ends absolutely
after a fixed time no matter how active somebody is, because a session that renews forever
is a permanent credential.

I set the absolute limit to **10 hours** and the idle limit to **30 minutes**. Ten hours
covers a working day with room, so almost nobody is signed out mid-task; thirty minutes
idle means a laptop left open in a cafe is not an open door for the afternoon.

**What it costs if it is wrong.** Too short and people re-authenticate several times a day,
which trains them to click through anything that asks. Too long and a stolen laptop is
useful for as long as the number says.

**What I need:** confirm 10 hours and 30 minutes, or give me two other numbers.

**One caution.** The absolute limit is written in two places: the code and the Keycloak realm
configuration. They have to stay in step, or people get logouts that look random. When you
change it, tell me rather than editing one of them.

**BUILT (2026-09-05).** That caution is now a gate rather than a request. Six tests in
`tests/unit/test_realm_config.py` read the realm export and the Python constants and refuse
to let them drift, and I broke each one to check it bites:

| What I changed | Caught by |
| --- | --- |
| Realm alone extended to a full day | the ten-hour agreement test |
| Realm alone doubles the idle window | the thirty-minute agreement test |
| Code alone extended to twelve hours | the ten-hour agreement test |
| Code alone relaxes idle to 45 minutes | the thirty-minute agreement test |
| Offline sessions lose their bound | the offline-session test |
| A client session set to outlive its sign-in | the client-session test |
| Token lifespan doubled to ten minutes | the effective-window test |
| Remember-me switched on with a week of its own | the remember-me test |

Three findings worth your time.

**The stated thirty minutes was never the true number.** A token minted just before somebody
walks away keeps working until it expires, so the real gap between the last action and the
last possible request is idle plus token lifespan: 35 minutes, not 30. That is a rounding
error and I have left it, but the test now bounds the *effective* window rather than the
token lifespan on its own.

**The existing token test did not catch a doubled token lifespan.** It caps the lifespan at
900 seconds, and 600 passes it. That ceiling alone would permit a fifty percent overshoot on
the idle policy. The two tests bound different things and neither implies the other.

**Offline sessions had one boolean between them and never expiring.**
`offlineSessionMaxLifespanEnabled` defaults to false, and false means an offline token
outlives the laptop it was issued to. It is set correctly and is now asserted.

---

---

## 17. Who holds the keys to the secrets vault? — DECIDED: five pieces, any three open it; both configurable, and the root token revoked

**Not a design question. A physical-custody question only you can answer, and it has to be
settled before the vault goes in rather than after.**

Connector credentials, provider keys and database passwords will live in a secrets vault
(OpenBao). It starts **sealed**: on every restart it is a locked box that cannot read its
own contents until somebody opens it with the unseal keys.

Those keys get split into several pieces, and a set number of pieces are needed to open it.
The point of splitting them is that no single person can open the vault alone, and no single
person losing their piece locks everyone out.

**What I need from you, three answers:**

1. **How many pieces, and how many needed to open?** My recommendation for a 126-person
   company with a small technical team: **five pieces, any three open it.** Three people
   have to agree, and you survive losing two.
2. **Who holds a piece?** Name five people. They should not all be reachable through the
   same laptop, the same phone or the same building. At least one should be someone who is
   never on call, so a piece exists outside the group that would be handling an incident.
3. **Where does the root token go after setup?** It can do anything, including undo every
   policy. Standard practice is to revoke it once normal access is configured, so nothing
   holds unlimited power permanently. I recommend revoking it.

**Why it cannot wait.** Everything about the vault is reversible except this. Deploy it,
put real credentials in, then decide custody, and you now have to re-key a live system while
it is holding the keys to your client data.

**What happens meanwhile:** I am building everything around the vault that does not need
it running, and the tasks that need real keys are already scheduled for go-live rather than
now. Nothing is blocked.

**BUILT (2026-09-05).** `src/brain/ops/vault_quorum.py` holds the split, and
`ops/openbao/UNSEAL.md` now derives its `bao operator init` command from that module rather
than repeating the numbers. Both directions of drift are tested: changing the module without
the runbook fails, and editing the runbook without the module fails.

**One disagreement with your wording, stated rather than quietly ignored.** You asked for the
setting to be "in the backend as well where we can select the options". It is a reviewed
constant in source, not a database row a screen can save, and here is why. The split is
fixed at `bao operator init`; changing it afterwards is `bao operator rekey` with three of
the current five people present. A save button would therefore report success for a change
that did not happen. Second, a row would put the policy governing the vault that holds the
database password inside that database, which makes it unreadable during exactly the
incident that needs it. A console screen can read and display this policy; what it cannot
honestly offer is a save button.

**Seven refusals at construction, and one of them is not in your list.** Beyond the
arithmetic (threshold above shares, of one, or equal to shares) and the holder count, the
policy refuses a list where every holder is on call. Your point 2 asked for at least one
holder outside the on-call group; that was prose, and it is now a refusal.

**A hole found in the duplicate-holder check, after it had been written and tested.** It
compared holder ids exactly, which is the right field and the wrong comparison: `r.jones`
beside `R.Jones` is one pair of hands and two strings, so five slots were accepted and a
declared three-of-five was really a two-of-four. Ids are now compared stripped and
case-folded.

**Root token: revoked, as recommended.** The rejected alternative is recorded in the module.
A sealed envelope protects the paper and not the token: it still bypasses every policy, the
audit device logs its use as an ordinary accessor with no field marking it root, and it was
already in the scrollback when init printed it. `bao operator generate-root` covers the
emergency, needs the same three people, and leaves a record.

**Still needs you, and it is not blocking anything.** The five holder slots read UNASSIGNED.
Naming them is one edit to a list in that module, and until it happens the setup command
exits non-zero rather than initialising a vault whose five pieces belong to nobody in
particular. Tell me five names and whether each is in the on-call rotation, and I will fill
them in; or fill them in yourself at the top of `vault_quorum.py`.

---

---

## 16. Two different things both mean "can approve", and nothing says which wins — DECIDED: the permission decides

**The plain problem: a person can look approved and not be, or be approved and not look it.**

There are two separate ways the system knows someone can sign something off:

1. **The Approver role**, which is a job title on the platform.
2. **An approve permission**, which is a specific right over specific things, like approving
   a payment for one department.

Right now those two do not talk to each other. Somebody can hold the Approver role and no
approve permission, in which case the role does nothing. Or hold an approve permission and
not the role, in which case the role is never consulted. Neither situation is an error, and
neither looks wrong on a screen.

**Why it matters:** whoever configures this will reasonably assume that giving someone the
Approver role lets them approve things. It does not, and nothing tells them.

**My recommendation:** the permission decides, always. The role is a label for the console
to filter on, not an authority. That matches the rule the rest of the system already
follows, that no role implies a permission, including Super Admin. What is missing is a
check that the two agree, so an Approver with no approve permission shows up as a
misconfiguration rather than a silent nothing.

**No action needed from you if you agree** with that, and I will add the consistency check.

---

---

## 15. How long should an emergency access session last? — DECIDED: 4 hours, set per grant

**The plain problem: someone needs to get into something urgently, out of hours, and we
need to let them without leaving the door open afterwards.**

"Break-glass" is the emergency override. Somebody with the right to use it opens a session,
gets access they would not normally have, and the system records it loudly and tells other
people it happened. It is for the 2am case where a client site is down and the one person
who can fix it does not have the access.

**It has to expire on its own**, because nobody remembers to close these. The specification
says "time-boxed" and never says how long.

**I chose four hours** and I want you to confirm it or change it. My reasoning: four hours
is one working session, so it covers a real incident; and a session opened at 11pm and
forgotten has expired before anyone starts work the next morning.

| Option | What it covers | What it costs |
|---|---|---|
| 1 hour | A quick fix | Someone mid-incident has to reopen it, and reopening becomes routine |
| **4 hours** (recommended) | A full incident, out of hours | Occasionally someone reopens once |
| 24 hours | Anything | It stops being an emergency and becomes an admin account with an awkward name |

**Just tell me a number.** Everything else about it is built.

---

---

## 14. An approval card can show the approver something they are not allowed to see — ACCEPTED: fix with the approval work

**Not a decision, a gap I am recording so it is not forgotten.**

When an agent wants to do something that needs sign-off, it renders a card showing what is
about to happen, and a person approves it. That card is currently built using the
permissions of the person who *asked*, not the person *approving*.

So if a junior asks for something, and a manager with narrower access to that particular
client approves it, the card can show the manager a value they would not be able to look up
themselves.

Nothing is broken yet, because approvals are not wired to a screen. It needs fixing before
they are, and it belongs with the approval work rather than here.

---

---

## 13. Can a leash rule say "supervise everywhere except maintenance"? — DECIDED: strictest wins

**The plain problem: today it cannot, and the safe choice I made is probably not the one
you would expect.**

A leash decides whether an agent does something by itself, shows a person first, or only
pretends. You set it per agent, per thing it touches, per part of the business.

When two of your rules both apply to one action, something has to decide which wins. I made
**the stricter one win**, because that is how every other permission in this system behaves
and it fails safely.

**What that costs you.** You cannot write "this agent needs supervision everywhere, except
in maintenance where it can just get on with it". The company-wide rule wins and the
maintenance exception never applies. To get that behaviour you would write the narrow rules
one by one and leave the broad one off.

**The alternative** is most-specific-wins, which reads more naturally and is how most people
expect settings to work. The cost is real: a company-wide "supervise everything" rule could
then be cancelled by somebody adding a narrower row, and working out what an agent may
actually do stops being a lookup and becomes a question of which rule is more specific.

**My recommendation:** keep strictest-wins. It is the same rule as everywhere else in the
system, and "the safe setting cannot be quietly overridden" is worth more than the
convenience. If you want the exception style, say so now rather than after leashes are
configured, because changing it later silently loosens every rule already written.

---

---

## 12. The opaque escape hatch depends on a promise the redaction module cannot keep — DECIDED: the rule goes to the channel adapters (M10.1.5)

M4.1.6 allows a payload to skip redaction entirely, for genuinely untypeable data. It is
guarded three ways: it needs its own capability, it flags the trace, and the answer is
labelled as unredacted.

**The label is the part that protects the person reading it**, and the redaction module
cannot make it survive. It attaches a label to the payload; a channel adapter that simply
does not render that label reintroduces the whole risk, silently, and every test in M4 goes
on passing.

**My recommendation:** make it a rule in M16, where the channel adapters live, that a
payload carrying a label renders that label or refuses to send. That turns "the adapter
remembered" into "the adapter cannot forget".

**No decision needed if you agree** — I will write it into M16 when I get there. It is here
because it is the kind of dependency that gets lost between two modules, and the failure is
invisible from either side.

---

---

## 11. A hidden count can still be worked out by subtraction — DECIDED: add the policy column

**This is a hole in a rule we already promise**, so it needs an owner rather than a
preference.

The system must never tell anyone how many things it hid from them. "3 results hidden" is
precisely the fact a person is not entitled to. The redaction walker enforces that
strictly: no placeholder, no null, no shortened list carrying its old length.

**But a count can survive as an ordinary field.** Imagine a client record showing
`ticket_count: 40` beside a list of tickets, where the asker may only see the 12 in their
own department. The list arrives correctly filtered to 12. The count says 40. The asker
subtracts and knows there are 28 tickets they cannot see, which is the number we said we
would never tell them.

Nothing inside the walker can catch this. It sees two fields, both legitimately visible on
their own, and cannot know that one counts the other.

**My recommendation:** a rule in the field policy rather than in code. A field that counts
a collection is marked as counting it, and becomes invisible whenever that collection is
filtered for this asker. It costs one column in the policy and a check at mask time.

**The alternative** is to accept it, on the grounds that the asker learns a number and not
a record. I do not think that holds: the whole point of the rule is that the number itself
is the disclosure, and a person who can see "28 hidden" for every client can map the shape
of the business without reading a single record they are not entitled to.

**What I need from you:** agreement that this is worth the column, and I will add the task.
It is roughly a day, and it is much cheaper now than after connectors start defining
projections.

---

---

## 10. What happens when even the largest model runs out of room? — DECIDED: trim retrieval and retry

**The gap.** Tier escalation is defined as upward only: a request too large for `small`
moves to `main`, and one too large for `main` moves to `heavy`. The specification never says
what happens when `heavy` overflows too.

**Why I did not just pick one.** The three plausible answers have very different
consequences for the person asking:

| Option | What the person gets | The problem with it |
|---|---|---|
| Truncate the context | An answer | An answer built on silently dropped evidence, which is the failure mode the whole design exists to prevent |
| Refuse | "That question is too large" | Honest, but a dead end with no path forward |
| Trim retrieval and retry | An answer, from fewer sources, and told so | More work, and it belongs in retrieval rather than routing |

**My recommendation:** the third. The real fix is upstream — if a question needs more
context than the largest model has, retrieval gathered too much, and routing is the wrong
layer to paper over it.

**For now** the classifier surfaces `context_overflows` as a fact rather than acting on it,
so nothing silently truncates. Something has to own the path before M8 ships.

---

---

## 9. Should a refusal make the system try a different AI model? — DECIDED: no

**The plain problem: if one AI says "I will not answer that", should we keep asking other
AIs until one says yes?**

The system uses several AI models. When one fails, it automatically tries the next. The
reasons to try the next one are all versions of *"this model is unwell right now"*: it did
not respond, it timed out, it was overloaded, it crashed.

The original design listed one more reason: **the model refused on its own content rules.**
I deliberately left that one out, and this is the one place the code knowingly departs from
the design document.

**Why. Two reasons.**

**First, it usually just wastes the person's time.** A refusal is not about the model being
unwell, it is about the question. So the next model refuses too, and the next. The person
waits three times as long for the same no.

**Second, and this is the real issue: when a different model does say yes, what actually
happened is that the system shopped around until something agreed.**

A concrete example. Someone asks the system to draft a letter about a staff member that
touches on their medical leave. Model A declines. If we automatically try B, then C, then D,
the answer your company gives depends on which AI happened to be running well that
afternoon. Same question on Tuesday and Thursday, different answers, and nobody can explain
why.

That is the same problem the design already rejects elsewhere: never retry simply because
you did not like the answer.

**What happens instead in my version:** the system says "I will not answer that", once,
honestly, and it is recorded.

**Decided 5 September: keep it excluded.** The architecture's routing table now says so outright rather than carrying an open question, and a refusal goes to the abstention path in M8 to be answered once and honestly.

---

---

## 8. Where does the audit anchor live? — DECIDED: a private GitHub repo

**The plain problem: someone could delete the last few days of the security log and nothing
would notice.**

The audit log records who gave whom access to what, and who looked at what. It is the thing
you would hand a client or a regulator to prove the system behaved.

It is built so **old entries cannot be edited**. Think of a receipt book where every page
writes down a summary of the page before it. Tear out page 50 and page 51 no longer matches,
so the tampering is obvious.

**But you can still tear off the last few pages.** If someone deletes the newest twenty
entries, everything remaining still matches perfectly. The book has no idea how long it was
meant to be. And the newest entries are exactly the ones someone covering their tracks would
want gone.

**The fix is simple.** Every so often, write down "we are up to entry 1,240" somewhere the
person who administers the database cannot reach. Later, if the log only goes up to 1,220,
you know twenty entries went missing. Without that note, there is no way to tell.

**What you are deciding: where that note gets written.**

| Where | Cost | What it protects against |
|---|---|---|
| **A second server you already own** (recommended to start) | Nothing | Someone who compromises the app or the database |
| A write-once cloud storage bucket | A few dollars a month | The above, plus a rogue administrator, plus you |
| A public timestamping service | Free | The above, plus arguments about *when* something happened |

**My recommendation:** start with the second server, because you already have one and it
costs nothing. Move to write-once storage before signing any client contract that makes a
promise about audit records.

**Where this stands today:** the log can prove nobody edited it. It cannot prove nobody
deleted the recent part. The code for checking against a note is already written and tested;
what does not exist is the place to keep the note.

---

---

## 7. The audit view will want to show a capability, and the ledger redacts it — DECIDED: capabilities allowed in the ledger

**The conflict.** The ledger's redaction rule is an allowlist: a value survives only if it
is a field name, a list of field names, a digest, or a boolean. A capability string like
`read:client.name` does not pass, so it is redacted.

But M24.1.5 is the client-visible audit view, and that screen will almost certainly want to
render "Aaron granted Wei Ling `read:client.name` on 4 September". Today the ledger cannot
tell it that.

**Why the ledger is strict.** It proves *that the reach changed*; the grant table says
*what the reach now is*. Splitting them means a leaked ledger export does not also hand
over the permission map.

**My recommendation:** allow capability strings in the ledger. They are not personal data,
they are already in the grant table, and an audit view that cannot say what was granted is
not an audit view. I would add them to the allowlist as a named exception rather than
loosening the rule generally.

**Decide before M24.1.5 is built**, not after. Retrofitting means either a migration over
the ledger or a screen that reads two sources and hopes they agree.

---

---

## 6. The audit ledger cannot record before-and-after values — DECIDED: field names only

**The conflict.** M24.1.4 asks the ledger to record "actor, timestamp, before and after
state, reason". But before-and-after state *is* field values, and the architecture says
values never enter the ledger. Both cannot hold.

**What I built.** Changed field *names* only. The entry says "Aaron changed
`hosting_expiry` and `status` on client 447", not what they changed them to.

I considered hashing the values instead and rejected it. A five-digit salary has about
90,000 possible values, so its hash is a lookup table away from being the salary itself.
A hash of a low-entropy value is not a redaction.

**What this costs you.** An auditor asking "what was the value before Aaron changed it"
cannot be answered from the ledger. It can be answered from the source system's own
history, if that system keeps one.

**My recommendation:** accept field names only, and reword M24.1.4. The ledger's job is to
prove *who* and *when* and *that something changed*. Making it also the value archive turns
the longest-retained table in the system into the most sensitive one.

**If you disagree**, the alternative is a separate value-history table with its own
retention and its own access rules, which is real work and belongs in M25, not M24.

---

---

## 5. Coolify's stale compose — LEFT AS IS, as you asked

Three changes remain unapplied there: the `migrate` service removed, PgBouncer added, and
`BRAIN_COMMIT_SHA` dropped.

The consequences while it stays stale, so they are not a surprise later:

| What happens | Why it does not matter yet |
|---|---|
| A `migrate` container is created on every deploy | Exits 0. The app migrates itself under an advisory lock |
| The app talks to Postgres directly | No connection pooling. Fine at this scale, and the code is ready for the pooler |
| `/health/ready` reports `commit: unknown` | The image knows its own commit; the stale compose overrides it |

None of it blocks anything. The current file is `docker-compose.yml` in the repo whenever
you want to paste it across.

---

## 4. Automatic deploys — NOW GENUINELY ON

You said "I see you have done this as well". It was not done, and it is worth being precise
about why it looked done: the Deploy workflow's Coolify step printed `secrets not set` and
exited **0** by design, so a missing secret would not resemble a broken pipeline. Every
deploy until today was me running `ops/deploy.sh` by hand. A green run looked like a ship.

**It is automatic now.** A systemd timer on the VPS checks the registry every three minutes
and deploys when the published image changes.

Pulled rather than pushed, deliberately: every other route gives something outside the
server a way in — a Coolify token over plain HTTP, or an SSH key held by GitHub. This way
nothing new reaches the box and no credential leaves it.

It compares the digest the registry serves against what the container is running. Comparing
tags is useless, since `:latest` always equals `:latest`, and comparing build times trusts
a clock.

**cosign is installed and the signature is verified before the container starts** — a
signature checked after the thing is live is checked too late. Verified by hand: the
certificate binds the running image to
`.github/workflows/deploy.yml@refs/heads/main` in your repository.

History: `ssh verz-vps journalctl -u brain-deploy -n 50`

---

---

## 3. Langfuse — no action, and a correction

You said to go ahead and install it. **It is already installed and running** on the box:
`verz-langfuse-server` and `verz-langfuse-db`, up several days. My note was not asking
whether to install it; it was flagging that it runs well under its documented minimum of
11 vCPU and 25.5 GiB, on a box with 11.7 GiB in total.

You are right that this is fine at your traffic. Nothing to do.

Connecting *our* system to it is separate work and belongs in **M27**, wave 3, with the
rest of observability. It is in the plan already.

---

---

## 2. AnyGen — DECIDED: replace

M37 now carries a second migration. **29 tasks, finish moves 6 Oct to 7 Oct.** One day to
replace an entire second system.

- **The twelve house skills come across, not rewritten.** `verz-master-theme`,
  `verz-doc-letterhead`, `seo-audit`, `website-cro-audit` go first — in daily use, and the
  real test of whether import works at all.
- **Agents are rebuilt.** AnyGen has no ceiling and no leash, so there is nothing to carry
  over; each starts at Shadow on writes regardless of how it behaved there.
- **Their adaptive learning does not transfer.** One toggle over an opaque store has no
  honest mapping onto four tiers with a review queue. Memory files are read as evidence and
  tier-one preferences re-derived; anything widening a scope is discarded.
- **Decommissioning a SaaS is not decommissioning a server.** Cancelling the subscription
  does not revoke the OAuth grants it holds on Gmail, Drive, Calendar, Sheets and Docs.
  Access ends with billing, so exports are verified restorable first.

---

---

## 1. Coolify on plain HTTP — FIXED

You said leave it, but fix it if I could. I could.

**The panel is now at https://coolify.194.233.66.89.sslip.io** with a real Let's Encrypt
certificate, valid to 3 December. **Plain HTTP on port 8000 is closed.**

I was wrong about something here, and being wrong changed the answer. I had told you
sslip.io could not get a real certificate, having tested one of your apps and found
Traefik's self-signed default. That app was simply configured for `http://`;
`brain.194.233.66.89.sslip.io` has had a genuine Let's Encrypt certificate all along. No
domain purchase was needed after all.

Two details worth keeping:

- **`ufw deny 8000` would have done nothing.** Docker publishes the port to `0.0.0.0` and
  inserts its own iptables rules ahead of ufw, so the packet never reaches ufw. The block
  lives in the `DOCKER-USER` chain, which Docker leaves alone for exactly this.
- **The rule survives a reboot** via a small systemd unit. Worth knowing: the pre-existing
  block on port 5003, belonging to your other project, does **not** — nothing persists it,
  so it disappears on the next restart. That is yours to decide about; I left it alone
  rather than quietly managing another project's firewall.

The ufw rule I removed was labelled `TEMPORARY - Coolify UI, close when GitHub source is
connected`, so this was always the intent.

To reopen the plain port if you ever need it:
`iptables -D DOCKER-USER -p tcp -m conntrack --ctorigdstport 8000 -j DROP`

Everything is in `ops/vps/`.

---

---

