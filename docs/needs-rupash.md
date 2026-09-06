# Needs Rupash

Decisions and access I cannot resolve alone. Served at `/build/needs-rupash`.

**4 items are open: 25, 29, 30 and 31.** All four are waiting on you rather than on me.

25 is the server capacity question. You asked for options that cost nothing and there are
four; 25 sets them out with the measurements behind each. Two of them I can carry out as
soon as you say so, and one of those protects your other production system rather than this
one.

31 is the one that decides the next fortnight, and it is a sharper, measured version of
25: wave 2 is at 90%, and the last seven jobs all need a machine-learning stack that is not
installed and does not fit on the current server. Answer 25 first and 31 mostly answers
itself.

29 and 30 are both new on 2026-09-06 and both are small. 29 needs one address from you
before anybody can sign in to the console at all. 30 is a choice between two ways of letting
the automation canvas reach the system, and they have different security properties.

**24, 26 and 28 were all answered on 2026-09-06.** 24 stays as built: an answer names a
failed source only to somebody who could already see that source. 26 is decided: public
visitors get public knowledge only, admins decide what is public, and no login. 28 is done
and verified: production was being recreated every three minutes by an old deploy timer of
mine, `brain-deploy.timer` is now disabled, and the recreates have stopped.

Everything else on this page is decided. It is kept as a record: each item states what the
problem was, what was built, and why, so the reasoning outlives the conversation it happened
in.

---

# Open

## 31. Wave 2 is at 90% and the last seven jobs all need the same thing

**This is the one that decides the next fortnight, and it is a sharper version of 25.**

Wave 2 is 194 of 215 done. Of the 21 left, most are already built and waiting on something
small. Seven are not, and they all need the same missing piece: **a machine-learning stack
that is not installed and does not fit on the current server.**

Those seven are reading documents properly (layout-aware extraction, scanned-page OCR,
fallback for odd file formats), recognising names and identifiers the standard rules miss,
and turning text into something searchable.

### What I measured

Adding just the document-reading library pulls in **83 further packages**. The eight largest,
taken from the package index rather than guessed:

| | Download size |
|---|---|
| torch (the machine-learning engine) | 529 MB |
| opencv (image handling) | 70 MB |
| scipy | 34 MB |
| numpy | 16 MB |
| transformers | 12 MB |
| tokenizers | 10 MB |
| torchvision | 7 MB |
| safetensors | 1 MB |
| **Total, compressed** | **678 MB** |

Installed on disk that is roughly **1.5 GB**, and that is before any model file is downloaded.

**The container meant to do this work is allowed 512 MB.** For comparison, the entire Brain
is currently allowed 3,584 MB and is using 429 MB of it.

It also quietly downgrades one library the system already uses, which is the sort of thing
that works until it does not.

### The thing that makes this decidable

**Not all seven need it in the same place.** The plan already says embedding runs "through the
inference server", meaning the model sits in its own service and the Brain just asks it
questions over the network. That half needs no machine-learning code inside the Brain at all.

The other half, reading documents and recognising names, is currently drawn as running
*inside* our own containers, and that is what does not fit.

### Three options

**Option A: put the document and name-recognition models behind the same inference server as
embedding.** One service does all the model work; every Brain container stays small; the plan
already has a service like this for embedding, so this is finishing a pattern rather than
inventing one. It is more setup, and the inference server needs its own memory, which brings
us back to 25. **This is what I would do.**

**Option B: give the parse worker enough memory and accept the bigger image.** Simplest to
build, and it makes the Brain's own image about 1.5 GB larger to deploy and update. On the
current server this does not fit without the right-sizing in 25.

**Option C: ship wave 2 without layout-aware reading.** Plain text and simple PDFs work; a
scanned contract or a document where the meaning is in the table layout does not. This costs
nothing and it is a real reduction in what the system can read, so it should be a choice
rather than a default.

None of this is urgent this week. It is the thing that decides whether wave 2 finishes at 90%
or at 100%, and A and B both depend on 25, which is why 25 is worth answering first.

---

## 29. Nobody can sign in to the console until you give me one web address

**Small, and it blocks everything visual.** One line from you and it is done.

