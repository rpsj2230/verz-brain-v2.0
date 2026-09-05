"""Importing a skill from the internet, without the server becoming somebody's HTTP client.

Every test here is an address that should not be reached, or a way an address that was
checked stops being the address that is connected to. The skill's own content is tested in
`test_skills.py` and `test_extract.py`; nothing about parsing is re-tested here.

The fake resolver is the important fixture. The interesting failures are a name with two
records where one is private, and a redirect to a target the first check never saw, and
neither is reachable against a real resolver on a machine with a network.

Task ids: M12.2.2, M12.2.3
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

import pytest

from brain.tools.fetch import (
    GITHUB_TARBALL_HOST,
    MAX_FETCH_BYTES,
    FetchedBytes,
    UnsafeAddressError,
    assert_fetchable,
    fetch,
    fetch_skill_source,
    github_tarball_url,
)
from brain.tools.skills import SkillError, SkillSource, SourceKind

COMMIT = "a" * 40
BODY = b"skill archive bytes"
DIGEST = hashlib.sha256(BODY).hexdigest()


class Resolver:
    """Whatever the test says a name resolves to. Unknown names resolve to one public
    address, so a test only has to state the case it is about."""

    def __init__(self, answers: dict[str, Sequence[str]] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[str] = []

    def resolve(self, host: str) -> Sequence[str]:
        self.calls.append(host)
        return self.answers.get(host, ["93.184.216.34"])


class Hops:
    """A fetcher with a scripted chain: each URL either redirects or returns bytes."""

    def __init__(self, script: dict[str, str | bytes]) -> None:
        self.script = script
        self.connected: list[tuple[str, str]] = []

    def get_once(self, url: str, *, address: str, max_bytes: int) -> FetchedBytes | str:
        del max_bytes
        self.connected.append((url, address))
        answer = self.script.get(url, BODY)
        if isinstance(answer, bytes):
            return FetchedBytes(body=answer, final_url=url)
        return answer


# ------------------------------------------------------------------ the address itself
def test_a_public_https_address_is_fetchable() -> None:
    """If this fails every refusal below passes for the wrong reason: a checker that refuses
    everything satisfies all of them."""
    ok = assert_fetchable("https://example.com/skill.tar.gz", Resolver())

    assert ok.host == "example.com"
    assert ok.address == "93.184.216.34"


def test_plain_http_is_refused() -> None:
    """A skill fetched over a channel anything on the path can rewrite is a skill somebody
    else wrote, and the reviewer approved the one they read."""
    with pytest.raises(UnsafeAddressError, match="not https"):
        assert_fetchable("http://example.com/skill.tar.gz", Resolver())


def test_a_url_carrying_credentials_is_refused() -> None:
    """The URL is written to the import record a reviewer later reads, so a token inside it
    is a token in a log with that log's retention rather than the token's.

    Delete this and `https://x:ghp_realtoken@host/` imports successfully and leaves the
    token in the audit trail, which is the one place nobody thinks to look for one."""
    with pytest.raises(UnsafeAddressError, match="no credentials"):
        assert_fetchable("https://user:token@example.com/skill.tar.gz", Resolver())


@pytest.mark.parametrize(
    ("address", "what"),
    [
        ("127.0.0.1", "loopback"),
        ("169.254.169.254", "the cloud metadata endpoint"),
        ("10.1.2.3", "a private range"),
        ("192.168.0.5", "a home or office range"),
        ("172.16.4.4", "the other private range"),
        ("100.64.0.1", "carrier-grade NAT"),
        # S104 reads this as a bind address. Here it is the opposite: an address this
        # module must refuse to *connect* to, which is why the suppression is local.
        ("0.0.0.0", "the unspecified address"),  # noqa: S104
        ("224.0.0.1", "multicast"),
        ("[::1]", "IPv6 loopback"),
        ("[fd00::1]", "an IPv6 unique local address"),
        ("[fe80::1]", "an IPv6 link-local address"),
    ],
)
def test_an_address_that_only_means_inside_is_refused(address: str, what: str) -> None:
    """The whole point of the module. This server sits inside a client's network, so an
    address supplied with an import request is a request to reach whatever that network can
    reach: instance credentials from the metadata endpoint, a database on a private subnet,
    an admin page that trusts its own network.

    Parameterised because each range is a separate mistake to make, and a checker written
    against one of them looks correct until somebody tries the next."""
    with pytest.raises(UnsafeAddressError, match="only from inside"):
        assert_fetchable(f"https://{address}/skill.tar.gz", Resolver())


@pytest.mark.parametrize("address", ["::1", "fd00::1", "::ffff:169.254.169.254"])
def test_an_unbracketed_ipv6_address_is_refused_rather_than_reparsed(address: str) -> None:
    """Found by this test file failing, and it is the sharper half of the mapped-address
    case above.

    `urlsplit("https://fd00::1/x").hostname` is `"fd00"`. Not an error, not an address: a
    *name*, which then goes to the resolver and is answered by whatever that says. The
    address check never sees an address, so every range check in this file is skipped by
    leaving out two square brackets.

    Refused as malformed rather than parsed generously, because a check that reinterprets
    its input is checking something else. Delete this and the whole private-range defence
    has a one-character bypass."""
    with pytest.raises(UnsafeAddressError, match="unbracketed"):
        assert_fetchable(f"https://{address}/skill.tar.gz", Resolver())


def test_the_metadata_endpoint_written_as_a_mapped_ipv6_address_is_refused() -> None:
    """`::ffff:169.254.169.254` is the metadata endpoint written as IPv6.

    Worth pinning even though CPython 3.13 already handles it. A check written by hand
    against `fe80::/10` does not match this address, and on interpreters before the
    IPv4-mapped delegation was added, `is_link_local` on it was false. The spelling is the
    classic bypass for a hand-rolled range check, and nothing stops somebody replacing the
    library predicates with one.

    Delete this and the mapped spelling is covered only by a library behaviour that has
    already changed once."""
    with pytest.raises(UnsafeAddressError, match="only from inside"):
        assert_fetchable("https://[::ffff:169.254.169.254]/skill.tar.gz", Resolver())


def test_a_name_is_refused_when_any_of_its_addresses_is_private() -> None:
    """A name with two records, one public and one private, passes a check that looks at the
    first answer and reaches the private one on the next connection, which is a retry away.

    Delete this and the check is correct for every name that resolves to exactly one thing,
    which is every name in every other test here."""
    resolver = Resolver({"split.example.com": ["93.184.216.34", "10.0.0.7"]})

    with pytest.raises(UnsafeAddressError, match=re.escape("10.0.0.7")):
        assert_fetchable("https://split.example.com/s.tar.gz", resolver)


def test_a_name_resolving_to_nothing_is_refused() -> None:
    """An empty answer is not permission. Without this the address list is empty, every loop
    over it passes, and the first index raises an unrelated error at a worse moment."""
    with pytest.raises(UnsafeAddressError, match="resolves to nothing"):
        assert_fetchable("https://void.example.com/s.tar.gz", Resolver({"void.example.com": []}))


def test_a_resolver_answering_with_something_unparseable_is_refused() -> None:
    """Refused rather than ignored. A resolver returning a name, an empty string or a
    hostname with a port is not a resolver this module understands, and treating what it
    cannot parse as safe means the one unparseable answer is the one that gets through."""
    resolver = Resolver({"odd.example.com": ["not-an-address"]})

    with pytest.raises(UnsafeAddressError, match="only from inside"):
        assert_fetchable("https://odd.example.com/s.tar.gz", resolver)


def test_the_checked_address_travels_with_the_result() -> None:
    """Checking a name and then connecting by name is a check that can be true when made and
    false when used, which is DNS rebinding and is the standard bypass for this defence.

    `Fetchable.address` is what a transport should connect to. This test asserts it is
    carried; nothing here can make a transport use it, and the type says so."""
    resolver = Resolver({"host.example.com": ["93.184.216.34"]})

    ok = assert_fetchable("https://host.example.com/s.tar.gz", resolver)

    assert ok.address == "93.184.216.34"
    assert ok.host == "host.example.com"


# --------------------------------------------------------------------- the redirects
def test_a_redirect_is_checked_before_it_is_followed() -> None:
    """A permitted public URL answering `302 Location: http://169.254.169.254/` defeats a
    check made only on the first address, and following redirects is the default in every
    HTTP client anybody reaches for.

    Delete this and the address rules apply to the address the requester was willing to show
    us, which is the one address that was never the problem."""
    hops = Hops({"https://example.com/s.tar.gz": "https://169.254.169.254/latest/meta-data/"})

    with pytest.raises(UnsafeAddressError, match="only from inside"):
        fetch("https://example.com/s.tar.gz", fetcher=hops, resolver=Resolver())

    assert len(hops.connected) == 1, "the redirect target was connected to before being checked"


def test_a_redirect_that_downgrades_to_http_is_refused() -> None:
    """The scheme check has to run on every hop for the same reason the address check does."""
    hops = Hops({"https://example.com/s.tar.gz": "http://example.com/s.tar.gz"})

    with pytest.raises(UnsafeAddressError, match="not https"):
        fetch("https://example.com/s.tar.gz", fetcher=hops, resolver=Resolver())


def test_a_short_redirect_chain_is_followed_to_the_bytes() -> None:
    """The other direction, so the redirect rules cannot be satisfied by refusing every
    redirect. A repository host that redirects once is ordinary."""
    hops = Hops(
        {
            "https://example.com/s.tar.gz": "https://cdn.example.com/s.tar.gz",
            "https://cdn.example.com/s.tar.gz": BODY,
        }
    )

    got = fetch("https://example.com/s.tar.gz", fetcher=hops, resolver=Resolver())

    assert got.body == BODY
    assert len(hops.connected) == 2


def test_an_endless_redirect_chain_is_abandoned() -> None:
    """A loop is a request that never returns, and a fetch that never returns holds a worker
    for as long as the other end cares to keep it."""
    hops = Hops({"https://example.com/a": "https://example.com/a"})

    with pytest.raises(SkillError, match="redirects"):
        fetch("https://example.com/a", fetcher=hops, resolver=Resolver())


def test_a_body_over_the_ceiling_is_refused_by_its_length_not_its_claim() -> None:
    """`Content-Length` is a claim made by the thing being fetched. A skill is a folder of
    markdown and small scripts, and anything at this size is not one."""
    hops = Hops({"https://example.com/s.tar.gz": b"x" * 200})

    with pytest.raises(SkillError, match="ceiling"):
        fetch("https://example.com/s.tar.gz", fetcher=hops, resolver=Resolver(), max_bytes=100)


# ------------------------------------------------------------------ pinning to bytes
def test_a_url_import_whose_bytes_changed_is_refused() -> None:
    """The reviewer approved bytes. An address with different bytes behind it today is a
    different skill wearing an approval it never had.

    Refused rather than recorded: recording the new digest makes the import succeed with
    content nobody read, which is the whole failure this module exists to prevent."""
    source = SkillSource(
        kind=SourceKind.URL, location="https://example.com/s.tar.gz", content_digest=DIGEST
    )
    hops = Hops({"https://example.com/s.tar.gz": b"different bytes entirely"})

    with pytest.raises(SkillError, match="pinned to"):
        fetch_skill_source(source, fetcher=hops, resolver=Resolver())


def test_a_url_import_whose_bytes_match_is_returned() -> None:
    """So the digest check cannot be satisfied by refusing everything."""
    source = SkillSource(
        kind=SourceKind.URL, location="https://example.com/s.tar.gz", content_digest=DIGEST
    )

    assert fetch_skill_source(source, fetcher=Hops({}), resolver=Resolver()) == BODY


def test_a_github_import_addresses_one_commit_at_a_host_this_module_chose() -> None:
    """The host is built here rather than accepted, so a GitHub import cannot be pointed at
    an arbitrary address by whoever asked for it. The commit is already refused by
    `SkillSource` unless it is a full sha, so the address names one immutable tree."""
    url = github_tarball_url("acme/skills", COMMIT)

    assert url.startswith(f"https://{GITHUB_TARBALL_HOST}/")
    assert COMMIT in url
    assert "acme/skills" in url


def test_a_github_import_is_not_also_digest_checked() -> None:
    """A commit sha is a hash over the tree, so it names the content exactly; the tarball
    GitHub builds from it is compressed on their side and its bytes are not promised to be
    stable, so a digest over the tarball would fail on a re-fetch of the same tree.

    Delete this and somebody adds a digest check to the GitHub path for symmetry, and every
    re-import starts failing for a reason that looks like tampering."""
    source = SkillSource(kind=SourceKind.GITHUB, location="acme/skills", commit=COMMIT)

    assert fetch_skill_source(source, fetcher=Hops({}), resolver=Resolver()) == BODY


def test_an_upload_has_nothing_to_fetch() -> None:
    """An upload arrives with its bytes. A fetch path that quietly accepted one would have to
    invent an address for it, and there is no address."""
    source = SkillSource(kind=SourceKind.UPLOAD, location="skill.zip", content_digest=DIGEST)

    with pytest.raises(SkillError, match="nothing to fetch"):
        fetch_skill_source(source, fetcher=Hops({}), resolver=Resolver())


def test_the_default_ceiling_is_small_enough_to_mean_something() -> None:
    """A ceiling set at a gigabyte is a ceiling that has never refused anything. Asserted as
    a bound rather than a value so lowering it stays legal."""
    assert MAX_FETCH_BYTES <= 50 * 1024 * 1024
