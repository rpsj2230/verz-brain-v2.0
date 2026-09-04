# Deploying to the VPS

Laptop → GitHub → GHCR → Coolify → VPS.

The first three legs are built and verified. The last two need two credentials that only
the account holder can create, so they are listed here as steps rather than automated.

## What already works

Every push to `main` runs CI. If CI passes, `Deploy` builds the image and pushes it to
`ghcr.io/rpsj2230/verz-brain-v2.0`, tagged with the short commit SHA and `latest`. The
Coolify step then checks for its two secrets and **skips cleanly if they are absent**, so
an unarmed pipeline shows green rather than looking broken.

Verified 2026-09-04: `ghcr.io/rpsj2230/verz-brain-v2.0:6593cf3`,
digest `sha256:131e228179c16705a9cb6c31fc15ef05a18bed91f892d0d3048e5e3d278d5f53`.

## The target box

`verz-vps` — 194.233.66.89, Docker 29.7.2, 11.7 GiB RAM, 86 GiB free disk.

**It is shared.** At last check it ran 29 containers using about 4.5 GiB, including
Coolify itself, a Dify stack, Langfuse, Activepieces, and the existing Coolify project
`verz-brain-platform`. That project is live and must not be touched.

Two consequences, already handled in `docker-compose.yml`:

- Every service declares an explicit memory limit. Without one, a runaway query here
  could take down someone else's production on the same host.
- Postgres is on `expose`, not `ports`. Nothing outside this compose project reaches the
  database, which matters more than usual on a box with no firewall.

Budget for this stack: about **3.8 GiB** (app 1 GiB, Postgres 2 GiB, cache 0.5 GiB,
migrate 0.25 GiB one-shot). That leaves roughly 3 GiB headroom. Adding OpenBao, a worker
and the browser sandbox later will need a hard look at what else the box is running —
Langfuse alone documents a 25.5 GiB minimum, so it does not belong on this host.

## Steps

### 1. Give Coolify a GHCR credential

The package is private because the repo is private, so Coolify cannot pull it anonymously.

Create a classic PAT on the `rpsj2230` account with **`read:packages`** only, then in
Coolify: **Keys & Tokens → Docker Registries → Add**, registry `ghcr.io`, username
`rpsj2230`, password the PAT.

### 2. Create the project

In Coolify: **Projects → + New**, name it `verz-brain-v2`. Do not add resources to
`verz-brain-platform`; that is the other live project.

Inside the new project add a **Docker Compose** resource, source this repository, compose
file `docker-compose.yml`.

### 3. Set the environment

On the resource, set:

| Variable | Value |
|---|---|
| `POSTGRES_PASSWORD` | generate one; Coolify can do this |
| `APP_IMAGE` | `ghcr.io/rpsj2230/verz-brain-v2.0:latest` |
| `BRAIN_ENV` | `production` |
| `BRAIN_COMMIT_SHA` | leave to the pipeline |

Set the health check path to `/health/ready`. **Not `/health/live`** — liveness only says
the process is running. A container that is up but cannot reach the database still answers
questions, from whatever it can still reach, which is how this system would start
returning wrong answers while appearing healthy.

### 4. Arm the pipeline

Copy the resource's deploy webhook URL, and create a Coolify API token
(**Keys & Tokens → API tokens**). Then set both as GitHub repository secrets:

```bash
gh secret set COOLIFY_WEBHOOK_URL --repo rpsj2230/verz-brain-v2.0
gh secret set COOLIFY_TOKEN --repo rpsj2230/verz-brain-v2.0
```

After that, every merge to `main` that passes CI deploys on its own.

## Rollback

Images are tagged by commit SHA, so rolling back is redeploying an older tag — set
`APP_IMAGE` to `ghcr.io/rpsj2230/verz-brain-v2.0:<sha>` and redeploy. Nothing needs
rebuilding, and the SHA in `/health/ready` says exactly what is running.

## What is deliberately not automated

Creating the Coolify project and its tokens is a manual step on purpose. Both require
credentials on the account holder's own infrastructure, and a pipeline that could mint
them would be a pipeline that could also point production somewhere else.
