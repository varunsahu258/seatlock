import hashlib
import hmac
import math
import secrets
from datetime import UTC, datetime, timedelta
from typing import cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_admin_token
from app.core.config import settings
from app.core.database import get_session
from app.core.redis import (
    ACQUIRE_HOLD_SCRIPT,
    ADMISSION_CHECK_SCRIPT,
    JOIN_QUEUE_SCRIPT,
    MAX_HELD_SEATS_PER_SHOW,
    QUEUE_STATUS_SCRIPT,
    QUEUE_TTL_SECONDS,
    RELEASE_CONFIRMED_HOLDS_SCRIPT,
    RELEASE_HOLD_SCRIPT,
    VERIFY_HOLDS_SCRIPT,
    eval_raw,
    eval_script,
    hold_key,
    hold_rate_key,
    join_rate_key,
    queue_keys,
    queue_user_token_key,
    redis_client,
    user_holds_key,
)
from app.models import Booking, Event, Payment, Seat, Show
from app.schemas.api import (
    BookingConfirm,
    BookingRead,
    EventCreate,
    EventRead,
    HealthRead,
    HoldRead,
    HoldRelease,
    PaymentIntentRead,
    PaymentWebhook,
    PaymentWebhookRead,
    QueueJoinRead,
    QueueStatusRead,
    SeatRead,
    ShowCreate,
    ShowRead,
)

router = APIRouter()
admin_router = APIRouter(
    prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin_token)]
)


@admin_router.post(
    "/events",
    response_model=EventRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an event",
    description=(
        "Creates a parent event record. Configure `ADMIN_TOKEN` to require bearer-token "
        "authorization; with no token configured this endpoint is open for local development."
    ),
)
async def create_event(payload: EventCreate, session: AsyncSession = Depends(get_session)) -> Event:
    event = Event(name=payload.name)
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return event


@admin_router.post(
    "/shows",
    response_model=ShowRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a show and its seats",
    description=(
        "Creates a show plus `seat_count` numbered seats in one database transaction. Set "
        "`queue_enabled` to require waiting-room admission."
    ),
)
async def create_show(
    payload: ShowCreate, session: AsyncSession = Depends(get_session)
) -> ShowRead:
    if await session.get(Event, payload.event_id) is None:
        raise HTTPException(status_code=404, detail="Event not found")
    show = Show(
        event_id=payload.event_id,
        starts_at=payload.starts_at,
        queue_enabled=payload.queue_enabled,
        queue_admit_per_second=payload.queue_admit_per_second,
    )
    session.add(show)
    await session.flush()
    session.add_all(
        [
            Seat(
                show_id=show.id,
                label=str(seat_number),
                price_minor=payload.seat_price_minor,
            )
            for seat_number in range(1, payload.seat_count + 1)
        ]
    )
    await session.commit()
    await session.refresh(show)
    return ShowRead(
        id=show.id,
        event_id=show.event_id,
        starts_at=show.starts_at,
        seat_count=payload.seat_count,
        queue_enabled=show.queue_enabled,
        queue_admit_per_second=show.queue_admit_per_second,
    )


async def require_admission(
    session: AsyncSession,
    show_id: int,
    user_id: str,
    admission_token: str | None,
) -> None:
    show = await session.get(Show, show_id)
    if show is None:
        raise HTTPException(status_code=404, detail="Show not found")
    if not show.queue_enabled:
        return
    if not admission_token:
        raise HTTPException(status_code=403, detail="admission_required")
    keys = queue_keys(show_id)
    admitted = await eval_script(
        ADMISSION_CHECK_SCRIPT,
        2,
        keys[3],
        keys[4],
        admission_token,
        user_id,
    )
    if admitted != 1:
        raise HTTPException(status_code=403, detail="admission_required")


