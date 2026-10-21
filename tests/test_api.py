from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from app.core.redis import hold_key


@pytest.mark.asyncio
async def test_create_500_seat_show_and_status_merge(client: AsyncClient) -> None:
    event_response = await client.post("/admin/events", json={"name": "Test event"})
    assert event_response.status_code == 201
    event_id = event_response.json()["id"]

    show_response = await client.post(
        "/admin/shows",
        json={
            "event_id": event_id,
            "starts_at": datetime(2030, 1, 1, tzinfo=UTC).isoformat(),
            "seat_count": 500,
            "seat_price_minor": 2500,
        },
    )
    assert show_response.status_code == 201
    show_id = show_response.json()["id"]
    assert show_response.json()["seat_count"] == 500

    from app.api import routes
    from app.core.database import get_session
    from app.models import Booking

    session_generator = client._transport.app.dependency_overrides[get_session]()
    session = await anext(session_generator)
    session.add(Booking(show_id=show_id, seat_id=1, user_id="u1", status="confirmed"))
    await session.commit()
    await session.close()
    await session_generator.aclose()
    routes.redis_client.values[hold_key(show_id, 2)] = "u2"

    seat_response = await client.get(f"/shows/{show_id}/seats")
    assert seat_response.status_code == 200
    seats = seat_response.json()
    assert len(seats) == 500
    assert seats[0]["status"] == "booked"
    assert seats[1]["status"] == "held"
    assert seats[2]["status"] == "available"
    assert all(seat["price_minor"] == 2500 for seat in seats)
