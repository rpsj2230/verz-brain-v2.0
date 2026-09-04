# Needs Rupash

Decisions and access I cannot resolve alone. Each has options and a recommendation so it
can be settled quickly. Nothing here is blocking other work — I have moved on and will
keep moving.

Served at `/build/needs-rupash`. Last updated 2026-09-04.

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

## 5. Local PostgreSQL for development

Docker Desktop needs administrator rights and WSL2, which this session does not have.

Currently worked around: CI runs Postgres 18 as a service container, and I test migrations
against a throwaway container on the VPS. That has been sufficient so far.

**Recommendation: no action needed yet.** Raise it if local iteration gets slow.
