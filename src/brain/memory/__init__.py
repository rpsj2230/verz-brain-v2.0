"""Memory: what the system retains between conversations.

Memory is written by a principal and read back at that principal's reach, never
wider. The failure this guards against is specific: something learnt while acting for
somebody with broad access, recalled later while acting for somebody without it.

**What does not belong here.** Anything that would be a fact about the company
rather than about a conversation. That is `brain.knowledge`, where it carries a
document's permissions instead of a conversation's.
"""
