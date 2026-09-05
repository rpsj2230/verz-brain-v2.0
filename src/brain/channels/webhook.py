"""Verifying that a webhook really came from the platform it claims to.

This is the boundary where an anonymous HTTP request becomes a message from a named person.
Everything downstream - binding, entitlement resolution, the whole gate - starts from the
identity in the body, so a forged body is a forged identity, and no amount of care further
in can recover from accepting one.

Four things are checked and each closes a different attack. Getting any of them wrong leaves
the other three looking like they work.

**The signature covers the raw bytes, never the parsed object.** Parse first and you verify
a re-serialisation: JSON has no canonical form, so `{"a":1,"b":2}` and `{"b":2,"a":1}` are
the same object and different bytes, and an attacker who can make the two differ has a body
that verifies as one thing and is read as another. `verify` therefore takes `bytes` and
there is no overload taking a dict.

**The comparison is constant time.** `==` on a digest returns as soon as two bytes differ,
so the time it takes says how much of a guess was right, and a few thousand requests turns
that into the signature. `hmac.compare_digest` exists for this and is not optional.

**A timestamp outside the window is refused.** Without it a signature captured once is valid
for ever, and a request replayed from a proxy log a month later arrives correctly signed.
The window is small and two-sided: a request from the future is as wrong as one from the
past, and only checking one side is the mistake that reads as thorough.

**A nonce is remembered for at least the window.** The timestamp alone bounds a replay to
five minutes rather than preventing it, and five minutes is long enough to resend an
approval. Anything that has already been seen inside the window is refused.

Task ids: M10.2.1
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

#: How far out of step a request may be. Small, because it bounds how long a captured
#: signature stays useful; not zero, because clocks differ and a strict equality would refuse
#: every request from a machine a second ahead.
DEFAULT_WINDOW = timedelta(minutes=5)


class WebhookRefusedError(Exception):
    """Raised when a request must not be treated as coming from the platform.

    One exception for every reason, and the message never says which check failed in terms
    an attacker could use. "bad signature" and "stale timestamp" and "seen before" tell
    somebody probing which part to fix next, which is the same argument
    `brain.gate.ingress` makes for having one prompt for every unrecognised sender.
    """


@dataclass
class SeenNonces:
    """Nonces seen inside the window, and nothing older.

    In memory, and that is a real limitation stated rather than hidden: two replicas do not
    share this, so a replayed request can be accepted once per replica. Closing that needs
    the shared cache, and this type exists so the seam is visible rather than so the problem
    is solved. The timestamp check still bounds the damage to the window.

    Pruned on write rather than on a timer. A timer is a second thing to run and get wrong,
    and the write path is the only place that can grow this.
    """

    window: timedelta = DEFAULT_WINDOW
    _seen: dict[str, datetime] = field(default_factory=dict)

    def remember(self, nonce: str, now: datetime) -> bool:
        """True if this nonce is new. False means it has been seen inside the window."""
        cutoff = now - self.window
        # Pruned first, so a nonce that has aged out is genuinely forgotten rather than
        # refused for ever - which would make this a growing list of permanent refusals.
        self._seen = {n: at for n, at in self._seen.items() if at > cutoff}
        if nonce in self._seen:
            return False
        self._seen[nonce] = now
        return True

    def __len__(self) -> int:
        return len(self._seen)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """The signature the platform would produce for this request.

    Exported so a test can produce a real one rather than asserting against a literal, and
    so the one definition of what is signed lives in one place. The timestamp is inside the
    signed material: signing only the body would let a captured signature be replayed with a
    fresh timestamp, which is the check the timestamp is there to make.
    """
    material = timestamp.encode("utf-8") + b"." + body
    return hmac.new(secret.encode("utf-8"), material, hashlib.sha256).hexdigest()


def verify(
    *,
    secret: str,
    signature: str,
    timestamp: str,
    body: bytes,
    now: datetime,
    nonce: str = "",
    seen: SeenNonces | None = None,
    window: timedelta = DEFAULT_WINDOW,
) -> None:
    """Refuse anything that is not a live, unrepeated request from the platform.

    `body` is bytes and there is deliberately no overload taking a parsed object. Verifying
    a re-serialisation verifies something the sender never signed: JSON has no canonical
    form, so an attacker who can make the parse and the re-serialisation differ has a body
    that verifies as one thing and is read as another.

    Raises rather than returning a bool. A function returning False is one whose result can
    be ignored by writing `verify(...)` on a line by itself, and that line reads as a check.
    """
    if not isinstance(body, bytes | bytearray):
        msg = (
            "the signature covers the raw bytes, not a parsed object; verifying a "
            "re-serialisation verifies something the sender never signed"
        )
        raise TypeError(msg)

    try:
        sent_at = datetime.fromtimestamp(int(timestamp), tz=now.tzinfo)
    except (ValueError, OverflowError, OSError) as exc:
        raise WebhookRefusedError("this request was not accepted") from exc

    # Two-sided. A request from the future is as wrong as one from the past, and checking
    # only the past is the mistake that reads as thorough: a sender with a fast clock, or an
    # attacker choosing a timestamp, would be accepted for as long as they liked.
    if abs(sent_at - now) > window:
        raise WebhookRefusedError("this request was not accepted")

    expected = sign(secret, timestamp, bytes(body))
    # Constant time. `==` returns as soon as two bytes differ, so the time it takes says how
    # much of a guess was right, and a few thousand requests turn that into the signature.
    if not hmac.compare_digest(expected, signature):
        raise WebhookRefusedError("this request was not accepted")

    if nonce and seen is not None and not seen.remember(nonce, now):
        # The timestamp bounds a replay to the window; this prevents one inside it. Five
        # minutes is long enough to resend an approval.
        raise WebhookRefusedError("this request was not accepted")


def verified_handler[T](
    secret: str,
    parse: Callable[[bytes], T],
    *,
    seen: SeenNonces | None = None,
) -> Callable[[bytes, str, str, str, datetime], T]:
    """Wrap a parser so it cannot be called on an unverified body.

    The shape is the point. A `verify` a caller must remember to call before `parse` is a
    check that goes missing from the one call site somebody adds later; a parser that can
    only be reached through verification cannot be called first. This is the same argument
    `brain.gate.catalogue.ProjectedCatalogue` makes with its constructor token.
    """

    def handle(body: bytes, signature: str, timestamp: str, nonce: str, now: datetime) -> T:
        verify(
            secret=secret,
            signature=signature,
            timestamp=timestamp,
            body=body,
            now=now,
            nonce=nonce,
            seen=seen,
        )
        return parse(body)

    return handle