The admin console now exists and builds. It signs in through Keycloak, which is the thing
that decides who your staff are and what they may see.

Keycloak will only send somebody back to an address that was registered in advance. That is
the right behaviour: without it, anybody could point a fake login page at your Keycloak and
collect real sessions. The address is registered in `ops/keycloak/realm-export.json`, and
today it reads `https://console.invalid`, which is a deliberate placeholder meaning "nobody
filled this in yet". `.invalid` is reserved and can never be a real address.

So right now sign-in cannot complete from anywhere, including from a laptop.

**What I need from you: the web address the console will live at.** For example
`https://console.yourdomain.com`, or a subdirectory of a domain you already own. If you do
not have one yet, say so and I will register `http://localhost:5173` only, which lets
development proceed and lets nothing else in.

While I was checking this I found a real bug in the same file and fixed it, so it is worth
knowing it was there. The setting that stamps "this token is for the Brain's API" onto a
login was attached to the wrong half of the configuration: it sat on a component that never
issues logins, so it could never have run. Every sign-in would have appeared to succeed and
then been refused by the system a moment later, which reads to a user as "login is broken"
and would have been very hard to trace. It is fixed and there is now a test that fails if
anybody moves it back. It is **not** verified against a running Keycloak, because there
isn't one I can reach from here; the first real sign-in is what settles it.

---

## 30. How should the automation canvas reach the Brain? Two options, different risk

**Not urgent, and it decides a piece of the design rather than a setting.**

The automation canvas is the drag-and-drop tool where somebody builds "when a ticket is
tagged urgent, look up the client and post to the channel". It runs in a locked box: it has
no route to the internet except through a proxy that only allows named addresses, so a step
somebody adds cannot quietly send your data somewhere.

The custom step that lets an automation ask the Brain a question is now built, and it is
built the right way round: the step names a *tool*, never an address, so the permission
check still runs and an automation can never see more than the person it runs as. That is
the part that mattered and it is done and tested.

**What is not decided is how the locked box talks to the Brain at all.** Today it cannot,
which is safe and useless. There are two ways:

**Option A: give the canvas a private line to the Brain and nothing else.** A second internal
network carrying only those two. Nothing else on your server can see it, and the canvas still
has no route to the internet. More configuration, and it is the option I would choose.

**Option B: put the Brain's public web address on the canvas's allowed list.** One line of
configuration. The cost is that the canvas's traffic to the Brain then goes out and back in
through the public internet, and the allowed list becomes the only thing standing between an
automation and that address.

**My recommendation is A**, because the whole argument for the locked box is that a step
somebody adds cannot reach out, and B spends a little of that to save a little configuration.

Either way nothing is exposed to the public that is not already, and neither option changes
what an automation is allowed to see. That is decided by the permission check, which is
built and does not depend on this.

---

## 25. The full feature set does not fit on the current server

**Measured, not estimated. Nothing is blocked today, and this needs deciding before
wave 4 rather than during it.**

**Re-measured on 2026-09-06, on the live box. Two numbers in the original version of this
note were wrong. The recommendation at the bottom does not change.**

What was written here before: "your server has about 6.4 GB usable, your other production
system already uses 3.7 GB of it". Neither is right.

- **The machine is 11.7 GB, not 6.4 GB.** The 6.4 figure is a budget the code sets for
  itself, deliberately well below the machine, so the Brain can never be the reason a
  neighbour falls over. It was written up as though it were the size of the server.
- **The 3.7 GB is the Brain's own, not your other project's.** The same number appears
  twice for two unrelated things, which is what made this confusing. 3,712 MB is what the
  Brain's four services are *allowed* to use, and separately it happens to be what the full
  feature set wants. Your other containers actually use about **5.2 GB** right now.

The corrected picture, all measured this morning:

| | Measured |
|---|---|
| The machine | 11,960 MB |
| Actually in use, everything on the box | 5,641 MB |
| Of that, the Brain | **394 MB** |
| Free right now | 6,318 MB |
| Swap in use | 1,352 MB of 2,047 |

The Brain today is using **394 MB** of the 3,584 MB it is allowed. It is not the thing
under pressure.

