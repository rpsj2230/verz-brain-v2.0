"""Channels: the surfaces a person reaches this system through.

Every adapter here is a translator and nothing else. It turns what arrived into a
`ChannelEvent` the gate understands, and turns a `ChannelPayload` the gate produced into
whatever the surface renders. It decides nothing about who may see what.

**What does not belong here.** Any query, any entitlement check, any redaction. The gate
already did all three, and a channel that could do them again would be a second opinion -
the day the two disagree, the permissive one is the one the person sees.
"""
