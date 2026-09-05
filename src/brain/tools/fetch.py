"""Bringing a skill in from somewhere else, without the server becoming the attacker's client.

`SkillSource` already refuses an import that could resolve differently later: a GitHub source
carries a full commit sha, and a URL source carries a sha256 of the bytes. That is the pin.
This module is the fetch, and it exists because a pin says nothing about *where the server
was made to connect*.

**The risk here is not the skill, it is the request.** Importing from a URL means a person
hands this server an address and the server connects to it. On a client-hosted deployment
that server sits inside the client's network, so an address is a request to reach anything
that network can reach: a cloud provider's metadata endpoint at 169.254.169.254 handing out
instance credentials, a database on a private subnet, an internal admin page that trusts its
own network. The skill never has to parse for any of that to work, because the damage is
done by the connection and by what comes back in the error message.

So the address is checked before anything is opened, and the rules are about reachability
rather than about content:

**https only, and no credentials in the URL.** Plain http is rewritable by anything on the
path, and `https://user:token@host/` is a way to smuggle a credential into a log line, since
the URL is written to the import record that a reviewer later reads.

**No literal address in a range that only means "inside".** Loopback, link-local, private,
carrier-grade NAT, multicast and the unspecified address, in both IPv4 and IPv6, plus
IPv4-mapped IPv6, which is the spelling that gets missed: `[::ffff:169.254.169.254]` is the
metadata endpoint written so that a naive IPv4 check does not see it.

**A hostname is resolved and every address it resolves to is checked, not just the first.**
A name with two A records, one public and one private, passes a check that looks at one
answer and fails at the second connection attempt, which is a retry away.

**The resolved address is handed back for the caller to connect to.** Checking a name and
then connecting by name is a check that can be true when it is made and false when it is
used: the second lookup can return a different answer, which is DNS rebinding, and it is the
standard bypass for exactly this defence. `Fetchable.address` exists so the transport can
connect to the address that was checked and carry the hostname in the `Host` header and in
SNI. Nothing here can force that, and the docstring on `Fetchable` says so plainly rather
than implying a guarantee this module cannot make.

**Every redirect is a new address and gets the whole check again.** A permitted public URL
that answers `302 Location: http://169.254.169.254/` defeats a check made only on the first
address, and following redirects is the default in every HTTP client anybody would reach for.
The hop count is bounded as well, because a redirect loop is a request that never ends.

**Size is enforced against the bytes, never against the declared length.** `Content-Length`
is a claim made by the thing being fetched.

Rejected: an allowlist of hosts instead of a denylist of ranges. It is genuinely safer, and
it is wrong for this feature, because the feature is "import a skill from anywhere on the
internet" and an allowlist turns it into "import a skill from the three places an
administrator has already thought of". The denylist is written against what "inside" means
rather than against known-bad hosts, which is why it is a closed set that does not need
maintaining.

Nothing here opens a socket. `Fetcher` is the seam, and a module that owned an HTTP client
could not be tested for the redirect chain, which is the part of this that is ever wrong.

Task ids: M12.2.2, M12.2.3
"""

from __future__ import annotations

import hashlib
import ipaddress
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import urlsplit

from brain.tools.skills import SkillError, SkillSource, SourceKind

#: How many redirects are followed before the fetch is abandoned. Five is what browsers and
#: curl settle on; the number matters less than it being finite, because a redirect loop is
#: a request that never returns and a fetch that never returns holds a worker.
MAX_REDIRECTS: Final = 5

#: The ceiling on what a skill import may transfer. A skill is a folder of markdown and small
#: scripts; a hundred megabytes of it is not a skill. Enforced against bytes received.
MAX_FETCH_BYTES: Final = 20 * 1024 * 1024

#: The one host GitHub imports may reach. Narrow on purpose: a GitHub source names
#: `owner/repo` rather than a URL, so this module builds the address instead of accepting
#: one, and there is no reason for that address to vary.
GITHUB_TARBALL_HOST: Final = "codeload.github.com"


class UnsafeAddressError(SkillError):
    """The address would have made this server connect somewhere it should not.

    A subclass of `SkillError` so that the import path's existing handling covers it, and a
    distinct type so an operator can tell "this skill is malformed" from "somebody pointed
    our importer at the metadata service".
    """


