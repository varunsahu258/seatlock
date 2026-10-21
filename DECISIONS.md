# Seatlock engineering decisions

## Redis holds and the PostgreSQL uniqueness guard

Seat holds need low-latency acquisition and automatic expiry, so Redis owns the temporary reservation. PostgreSQL owns durable bookings. A partial unique index on `(show_id, seat_id)` while a booking is active is the final concurrency invariant: Redis and PostgreSQL do not share a transaction, and a Redis TTL, process crash, or stale client can leave their views momentarily inconsistent.

## Compare-and-delete holds

Hold release uses a Lua script that compares the stored owner and deletes only when it still matches. GET followed by DEL would create a race: a hold can expire and be replaced between those commands, allowing an old owner's delayed release to delete the newer owner's hold.

## Queue admission and rate limiting

Each show has a Redis sorted-set queue with monotonically increasing join order. The admitter transfers a configured number of entries per second into an owner-bound admitted set with TTL. Queue join and hold acquisition use token-bucket Lua scripts so rate checks and Redis mutations happen atomically.

## Unpaid booking cleanup

Cleanup selects expired unpaid bookings with `FOR UPDATE SKIP LOCKED` in bounded batches. PostgreSQL hands different rows to concurrent workers without making workers wait on rows already claimed by another worker.

## Payment event processing

The provider event ID is unique and inserted with `ON CONFLICT DO NOTHING`. Event insertion, booking row locking, state transition, and intent update share one transaction. Duplicate deliveries are no-ops. The first terminal outcome is retained, so delayed success cannot resurrect a cancelled booking and delayed failure cannot cancel a paid booking.

## Load test and replay

The 10,000-user run records every winning hold plus a sample of rejected seat attempts. Events are emitted by k6 and sorted into `loadtest/events.json` for the static replay. The database verifier runs after k6 and records the benchmark metrics and invariants in `loadtest/results.json`; it is the source of truth for double-booking results.
