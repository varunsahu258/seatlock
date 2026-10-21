"""Support payment intents and terminal booking states."""

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

revision = "0002_payment_flow"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("uq_bookings_confirmed_show_seat", table_name="bookings")
    check_names = {
        constraint.get("name")
        for constraint in inspect(op.get_bind()).get_check_constraints("bookings")
    }
    if "ck_bookings_status" in check_names:
        op.drop_constraint("ck_bookings_status", "bookings", type_="check")
    op.create_check_constraint(
        "ck_bookings_status",
        "bookings",
        "status IN ('confirmed', 'paid', 'cancelled')",
    )
    op.create_index(
        "uq_bookings_confirmed_show_seat",
        "bookings",
        ["show_id", "seat_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('confirmed', 'paid')"),
    )
    op.alter_column(
        "payments",
        "provider_event_id",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.create_index(
        "uq_payments_pending_intent_booking",
        "payments",
        ["booking_ref"],
        unique=True,
        postgresql_where=sa.text("provider_event_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_payments_pending_intent_booking", table_name="payments")
    op.alter_column(
        "payments",
        "provider_event_id",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.drop_index("uq_bookings_confirmed_show_seat", table_name="bookings")
    check_names = {
        constraint.get("name")
        for constraint in inspect(op.get_bind()).get_check_constraints("bookings")
    }
    if "ck_bookings_status" in check_names:
        op.drop_constraint("ck_bookings_status", "bookings", type_="check")
    op.create_check_constraint(
        "ck_bookings_status",
        "bookings",
        "status IN ('confirmed', 'cancelled')",
    )
    op.create_index(
        "uq_bookings_confirmed_show_seat",
        "bookings",
        ["show_id", "seat_id"],
        unique=True,
        postgresql_where=sa.text("status = 'confirmed'"),
    )
