# Needs Rupash

Decisions and access I cannot resolve alone. Served at `/build/needs-rupash`.

**Five of the seven were decided on 5 September and are marked below.** **Item 9 was decided on 5 September.** One remains open: **8**, rewritten in plainer words because the first versions led with the mechanism rather than with what is at stake, which is not a decidable thing to put in front of somebody. They came out of building the audit
ledger, the routing matrix and the redaction walker, where the specification asks for two
things that cannot both be true. Each one has my recommendation attached, so most should
take you a minute.

Items 6 to 10 are choices. **Item 11 is different: it is a gap in a rule we already
promise**, and it needs a yes rather than a preference.

Nothing is blocked on them. I have built the version I think is right in every case, and
the code says so in a comment. If you disagree, the change is small now and expensive
later, which is why they are here rather than in a footnote.

**Items 1 to 5 were answered on 5 September** and are kept below for the record.

---

# Open

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

## 8. Where does the audit anchor live?

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

## 12. The opaque escape hatch depends on a promise the redaction module cannot keep — DECIDED: the rule goes in M16

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

# Answered on 5 September

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

## 3. Langfuse — no action, and a correction

You said to go ahead and install it. **It is already installed and running** on the box:
`verz-langfuse-server` and `verz-langfuse-db`, up several days. My note was not asking
whether to install it; it was flagging that it runs well under its documented minimum of
11 vCPU and 25.5 GiB, on a box with 11.7 GiB in total.

You are right that this is fine at your traffic. Nothing to do.

Connecting *our* system to it is separate work and belongs in **M27**, wave 3, with the
rest of observability. It is in the plan already.

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
