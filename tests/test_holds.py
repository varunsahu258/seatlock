import asyncio
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from app.core.redis import hold_key
from tests.conftest import FakeRedis


async def create_show(client: AsyncClient, seat_count: int = 8) -> int:
    event_response = await client.post("/admin/events", json={"name": "Hold test"})
    event_id = event_response.json()["id"]
    show_response = await client.post(
        "/admin/shows",
        json={
            "event_id": event_id,
            "starts_at": datetime(2030, 1, 1, tzinfo=UTC).isoformat(),
            "seat_count": seat_count,
            "seat_price_minor": 2500,
        },
    )
    assert show_response.status_code == 201
    return show_response.json()["id"]


def fake_redis() -> FakeRedis:
    from app.api.routes import redis_client

    assert isinstance(redis_client, FakeRedis)
    return redis_client


@pytest.mark.asyncio
async def test_two_users_racing_for_one_seat_exactly_one_wins(client: AsyncClient) -> None:
    show_id = await create_show(client)
    responses = await asyncio.gather(
        client.post(f"/shows/{show_id}/seats/1/hold", headers={"X-User-Id": "alice"}),
        client.post(f"/shows/{show_id}/seats/1/hold", headers={"X-User-Id": "bob"}),
    )
    assert sorted(response.status_code for response in responses) == [201, 409]
    winner = next(response for response in responses if response.status_code == 201)
    assert winner.json()["seat_id"] == 1
    assert "expires_at" in winner.json()


@pytest.mark.asyncio
async def test_expired_hold_becomes_available_again(client: AsyncClient) -> None:
    show_id = await create_show(client)
    response = await client.post(f"/shows/{show_id}/seats/1/hold", headers={"X-User-Id": "alice"})
    assert response.status_code == 201
    fake_redis().expire_now(hold_key(show_id, 1))
    retry = await client.post(f"/shows/{show_id}/seats/1/hold", headers={"X-User-Id": "bob"})
    assert retry.status_code == 201


@pytest.mark.asyncio
async def test_releasing_another_users_hold_is_rejected(client: AsyncClient) -> None:
    show_id = await create_show(client)
    held = await client.post(f"/shows/{show_id}/seats/1/hold", headers={"X-User-Id": "alice"})
    assert held.status_code == 201
    rejected = await client.post(
        "/holds/1/release", headers={"X-User-Id": "bob"}, json={"show_id": show_id}
    )
    assert rejected.status_code == 409
    seat_map = await client.get(f"/shows/{show_id}/seats")
    assert seat_map.json()[0]["status"] == "held"


@pytest.mark.asyncio
async def test_confirm_without_hold_is_rejected(client: AsyncClient) -> None:
    show_id = await create_show(client)
    response = await client.post(
        "/bookings/confirm",
        headers={"X-User-Id": "alice"},
        json={"show_id": show_id, "seat_ids": [1]},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "hold_missing_or_expired"


@pytest.mark.asyncio
async def test_confirm_after_expiry_is_rejected(client: AsyncClient) -> None:
    show_id = await create_show(client)
    held = await client.post(f"/shows/{show_id}/seats/1/hold", headers={"X-User-Id": "alice"})
    assert held.status_code == 201
    fake_redis().expire_now(hold_key(show_id, 1))
    confirmed = await client.post(
        "/bookings/confirm",
        headers={"X-User-Id": "alice"},
        json={"show_id": show_id, "seat_ids": [1]},
    )
    assert confirmed.status_code == 409
    assert confirmed.json()["detail"] == "hold_missing_or_expired"


@pytest.mark.asyncio
async def test_user_cannot_hold_more_than_six_seats_per_show(client: AsyncClient) -> None:
    show_id = await create_show(client, seat_count=7)
    for seat_id in range(1, 7):
        response = await client.post(
            f"/shows/{show_id}/seats/{seat_id}/hold", headers={"X-User-Id": "alice"}
        )
        assert response.status_code == 201
    seventh = await client.post(f"/shows/{show_id}/seats/7/hold", headers={"X-User-Id": "alice"})
    assert seventh.status_code == 409
    assert seventh.json()["detail"] == "hold_limit_reached"


@pytest.mark.asyncio
async def test_confirm_inserts_bookings_and_clears_owned_holds(client: AsyncClient) -> None:
    show_id = await create_show(client)
    for seat_id in (1, 2):
        response = await client.post(
            f"/shows/{show_id}/seats/{seat_id}/hold", headers={"X-User-Id": "alice"}
        )
        assert response.status_code == 201
    response = await client.post(
        "/bookings/confirm",
        headers={"X-User-Id": "alice"},
        json={"show_id": show_id, "seat_ids": [1, 2]},
    )
    assert response.status_code == 201
    assert len(response.json()) == 2
    seat_map = await client.get(f"/shows/{show_id}/seats")
    assert [seat["status"] for seat in seat_map.json()[:2]] == ["booked", "booked"]