@router.post(
    "/shows/{show_id}/queue/join",
    response_model=QueueJoinRead,
    status_code=status.HTTP_201_CREATED,
    tags=["queue"],
    summary="Join the waiting room",
    description=(
        "Returns the caller's stable queue token, current position, and estimated wait. "
        "Repeated joins for a user reuse their token; the endpoint is token-bucket rate limited."
    ),
)
async def join_queue(
    show_id: int,
    session: AsyncSession = Depends(get_session),
    user_id: str = Header(..., alias="X-User-Id"),
) -> QueueJoinRead:
    show = await session.get(Show, show_id)
    if show is None:
        raise HTTPException(status_code=404, detail="Show not found")
    if not show.queue_enabled:
        raise HTTPException(status_code=409, detail="queue_not_enabled")
    keys = queue_keys(show_id)
    result = cast(
        list[str | int],
        await eval_raw(
            JOIN_QUEUE_SCRIPT,
            7,
            keys[0],
            keys[1],
            queue_user_token_key(show_id, user_id),
            keys[2],
            join_rate_key(show_id, user_id),
            keys[3],
            keys[4],
            user_id,
            secrets.token_urlsafe(24),
            str(QUEUE_TTL_SECONDS),
            str(settings.queue_join_rate_capacity),
            str(settings.queue_join_rate_per_second),
        ),
    )
    state = str(result[0])
    if state == "rate_limited":
        raise HTTPException(
            status_code=429, detail="rate_limit_exceeded", headers={"Retry-After": "1"}
        )
    token = str(result[1])
    position = int(result[2])
    admitted = state == "admitted"
    wait = 0 if admitted else math.ceil(position / show.queue_admit_per_second)
    return QueueJoinRead(
        token=token,
        position=position,
        admitted=admitted,
        estimated_wait_seconds=wait,
    )


@router.get(
    "/shows/{show_id}/queue/status",
    response_model=QueueStatusRead,
    tags=["queue"],
    summary="Read queue position or admission",
    description=(
        "Checks a queue token for the caller identified by `X-User-Id`. Once admitted, send "
        "this token as `X-Admission-Token` to hold, confirm, and pay."
    ),
)
async def queue_status(
    show_id: int,
    token: str,
    session: AsyncSession = Depends(get_session),
    user_id: str = Header(..., alias="X-User-Id"),
) -> QueueStatusRead:
    show = await session.get(Show, show_id)
    if show is None:
        raise HTTPException(status_code=404, detail="Show not found")
    if not show.queue_enabled:
        raise HTTPException(status_code=409, detail="queue_not_enabled")
    keys = queue_keys(show_id)
    result = cast(
        list[str | int],
        await eval_raw(
            QUEUE_STATUS_SCRIPT,
            4,
            keys[0],
            keys[3],
            keys[1],
            keys[4],
            token,
            user_id,
        ),
    )
    state = str(result[0])
    if state == "not_found":
        raise HTTPException(status_code=404, detail="queue_token_not_found")
    position = int(result[1])
    admitted = state == "admitted"
    return QueueStatusRead(
        token=token,
        position=position,
        admitted=admitted,
        estimated_wait_seconds=(
            0 if admitted else math.ceil(position / show.queue_admit_per_second)
        ),
    )


@router.get(
    "/shows/{show_id}/seats",
    response_model=list[SeatRead],
    tags=["shows"],
    summary="Get the live seat map",
    description=(
        "Returns every seat and merges PostgreSQL active bookings with live Redis holds. "
        "`booked` takes precedence over `held`."
    ),
)
async def get_seats(show_id: int, session: AsyncSession = Depends(get_session)) -> list[SeatRead]:
    seats = list(
        (await session.scalars(select(Seat).where(Seat.show_id == show_id).order_by(Seat.id))).all()
    )
    if not seats:
        if await session.get(Show, show_id) is None:
            raise HTTPException(status_code=404, detail="Show not found")
    bookings = (
        (
            await session.execute(
                select(Booking.seat_id).where(
                    Booking.show_id == show_id,
                    Booking.status.in_(["confirmed", "paid"]),
                )
            )
        )
        .scalars()
        .all()
    )
    booked_ids = set(bookings)
    hold_keys = [hold_key(show_id, seat.id) for seat in seats]
    hold_users = await redis_client.mget(hold_keys) if hold_keys else []
    return [
        SeatRead(
            id=seat.id,
            label=seat.label,
            price_minor=seat.price_minor,
            status=(
                "booked" if seat.id in booked_ids else "held" if hold_users[i] else "available"
            ),
        )
        for i, seat in enumerate(seats)
    ]


