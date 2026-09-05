"""Connectors: adapters onto systems the company already runs.

Each fetches records and returns them typed, and does nothing else. A connector never
decides what a caller may see: it returns everything it fetched and the redactor removes
what is not covered. Putting that decision here would mean auditing every connector for
permission logic instead of auditing one redactor.

**What does not belong here.** Credentials. A connector borrows a lease from
`brain.ops.secrets` for the duration of a call and cannot read one by path.
"""
