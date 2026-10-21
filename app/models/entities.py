from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    type_annotation_map: dict[type[Any], Any] = {datetime: DateTime(timezone=True)}


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    shows: Mapped[list["Show"]] = relationship(back_populates="event")


class Show(Base):
    __tablename__ = "shows"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    queue_enabled: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    queue_admit_per_second: Mapped[int] = mapped_column(default=10, server_default=text("10"))
    event: Mapped[Event] = relationship(back_populates="shows")
    seats: Mapped[list["Seat"]] = relationship(back_populates="show", cascade="all, delete-orphan")


class Seat(Base):
    __tablename__ = "seats"
    __table_args__ = (UniqueConstraint("show_id", "label", name="uq_seats_show_label"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(32))
    price_minor: Mapped[int] = mapped_column(Integer)
    show: Mapped[Show] = relationship(back_populates="seats")


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("status IN ('confirmed', 'paid', 'cancelled')", name="ck_bookings_status"),
        Index(
            "uq_bookings_confirmed_show_seat",
            "show_id",
            "seat_id",
            unique=True,
            postgresql_where=text("status IN ('confirmed', 'paid')"),
            sqlite_where=text("status IN ('confirmed', 'paid')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    show_id: Mapped[int] = mapped_column(ForeignKey("shows.id", ondelete="CASCADE"), index=True)
    seat_id: Mapped[int] = mapped_column(ForeignKey("seats.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16))
    payment_ref: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("provider_event_id", name="uq_payments_provider_event_id"),
        Index(
            "uq_payments_pending_intent_booking",
            "booking_ref",
            unique=True,
            postgresql_where=text("provider_event_id IS NULL"),
            sqlite_where=text("provider_event_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    booking_ref: Mapped[str] = mapped_column(String(255), index=True)
    provider_event_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32))