class Resolver(Protocol):
    """Turns a hostname into every address it currently answers with.

    A protocol rather than `socket.getaddrinfo` directly, because the interesting cases are
    a name with two records where one is private, and a name that resolves differently on
    the second lookup. Neither is reachable in a test against the real resolver.
    """

    def resolve(self, host: str) -> Sequence[str]: ...


@dataclass(frozen=True)
class FetchedBytes:
    """What came back, and where it came back from after any redirects."""

    body: bytes
    final_url: str


class Fetcher(Protocol):
    """One hop. Returns either a body or the next address, and never follows a redirect itself.

    Deliberately one hop rather than a whole fetch: the redirect chain is where this module's
    rules are applied, and a client that followed redirects internally would apply them to
    the first address only.
    """

    def get_once(self, url: str, *, address: str, max_bytes: int) -> FetchedBytes | str: ...


@dataclass(frozen=True)
class Fetchable:
    """An address that passed every check, and the resolved address it passed them for.

    **Connecting to `url` by name reopens the hole this type exists to close.** The check ran
    against `address`; a fresh lookup at connect time may answer differently, and that
    difference is the whole of DNS rebinding. A transport should connect to `address`, send
    `host` in the `Host` header and in SNI, and verify the certificate against `host`.

    This module cannot enforce that, and saying so here is the point: a type that implied a
    guarantee it does not provide would be worse than no type at all.
    """

    url: str
    host: str
    address: str


