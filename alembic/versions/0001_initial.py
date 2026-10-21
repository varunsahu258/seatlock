"""Create the seat booking schema."""

import sqlalchemy as sa

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "shows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "seats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "show_id", sa.Integer(), sa.ForeignKey("shows.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("label", sa.String(32), nullable=False),
        sa.Column("price_minor", sa.Integer(), nullable=False),
        sa.UniqueConstraint("show_id", "label", name="uq_seats_show_label"),
    )
    op.create_index("ix_seats_show_id", "seats", ["show_id"])
    op.create_table(
        "bookings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "show_id", sa.Integer(), sa.ForeignKey("shows.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "seat_id", sa.Integer(), sa.ForeignKey("seats.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payment_ref", sa.String(255)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_bookings_show_id", "bookings", ["show_id"])
    op.create_index("ix_bookings_seat_id", "bookings", ["seat_id"])
    op.create_index(
        "uq_bookings_confirmed_show_seat",
        "bookings",
        ["show_id", "seat_id"],
        unique=True,
        postgresql_where=sa.text("status = 'confirmed'"),
    )
    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("booking_ref", sa.String(255), nullable=False),
        sa.Column("provider_event_id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.UniqueConstraint("provider_event_id", name="uq_payments_provider_event_id"),
    )
    op.create_index("ix_payments_booking_ref", "payments", ["booking_ref"])


def downgrade() -> None:
    op.drop_index("ix_payments_booking_ref", table_name="payments")
    op.drop_table("payments")
    op.drop_index("uq_bookings_confirmed_show_seat", table_name="bookings")
    op.drop_index("ix_bookings_seat_id", table_name="bookings")
    op.drop_index("ix_bookings_show_id", table_name="bookings")
    op.drop_table("bookings")
    op.drop_index("ix_seats_show_id", table_name="seats")
    op.drop_table("seats")
    op.drop_table("shows")
    op.drop_table("events")
