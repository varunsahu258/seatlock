from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from app.workers.admitter import admit_show_queue
from tests.test_holds import create_show


async def create_queued_show(client: AsyncClient, admit_rate: int = 1) -> int:
    event = await client.post("/admin/events", json={"name": "Queue test"})
    response = await client.post(
        "/admin/shows",
        json={
            "event_id": event.json()["id"],
            "starts_at": datetime(2030, 1, 1, tzinfo=UTC).isoformat(),
            "seat_count": 3,
            "seat_price_minor": 2500,
            "queue_enabled": True,
            "queue_admit_per_second": admit_rate,
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


@pytest.mark.asyncio
async def test_waiting_room_fifo_admission_and_required_token(client: AsyncClient) -> None:
    show_id = await create_queued_show(client)
    first = await client.post(f"/shows/{show_id}/queue/join", headers={"X-User-Id": "alice"})
    second = await client.post(f"/shows/{show_id}/queue/join", headers={"X-User-Id": "bob"})
    assert first.status_code == second.status_code == 201
    first_data = first.json()
    second_data = second.json()
    assert (first_data["position"], second_data["position"]) == (1, 2)
    assert second_data["estimated_wait_seconds"] == 2

    status_response = await client.get(
        f"/shows/{show_id}/queue/status?token={second_data['token']}",
        headers={"X-User-Id": "bob"},
    )
    assert status_response.json()["position"] == 2

    seat = (await client.get(f"/shows/{show_id}/seats")).json()[0]
    blocked = await client.post(
        f"/shows/{show_id}/seats/{seat['id']}/hold", headers={"X-User-Id": "alice"}
    )
    assert blocked.status_code == 403

    assert await admit_show_queue(show_id, 1) == 1
    admitted = await client.get(
        f"/shows/{show_id}/queue/status?token={first_data['token']}",
        headers={"X-User-Id": "alice"},
    )
    assert admitted.json()["admitted"] is True
    held = await client.post(
        f"/shows/{show_id}/seats/{seat['id']}/hold",
        headers={"X-User-Id": "alice", "X-Admission-Token": first_data["token"]},
    )
    assert held.status_code == 201
    confirmed = await client.post(
        "/bookings/confirm",
        headers={"X-User-Id": "alice", "X-Admission-Token": first_data["token"]},
        json={"show_id": show_id, "seat_ids": [seat["id"]]},
    )
    assert confirmed.status_code == 201


@pytest.mark.asyncio
async def test_admission_token_is_bound_to_the_user(client: AsyncClient) -> None:
    show_id = await create_queued_show(client)
    joined = await client.post(f"/shows/{show_id}/queue/join", headers={"X-User-Id": "alice"})
    token = joined.json()["token"]
    await admit_show_queue(show_id, 1)
    seat = (await client.get(f"/shows/{show_id}/seats")).json()[0]
    response = await client.post(
        f"/shows/{show_id}/seats/{seat['id']}/hold",
        headers={"X-User-Id": "mallory", "X-Admission-Token": token},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_hold_endpoint_rate_limiter_returns_429(client: AsyncClient) -> None:
    from app.core import config

    show_id = await create_show(client, seat_count=3)
    original_capacity = config.settings.hold_rate_capacity
    config.settings.hold_rate_capacity = 2
    try:
        responses = []
        for seat_id in range(1, 4):
            responses.append(
                await client.post(
                    f"/shows/{show_id}/seats/{seat_id}/hold",
                    headers={"X-User-Id": "fast-client"},
                )
            )
    finally:
        config.settings.hold_rate_capacity = original_capacity
    assert [response.status_code for response in responses] == [201, 201, 429]
