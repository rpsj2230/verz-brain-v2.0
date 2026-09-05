# Credential slots: what each connector needs, and what it must not be given

Task ids: M38.4.1.3

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
