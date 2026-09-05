# Credential slots: what each connector needs, and what it must not be given

Task ids: M38.4.1.3, M5.1.2

Every slot below is **defined and empty**. The path exists, the policy that reaches it
exists, and there is no credential in it until go-live. That ordering is the point: the
scopes are argued about now, while it costs nothing, rather than during the hour somebody
is trying to get a connector working.

**The rule for every row: request the narrowest scope the connector can do its job with, and
never a write scope for a connector that only reads.** A scope granted "to save a round trip
later" is a scope nobody removes.

| Slot | Connector | Scope to request | Deliberately NOT requested |
|---|---|---|---|
| `connectors/creds/xero` | Xero | `accounting.transactions.read`, `accounting.contacts.read` | `.write` on anything. The Brain answers questions about invoices; it does not raise them |
| `connectors/creds/lark_base` | Lark Base | `bitable:app:readonly`, `base:record:read` | `base:record:write`, `drive:drive`. Read-only is already what the existing bot holds |
| `connectors/creds/lark_wiki` | Lark Wiki | `wiki:wiki:readonly` | Anything under `docs:document` that would allow editing |
| `connectors/creds/freshdesk` | Freshdesk | Agent key, read scope | An admin key. An admin key can change SLAs and delete tickets |
| `connectors/creds/hubspot` | HubSpot | `crm.objects.contacts.read`, `crm.objects.deals.read` | `crm.objects.*.write`, and anything touching `settings` |
| `connectors/creds/laravel_readonly` | Laravel MySQL | A database user with SELECT on the allowlisted views only | SELECT on tables. The views are the contract; tables change shape without warning |
| `connectors/creds/google_drive` | Drive or M365 | Read on the specific shared drive | Domain-wide delegation. It reads everything, for everyone, for ever |
| `browser/creds/*` | Browser runner | One credential per site, per task | Anything reusable across sites |

## Model provider keys, which work differently

These are the one category that **cannot be leased**, and the difference is worth
understanding before somebody tries.

Every slot above is a credential the vault can mint fresh and take back: a database user, a
scoped OAuth token. A model provider's API key is not. OpenAI, Anthropic and Moonshot each
issue a key that is valid until a person revokes it in a dashboard, and no engine mints one
per request. So there is nothing to hand back, and wrapping one in a lease with an invented
expiry would be worse than admitting it: the caller would believe the key stops working at a
time nothing enforces.

They are therefore read once at startup, straight into the process environment where the
provider SDK finds it, and never held by application code. Rotation is a restart, which
costs three minutes here.

| Slot | Provider | Environment variable | Notes |
|---|---|---|---|
| `providers/anthropic` | Anthropic | `ANTHROPIC_API_KEY` | Claude, the default reasoner |
| `providers/openai` | OpenAI | `OPENAI_API_KEY` | Embeddings, and a fallback for completion |
| `providers/moonshot` | Moonshot | `MOONSHOT_API_KEY` | The cheaper reasoner; the v1 system routes here by default |

`providers/` is the **only** prefix the static read will touch, and the refusal lives on
`OpenBaoVault.read_static_kv` rather than on its caller. That placement matters: a guard on
the caller is a guard somebody bypasses by calling the other thing. Reading
`connectors/creds/xero` this way would work perfectly, hand out a standing credential with
nothing to revoke and no record of which run held it, and nobody would see the difference
until an audit asked.

## Three things worth deciding before the keys are issued, not after

**Xero's limit is per tenant and it is 5,000 a day.** That is a documented ceiling and it is
the one most likely to be hit by a backfill. The rate limits configured in the connector must
match the real account tier rather than the documentation, which is M38.4.2.3.

**Freshdesk search returns at most 300 records, ever.** Not a page size: a ceiling. Anything
that reads "all tickets matching" is wrong beyond 300 and will look correct in testing.

**Lark Base's 100 requests per minute is permanently uncapped** and does not rise with a
plan. Sizing anything against a higher number is sizing against a number that does not exist.

## What happens at go-live

M38.4.2.1 puts real credentials in these slots. Until then every one of them returns nothing,
and a connector asking for one gets a vault error rather than an empty string, which is the
difference between a visible outage and a connector that silently reads nothing and reports
that there is nothing there.