def _is_reachable_only_from_inside(address: str) -> bool:
    """Whether an address means "somewhere on this network" rather than "on the internet".

    Written against what the address ranges mean rather than against a list of known targets,
    which is why it is a closed set rather than a list somebody has to maintain.

    **On Python 3.13, `is_global` alone answers every case here, and everything beside it is
    redundant.** Measured rather than assumed: `ipaddress.ip_address("::ffff:169.254.169.254")`
    reports `is_global` false and `is_link_local` *true*, because CPython now delegates an
    IPv4-mapped address to its v4 meaning. An earlier version of this docstring said the
    opposite, and it was describing an older interpreter.

    The redundancy is kept on purpose, and the reason is that change itself. The mapped
    delegation was added at some point, which means that on the interpreter before it the
    naive check genuinely was the hole. These predicates are library definitions of a
    security boundary and they have moved before; resting the whole defence on one of them
    is resting it on a definition somebody else maintains.

    **So a mutation of any single predicate below survives the suite, and that is expected.**
    `test_an_address_that_only_means_inside_is_refused` pins the behaviour for every range by
    name, which is the property that matters; it cannot distinguish which predicate did the
    work, because on this interpreter any one of them would. Removing them all is caught.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        # Not an address at all. The caller resolves names before reaching here, so this is
        # a resolver returning something unparseable, which is refused rather than assumed
        # harmless.
        return True

    mapped = getattr(parsed, "ipv4_mapped", None)
    if mapped is not None:
        parsed = mapped

    return (
        not parsed.is_global
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_private
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


def assert_fetchable(url: str, resolver: Resolver) -> Fetchable:
    """Refuse an address before anything connects to it.

    Every rule is about where the connection would go. Nothing here looks at what might come
    back, because by the time something has come back the connection has already happened
    and, for the metadata endpoint, that was the whole attack.
    """
    parts = urlsplit(url)

    if parts.scheme != "https":
        msg = (
            f"{url!r} is not https. A skill fetched over a channel somebody on the path can "
            "rewrite is a skill somebody else wrote."
        )
        raise UnsafeAddressError(msg)

    if parts.username or parts.password:
        msg = (
            "a skill URL carries no credentials. The URL is written to the import record a "
            "reviewer reads, so a token in it is a token in a log."
        )
        raise UnsafeAddressError(msg)

    # An IPv6 literal must be bracketed, and one that is not gets *reinterpreted* rather
    # than rejected: `urlsplit("https://fd00::1/x").hostname` is `"fd00"`, a name, which
    # then goes to the resolver and is answered by whatever that says. The address check
    # never sees an address at all. Refused as malformed rather than parsed generously,
    # because a security check that reinterprets its input is checking something else.
    # One colon is a port; two or more unbracketed is an address written wrongly.
    authority = parts.netloc.rpartition("@")[2]
    if authority.count(":") > 1 and not authority.startswith("["):
        msg = (
            f"{url!r} has an unbracketed IPv6 authority. Bracket it as https://[::1]/ so it "
            "is read as an address; unbracketed it parses as a hostname and is resolved."
        )
        raise UnsafeAddressError(msg)

    host = parts.hostname
    if not host:
        msg = f"{url!r} names no host"
        raise UnsafeAddressError(msg)

    # A literal address needs no resolver, and passing one through the resolver would let a
    # resolver implementation decide whether `169.254.169.254` is acceptable.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        addresses = list(resolver.resolve(host))
    else:
        addresses = [host]

    if not addresses:
        msg = f"{host!r} resolves to nothing"
        raise UnsafeAddressError(msg)

    # Every answer, not the first. A name with one public and one private record passes a
    # first-answer check and reaches the private one on a retry.
    for address in addresses:
        if _is_reachable_only_from_inside(address):
            msg = (
                f"{host!r} resolves to {address}, which is reachable only from inside this "
                "network. A skill import must not be a way to make this server open a "
                "connection to its own infrastructure."
            )
            raise UnsafeAddressError(msg)

    return Fetchable(url=url, host=host, address=addresses[0])


def fetch(
    url: str,
    *,
    fetcher: Fetcher,
    resolver: Resolver,
    max_bytes: int = MAX_FETCH_BYTES,
    max_redirects: int = MAX_REDIRECTS,
) -> FetchedBytes:
    """Follow the chain, checking every hop, and return the bytes.

    The loop is here rather than in the client because a redirect is a new address chosen by
    the thing being fetched. A client following redirects internally applies the address
    rules to the first address only, which is the same as not applying them.
    """
    seen: list[str] = []
    current = url
    for _hop in range(max_redirects + 1):
        target = assert_fetchable(current, resolver)
        seen.append(target.url)
        result = fetcher.get_once(target.url, address=target.address, max_bytes=max_bytes)
        if isinstance(result, FetchedBytes):
            if len(result.body) > max_bytes:
                msg = (
                    f"the fetch returned {len(result.body)} bytes against a ceiling of "
                    f"{max_bytes}. A declared length is a claim; this is the count."
                )
                raise SkillError(msg)
            return result
        current = result
    msg = (
        f"stopped after {max_redirects} redirects starting at {url!r}. A chain this long is "
        "either a loop or a redirector, and neither is a skill."
    )
    raise SkillError(msg)


def github_tarball_url(location: str, commit: str) -> str:
    """The address for one commit of one repository, built here rather than accepted.

    A GitHub source names `owner/repo` and a commit, and `SkillSource` has already refused
    anything that is not a full sha. Building the URL means the host cannot be chosen by
    whoever requested the import, so the whole address-check surface below applies to URL
    imports and this one is narrow by construction.
    """
    return f"https://{GITHUB_TARBALL_HOST}/{location}/tar.gz/{commit}"


def fetch_skill_source(
    source: SkillSource,
    *,
    fetcher: Fetcher,
    resolver: Resolver,
    max_bytes: int = MAX_FETCH_BYTES,
) -> bytes:
    """Fetch what a source points at, and prove it is what was pinned.

    **A GitHub commit is verified differently from a URL, and the difference is not an
    oversight.** A commit sha is a hash over the tree, so fetching that sha names the content
    exactly; but the *tarball* GitHub builds from it is compressed on their side and its
    bytes are not promised to be stable, so a stored digest over the tarball would fail on a
    re-fetch that returned the same tree. The sha is the pin, and `content_digest` on a
    GitHub source stays empty for that reason.

    A URL has no such identifier, so the digest over the bytes is the only pin there is, and
    a mismatch is refused rather than recorded. Recording it would make the import succeed
    with content nobody reviewed, which is the failure this whole module exists to prevent.
    """
    if source.kind is SourceKind.UPLOAD:
        msg = "an upload arrives with its bytes; there is nothing to fetch"
        raise SkillError(msg)

    if source.kind is SourceKind.GITHUB:
        url = github_tarball_url(source.location, source.commit)
        return fetch(url, fetcher=fetcher, resolver=resolver, max_bytes=max_bytes).body

    fetched = fetch(source.location, fetcher=fetcher, resolver=resolver, max_bytes=max_bytes)
    digest = hashlib.sha256(fetched.body).hexdigest()
    if digest != source.content_digest:
        msg = (
            f"{source.location} returned bytes with digest {digest}, and the source is pinned "
            f"to {source.content_digest}. The address answered with something other than what "
            "was reviewed."
        )
        raise SkillError(msg)
    return fetched.body
