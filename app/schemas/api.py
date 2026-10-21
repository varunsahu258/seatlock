from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EventCreate(BaseModel):
    """Admin-created event used to group shows."""

    name: str = Field(min_length=1, max_length=200)
    model_config = ConfigDict(json_schema_extra={"examples": [{"name": "Summer festival"}]})


class EventRead(BaseModel):
    id: int
    name: str
    model_config = ConfigDict(from_attributes=True)


class ShowCreate(BaseModel):
    """Create a show and a numbered block of seats."""

    event_id: int
    starts_at: datetime
    seat_count: int = Field(gt=0, le=10000)
    seat_price_minor: int = Field(ge=0)
    queue_enabled: bool = False
    queue_admit_per_second: int = Field(default=10, gt=0, le=10000)
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "event_id": 1,
                    "starts_at": "2030-06-01T19:30:00Z",
                    "seat_count": 500,
                    "seat_price_minor": 2500,
                    "queue_enabled": True,
                    "queue_admit_per_second": 100,
                }
            ]
        }
    )


class ShowRead(BaseModel):
    id: int
    event_id: int
    starts_at: datetime
    seat_count: int
    queue_enabled: bool = False
    queue_admit_per_second: int = 10
    model_config = ConfigDict(from_attributes=True)


class SeatRead(BaseModel):
    id: int
    label: str
    price_minor: int
    status: Literal["available", "held", "booked"]


class HealthRead(BaseModel):
    status: Literal["ok"]


class HoldRead(BaseModel):
    show_id: int
    seat_id: int
    expires_at: datetime


class BookingConfirm(BaseModel):
    """Confirm one to six caller-owned seat holds for one show."""

    show_id: int
    seat_ids: list[int] = Field(min_length=1, max_length=6)
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"show_id": 12, "seat_ids": [101, 102]}]}
    )

    @field_validator("seat_ids")
    @classmethod
    def seat_ids_must_be_unique(cls, seat_ids: list[int]) -> list[int]:
        if len(set(seat_ids)) != len(seat_ids):
            raise ValueError("seat_ids must be unique")
        return seat_ids


class HoldRelease(BaseModel):
    """Identifies which show the path seat belongs to."""

    show_id: int
    model_config = ConfigDict(json_schema_extra={"examples": [{"show_id": 12}]})


class BookingRead(BaseModel):
    id: int
    show_id: int
    seat_id: int
    user_id: str
    status: Literal["confirmed"]
    payment_ref: str | None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class PaymentIntentRead(BaseModel):
    id: int
    booking_id: int
    status: Literal["pending"]
    payment_ref: str


class PaymentWebhook(BaseModel):
    """Signed provider outcome for a booking."""

    provider_event_id: str = Field(min_length=1, max_length=255)
    booking_id: int = Field(gt=0)
    status: Literal["success", "failure"]
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"provider_event_id": "evt_01HXYZ", "booking_id": 9001, "status": "success"}
            ]
        }
    )


class PaymentWebhookRead(BaseModel):
    status: Literal["processed", "duplicate"]


class QueueJoinRead(BaseModel):
    token: str
    position: int
    admitted: bool
    estimated_wait_seconds: int


class QueueStatusRead(BaseModel):
    token: str
    position: int
    admitted: bool
    estimated_wait_seconds: int
