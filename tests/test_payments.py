import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.main import app
from app.models import Booking, Payment
from tests.test_holds import create_show


async def create_confirmed_booking(client: AsyncClient, user_id: str = "payer") -> tuple[int, int]:
    show_id = await create_show(client)
    seats = (await client.get(f"/shows/{show_id}/seats")).json()
    seat_id = seats[0]["id"]
    hold = await client.post(
        f"/shows/{show_id}/seats/{seat_id}/hold", headers={"X-User-Id": user_id}
    )
    assert hold.status_code == 201
    confirmed = await client.post(
        "/bookings/confirm",
        headers={"X-User-Id": user_id},
        json={"show_id": show_id, "seat_ids": [seat_id]},
    )
    assert confirmed.status_code == 201
    return confirmed.json()[0]["id"], seat_id


def webhook_body(event_id: str, booking_id: int, status: str) -> bytes:
    return json.dumps(
        {"provider_event_id": event_id, "booking_id": booking_id, "status": status},
        separators=(",", ":"),
    ).encode()


def webhook_headers(body: bytes, *, valid: bool = True) -> dict[str, str]:
    signature = hmac.new(settings.payment_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    if not valid:
        signature = "0" * len(signature)
    return {"X-Payment-Signature": signature, "Content-Type": "application/json"}


async def create_intent(client: AsyncClient, booking_id: int, user_id: str = "payer") -> None:
    response = await client.post(f"/bookings/{booking_id}/pay", headers={"X-User-Id": user_id})
    assert response.status_code == 201
    assert response.json()["status"] == "pending"


async def payment_rows(factory: async_sessionmaker[AsyncSession], booking_id: int) -> list[Payment]:
    async with factory() as session:
        return list(
            (
                await session.scalars(select(Payment).where(Payment.booking_ref == str(booking_id)))
            ).all()
        )


@pytest.mark.asyncio
async def test_same_webhook_replayed_20_times_has_one_effect(client: AsyncClient) -> None:
    booking_id, _ = await create_confirmed_booking(client)
    await create_intent(client, booking_id)
    body = webhook_body("evt-replay-once", booking_id, "success")
    responses = await asyncio.gather(
        *(
            client.post("/webhooks/payment", content=body, headers=webhook_headers(body))
            for _ in range(20)
        )
    )
    assert all(response.status_code == 200 for response in responses)
    assert sum(response.json()["status"] == "processed" for response in responses) == 1
    assert sum(response.json()["status"] == "duplicate" for response in responses) == 19

    factory = app.state.test_session_factory
    rows = await payment_rows(factory, booking_id)
    async with factory() as session:
        booking = await session.get(Booking, booking_id)
    assert sum(row.provider_event_id == "evt-replay-once" for row in rows) == 1
    assert booking is not None and booking.status == "paid"
    assert sum(row.status == "paid" for row in rows) == 1


@pytest.mark.asyncio
async def test_bad_webhook_signature_is_401(client: AsyncClient) -> None:
    booking_id, _ = await create_confirmed_booking(client)
    body = webhook_body("evt-bad-signature", booking_id, "success")
    response = await client.post(
        "/webhooks/payment", content=body, headers=webhook_headers(body, valid=False)
    )
    assert response.status_code == 401
    rows = await payment_rows(app.state.test_session_factory, booking_id)
    assert len(rows) == 1 and rows[0].provider_event_id is None
    assert rows[0].status == "pending"


@pytest.mark.asyncio
async def test_success_after_cancel_does_not_revive_booking(client: AsyncClient) -> None:
    booking_id, _ = await create_confirmed_booking(client)
    await create_intent(client, booking_id)
    for event_id, result in (("evt-failure", "failure"), ("evt-late-success", "success")):
        body = webhook_body(event_id, booking_id, result)
        response = await client.post(
            "/webhooks/payment", content=body, headers=webhook_headers(body)
        )
        assert response.status_code == 200

    factory = app.state.test_session_factory
    async with factory() as session:
        booking = await session.get(Booking, booking_id)
        intent = await session.scalar(
            select(Payment).where(
                Payment.booking_ref == str(booking_id), Payment.provider_event_id.is_(None)
            )
        )
    assert booking is not None and booking.status == "cancelled"
    assert intent is not None and intent.status == "cancelled"
    seat_map = await client.get(f"/shows/{booking.show_id}/seats")
    seat = next(seat for seat in seat_map.json() if seat["id"] == booking.seat_id)
    assert seat["status"] == "available"


@pytest.mark.asyncio
async def test_late_failure_does_not_cancel_paid_booking(client: AsyncClient) -> None:
    booking_id, _ = await create_confirmed_booking(client)
    await create_intent(client, booking_id)
    for event_id, result in (("evt-success-first", "success"), ("evt-late-failure", "failure")):
        body = webhook_body(event_id, booking_id, result)
        response = await client.post(
            "/webhooks/payment", content=body, headers=webhook_headers(body)
        )
        assert response.status_code == 200
    async with app.state.test_session_factory() as session:
        booking = await session.get(Booking, booking_id)
    assert booking is not None and booking.status == "paid"


@pytest.mark.asyncio
async def test_two_cleanup_workers_cancel_expired_booking_once() -> None:
    import os

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base, Event, Seat, Show
    from app.workers.cleanup import cleanup_expired_bookings

    database_url = os.getenv("DATABASE_URL", "")
    if not database_url.startswith("postgresql"):
        pytest.skip("SKIP LOCKED concurrency test requires PostgreSQL")
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        event = Event(name="cleanup worker concurrency")
        session.add(event)
        await session.flush()
        show = Show(event_id=event.id, starts_at=datetime(2030, 1, 1, tzinfo=UTC))
        session.add(show)
        await session.flush()
        seat = Seat(show_id=show.id, label="cleanup", price_minor=1)
        session.add(seat)
        await session.flush()
        booking = Booking(
            show_id=show.id,
            seat_id=seat.id,
            user_id="cleanup-test",
            status="confirmed",
            created_at=datetime(2000, 1, 1, tzinfo=UTC),
        )
        session.add(booking)
        await session.commit()
        booking_id = booking.id
        event_id = event.id
        show_id = show.id

    try:
        async with factory() as session:
            eligible_before = await session.scalar(
                select(func.count(Booking.id)).where(
                    Booking.status == "confirmed",
                    Booking.created_at < datetime.now(UTC) - timedelta(minutes=10),
                )
            )
        results = await asyncio.gather(
            cleanup_expired_bookings(factory), cleanup_expired_bookings(factory)
        )
        assert sum(results) == eligible_before
        assert eligible_before is not None and eligible_before >= 1
        async with factory() as session:
            booking = await session.get(Booking, booking_id)
            assert booking is not None and booking.status == "cancelled"
    finally:
        async with factory() as session:
            await session.execute(
                Payment.__table__.delete().where(Payment.booking_ref == str(booking_id))
            )
            await session.execute(delete(Booking).where(Booking.id == booking_id))
            await session.execute(delete(Seat).where(Seat.show_id == show_id))
            await session.execute(delete(Show).where(Show.id == show_id))
            await session.execute(delete(Event).where(Event.id == event_id))
            await session.commit()
        await engine.dispose()
