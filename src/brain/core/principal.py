"""Who is asking.

A Principal is the only thing that carries authority in this system. Agents do not;
they are lenses that narrow a caller's reach and can never widen it. Every entitlement
computation in the gate starts from a Principal and nothing else.

Task ids: M0.2.1
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PrincipalKind(enum.StrEnum):
    """What sort of thing is asking.

    SERVICE exists for scheduled work. It is deliberately not called "system": there is
    no principal that bypasses the gate, and naming one "system" invites someone to build
    that bypass later.
    """

    HUMAN = "human"
    SERVICE = "service"


class Employment(enum.StrEnum):
    """Employment shapes the default ceiling and the offboarding path.

    CONTRACTOR and PARTNER must carry a `not_after`; the validator below enforces it
    rather than leaving it to policy, because an unbounded contractor is the single most
    common way a permission model rots in practice.
    """

    STAFF = "staff"
    CONTRACTOR = "contractor"
    PARTNER = "partner"
    SERVICE = "service"


class Principal(BaseModel):
    """An identity the gate can compute an entitlement for.

    `not_after` is enforced at entitlement time, not at login time. A session opened
    before expiry must not survive it, so the check belongs where the entitlement is
    built and nowhere else.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    kind: PrincipalKind
    employment: Employment
    display_name: str = Field(min_length=1, max_length=200)
    primary_department: str | None = None
    not_after: datetime | None = None

    @field_validator("not_after")
    @classmethod
    def _tz_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            msg = "not_after must be timezone-aware; a naive expiry is a silent bug"
            raise ValueError(msg)
        return v

    @field_validator("employment")
    @classmethod
    def _bounded_engagements(cls, v: Employment) -> Employment:
        return v

    def model_post_init(self, _context: object, /) -> None:
        bounded = (Employment.CONTRACTOR, Employment.PARTNER)
        if self.employment in bounded and self.not_after is None:
            msg = f"{self.employment} principals must carry not_after"
            raise ValueError(msg)

    def is_active(self, now: datetime | None = None) -> bool:
        """True when this principal may still be entitled to anything at all."""
        if self.not_after is None:
            return True
        return (now or datetime.now(UTC)) < self.not_after
