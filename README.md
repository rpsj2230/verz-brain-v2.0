# Verz Company Brain

A permission-aware AI platform over a company's own business data. Single-tenant: the
client hosts the entire stack on their own server.

The specification lives outside this repository — architecture, task tracker and key
screens. See `_LINKS.md` in the parent folder for the current URLs.

## The rule everything else follows

```
E_run(caller, agent) = E(caller) ∩ agent_ceiling
```

An agent is a lens, never a principal. It can narrow what a caller reaches and can never
widen it. Two people asking the same agent the same question in the same thread get
different answers, and nothing was configured to make that happen.

Two consequences worth stating before you read any code:

- **Audience is not authority.** Who can *find* an agent and what an agent *returns* are
  separate questions with separate answers. Notion and Dust both ship the opposite — the
  creator's access travels with the agent — which turns every shared assistant into an
  escalation path.
- **Entitlements are additive only.** There is no deny list anywhere. A field is hidden
  because no grant covers it. Deny rules make "can X see Y" an evaluation-order problem
  instead of a lookup.

## Layout

| Path | What lives there |
|---|---|
| `src/brain/core/` | Principal, Scope, Capability, EntitlementSet, the envelope, the error taxonomy |
| `src/brain/gate/` | The fourteen-step request gate |
| `src/brain/agents/` | Agent composition, templates, leashes |
| `src/brain/connectors/` | Source adapters; projection, never bulk copy |
| `src/brain/knowledge/` | Ingestion, chunking, retrieval |
| `src/brain/memory/` | The three memory kinds |
| `src/brain/console/` | Admin console and the member application |
| `src/brain/ext/` | Extension points |
| `tests/invariants/` | Rules that must never break; a failure blocks deploy |

## Running it

```bash
uv sync
uv run pytest
```

Everything above runs today. Anything marked `needs_db` skips until `DATABASE_URL` is
set — CI provides Postgres 18 with pgvector, so the database suites run there from the
first commit.

## Conventions

- Commit subjects start with the task ids they close: `M0.2.4: ...`. The live status page
  is generated from commit messages, so a task with no id in a merged commit does not
  count as done. Nothing is marked done by hand.
- A branch is named for its module, so concurrent tracks do not collide.
- The three files under CODEOWNERS need a second reviewer. A permission bug there is
  silent — it returns a plausible answer that is merely too wide.
