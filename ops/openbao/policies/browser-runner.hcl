# What the browser runner may do with the secrets vault.
#
# Task ids: M31.3.2.2
#
# The browser runner drives a real browser against real websites, which makes it the most
# exposed component in the system: it executes content it did not write, on pages it does
# not control. Its policy is therefore the narrowest of the three.
#
# It gets no database credential at all. It never queries anything; it is handed a task and
# returns what it saw. A browser process holding a database credential is one page-level
# exploit away from being a database client.

path "browser/creds/+" {
  capabilities = ["read"]
}

path "sys/leases/revoke" {
  capabilities = ["update"]
}

# No renew, deliberately. A browser task that has run past its lease should stop rather than
# extend: a runner stuck in a loop on a hostile page is exactly the thing that should lose
# its credential rather than keep asking for more time.
