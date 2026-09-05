# The secrets vault: setting it up, and opening it after a restart

For whoever operates the Company Brain. Written to be followed by someone who has not read
the code.

Task ids: M31.3.2.1

---

## What this is, in one paragraph

The vault holds the passwords and API keys the system uses to reach Xero, Lark, Freshdesk
and its own database. It exists so those keys are not sitting in configuration files, where
they live as long as the server and appear in every backup and every screen-share.

The vault starts **sealed**. Sealed means locked: it is running, it will answer that it is
alive, and it cannot read a single one of its own secrets until somebody opens it. That
happens on first install and again after every restart of the vault container.

**While it is sealed, the system cannot reach any connector.** The Brain will start, the
console will load, and questions that need Xero or Lark will say the source could not be
reached. That is the correct behaviour, and it is also the symptom you will see if nobody
noticed a restart.

---

## Before you start: the three decisions, and where they now live

These were item 17 on the Needs Rupash page and they are settled.

1. **How many key pieces, and how many are needed to open it: five pieces, any three open
   it.** Three people have to agree, and you survive losing two.
2. **Who holds a piece: five people, named in the policy.** They should not all be reachable
   through the same laptop, the same phone or the same building, and at least one must be
   someone who is never on call, so a piece exists outside the group that would be handling
   an incident.
3. **The root token is destroyed after setup.** It can do anything, including undo every
   policy in this directory. Step 6 destroys it.

**None of these numbers is typed into this page.** They live in `brain.ops.vault_quorum`,
which is where they get reviewed and where the five holders are named, and every command
below is printed by that module. Two numbers on one shell line have no way of refusing a
combination that is arithmetically fine and operationally ruinous, and three of those exist:
a threshold of one hands every holder a key that opens the vault alone; a threshold equal to
the share count means one lost laptop destroys the vault permanently; and one person holding
two pieces makes a three-of-five secretly a two-of-four. The module refuses all three, and a
test compares what it prints against this page, so the two cannot drift apart.

To see the numbers and the commands, and to check whether the five holders have actually
been named yet:

    uv run python -m brain.ops.vault_quorum

It exits non-zero while any holder slot is still `UNASSIGNED`. That is not a bug to work
around: a vault whose custodians have not been chosen is not ready to be initialised, and
initialising it anyway hands five pieces to nobody in particular.

**Changing the split later is not an edit.** The split is fixed at `bao operator init`. The
only way to change it afterwards is `bao operator rekey`, which itself needs three of the
current five holders to take part. Editing the policy after initialisation changes what this
page says and nothing about the running vault.

---

## First install

**Step 1. Start the vault.**

    docker compose -f ops/openbao/compose.yml up -d

You should see one container running and reporting itself as sealed.

**Step 2. Initialise it. This happens exactly once, ever.**

The line below is the one `brain.ops.vault_quorum` prints. Do not retype it from memory and
do not adjust the numbers here; if they need to change, change the policy and this page will
be updated with it, because a test fails when they disagree.

    docker exec -it brain-vault bao operator init -key-shares=5 -key-threshold=3

The screen will print **five unseal key pieces and one root token**. This is the only time
they are ever shown. Nobody can recover them later, including the vault itself.

**Step 3. Distribute the pieces, now, before doing anything else.**

Each of the five people takes one piece, in the order the policy names them. Send each piece
to its holder **individually and through a different channel from the one you used to tell
them it was coming**: not all five in one group chat, and not in the same thread that says
what they are for.

Do not put the pieces in the Brain. Do not put them in a document in the same Google account
that the Brain can reach. The whole point is that they live somewhere the system does not.

**Step 4. Open it for the first time.** Run this once per piece, with three different
pieces:

    docker exec -it brain-vault bao operator unseal

It will ask for a key, and tell you how many more it needs. After the third, it reports
`Sealed: false`.

**Step 5. Load the policies.**

    sh ops/openbao/load-policies.sh

**Step 6, `revoke_root`. Check that a normal role works, then destroy the root token.**

Confirm the application can get a lease using its own policy, and confirm whichever admin
login you will use from now on actually works. Then, and only then:

    docker exec -it brain-vault bao token revoke -self

From this point nothing holds unlimited power over the vault. That is the intended state.

**Why this step is not optional, and why it is not enough to just put the token somewhere
safe.** A root token that still exists is a credential that bypasses every policy in
`ops/openbao/policies`, and the audit log cannot tell you when it was used: an entry records
a token accessor as an HMAC, with no field marking one as root, so "somebody used the root
token" and "an administrator did their job" are the same line in the log. Locking the token
in a drawer or a sealed envelope does not change either fact. It protects the paper, not the
token, and the token was also in whatever terminal scrollback, screen recording or shell
history existed when step 2 printed it, so the envelope was never the only copy.

**If a root token is genuinely needed again**, it is regenerated rather than retrieved:

    docker exec -it brain-vault bao operator generate-root

That needs three of the five holders, which is the same three people who could open the
vault anyway, and it leaves a record that it happened. Nothing an envelope offers is
missing, and the permanent unattributable bypass is.

---

## After a restart

This is the one you will actually do, and it is short.

**How you will know.** Questions that need an outside system start answering "I could not
reach one of the systems needed to answer that". The vault container will be running.

**What to do.** Three of the five holders each run:

    docker exec -it brain-vault bao operator unseal

Once the third piece goes in, it opens. The Brain reconnects on its own; nothing needs
restarting.

**How long it takes:** about two minutes, most of which is reaching three people.

---

## Things worth knowing before they happen

**Losing three pieces means losing the vault.** Not the data the Brain holds, which is in
Postgres, but every credential stored here. Recovery means re-issuing every API key from
every provider by hand. This is the reason for five pieces rather than three.

**A restart is not an emergency, but it is silent.** The vault does not announce that it
sealed. The first sign is a connector failing. Worth knowing so that the answer is "somebody
restarted the vault" rather than an hour spent looking at Xero.

**Automatic unsealing exists and is deliberately not used here.** It works by keeping the
key somewhere the machine can read it, which means a vault that opens itself for anyone who
gets the machine. That trade is right for a large estate with hardware key storage. It is
not right for one server holding one company's credentials.

**The root token is not a spare key.** If it still exists, it is a permanent way around
every policy in this directory. That is why step 6 destroys it, and why a new one has to be
generated by the same people who hold the unseal pieces if it is ever genuinely needed.
