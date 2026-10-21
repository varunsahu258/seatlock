import asyncio
import logging

from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionFactory
from app.core.redis import (
    ADMISSION_TTL_SECONDS,
    ADMIT_QUEUE_SCRIPT,
    eval_script,
    queue_keys,
)
from app.models import Show

logger = logging.getLogger(__name__)
ADMITTER_INTERVAL_SECONDS = 1


async def admit_show_queue(show_id: int, users_per_second: int) -> int:
    keys = queue_keys(show_id)
    return await eval_script(
        ADMIT_QUEUE_SCRIPT,
        5,
        keys[0],
        keys[1],
        keys[3],
        keys[4],
        keys[5],
        str(users_per_second),
        str(settings.admission_ttl_seconds or ADMISSION_TTL_SECONDS),
    )


async def admit_waiting_users_once() -> int:
    async with SessionFactory() as session:
        shows = list(
            (
                await session.execute(
                    select(Show.id, Show.queue_admit_per_second).where(Show.queue_enabled.is_(True))
                )
            ).all()
        )
    admitted = 0
    for show_id, rate in shows:
        admitted += await admit_show_queue(show_id, rate)
    return admitted


async def run_admitter(
    *,
    interval_seconds: int = ADMITTER_INTERVAL_SECONDS,
    stop_event: asyncio.Event | None = None,
) -> None:
    while stop_event is None or not stop_event.is_set():
        try:
            admitted = await admit_waiting_users_once()
            if admitted:
                logger.info("Admitted %d users", admitted)
        except Exception:
            logger.exception("Queue admission run failed")
        if stop_event is None:
            await asyncio.sleep(interval_seconds)
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_admitter())


if __name__ == "__main__":
    main()
