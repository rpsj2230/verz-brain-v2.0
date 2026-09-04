#!/bin/sh
# Blocks port 8000 (the Coolify panel) from the public internet.
#
# The panel is served over HTTPS on coolify.194.233.66.89.sslip.io instead, with a real
# Let's Encrypt certificate. Before this, the login page answered plain HTTP on a public
# IP and the password crossed the internet in clear on every sign-in.
#
# ufw cannot do this alone: Docker publishes the port to 0.0.0.0 and inserts its own
# iptables rules ahead of ufw, so `ufw deny 8000` never sees the packet. DOCKER-USER is
# the chain Docker leaves alone for exactly this.
#
# Manages port 8000 only. There is a separate 5003 rule on this box belonging to another
# project; it is deliberately left alone, and note that it will NOT survive a reboot -
# nothing persists it.
#
# To reopen: iptables -D DOCKER-USER -p tcp -m conntrack --ctorigdstport 8000 -j DROP
set -eu
iptables -C DOCKER-USER -p tcp -m conntrack --ctorigdstport 8000 -j DROP 2>/dev/null \
  || iptables -I DOCKER-USER -p tcp -m conntrack --ctorigdstport 8000 -j DROP
echo "port 8000 blocked; panel is at https://coolify.194.233.66.89.sslip.io"