**The conclusion is unchanged: the full feature set is over budget by about 1.3 GB.** That
was right for the right reason, because the budget is a deliberate self-imposed cap rather
than the size of the machine, and the arithmetic behind it was never wrong.

### The thing worth knowing that was not in here before

**15 of the 31 containers on that box have no memory limit at all.** Not a high one, none.
Among them are Coolify itself, `verzbrain-activepieces`, and the two largest consumers on
the machine. Together the containers that *do* have limits are already allowed 9,600 MB on
an 11,960 MB machine, and the 15 unlimited ones are on top of that.

In plain terms: the box is already promised more memory than it has, and the swap figure
above is the evidence that it has been asked for it at least once. Nothing has fallen over,
and the Brain is the well-behaved tenant here rather than the risk. But it does mean that if
something on that machine goes wrong at 3am, the kernel picks what to kill, and it does not
know which container is your client-facing system.

That is not a Brain problem to fix, and I have not touched anything outside our own project.
It is an argument for the second server being about resilience as well as capacity.

**What is using it.** The single biggest item is the database behind the tracing tool
(`langfuse-clickhouse`) at 1 GB. That is the component that records what the AI did, step
by step, so a wrong answer can be explained afterwards. It is genuinely useful and it is
genuinely large.

### You asked for options that cost nothing. There are four, and together they are enough.

**Answered 2026-09-06. You said you did not like any of the paid options and asked whether
there are free ones. There are, and I should have led with them.**

The reason I did not is that I had been treating the declared limits as though they were
requirements. They are not. They are numbers somebody wrote, and one of those numbers is
mine. Measured on the live box this morning:

| | Declared | Actually using |
|---|---|---|
| The Brain's application | 1,024 MB | 330 MB |
| The Brain's database | 2,048 MB | 63 MB |
| The Brain's cache | 512 MB | 5 MB |
| **The Brain, total** | **3,584 MB** | **429 MB** |

The Brain reserves 3.5 GB and uses under half a gigabyte. That reservation is what the
"it does not fit" arithmetic was subtracting.

### Option 1: right-size what is already reserved. Frees about 1.2 GB. Costs nothing.

A limit should be above the real peak with headroom, not eight times it. Proposed, and each
figure is derived from what the service is configured to do rather than from what it happens
to use today: application 768 MB, database 1,280 MB (its `shared_buffers` is 512 MB and the
pooler caps it to twenty backends), cache 384 MB, pooler 64 MB. That is 2,496 MB instead of
3,712 MB.

**The honest caveat, and it is the same one I raised against shrinking the tracing
database.** Those measurements are of a system almost nobody is using. Right-sizing on idle
numbers is how you get an outage under real load. So this is safe to do now and needs a load
test before wave 4 to confirm it, which is a task already on the plan (M22.3.3).

### Option 2: cap the fifteen containers on your box that have no limit at all. Costs nothing.

This is the one I would do first, and it is not really about the Brain.

Your server runs 31 containers. Sixteen declare a memory limit and together they are allowed
9,600 MB. **The other fifteen declare nothing.** They are using 3,693 MB right now and
nothing stops them using more. Among them are Coolify itself and your Activepieces instance.

That means every "how much room is left" answer on this page, including mine, is a guess.
The machine is 11,960 MB and the promises already add up to more than that. Capping those
fifteen near what they actually use would not create a single megabyte, and it would turn
the headroom from something we hope for into something the kernel guarantees. It also
decides, in advance and in daylight, which container dies at three in the morning instead of
leaving that to whichever one asks for memory first.

### Option 3: stop treating the full feature set as all or nothing. Frees 512 MB to 1 GB.

"Full" is a label I put on a list, and the list bundles things that do not have to arrive
together:

- **Activepieces, 512 MB.** The plan itself calls it "an optional sandboxed container ...
  enabled per client by configuration". It has nothing to do with tracing.
- **The PII analyser, 512 MB.** Only needed when text is sent to a third-party model. If the
  reasoning model is self-hosted, it is not on the path at all.

Dropping either from what you actually deploy is a configuration choice, not a downgrade.

### Option 4: run the trace ledger without its database. Frees 1 GB.

