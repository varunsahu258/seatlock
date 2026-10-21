"""Add per-show flash-sale waiting-room settings."""

import sqlalchemy as sa

from alembic import op

revision = "0003_waiting_room"
down_revision = "0002_payment_flow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "shows",
        sa.Column("queue_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "shows",
        sa.Column("queue_admit_per_second", sa.Integer(), server_default="10", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("shows", "queue_admit_per_second")
    op.drop_column("shows", "queue_enabled")
