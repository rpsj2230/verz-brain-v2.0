# What the application may do with the secrets vault.
#
# Task ids: M31.3.2.2
#
# The application answers questions. It borrows connector credentials for the length of one
# request and gives them back, which is why every rule below is about *creating and
# revoking a lease* and none is about reading a stored secret.
#
# It cannot read a static secret at all. That is the point of the split: a role that can
# read `connectors/xero` holds Xero's key for as long as the process lives, and every leak
# after that is a copy of a key nobody can rotate without noticing what broke.

# Ask for a lease against a dynamic role. The credential the vault mints is scoped and
# expires; the application never sees the underlying key.
path "database/creds/brain_app" {
  capabilities = ["read"]
}

path "connectors/creds/+" {
  capabilities = ["read"]
}

# Give a lease back. Explicitly granted rather than assumed: a role that can create leases
# and cannot revoke them accumulates live credentials until they expire, and the whole
# design rests on revocation happening at the end of a run.
path "sys/leases/revoke" {
  capabilities = ["update"]
}

path "sys/leases/renew" {
  capabilities = ["update"]
}

# Deny by omission is the default here, so the following are listed only to say they were
# considered and refused rather than forgotten:
#
#   sys/policy*   changing its own policy is the escalation this file exists to prevent
#   sys/unseal    the application is not the operator
#   auth/*        minting tokens for other roles
#   secret/data/* static secrets, for the reason at the top
