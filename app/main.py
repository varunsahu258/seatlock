from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import admin_router, router
from app.core.config import validate_runtime_settings
from app.core.database import engine
from app.core.redis import redis_client


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    validate_runtime_settings()
    yield
    await redis_client.aclose()
    await engine.dispose()


app = FastAPI(
    title="Seatlock API",
    version="0.1.0",
    description=(
        "A flash-sale seat reservation demo. Redis owns short-lived holds and queue state; "
        "PostgreSQL owns durable booking and payment state. Use `X-User-Id` to identify a "
        "caller in this development build. Queue-enabled shows also require the owner's "
        "`X-Admission-Token` on booking actions."
    ),
    openapi_tags=[
        {
            "name": "admin",
            "description": (
                "Development endpoints for event and show setup. Configure ADMIN_TOKEN to "
                "require bearer-token authorization."
            ),
        },
        {"name": "shows", "description": "Seat inventory and the merged live seat map."},
        {
            "name": "queue",
            "description": "FIFO waiting-room join and owner-bound admission status.",
        },
        {"name": "holds", "description": "Temporary seat reservations with ownership and expiry."},
        {"name": "bookings", "description": "Atomic confirmation of caller-owned seat holds."},
        {
            "name": "payments",
            "description": "Simulated payment intents and signed provider callbacks.",
        },
        {"name": "health", "description": "Dependency readiness check."},
    ],
    lifespan=lifespan,
)
app.include_router(router)
app.include_router(admin_router)
