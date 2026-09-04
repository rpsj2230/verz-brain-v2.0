# Needs Rupash

Decisions and access I cannot resolve alone. Each has options and a recommendation so it
can be settled quickly. Nothing here is blocking other work.

Served at `/build/needs-rupash`.

## While you were asleep

Wave 0 went from 57% to 85% of what I can build without you. Three bugs found and fixed,
two of them in my own progress reporting:

- **The entitlement type never checked expiry.** A contractor whose contract ended
  yesterday, with grants still on file, was granted access. Found by the permission
  canaries on their first run — which is the entire reason for writing tests whose correct
  answer is a refusal. Expiry now lives on the entitlement itself rather than in whoever
  builds one.
- **The app never ran migrations.** `Settings` used an env prefix, so it read
  `BRAIN_DATABASE_URL` while compose provided `DATABASE_URL`. It found nothing, skipped
  migrations, and reported healthy — because an app with no database serves documents
  perfectly well. Found by the new CI job that starts the whole stack.
- **The progress number was inflated, twice.** A parent task id closed every leaf beneath
  it, so a commit saying `M0.6` claimed connector cassettes that were not written. Then my
  correction listed ten ids under "deliberately NOT claimed" and claimed all ten, because
  a scanner has no concept of negation. Both rules are now strict: only exact leaf ids, only
  in the subject or a `Closes:` trailer. The number fell from 8.0% to 5.6% and is now true.

The number on the status page is lower than it was last night. That is the correction, not
a regression.

---

## 1. Coolify's admin panel is on plain HTTP, publicly

**Severity: highest thing on this list.**

`http://194.233.66.89:8000/login` answers **200 from the open internet**. Your Coolify
password crosses the network unencrypted every time you sign in, and Coolify controls
every container on that box — including the live `verz-brain-platform`.

This is not about our pipeline; it is true whether or not we automate anything.

| Option | Effort | Result |
|---|---|---|
| **Point a domain at Coolify** (e.g. `coolify.verzdesign.com`) | ~15 min, mostly DNS | Real HTTPS via Let's Encrypt. Fixes it properly |
| Restrict port 8000 in ufw to your IP | ~5 min | Closes public exposure, still plaintext for you |
| Leave it | 0 | Password keeps crossing in clear |

**Recommendation: the domain.** It also lets deploys be automated later over HTTPS, which
is currently the only reason they are not.

---

## 2. Does the Company Brain replace AnyGen for Verz internally?

You already run house skills inside AnyGen today — `verz-master-theme`,
`verz-doc-letterhead`, `seo-audit`, `website-cro-audit`. I asked this when I wrote the
AnyGen teardown and it has not been answered.

It changes real work in **M37 (migration and launch)**: whether those skills are imported
into the new system or rebuilt, whether there is a parallel-running period, and whether
AnyGen gets decommissioned or stays alongside.

| Option | What it means for the build |
|---|---|
| **Replace** | M37 gains skill import, a parallel-running period, and AnyGen decommissioning |
| **Coexist** | M37 shrinks; the two systems split by use case and the skills stay where they are |

**Recommendation: decide late, but decide before wave 5.** Nothing before then depends on
it. I have left M37 as written.

---

## 3. Langfuse is on a box too small for it

`verz-langfuse-*` is running on the VPS. Langfuse documents a minimum of **11 vCPU and
25.5 GiB**; the box has **11.7 GiB total** and about 4.5 GiB already in use by 29
containers.

It is not falling over, but it is running well under its stated floor, and our stack now
sits beside it. This is your other project, so I have not touched it.

**Recommendation: worth a look independently of this build.** If observability matters
later, it needs its own host.

---

## 4. Should deploys happen automatically on every push?

Right now I deploy by running `ops/deploy.sh` over SSH after CI goes green. That works and
needs nothing from you.

Fully automatic would mean GitHub Actions triggering the deploy, which needs either a
Coolify API token crossing plain HTTP (see item 1) or an SSH key held by GitHub.

| Option | Trade-off |
|---|---|
| **Keep as is** — I deploy after each wave | No credentials anywhere. Needs me to be running |
| Watcher on the VPS polling the registry | No inbound access, no secrets in GitHub. Few minutes' delay |
| GitHub Actions over SSH with a forced command | Immediate. GitHub holds a key to your server |

**Recommendation: keep as is until item 1 is fixed**, then revisit. Waves are days apart.

---

## 5. Coolify's copy of the compose file is now stale

Coolify stores the compose you pasted in its own database and runs from that. The repo
version has since changed twice — the Postgres 18 volume path, and removing the `migrate`
service now that migrations run inside the application.

The volume fix you already applied by hand. The migrate removal has not been, so a
`migrate` container is still created on every deploy. It exits 0 and does no harm — the
app migrates itself under an advisory lock and the second attempt finds nothing to do —
but it will keep showing as a red "Exited" beside three healthy services.

I am blocked from writing to Coolify's database, so I cannot sync it.

| Option | Effort |
|---|---|
| **Paste the current compose into Coolify's editor** | 1 min. Removes the red dot for good |
| Leave it | 0. Harmless, but the red dot stays and will mislead again |

The current file is `brain/docker-compose.yml` in the repo.

**Recommendation: paste it next time you are in there.** Nothing depends on it.

---

## 6. Local PostgreSQL for development

Docker Desktop needs administrator rights and WSL2, which this session does not have.

Currently worked around: CI runs Postgres 18 as a service container, and I test migrations
against a throwaway container on the VPS. That has been sufficient so far.

**Recommendation: no action needed yet.** Raise it if local iteration gets slow.
