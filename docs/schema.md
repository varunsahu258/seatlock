# Database schema

```mermaid
erDiagram
    EVENTS ||--o{ SHOWS : schedules
    SHOWS ||--o{ SEATS : contains
    SHOWS ||--o{ BOOKINGS : receives
    SEATS ||--o{ BOOKINGS : booked_as
    BOOKINGS ||--o{ PAYMENTS : records

    EVENTS {
        int id PK
        string name
        datetime created_at
    }
    SHOWS {
        int id PK
        int event_id FK
        datetime starts_at
        boolean queue_enabled
        int queue_admit_per_second
    }
    SEATS {
        int id PK
        int show_id FK
        string label
        int price_minor
    }
    BOOKINGS {
        int id PK
        int show_id FK
        int seat_id FK
        string user_id
        string status "confirmed, paid, or cancelled"
        string payment_ref
        datetime created_at
    }
    PAYMENTS {
        int id PK
        string booking_ref
        string provider_event_id UK "nullable on payment intents"
        string status
    }
```

A partial unique index on `(show_id, seat_id)` where status is `confirmed` or `paid` prevents two active/paid bookings for one seat while allowing cancelled booking history. A second partial unique index permits one pending intent (no provider event ID yet) per booking. Webhook events have a unique provider event ID for idempotency. Redis holds are ephemeral keys in the form `hold:{show_id}:{seat_id}` with a TTL controlled by `HOLD_TTL_SECONDS` (default 300). The seat map treats confirmed and paid bookings as booked and gives them precedence over live holds. Queue-enabled shows keep FIFO waiting tokens in a Redis sorted set; a Redis Lua token bucket globally caps admissions per show per second, and admitted tokens are kept in a TTL-backed set and owner hash.