ClickHouse is the single biggest item and the reason the total does not fit. Langfuse's
earlier line runs on PostgreSQL alone, against the database you already have. You lose the
query speed that ClickHouse buys over millions of spans, which is a real loss at scale and
not one you are near. Sampling is the smaller version of the same idea: trace one request in
twenty and the same store runs in half the memory.

### Put together

Right-sizing (option 1) plus dropping the optional canvas (option 3) is enough on its own:
the wave-2 components then want 3,200 MB against 3,648 MB available, and it fits with room
to spare. Add option 2 and the budget stops being a guess.

**So my recommendation changes: do options 1 and 2 now, and you do not need to buy
anything.** Option 2 is the one with a deadline, because it protects your other production
system as much as this one.

**What I would still not do** is shrink the tracing database and call it sized. It would
work, then fail under load, and it would fail as "the AI is broken" rather than as "we
undersized a component on purpose in September".

---

**The paid options, kept for the record, since they are what this page said before.**

| Option | Cost | What you give up |
|---|---|---|
| Use a hosted tracing service instead of running one | A subscription, roughly the price of a small server | Your traces sit on somebody else's infrastructure. They contain no client data by design, but they do show what your staff asked |
| A second small server just for tracing | Another VPS, similar to what you pay now | Nothing functionally; one more machine to keep patched |
| Run without full tracing | Nothing | When an answer is wrong, "why" gets much harder to establish. This is the thing that makes an AI system auditable |
| Move your other project off this box | Depends where it goes | Nothing here, but it is work on the other project |

---

---

# Answered

## 28. Production restarts every few minutes - DONE, and it was a leftover timer of mine

**Fixed and verified on 2026-09-06.** You told me to run the command, I ran it, and the
recreates stopped.

**The evidence, before and after.** In the ninety minutes before the fix, `brain-deploy`
ran 21 times and deployed on all 21, recreating the container every time. No new images were
being published in that window, so every one of those was pointless and every one dropped
whatever was in flight. The last was at 02:41:22. `sudo systemctl disable --now
brain-deploy.timer` ran at 02:57, and there has not been another since. `brain-autodeploy`
is untouched and still deploying real changes.

Still worth doing when convenient, and not urgent: delete `/usr/local/bin/brain-deploy`, so
that the next person reading that directory does not find two scripts that look
interchangeable and re-enable the wrong one.

The diagnosis below is kept because it was wrong first, and the way it was wrong is the
useful part.

---

**Corrected on 2026-09-06. The earlier diagnosis on this page was wrong, and it was wrong
because I checked one of two things that could have caused it.** What follows replaces it.

**Nothing is broken and nothing is lost.** The app is healthy and serving the right commit,
and every recreate passes its health check within about eight seconds. But it is being
recreated every three minutes and is briefly unreachable each time, which drops in-flight
requests.

**The cause: there are two deploy timers on your server, and the old one redeploys
unconditionally.**

| Timer | Interval | Behaviour |
|---|---|---|
| `brain-autodeploy.timer` | 2 min | Correct. Compares the pulled image against the running one and does nothing when they match. |
| `brain-deploy.timer` | 3 min | **The problem.** Pulls and recreates the container every single run, whether or not anything changed. |

`brain-deploy` is the older script, the one with the redeploy-loop bug. I rewrote it as
`brain-autodeploy` and installed that. **I never disabled the old timer,** so both have been
running side by side ever since, and the old one has been recreating production every three
minutes.

**How I know, rather than inferring it.** The recreate is logged by the process that did it.
At 08:28:39 `brain-deploy.service` started; at 08:28:42 it logged `pulling` and `deploying`;
at 08:28:42 to 08:28:44 it logged `Recreate`, `Recreated`, `Starting`, `Started` against the
app container; Docker's own event stream shows the matching create/kill/die/destroy/start
burst at 08:28:43. Across the same window `brain-autodeploy` ran twice, at 08:27:07 and
08:29:17, and deployed nothing both times.

**Why the previous answer was wrong.** It blamed Coolify healing an exited one-shot `migrate`
container, and its evidence line said "my own deploy timer logged no deploys across the same
window, so it is not me". That check was real, and it looked at `brain-autodeploy`. It never
occurred to me to ask whether there was a second timer. There was, it was also mine, and it
was the one doing it. The `migrate` service is not involved: the current compose has no
`migrate` service at all.

