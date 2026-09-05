"""What every channel adapter must do, and the two things it may not.

An adapter is a translator: what arrived becomes a `ChannelEvent`, and a `ChannelPayload`
becomes whatever the surface renders. It decides nothing about who may see what, because
the gate already did - and a channel that could decide again would be a second opinion,
with the permissive one winning the day the two disagree.

Two refusals are built into the shape rather than left to each adapter to remember.

**An adapter that cannot render the payload's label must refuse to send (M10.1.5).** The
opaque escape hatch exists so a tool that returns something the redactor cannot walk is not
simply unusable; the price is a label saying so, carried all the way to the person. An
adapter that dropped the label - because SMS has no formatting, because a card template had
nowhere to put it - would turn "here is something nobody checked" into "here is an answer",
which is the one transformation the escape hatch must never undergo. So `send` is written to
refuse rather than to degrade, and `can_carry_label` is what an adapter answers with.

**An adapter may not carry a classification above its ceiling (M10.1.3).** WhatsApp is a
consumer messaging app on somebody's personal phone; the console is behind the identity
provider. Those are not the same surface and a field classified `restricted` should not be
in the first one because the answer happened to be asked for there. The ceiling is per
channel and it is checked here rather than trusted to whoever writes the next adapter.

Task ids: M10.1.1, M10.1.2, M10.1.3, M10.1.4, M10.1.5
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from brain.core.field_policy import Classification
from brain.core.redaction import OPAQUE_LABEL, ChannelPayload
from brain.gate.context import Channel
from brain.gate.ingress import ChannelEvent


class DeliveryRefusedError(Exception):
    """Raised when a payload must not go out over this channel.

    Not a `BrainError`, because it is not an outcome of a request: it is a wiring fault or a
    policy boundary, and reporting it as `Degraded` would put "this channel cannot carry
    this" in the same bucket as "the provider is down".
    """


class Feature(enum.StrEnum):
    """What a surface can actually do. Closed, because each member gates a code path.

    Declared per adapter rather than inferred. Inferring means guessing, and a wrong guess
    about `EPHEMERAL` is the one that matters: a per-viewer body sent to a channel that
    cannot do ephemeral messages is a private answer posted into a room.
    """

    #: Messages only that person sees, in a shared room. The mechanism M10.4.2 needs.
    EPHEMERAL = "ephemeral"
    #: Structured, actionable messages. Approvals need these or they degrade to a link.
    CARDS = "cards"
    #: Tokens as they arrive rather than one finished message.
    STREAMING = "streaming"
    #: Files out, and files in.
    ATTACHMENTS = "attachments"
    #: Editing a message already sent, which is how a card stops being actionable once the
    #: approval it offers has been taken by somebody else.
    EDIT_IN_PLACE = "edit_in_place"


@dataclass(frozen=True)
class ChannelCapabilities:
    """What one adapter can do and how sensitive a thing it may carry (M10.1.2, M10.1.3).

    Frozen, and read at send time rather than at registration, so an adapter cannot widen
    itself between the check and the send.
    """

    channel: Channel
    features: frozenset[Feature] = frozenset()
    #: The most sensitive classification this surface may carry. Not a list of allowed
    #: classes: sensitivity is ordered, and a list would let somebody permit `restricted`
    #: while forbidding `confidential`, which is a configuration nobody means and which
    #: reads as deliberate.
    max_classification: Classification = Classification.INTERNAL
    #: Whether a label can be rendered where a person will see it. An adapter answering
    #: false here can still be used for anything unlabelled; it simply cannot carry the
    #: opaque escape hatch. See `assert_can_send`.
    can_carry_label: bool = True

    def supports(self, feature: Feature) -> bool:
        return feature in self.features

    def may_carry(self, classification: Classification) -> bool:
        return classification.rank <= self.max_classification.rank


@runtime_checkable
class ChannelAdapter(Protocol):
    """One surface. Three methods and no fourth.

    There is deliberately no `query`, no `fetch` and no `check`. An adapter that could ask
    the database a question would be a second path to data with its own idea of what may be
    seen; everything it sends comes from a `ChannelPayload` the gate produced.
    """

    def capabilities(self) -> ChannelCapabilities: ...

    def normalise(self, raw: object) -> ChannelEvent:
        """Whatever arrived, as the one shape the gate reads."""
        ...

    def send(self, payload: ChannelPayload, *, to: str) -> None:
        """Deliver. Must call `assert_can_send` first, or inherit a base that does."""
        ...

    def healthy(self, now: datetime) -> bool:
        """Whether this adapter can currently deliver (M10.1.4).

        Separate from whether it is *registered*. An adapter that is configured and
        unreachable must read as unhealthy rather than as absent: absent means nobody set it
        up, and the two send a person to different places.
        """
        ...


def assert_can_send(
    capabilities: ChannelCapabilities,
    payload: ChannelPayload,
    *,
    highest: Classification = Classification.INTERNAL,
) -> None:
    """The two refusals every adapter shares, in one place so no adapter can forget one.

    Called by `send`, not by the caller of `send`. A check the caller performs is a check
    that is missing from the one call site somebody adds later, and the point of putting it
    here is that an adapter has to go out of its way to skip it.

    `highest` is the most sensitive classification in what is being sent. It is passed in
    rather than computed from the payload, because a `ChannelPayload` carries values and not
    the policy that classified them - by design, since a payload that knew its own
    classification would be carrying the policy to the channel.
    """
    if payload.label == OPAQUE_LABEL and not capabilities.can_carry_label:
        raise DeliveryRefusedError(
            f"{capabilities.channel} cannot render a payload label, and this payload carries "
            f"{OPAQUE_LABEL!r}. Sending it without the label turns 'here is something nobody "
            "checked' into 'here is an answer', which is the one thing the opaque escape "
            "hatch must never become."
        )

    if not capabilities.may_carry(highest):
        # The message names the channel and the classification and not the field or the
        # value: a refusal that quoted either would put the sensitive thing into whatever
        # log records the refusal.
        raise DeliveryRefusedError(
            f"{capabilities.channel} may carry at most {capabilities.max_classification} "
            f"and this answer contains {highest}"
        )


def registered(adapters: dict[Channel, ChannelAdapter], now: datetime) -> dict[Channel, bool]:
    """Every registered adapter and whether it can currently deliver (M10.1.4).

    Returns a mapping rather than a list of the healthy ones, because "not registered" and
    "registered and unhealthy" are different problems that send a person to different
    places, and a filtered list makes them look identical.

    An adapter whose health check raises is unhealthy rather than an error out of this
    function. A single broken adapter must not make the health of every other one
    unanswerable, which is what an exception escaping here would do.
    """
    out: dict[Channel, bool] = {}
    for channel, adapter in adapters.items():
        try:
            out[channel] = adapter.healthy(now)
        except Exception:
            out[channel] = False
    return out
