# Deployment

Seatlock is a demo that needs three continuously running Python processes in a queue-enabled
deployment: the HTTP API, the queue admitter, and the unpaid-booking cleanup worker. PostgreSQL
stores durable event and booking data; Redis stores holds, waiting-room state, and rate buckets.
Keep the API and all three services close to the datastores to reduce network latency.

## Before deploying

- Create a unique `PAYMENT_WEBHOOK_SECRET` and `ADMIN_TOKEN`. Do not use the values from the
  example environment file in production. `ENVIRONMENT=production` makes the API fail startup
  if the webhook secret is still the built-in development default.
- Back up PostgreSQL and decide how Redis persistence/recovery should work. Redis contains
  short-lived state, but losing holds or queue state can still affect an in-progress sale.
- This demo's `X-User-Id` header is not authentication. Admin bearer-token protection does not
  replace user authentication or authorization.
- Choose a PostgreSQL connection pool size appropriate for the provider's connection cap and the
  number of API replicas. Neon pooled connection strings are useful for runtime connection
  pressure; confirm the database provider's pooling mode fits SQLAlchemy and Alembic.

## Path 1: One Oracle Cloud Always Free VM with Docker Compose and Caddy

Oracle's Always Free Ampere A1 allowance has been listed as up to 2 OCPUs and 12 GB RAM in the
home region, but instance capacity varies and idle Always Free instances may be reclaimed. Check
the current eligibility and limits in [Oracle's Always Free resources documentation](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
before relying on it. A VM is a convenient portfolio/demo host, not a high availability design.

1. Provision an eligible Ubuntu ARM64 VM and reserve a public IP. Point a domain's A record at the
   IP. In the cloud security list and host firewall, allow inbound TCP 80 and 443 (and optionally
   UDP 443 for HTTP/3); keep SSH limited to your own IP. Do not expose PostgreSQL or Redis ports.
2. Install Docker Engine and the Docker Compose plugin using the official instructions for the VM's
   distribution. Clone the repository and enter its directory.
3. Create `.env` in the repository root. Use a PostgreSQL password made of URL-safe characters
   (hex is convenient because `DATABASE_URL` embeds it) and generate distinct random secrets:

   ```sh
   printf 'DOMAIN=api.example.com\n' > .env
   printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 24)" >> .env
   printf 'PAYMENT_WEBHOOK_SECRET=%s\n' "$(openssl rand -hex 32)" >> .env
   printf 'ADMIN_TOKEN=%s\n' "$(openssl rand -hex 32)" >> .env
   printf 'ENVIRONMENT=production\n' >> .env
   ```

   Store a protected copy of these values outside the VM. Never commit `.env`.
4. Start the stack:

   ```sh
   docker compose -f docker-compose.prod.yml up --build -d
   docker compose -f docker-compose.prod.yml ps
   docker compose -f docker-compose.prod.yml logs -f api caddy
   ```

   Compose waits for Postgres and Redis health, applies Alembic migrations before starting the
   API, then runs the admitter and cleanup as separate containers. Caddy obtains and renews HTTPS
   certificates for `DOMAIN` and reverse-proxies to the API. Swagger is at
   `https://api.example.com/docs`; the static portfolio remains on GitHub Pages.
5. Confirm `https://api.example.com/health` responds with `{"status":"ok"}`. Send
   `Authorization: Bearer $ADMIN_TOKEN` to `/admin/*`. Configure the payment provider to send the
   raw-body HMAC in `X-Payment-Signature`.

Update with `git pull` and `docker compose -f docker-compose.prod.yml up --build -d`. Before
schema changes, take a database backup. This compose setup runs migrations as the API container
starts; for multiple API replicas or zero-downtime deployments, run migrations as a single
release step instead.

## Path 2: Render API and workers, Neon Postgres, Upstash Redis

This option separates the managed data stores from the API host. Service availability, free
allowances, and pricing change; verify current plan limits with each provider before deploying.
Render currently supports Docker-based web services and background workers, and the workers
below must be provisioned as **separate, always-running background services**. The admitter and
cleanup worker are not scheduled jobs: each runs a polling loop. Render's background worker
instances may require a paid plan.

1. Create a Neon PostgreSQL database. Copy a connection string from Neon and keep it secret.
   Neon URLs commonly look like:

   ```text
   postgresql://USER:PASSWORD@HOST/DB?sslmode=require&channel_binding=require
   ```

   Seatlock normalizes `postgresql://` and `postgres://` schemes to SQLAlchemy's `asyncpg`
   driver, maps `sslmode=require` to asyncpg's `ssl=require`, and removes the libpq-only
   `channel_binding` query option. Use a Neon pooled hostname for a runtime API with concurrent
   requests when appropriate; keep migration and pool limits in mind for the provider's selected
   endpoint mode.
2. Create an Upstash Redis database with its **TCP** connection details. Set `REDIS_URL` to the
   TLS URI, normally `rediss://default:<TOKEN>@<HOST>:<PORT>/0`. redis-py `from_url` supports
   `rediss://`, and Seatlock's Lua scripts require the Redis TCP protocol and `EVAL`. Do not use
   Upstash's REST URL/token with this app; that is a different HTTP client interface.
3. In Render, create one Docker **Web Service** from this repository for the API. Set the Docker
   file path to `./Dockerfile`; the container honors Render's `PORT` environment variable. Add
   `DATABASE_URL`, `REDIS_URL`, `ENVIRONMENT=production`, `PAYMENT_WEBHOOK_SECRET`, and
   `ADMIN_TOKEN` as secret environment variables. Set `HOLD_TTL_SECONDS`, rate limits, and
   `ADMISSION_TTL_SECONDS` only if their defaults are not suitable. Set the health check path to
   `/health`.
4. Configure the API service's pre-deploy command as `alembic upgrade head` so migrations run
   once for a deploy, rather than in every worker. If the selected Render plan does not support a
   pre-deploy command, run that migration as an explicit one-off release operation before sending
   traffic to the new version.
5. Create two **Background Worker** services from the same repository and Dockerfile. Set the
   start command for the first to `python -m app.workers.admitter`; set the second to
   `python -m app.workers.cleanup`. Give both the same `DATABASE_URL`, `REDIS_URL`, and app secret
   variables as the API. These must be independently running processes; do not try to run either
   worker inside the web-service command.
6. Use the Render API hostname or attach a custom domain. Verify the `/health` endpoint, then
   create a queue-enabled show and confirm that queue positions advance while the admitter is
   running.

Render's free web services may sleep when idle, while a flash sale needs the API and both workers
to be available continuously. Plan for a paid always-on service tier for the workers and for
enough API/database connections for the intended traffic.

### Environment variables

The application reads every setting documented in the repository's `.env.example`. Production
should always set `ENVIRONMENT=production`, `DATABASE_URL`, `REDIS_URL`, `PAYMENT_WEBHOOK_SECRET`,
and `ADMIN_TOKEN`. Use provider-issued TLS URLs (`rediss://` for Redis and an SSL-required
PostgreSQL URL for Neon); do not put credentials in source control, images, or public logs.