**The fix is one command, and it is reversible.**

```
sudo systemctl disable --now brain-deploy.timer
```

`brain-autodeploy.timer` already does the job properly and is unaffected. It deployed
tonight's work correctly seven times, most recently `db2a227`, healthy in eight seconds. If
anything goes wrong, `sudo systemctl enable --now brain-deploy.timer` puts the old one back.

**Why I have not run it.** Changing systemd units on your production host is outside what I
am permitted to do unattended, and the guard that stopped me is the right guard. It is one
command and the diagnosis above is measured, so it should take you a minute.

**Worth doing afterwards, and not urgent:** remove `/usr/local/bin/brain-deploy` as well, so
the next person reading `/usr/local/bin` does not find two scripts that look
interchangeable and re-enable the wrong one.

---


## 26. The chat widget on a client's marketing site - DECIDED: public knowledge only, no login

**Decided 2026-09-06.** In your words: public users get public knowledge only, a Super Admin
or Department Admin decides what counts as public knowledge, and there is no login for a
visitor to chat with the widget.

**What that means in this system, and it fits the existing model rather than bending it.**
An anonymous widget session holds exactly one grant, over knowledge explicitly marked
public, and nothing else. Entitlements here are additive only, so a stranger still holds
nothing by default: the difference is that one narrow grant now exists to be held, instead
of none.

Three consequences worth reading before this is built, because they are the parts that go
wrong quietly:

**Marking something public is a one-way door in practice.** Once an answer has been given to
the internet it has been given, and un-marking the source afterwards does not retrieve it.
So the marking action is going to be audited, and it is going to name the person who did it,
in the same way a grant does.

**Public is a property of the knowledge, never of the question.** The widget cannot be
allowed to reach a general search that then filters for public items, because the filter
becomes the only thing standing between a stranger and everything else. The reach is
computed the same way it is for staff, and a public grant simply resolves to a narrow scope.
That is the whole reason this fits: it is the same code path, with a smaller set.

**A department admin can only publish their own department's knowledge.** Their role already
requires a scope, and this is exactly what that scope is for. A Super Admin has no such
limit, which is the distinction between the two roles here.

**Rate limiting and abuse become load-bearing rather than hygiene**, because the widget is
now a service anybody on the internet can call. M23 already carries the widget session
minting and abuse guard, and it stops being an optional refinement the moment this ships.

Not yet built: the public marking itself, the audit row for it, and the anonymous grant.
Those are wave 3 and 4 work and they now have a decision to be built against.

---

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


## 24. When a source is down, should the answer name it? - DECIDED: keep it as built

**Decided 2026-09-06: go with the recommendation. No code changes.** The answer names a
source only when that person could already see it in their own tool list; everyone else is
told part of the answer is unavailable, and the full list goes to the operator's log.

The reasoning is kept below because the cost is real and somebody will meet it: a
narrowly-permissioned person gets a vaguer message and has to ask. When that happens, the
person they ask can read the log, and that is the intended path rather than a workaround.

---

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


## 27. Automatic deploys have never worked, and the pipeline said they had - DONE

**Closed 2026-09-05. Deploys are automatic and verified.** You ran the installer, the timer
fired on install, deployed, and reported the app healthy at schema revision 0007. Next check
was scheduled two minutes later. Nothing further is needed from you.

**What was wrong.** The deploy step checked for three Coolify secrets, did not find them,
printed `Coolify secrets not set - skipping deploy` and exited *successfully*. The next step
then printed **"Deployed"**. So every run looked like a deploy on the summary page and was a
no-op. Your server ran commit `d58b3ce` at 24.1% while the work was at 31.3%: roughly a
hundred tasks finished, tested, pushed and not live.

**How it works now, and why it is not what you were asked for.** You were going to give me a
Coolify URL for GitHub to call. I did not use it. That port is private only because your
firewall allows 22, 80 and 443, and letting GitHub reach it means allowlisting GitHub's
Actions ranges: thousands of them, changed without notice, and anybody with a GitHub account
can run a job from one. That would put the panel controlling every container, database
included, in front of a large slice of the internet to save about a minute of latency.

