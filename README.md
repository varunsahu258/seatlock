# Seatlock

[![CI](https://github.com/OWNER/REPO/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/ci.yml)

> **A flash-sale reservation service that turns a 10,000-user rush into one durable winner per seat.**

## At a glance

**10,000 users, 500 seats, 0 double bookings.** The verified run sold all 500 seats and the Postgres invariant checker found zero duplicate active bookings. It ran on a Mac Apple Silicon host with 8 logical CPUs and 8 GiB RAM. k6 was limited to `GOMAXPROCS=2`; PostgreSQL and Redis ran in Docker Desktop allocated 4 CPUs and 3.8 GiB. The API and load generator shared the Mac.

This local stress run measured p50 **2.64 s**, p95 **30.03 s**, p99 **30.86 s**, and **499 HTTP requests/s**. **65.03% of requests failed and 3,316 booking flows did not complete** under peak local load. The safety invariants held, but this run is not a production capacity claim. See [the machine-readable result](loadtest/results.json), [all recorded runs](docs/benchmarks.md), and the [sampled seat replay data](loadtest/events.json).

The high failure rate came from running the load generator and the entire API/database/cache stack on the same 8 GB laptop while k6 drove 10,000 virtual users. For a clean capacity run, I would move k6 to a separate machine and tune API/database pool sizes to the service host instead of letting local connection pressure dominate the result. The recorded benchmark values above are unchanged.

## What the application does

1. An admin creates an event and one or more shows. A show creation request bulk-creates its seats.
2. The client requests a seat map. The API combines durable active bookings from Postgres with unexpired holds in Redis.
3. For a queue-enabled show, a user joins the FIFO waiting room and polls until the Redis admitter grants an owner-bound token.
4. The user places short-lived holds. Redis Lua scripts perform ownership, quota, rate-limit, and acquisition checks atomically.
5. The user confirms up to six held seats. Postgres inserts the bookings and pending payment intents in one transaction. A partial unique index is the final protection against duplicate active bookings.
6. A simulated provider sends an HMAC-signed webhook. Provider event IDs make retries idempotent; the first terminal result determines whether a booking becomes paid or cancelled.
7. A background cleanup worker cancels confirmed bookings that remain unpaid for more than ten minutes.

## Architecture

```mermaid
flowchart LR
    Client[Browser / API client / k6] --> API[FastAPI async API]
    API -->|Lua holds, queue, admission, rate limits| Redis[(Redis 7)]
    API -->|events, shows, seats, bookings, payment events| PG[(PostgreSQL 16)]
    Admitter[Queue admitter · 1s loop] -->|FIFO sorted set → admitted TTL set| Redis
    Cleanup[Cleanup workers · 30s loop] -->|FOR UPDATE SKIP LOCKED| PG
    Provider[Simulated payment provider] -->|raw-body HMAC webhook| API
    API -->|OpenAPI schema| Docs[Redoc static API reference]
    Load[k6 flash_sale · 10k VUs] --> API
    Load --> Events[events.json sampled timeline]
    PG --> Verify[verify.py invariants + metrics]
    Verify --> Results[results.json + benchmarks.md]
    Events --> Replay[Static replay UI]
    Results --> Charts[Static results dashboard]
```

## Quick start

### Requirements

- Python **3.12**
- [uv](https://docs.astral.sh/uv/)
- Docker Compose v2
- k6 and `jq` for the load test
- `curl` for the manual API examples

### Install, start dependencies, and run the API

```sh
uv sync --all-groups
docker compose up -d --wait
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

The default local services are PostgreSQL 16 at `localhost:5432` and Redis 7 at `localhost:6379`. The database is `seatlock` with user/password `seatlock`. Compose persists Postgres data in a named volume. The API is at `http://localhost:8000`; interactive Swagger docs are at `/docs`, and the generated OpenAPI JSON is at `/openapi.json`.

## Live demo

- Live API docs: `https://<API_HOST>/docs` (replace the placeholder after deploying the API).
- Static Pages showcase: `https://<OWNER>.github.io/<REPO>/`.
- Demo GIF capture: `docs/demo.gif` (placeholder path; add a screen recording before publishing).

For a one-command API development start after installation, `make run` runs Compose, migrations, and the development server. The server stays in the foreground. If a local service already uses port 5432 or 6379, choose ports with `POSTGRES_PORT` and `REDIS_PORT` and set matching `DATABASE_URL` and `REDIS_URL` environment variables.

### Environment configuration

Settings are loaded from environment variables or a local `.env` file. Start with the safe placeholders in [.env.example](.env.example) by copying it to `.env`, then set values for your environment. Never commit production secrets.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://seatlock:seatlock@localhost:5432/seatlock` | Async SQLAlchemy connection string |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis database used for holds, queue, and rate limits |
| `ENVIRONMENT` | `development` | Set to `production` to reject the built-in webhook secret at startup |
| `ADMIN_TOKEN` | unset | Optional bearer token required by `/admin/*` when configured |
| `PAYMENT_WEBHOOK_SECRET` | `dev-payment-secret-change-me` | Shared HMAC-SHA256 secret; replace outside local development |
| `HOLD_TTL_SECONDS` | `300` | Hold duration in seconds (current Lua/API contract is five minutes) |
| `HOLD_RATE_CAPACITY` | `20` | Per-user/show hold token-bucket burst |
| `HOLD_RATE_PER_SECOND` | `10` | Hold bucket refill rate |
| `QUEUE_JOIN_RATE_CAPACITY` | `5` | Per-user/show queue-join burst |
| `QUEUE_JOIN_RATE_PER_SECOND` | `2` | Queue-join bucket refill rate |
| `ADMISSION_TTL_SECONDS` | `600` | Admission-token lifetime in seconds |

Hold-count limit is six per user per show. Waiting queue records have a 24-hour TTL. Queue admission rate is configured on each show (`queue_admit_per_second`).

## API guide

Every protected user action uses `X-User-Id: <user>` as a development identity. On queue-enabled shows, hold, confirm, and payment-intent requests also use `X-Admission-Token: <token>`. Set `ADMIN_TOKEN` to require `Authorization: Bearer <token>` on `/admin/*`; if unset, admin setup endpoints remain open for local development. This project does not provide real user authentication.

| Method and path | Purpose | Success | Common errors |
| --- | --- | --- | --- |
| `POST /admin/events` | Create an event `{ "name": "Summer festival" }` | `201` event | `422` invalid input |
| `POST /admin/shows` | Create a show and numbered seats | `201` show | `404` unknown event, `422` invalid count/price |
| `GET /shows/{show_id}/seats` | Get live seat map with `available`, `held`, `booked` states | `200` seat array | `404` unknown show |
| `POST /shows/{show_id}/queue/join` | Join/rejoin a queue; returns token, position, estimated wait | `201` queue state | `404` show missing, `409` queue disabled, `429` rate limited |
| `GET /shows/{show_id}/queue/status?token=...` | Poll the caller's queue position/admission | `200` queue state | `404` missing/wrong-owner token, `409` queue disabled |
| `POST /shows/{show_id}/seats/{seat_id}/hold` | Hold an available seat for 300 seconds | `201` expiry time | `403` admission required, `404` missing show/seat, `409` unavailable or six-seat limit, `429` rate limited |
| `POST /bookings/confirm` | Confirm 1–6 holds and create pending payment intents | `201` booking array | `403` admission required, `404` invalid seat, `409` hold missing/expired or active-seat conflict |
| `POST /holds/{seat_id}/release` | Voluntarily release an owned hold; JSON body includes `show_id` | `204` empty response | `409` hold is missing or owned by someone else |
| `POST /bookings/{booking_id}/pay` | Create or return the caller's pending payment intent | `201` intent | `403` admission required, `404` not caller's booking, `409` no longer payable |
| `POST /webhooks/payment` | Process a signed payment result | `200` processed or duplicate | `401` bad signature, `404` booking missing, `422` invalid event |
| `GET /health` | Check Postgres and Redis connectivity | `200 {"status":"ok"}` | `5xx` dependency failure |

The complete request/response models, tags, and examples are generated in [Redoc](site/api.html) and the raw [OpenAPI file](site/openapi.json). API reference generation is part of `make site` and CI.

### Create an event and a normal show

```sh
EVENT=$(curl -sS -X POST http://localhost:8000/admin/events \
  -H 'Content-Type: application/json' \
  -d '{"name":"Summer festival"}')
EVENT_ID=$(printf '%s' "$EVENT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

SHOW=$(curl -sS -X POST http://localhost:8000/admin/shows \
  -H 'Content-Type: application/json' \
  -d "{\"event_id\":$EVENT_ID,\"starts_at\":\"2030-06-01T19:30:00Z\",\"seat_count\":500,\"seat_price_minor\":2500}")
SHOW_ID=$(printf '%s' "$SHOW" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
echo "Created show $SHOW_ID"
```

Prices are integer minor currency units (for example cents/paise), not decimal floats. Labels are generated as `1` through `seat_count`; `seat.id` is a database identifier and is not guaranteed to equal the label.

### Queue-enabled flash sale flow

Create a show with `queue_enabled: true` and a positive `queue_admit_per_second`. Start the separate admitter process:

```sh
make admitter
```

Clients join with their user identity, keep the returned token, and poll status:

```sh
curl -sS -X POST http://localhost:8000/shows/1/queue/join \
  -H 'X-User-Id: user-42'

curl -sS 'http://localhost:8000/shows/1/queue/status?token=TOKEN_FROM_JOIN' \
  -H 'X-User-Id: user-42'
```

Once `admitted` is `true`, send the same token as `X-Admission-Token` on the hold, confirmation, and payment-intent requests. Admission is owner-bound and expires after ten minutes by default. The estimated wait is queue position divided by the show's admission rate; it is an estimate, not a reservation guarantee.

### Hold, confirm, and pay

```sh
SEAT_MAP=$(curl -sS "http://localhost:8000/shows/$SHOW_ID/seats")
SEAT_ID=$(printf '%s' "$SEAT_MAP" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')

curl -sS -X POST "http://localhost:8000/shows/$SHOW_ID/seats/$SEAT_ID/hold" \
  -H 'X-User-Id: user-42'

BOOKINGS=$(curl -sS -X POST http://localhost:8000/bookings/confirm \
  -H 'X-User-Id: user-42' -H 'Content-Type: application/json' \
  -d "{\"show_id\":$SHOW_ID,\"seat_ids\":[$SEAT_ID]}")
BOOKING_ID=$(printf '%s' "$BOOKINGS" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')

curl -sS -X POST "http://localhost:8000/bookings/$BOOKING_ID/pay" \
  -H 'X-User-Id: user-42'
```

The confirmation response includes booking IDs. Confirmation normally created a pending intent in the same Postgres transaction, so `/pay` returns that intent (and is retained as a retryable create-or-return endpoint).

### Payment webhook signature

The signature is lowercase hexadecimal HMAC-SHA256 of the **exact raw request body**, sent as `X-Payment-Signature` (the optional `sha256=` prefix is also accepted). For example:

```sh
export PAYMENT_WEBHOOK_SECRET="${PAYMENT_WEBHOOK_SECRET:-dev-payment-secret-change-me}"
# Use the booking_id returned by POST /bookings/confirm.
BODY='{"provider_event_id":"evt_demo_001","booking_id":123,"status":"success"}'
SIGNATURE=$(printf '%s' "$BODY" | PAYMENT_WEBHOOK_SECRET="$PAYMENT_WEBHOOK_SECRET" python3 -c \
  'import hashlib,hmac,os,sys; print(hmac.new(os.environ["PAYMENT_WEBHOOK_SECRET"].encode(), sys.stdin.buffer.read(), hashlib.sha256).hexdigest())')
curl -sS -X POST http://localhost:8000/webhooks/payment \
  -H "X-Payment-Signature: $SIGNATURE" -H 'Content-Type: application/json' \
  --data-binary "$BODY"
```

For a failure callback, use `"status":"failure"`. Reuse a `provider_event_id` to simulate a delivery retry: the first request returns `processed`; later copies return `duplicate` without changing payment or booking state. Separate event IDs for the same booking are serialized on the booking row; the first terminal outcome wins, so a late success cannot revive a cancelled booking and a late failure cannot cancel a paid booking.

## Booking and payment lifecycle

```mermaid
stateDiagram-v2
    [*] --> Held: Redis SET NX EX 300
    Held --> Available: expiry or owner release
    Held --> Confirmed: Postgres transaction + pending intent
    Confirmed --> Paid: first successful webhook
    Confirmed --> Cancelled: failed webhook or 10-minute cleanup
    Cancelled --> Available: seat map sees no active booking
    Paid --> Paid: later failure is ignored
    Cancelled --> Cancelled: later success is ignored
```

An active booking is `confirmed` (awaiting provider result) or `paid`. A cancelled booking remains as history and releases the seat because the partial unique index only covers active statuses. A seat map shows active bookings as `booked` before considering Redis holds.

## Concurrency and reliability choices

- **Redis holds plus Postgres `UNIQUE`:** Redis gives fast expiring reservations; Postgres is the durable authority. The partial unique index on `(show_id, seat_id)` where status is `confirmed` or `paid` protects the final invariant even if Redis expires, loses data, or races a database transaction.
- **Lua compare-and-delete:** release compares the stored owner and deletes in one Redis operation. A GET then DEL has a race: the old hold may expire and a new owner may acquire the key between commands, letting the old release delete the new hold.
- **Atomic confirmation:** Redis Lua checks all hold owners before Postgres inserts any bookings. Bookings and pending intents commit together; a uniqueness conflict maps to HTTP 409. Holds are then released with a compare-and-delete script. Redis and Postgres cannot share one transaction, so Postgres uniqueness remains essential.
- **Token bucket Lua:** hold and queue join rate checks happen in the same Redis script as the operation. Defaults are per user and show: hold capacity 20/refill 10 tokens per second; join capacity 5/refill 2 tokens per second. `429` includes `Retry-After: 1`.
- **Waiting room:** Redis sorted-set scores give FIFO order. The admitter runs once per second and uses a per-show token bucket to move up to `queue_admit_per_second` users into an owner map and admitted set with TTL.
- **Webhook idempotency:** provider event IDs are unique. Insert with `ON CONFLICT DO NOTHING`, booking row lock, payment-intent transition, and booking transition happen transactionally. Replayed IDs cause no state changes.
- **Cleanup workers:** every 30 seconds, each worker selects up to 500 unpaid bookings older than ten minutes with `FOR UPDATE SKIP LOCKED`, cancels them, and marks their pending intents cancelled. Concurrent workers claim disjoint rows.

The ERD and concrete database constraints are in [docs/schema.md](docs/schema.md); implementation rationale is also summarized in [DECISIONS.md](DECISIONS.md).

## Background workers

Run one worker in a separate terminal for a queue-enabled sale:

```sh
make admitter
```

Run one or more unpaid-booking cleanup workers as needed:

```sh
make worker
```

Each worker polls immediately at startup, then at its configured interval. `loadtest/run.sh` starts and stops its own admitter for the benchmark show. The cleanup worker is independent of the API process and can be horizontally scaled because of `SKIP LOCKED`.

## Testing, linting, and type checking

```sh
make check       # Ruff, mypy, pytest
make test        # pytest only
make lint        # Ruff
make typecheck   # mypy app
```

Tests cover the API/schema, 500-seat creation and seat map, racing seat holds, six-seat quota, hold expiration and release ownership, missing/expired holds, queue ordering/admission/rate limiting, concurrent webhook replay, invalid signatures, late/out-of-order payment outcomes, and concurrent cleanup workers. The cleanup `SKIP LOCKED` concurrency test requires PostgreSQL; it is skipped if `DATABASE_URL` is not PostgreSQL.

GitHub Actions runs Ruff, mypy, Alembic migrations, and tests against PostgreSQL 16 and Redis 7. A separate Pages workflow deploys the already-committed `site/` directory on pushes to `main` or a manual dispatch. Select **GitHub Actions** under repository Pages settings to enable deployment.

## Load test and static showcase

Requirements for the load test are k6 and `jq`, in addition to the running API/Postgres/Redis. In separate terminals:

```sh
docker compose up -d --wait
uv run alembic upgrade head
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then run:

```sh
make loadtest
```

The harness creates an isolated queue-enabled show with 500 seats, starts the admitter, and executes the `flash_sale` k6 scenario with 10,000 VUs. Users join, wait for admission, choose random seats, contend for holds, confirm, create/reuse payment intents, and send signed success webhooks. k6 logs every winning hold and samples 5% of rejected seat attempts. `loadtest/extract_events.py` converts those outcomes to the time-sorted `{ "t", "seat", "outcome" }` timeline in `loadtest/events.json`, with seat labels normalized to 1–500.

After k6, `loadtest/verify.py` connects directly to Postgres and Redis and checks:

1. No seat has more than one active (`confirmed` or `paid`) booking.
2. Active confirmed/paid bookings do not exceed the configured seat capacity.
3. Every active booking has a pending/paid payment record or a matching live Redis hold.

It writes `loadtest/results.json` with p50/p95/p99 request latency, throughput, request count, sold seats, and invariant results; each verification appends the hardware and k6 command to `docs/benchmarks.md`. Generated k6 summaries and console logs are ignored by git; the replay events and verified results are retained in the repository.

Useful overrides:

```sh
VUS=1000 ADMIT_RATE=250 make loadtest
BASE_URL=http://127.0.0.1:8000 VUS=10000 make loadtest
LOADTEST_HARDWARE_DETAIL='remote 8-core load generator; DB on 4-core host' make loadtest
```

`GOMAXPROCS=2` caps the local k6 Go runtime. A lower VU count is useful for iteration, but only the full run reproduces the headline scenario. The measured host had substantial connection pressure and long request tails; use a separate load generator and production-like sizing for meaningful capacity planning.

### Build and preview the static site

```sh
make site
python3 -m http.server 8001 --directory site
```

Open `http://localhost:8001`. The site includes:

- `index.html`: showcase overview and links.
- `guide.html`: visual explanation of state ownership and safety invariants.
- `replay.html`: browser-only animated 500-seat map loading `events.json`.
- `results.html`: latency bars, throughput, sale totals, and invariant status loaded from `results.json`.
- `api.html`: Redoc UI loading the generated `openapi.json`.
- `openapi.json`, `events.json`, `results.json`: static data files.

`make site` imports the FastAPI app to generate OpenAPI and copies the verified load-test artifacts into `/site`; it does not need the API server running.

## Project layout

```text
app/
  api/          FastAPI routers and request handling
  core/         settings, async DB setup, Redis Lua scripts
  models/       SQLAlchemy entities and constraints
  schemas/      Pydantic request/response contracts
  services/     currently empty package; workflow orchestration remains in api/routes.py
  workers/      waiting-room admitter and unpaid-booking cleanup
alembic/        database migrations
docs/           schema ERD, benchmark log, and deployment guide
loadtest/       k6 scenario, runner, event extractor, DB verifier, artifacts
scripts/        static-site builder
site/           backend-free showcase, replay, dashboard, Redoc
tests/          API, Redis behavior, payments, and concurrency coverage
Dockerfile      API and worker image
docker-compose.prod.yml  VM deployment stack with Caddy, Postgres, and Redis
```

## Limitations and next steps

- `X-User-Id` is caller-provided and is not authentication. Admin endpoints accept a configured bearer token; with `ADMIN_TOKEN` unset, they remain open for demo setup.
- The admin token only protects setup routes and is not a replacement for user authentication.
- Payment is simulated. A real provider needs secret rotation, timestamp/replay windows, amount/currency reconciliation, retries, alerting, and reconciliation jobs.
- The local benchmark shares CPU and networking between k6 and the service, and produced a high failure rate. It proves the observed database invariant for this run; it does not establish production throughput or availability.
- The replay is a sampled visual summary, not a full audit trace. It records winning holds and sampled rejected attempts.
- Redis queue/hold state is ephemeral. A production setup needs Redis HA/persistence policy, operational monitoring, backup/recovery decisions, and clear behavior during Redis outages.
- Admin endpoints, schema migrations, rate-limit tuning, and cleanup/admitter lifecycle need production auth, observability, and deployment orchestration.

## License

Seatlock is distributed under the [MIT License](LICENSE).

## Further reading

- [Static showcase](site/index.html) · [System guide](site/guide.html) · [Seat replay](site/replay.html) · [Results charts](site/results.html) · [Redoc API](site/api.html)
- [OpenAPI schema](site/openapi.json) · [Database ERD](docs/schema.md) · [Deployment guide](docs/DEPLOY.md) · [Design decisions](DECISIONS.md) · [Benchmark history](docs/benchmarks.md)
