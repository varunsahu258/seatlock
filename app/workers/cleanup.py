import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import SessionFactory
from app.models import Booking, Payment

logger = logging.getLogger(__name__)
CLEANUP_INTERVAL_SECONDS = 30
UNPAID_TIMEOUT = timedelta(minutes=10)


async def cleanup_expired_bookings(
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
    *,
    batch_size: int = 500,
) -> int:
    """Cancel a batch of unpaid bookings; row locks let workers safely share the queue."""
    cutoff = datetime.now(UTC) - UNPAID_TIMEOUT
    async with session_factory() as session:
        async with session.begin():
            bookings = list(
                (
                    await session.scalars(
                        select(Booking)
                        .where(Booking.status == "confirmed", Booking.created_at < cutoff)
                        .order_by(Booking.id)
                        .limit(batch_size)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            booking_refs = [str(booking.id) for booking in bookings]
            for booking in bookings:
                booking.status = "cancelled"
            if booking_refs:
                await session.execute(
                    update(Payment)
                    .where(
                        Payment.booking_ref.in_(booking_refs),
                        Payment.provider_event_id.is_(None),
                        Payment.status == "pending",
                    )
                    .values(status="cancelled")
                )
            return len(bookings)


async def run_cleanup_worker(
    *,
    interval_seconds: int = CLEANUP_INTERVAL_SECONDS,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run cleanup immediately, then repeat on the configured interval."""
    while stop_event is None or not stop_event.is_set():
        try:
            count = await cleanup_expired_bookings()
            if count:
                logger.info("Cancelled %d expired unpaid bookings", count)
        except Exception:
            logger.exception("Booking cleanup failed")
        if stop_event is None:
            await asyncio.sleep(interval_seconds)
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_cleanup_worker())


if __name__ == "__main__":
    main()
