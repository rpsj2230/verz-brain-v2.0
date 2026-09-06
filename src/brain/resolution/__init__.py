"""Entity resolution: deciding that two records from two systems are one thing.

Freshdesk company 42, Xero contact `CON-99` and a Lark Base row can all be the same client,
and nothing in any of those systems says so. This package is where that judgement is made
and where it is written down.

**The judgement is a probability and the permission is not.** A match score decides whether
two records are believed to be one entity. It never decides who may read either of them.
`canonical` is where that separation is a shape rather than a rule: the value a reader is
handed carries no score, and the predicate that decides reach is never shown one.

**What does not belong here.** Anything that opens a connection or reads a clock. The
records arrive as `brain.connectors.projection.ProjectedRecord`, the fields a reader may see
are `brain.core.redaction`'s, and the split this repository keeps between a module holding a
policy and a module holding a client applies here as everywhere.
"""
