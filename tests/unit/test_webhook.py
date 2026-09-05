"""Webhook verification. Every test is a way a forged request becomes a named person.

Task ids: M10.2.1
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from brain.channels.webhook import (
    DEFAULT_WINDOW,
    SeenNonces,
    WebhookRefusedError,
    sign,
    verified_handler,
    verify,
)

SECRET = "a-shared-secret"
NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
STAMP = str(int(NOW.timestamp()))
BODY = b'{"event":"message","from":"u_weiling"}'


def _ok(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "secret": SECRET,
        "signature": sign(SECRET, STAMP, BODY),
        "timestamp": STAMP,
        "body": BODY,
        "now": NOW,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------- the signature
def test_a_correctly_signed_request_is_accepted() -> None:
    """The happy path. Without it nothing else here is testing a verifier that can accept
    anything at all, and a verifier that refuses everything passes every other test."""
    verify(**_ok())  # type: ignore[arg-type]


def test_a_wrong_signature_is_refused() -> None:
    verify_kwargs = _ok(signature="0" * 64)
    with pytest.raises(WebhookRefusedError):
        verify(**verify_kwargs)  # type: ignore[arg-type]


def test_a_body_changed_after_signing_is_refused() -> None:
    """The attack the signature exists for: a real signature over a body somebody edited in
    flight. One byte is enough."""
    with pytest.raises(WebhookRefusedError):
        verify(**_ok(body=BODY.replace(b"u_weiling", b"u_rupash")))  # type: ignore[arg-type]


def test_the_timestamp_is_inside_the_signed_material() -> None:
    """Signing only the body would let a captured signature be replayed with a fresh
    timestamp, which is exactly the check the timestamp is there to make. Asserted by
    signing the same body at two times and requiring different signatures."""
    other = str(int(NOW.timestamp()) + 1)
    assert sign(SECRET, STAMP, BODY) != sign(SECRET, other, BODY)


def test_a_parsed_object_cannot_be_verified() -> None:
    """The signature covers the raw bytes. Verifying a re-serialisation verifies something
    the sender never signed: JSON has no canonical form, so `{"a":1,"b":2}` and
    `{"b":2,"a":1}` are the same object and different bytes, and an attacker who can make
    the parse and the re-serialisation differ has a body that verifies as one thing and is
    read as another.

    Refused at the type, so there is no way to pass a dict by accident."""
    with pytest.raises(TypeError, match="raw bytes"):
        verify(**_ok(body=json.loads(BODY)))  # type: ignore[arg-type]


def test_the_comparison_is_constant_time() -> None:
    """`==` on a digest returns as soon as two bytes differ, so the time it takes says how
    much of a guess was right, and a few thousand requests turn that into the signature.

    Asserted by reading the source rather than by timing, because a timing test on a laptop
    measures the scheduler. What matters is that `compare_digest` is what is called."""
    import inspect

    from brain.channels import webhook

    source = inspect.getsource(webhook.verify)
    assert "hmac.compare_digest" in source
    assert "expected == signature" not in source


# --------------------------------------------------------------- the timestamp
def test_a_stale_request_is_refused() -> None:
    """Without this a signature captured once is valid for ever, and a request replayed
    from a proxy log a month later arrives correctly signed."""
    old = str(int((NOW - DEFAULT_WINDOW - timedelta(seconds=1)).timestamp()))
    with pytest.raises(WebhookRefusedError):
        verify(**_ok(timestamp=old, signature=sign(SECRET, old, BODY)))  # type: ignore[arg-type]


def test_a_request_from_the_future_is_refused_too() -> None:
    """Checking only the past is the mistake that reads as thorough. A sender with a fast
    clock, or an attacker choosing a timestamp, would be accepted for as long as they
    liked."""
    ahead = str(int((NOW + DEFAULT_WINDOW + timedelta(seconds=1)).timestamp()))
    with pytest.raises(WebhookRefusedError):
        verify(**_ok(timestamp=ahead, signature=sign(SECRET, ahead, BODY)))  # type: ignore[arg-type]


def test_a_small_clock_difference_is_tolerated() -> None:
    """Not zero tolerance: clocks differ, and refusing every request from a machine a second
    ahead would make this look like an outage."""
    near = str(int((NOW + timedelta(seconds=30)).timestamp()))
    verify(**_ok(timestamp=near, signature=sign(SECRET, near, BODY)))  # type: ignore[arg-type]


def test_a_timestamp_that_is_not_a_number_is_refused_and_not_a_crash() -> None:
    """A malformed header is an attacker probing, not an internal error. A traceback would
    make it a 500, and a 500 is a different, louder thing than a refusal."""
    with pytest.raises(WebhookRefusedError):
        verify(**_ok(timestamp="not-a-timestamp"))  # type: ignore[arg-type]


# ------------------------------------------------------------------- the nonce
def test_a_repeated_nonce_is_refused_inside_the_window() -> None:
    """The timestamp bounds a replay to five minutes rather than preventing one, and five
    minutes is long enough to resend an approval."""
    seen = SeenNonces()
    verify(**_ok(nonce="n1", seen=seen))  # type: ignore[arg-type]
    with pytest.raises(WebhookRefusedError):
        verify(**_ok(nonce="n1", seen=seen))  # type: ignore[arg-type]


def test_a_nonce_that_has_aged_out_is_forgotten() -> None:
    """Otherwise this is a permanently growing list of refusals, and a legitimate sender
    that reuses an identifier after an hour is refused for ever."""
    seen = SeenNonces()
    assert seen.remember("n1", NOW)
    assert seen.remember("n1", NOW + DEFAULT_WINDOW + timedelta(seconds=1))


def test_remembering_prunes_rather_than_growing_without_bound() -> None:
    """It is written on the write path rather than on a timer, because a timer is a second
    thing to run and get wrong."""
    seen = SeenNonces()
    for i in range(50):
        seen.remember(f"n{i}", NOW)
    seen.remember("later", NOW + DEFAULT_WINDOW + timedelta(seconds=1))
    assert len(seen) == 1


# ------------------------------------------------------- what a refusal says
def test_every_refusal_says_the_same_thing() -> None:
    """ "bad signature" and "stale timestamp" and "seen before" tell somebody probing which
    part to fix next, and the difference is invisible in a screenshot. One sentence for all
    of them, which is the rule `brain.gate.ingress` applies to unrecognised senders.

    Deleting this turns the verifier into an oracle that explains how to get past it."""
    messages: set[str] = set()
    seen = SeenNonces()
    verify(**_ok(nonce="n1", seen=seen))  # type: ignore[arg-type]
    old = str(int((NOW - timedelta(hours=1)).timestamp()))
    for kwargs in (
        _ok(signature="0" * 64),
        _ok(timestamp=old, signature=sign(SECRET, old, BODY)),
        _ok(nonce="n1", seen=seen),
        _ok(timestamp="nonsense"),
    ):
        with pytest.raises(WebhookRefusedError) as caught:
            verify(**kwargs)  # type: ignore[arg-type]
        messages.add(str(caught.value))
    assert len(messages) == 1, f"the refusal distinguishes its reasons: {messages}"


# ------------------------------------------------------------ the shape itself
def test_a_parser_cannot_be_reached_without_verification() -> None:
    """A `verify` the caller must remember to call before `parse` is a check that goes
    missing from the one call site somebody adds later. A parser reachable only through
    verification cannot be called first.

    The same argument `ProjectedCatalogue` makes with its constructor token."""
    parsed: list[bytes] = []
    handle = verified_handler(SECRET, lambda b: parsed.append(b) or b, seen=SeenNonces())

    with pytest.raises(WebhookRefusedError):
        handle(BODY, "0" * 64, STAMP, "n1", NOW)
    assert parsed == [], "the body was parsed despite a bad signature"

    handle(BODY, sign(SECRET, STAMP, BODY), STAMP, "n2", NOW)
    assert parsed == [BODY]


def test_verify_raises_rather_than_returning_a_boolean() -> None:
    """A function returning False is one whose result can be ignored by writing
    `verify(...)` on a line by itself, and that line reads as a check."""
    assert verify(**_ok()) is None  # type: ignore[arg-type]


def test_a_real_platform_timestamp_shape_is_accepted() -> None:
    """Lark and Slack both send whole seconds since the epoch as a string. Parsing anything
    else would be this system inventing a format the platform does not use."""
    stamp = str(int(time.time()))
    now = datetime.fromtimestamp(int(stamp), tz=UTC)
    verify(**_ok(timestamp=stamp, signature=sign(SECRET, stamp, BODY), now=now))  # type: ignore[arg-type]