@router.post(
    "/shows/{show_id}/seats/{seat_id}/hold",
    response_model=HoldRead,
    status_code=status.HTTP_201_CREATED,
    tags=["holds"],
    summary="Hold a seat for five minutes",
    description=(
        "Acquires an unheld seat with one atomic Redis script. Caller may hold at most six "
        "seats per show. Queue-enabled shows require the owner's admission token."
    ),
)
async def hold_seat(
    show_id: int,
    seat_id: int,
    session: AsyncSession = Depends(get_session),
    user_id: str = Header(..., alias="X-User-Id"),
    admission_token: str | None = Header(None, alias="X-Admission-Token"),
) -> HoldRead:
    show = await session.get(Show, show_id)
    if show is None:
        raise HTTPException(status_code=404, detail="Show not found")
    seat = await session.scalar(select(Seat).where(Seat.id == seat_id, Seat.show_id == show_id))
    if seat is None:
        raise HTTPException(status_code=404, detail="Seat not found")
    confirmed_booking = await session.scalar(
        select(Booking.id).where(
            Booking.show_id == show_id,
            Booking.seat_id == seat_id,
            Booking.status.in_(["confirmed", "paid"]),
        )
    )
    if confirmed_booking is not None:
        raise HTTPException(status_code=409, detail="seat_unavailable")
    key = hold_key(show_id, seat_id)
    redis_queue_keys = queue_keys(show_id)
    result = await eval_script(
        ACQUIRE_HOLD_SCRIPT,
        5,
        key,
        user_holds_key(show_id, user_id),
        hold_rate_key(show_id, user_id),
        redis_queue_keys[3],
        redis_queue_keys[4],
        user_id,
        str(settings.hold_ttl_seconds),
        str(MAX_HELD_SEATS_PER_SHOW),
        f"hold:{show_id}:",
        str(seat_id),
        str(settings.hold_rate_capacity),
        str(settings.hold_rate_per_second),
        "1" if show.queue_enabled else "0",
        admission_token or "",
    )
    if result != 1:
        if result == -1:
            raise HTTPException(status_code=409, detail="hold_limit_reached")
        if result == -2:
            raise HTTPException(
                status_code=429, detail="rate_limit_exceeded", headers={"Retry-After": "1"}
            )
        if result == -3:
            raise HTTPException(status_code=403, detail="admission_required")
        raise HTTPException(status_code=409, detail="seat_unavailable")
    return HoldRead(
        show_id=show_id,
        seat_id=seat_id,
        expires_at=datetime.now(UTC) + timedelta(seconds=settings.hold_ttl_seconds),
    )


@router.post(
    "/bookings/confirm",
    response_model=list[BookingRead],
    status_code=201,
    tags=["bookings"],
    summary="Confirm held seats",
    description=(
        "Requires caller-owned, unexpired holds for every requested seat. Creates bookings "
        "and pending payment intents in one PostgreSQL transaction, relies on the active-seat "
        "unique index, then removes the holds."
    ),
)
async def confirm_booking(
    payload: BookingConfirm,
    session: AsyncSession = Depends(get_session),
    user_id: str = Header(..., alias="X-User-Id"),
    admission_token: str | None = Header(None, alias="X-Admission-Token"),
) -> list[Booking]:
    show = await session.get(Show, payload.show_id)
    if show is None:
        raise HTTPException(status_code=404, detail="Show not found")
    queue_enabled = show.queue_enabled
    valid_seat_ids = set(
        (
            await session.scalars(
                select(Seat.id).where(
                    Seat.show_id == payload.show_id,
                    Seat.id.in_(payload.seat_ids),
                )
            )
        ).all()
    )
    if valid_seat_ids != set(payload.seat_ids):
        await session.rollback()
        raise HTTPException(status_code=404, detail="Seat not found for show")
    await session.rollback()

    keys = [hold_key(payload.show_id, seat_id) for seat_id in payload.seat_ids]
    redis_queue_keys = queue_keys(payload.show_id)
    owns_holds = await eval_script(
        VERIFY_HOLDS_SCRIPT,
        len(keys) + 2,
        *keys,
        redis_queue_keys[3],
        redis_queue_keys[4],
        user_id,
        "1" if queue_enabled else "0",
        admission_token or "",
    )
    if owns_holds == -1:
        raise HTTPException(status_code=403, detail="admission_required")
    if owns_holds != 1:
        raise HTTPException(status_code=409, detail="hold_missing_or_expired")

    bookings = [
        Booking(show_id=payload.show_id, seat_id=seat_id, user_id=user_id, status="confirmed")
        for seat_id in payload.seat_ids
    ]
    try:
        async with session.begin():
            session.add_all(bookings)
            await session.flush()
            intents = [
                Payment(
                    booking_ref=str(booking.id),
                    provider_event_id=None,
                    status="pending",
                )
                for booking in bookings
            ]
            session.add_all(intents)
            await session.flush()
            for booking, intent in zip(bookings, intents, strict=True):
                booking.payment_ref = str(intent.id)
            await session.flush()
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="seat_unavailable") from exc

    await eval_script(
        RELEASE_CONFIRMED_HOLDS_SCRIPT,
        len(keys) + 1,
        *keys,
        user_holds_key(payload.show_id, user_id),
        user_id,
        *(str(seat_id) for seat_id in payload.seat_ids),
    )
    return bookings