So the server watches instead. A timer checks every two minutes whether the `:latest` image
has moved and deploys when it has. The CI gate survives, which is the part worth checking
rather than assuming: that tag is only moved by the Deploy workflow, which runs after CI
passes, so the tag moving is itself the statement that the gate passed.

**Two things stop this failing silently the way the last one did.** The deploy script waits
for the container to report healthy before reporting success, and the Deploy workflow polls
the live site for eight minutes and fails unless it reports the commit that run published. A
trigger is not an outcome, and the previous version only ever checked the trigger.

**One mistake of mine in the middle of this**, recorded because it is the same class as the
bug: my first install command named `/usr/local/bin/brain-install-autodeploy`, which had
never been installed, because writing it was blocked as a privileged change. I listed the
steps I could not do and did not check that the ones I could had actually happened. You hit
the error. Checking that a file exists after claiming to install it costs one command.

**Your Coolify token is still unused and still optional.** With it on the server the deploy
goes through Coolify's API so its UI stays truthful about what is running; without it the
deploy uses Coolify's own compose file, which is what runs today. The two commands are in
this repository at `ops/deploy/brain-deploy`.

---

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

**BUILT (2026-09-05).** `auth.directory_role_grant`, migration 0006, with the reconciliation
in `brain.identity.directory`. The sync may delete anything in that table and nothing else,
and that is structural rather than careful: the reconciler's signature has no parameter a
hand-made grant fits into, and a guard refuses any future reconciler whose annotations admit
one. A check inside the function would be removable by whoever adds the feature that needs
it; a parameter that does not exist has to be added first, which is a diff with a reviewer
on it.

**The natural key is (person, role, group), not a generated id.** Two groups both conferring
Approver are two rows, so leaving one group keeps the role. A generated id would let the same
assertion be stored twice, and then removal would delete one row, report the role removed,
and leave the person holding it from the other.

**This is the first DELETE permission granted anywhere in the system**, and it is scoped to
this one table. There is a test whose only job is to fail if any future migration grants
DELETE on anything else, so it does not become a precedent by being copied.

**One thing I got wrong in the brief, worth recording.** I told the agent to wire the union
into the entitlement resolver. That resolver structurally refuses to see a role at all, which
is a rule from earlier in the build, and the agent pushed back rather than breaking it. The
union belongs in the role path and that is where it is. I verified the pushback before
accepting it.

**A near-miss worth stating because it looks like a bug and is not.** Two spellings of a
group name reconcile as a delete plus an insert every run. In the vault holder list (item 17)
I normalised exactly this, because those names are typed by a person and two spellings are
one pair of hands. Here they are not: Keycloak treats `Sales` and `sales` as two groups, so
folding them would merge two sources of one role, and leaving one group would then remove a
role the other still justifies. Exact comparison is correct here. Same-looking problem,
opposite answer.

**Still not built, and not part of this:** the hand-made `role_grant` table (M1.3.2). Only
the type exists. Every docstring in this change says so, so nobody mistakes the new table for
it.

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

**FIXED (2026-09-06), not merely deferred.** The card-building code is written and this gap
is closed by its shape rather than by a check somebody has to remember.

The obvious design was the bug. `card_for(suspension)` rendering the suspended action's
artefact is exactly the leak: that artefact was built from what the *asker* could see and it
carries values. So the builder takes no suspension at all. It takes a body plus the
*approver's* own entitlements, and refuses unless the body was built at the approver's
reach. What survives of the original request is a suspension id and an action digest: two
identifiers, and no value from anybody's data.

Two further guards fell out of it. The card records who it was rendered for and refuses a
press from anybody else, which closes the same leak in the other direction: a card
forwarded to a colleague is inert. And a structural test pins the card's field list, so an
`artefact` field cannot quietly return later for a caller who wants a richer card.

I verified this myself rather than accepting the report: I broke the approver-reach
comparison and confirmed the named test fails.

**What is still true from the original note.** Approvals are still not wired to a screen, so
nothing was ever exposed. The difference is that when they are wired, the leak cannot be
reintroduced by writing the natural code.

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

