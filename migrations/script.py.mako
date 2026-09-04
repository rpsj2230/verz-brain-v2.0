"""${message}

Every migration must be reversible. If a downgrade is genuinely impossible — a destructive
data change, say — raise NotImplementedError with the reason rather than leaving `pass`,
so a failed deploy discovers it here and not at three in the morning.

Task ids:

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