@router.post(
    "/holds/{seat_id}/release",
    status_code=204,
    tags=["holds"],
    summary="Release a seat hold",
    description=(
        "Only the hold owner can release it. Compare-and-delete runs atomically in Redis so a "
        "stale owner cannot delete a newer hold."
    ),
)
async def release_hold(
    seat_id: int,
    payload: HoldRelease,
    user_id: str = Header(..., alias="X-User-Id"),
) -> None:
    released = await eval_script(
        RELEASE_HOLD_SCRIPT,
        2,
        hold_key(payload.show_id, seat_id),
        user_holds_key(payload.show_id, user_id),
        user_id,
        str(seat_id),
    )
    if released != 1:
        raise HTTPException(status_code=409, detail="hold_not_owned")


@router.post(
    "/bookings/{booking_id}/pay",
    response_model=PaymentIntentRead,
    status_code=status.HTTP_201_CREATED,
    tags=["payments"],
    summary="Create or return a payment intent",
    description=(
        "Returns the caller's pending payment intent for a confirmed booking. Booking "
        "confirmation normally creates the intent atomically already."
    ),
)
async def create_payment_intent(
    booking_id: int,
    session: AsyncSession = Depends(get_session),
    user_id: str = Header(..., alias="X-User-Id"),
    admission_token: str | None = Header(None, alias="X-Admission-Token"),
) -> PaymentIntentRead:
    async with session.begin():
        booking = await session.scalar(
            select(Booking)
            .where(Booking.id == booking_id, Booking.user_id == user_id)
            .with_for_update()
        )
        if booking is None:
            raise HTTPException(status_code=404, detail="Booking not found")
        if booking.status != "confirmed":
            raise HTTPException(status_code=409, detail="booking_not_payable")
        await require_admission(session, booking.show_id, user_id, admission_token)

        intent = await session.scalar(
            select(Payment).where(
                Payment.booking_ref == str(booking_id),
                Payment.provider_event_id.is_(None),
            )
        )
        if intent is None:
            intent = Payment(
                booking_ref=str(booking_id),
                provider_event_id=None,
                status="pending",
            )
            session.add(intent)
            await session.flush()
        booking.payment_ref = str(intent.id)
        await session.flush()
        return PaymentIntentRead(
            id=intent.id,
            booking_id=booking_id,
            status="pending",
            payment_ref=str(intent.id),
        )


@router.post(
    "/webhooks/payment",
    response_model=PaymentWebhookRead,
    tags=["payments"],
    summary="Process a signed payment event",
    description=(
        "Verifies HMAC-SHA256 over the exact raw request body using `X-Payment-Signature`. "
        "Provider event IDs are unique; duplicates have no state-changing effects. The first "
        "terminal outcome wins."
    ),
)
async def payment_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
    signature: str = Header(..., alias="X-Payment-Signature"),
) -> PaymentWebhookRead:
    body = await request.body()
    expected_signature = hmac.new(
        settings.payment_webhook_secret.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature.removeprefix("sha256="), expected_signature):
        raise HTTPException(status_code=401, detail="Invalid payment signature")
    payload = PaymentWebhook.model_validate_json(body)

    dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
    insert_statement = pg_insert if dialect == "postgresql" else sqlite_insert
    is_duplicate = False
    async with session.begin():
        inserted_id = await session.scalar(
            insert_statement(Payment)
            .values(
                booking_ref=str(payload.booking_id),
                provider_event_id=payload.provider_event_id,
                status=payload.status,
            )
            .on_conflict_do_nothing(index_elements=["provider_event_id"])
            .returning(Payment.id)
        )
        if inserted_id is None:
            is_duplicate = True
        else:
            booking = await session.scalar(
                select(Booking).where(Booking.id == payload.booking_id).with_for_update()
            )
            if booking is None:
                raise HTTPException(status_code=404, detail="Booking not found")
            intent = await session.scalar(
                select(Payment)
                .where(
                    Payment.booking_ref == str(payload.booking_id),
                    Payment.provider_event_id.is_(None),
                )
                .with_for_update()
            )
            if booking.status == "confirmed":
                booking.status = "paid" if payload.status == "success" else "cancelled"
            if intent is not None and intent.status == "pending":
                intent.status = "paid" if booking.status == "paid" else "cancelled"
    return PaymentWebhookRead(status="duplicate" if is_duplicate else "processed")


@router.get(
    "/health",
    response_model=HealthRead,
    tags=["health"],
    summary="Check API and dependency readiness",
    description="Returns `ok` only when PostgreSQL and Redis answer a lightweight health query.",
)
async def health(session: AsyncSession = Depends(get_session)) -> HealthRead:
    await session.execute(text("SELECT 1"))
    await redis_client.ping()
    return HealthRead(status="ok")


@router.get("/whoami", include_in_schema=False)
async def whoami(x_user_id: str = Header(...)) -> dict[str, str]:
    return {"user_id": x_user_id}
