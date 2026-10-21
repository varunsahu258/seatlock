import asyncio
import time
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.database import get_session
from app.main import app
from app.models import Base


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, float] = {}
        self.sets: dict[str, set[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sorted_sets: dict[str, list[tuple[int, str]]] = {}
        self.counters: dict[str, int] = {}
        self.rate_buckets: dict[str, tuple[float, float]] = {}
        self.lock = asyncio.Lock()

    def _get(self, key: str) -> str | None:
        expires_at = self.expirations.get(key)
        if expires_at is not None and expires_at <= time.monotonic():
            self.values.pop(key, None)
            self.expirations.pop(key, None)
            return None
        return self.values.get(key)

    def expire_now(self, key: str) -> None:
        self.expirations[key] = time.monotonic() - 1

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self._get(key) for key in keys]

    async def eval(self, script: str, numkeys: int, *args: str | int) -> object:
        keys = [str(arg) for arg in args[:numkeys]]
        argv = [str(arg) for arg in args[numkeys:]]
        async with self.lock:
            if "local seat_limit" in script:
                seat_key, user_set, rate_key, admitted_set, admitted_owners = keys
                user_id, ttl, limit, prefix, seat_id, capacity, refill, queue_enabled, token = argv
                if not self._consume_rate(rate_key, float(capacity), float(refill)):
                    return -2
                if queue_enabled == "1" and (
                    token not in self.sets.get(admitted_set, set())
                    or self.hashes.get(admitted_owners, {}).get(token) != user_id
                ):
                    return -3
                if self._get(seat_key) is not None:
                    return 0
                seats = self.sets.setdefault(user_set, set())
                for held_seat in list(seats):
                    if self._get(f"{prefix}{held_seat}") is None:
                        seats.discard(held_seat)
                if len(seats) >= int(limit):
                    return -1
                self.values[seat_key] = user_id
                self.expirations[seat_key] = time.monotonic() + int(ttl)
                seats.add(seat_id)
                return 1
            if "local hold_count = #KEYS - 2" in script:
                hold_count = len(keys) - 2
                user_id, queue_enabled, token = argv[:3]
                if queue_enabled == "1":
                    if (
                        token not in self.sets.get(keys[hold_count], set())
                        or self.hashes.get(keys[hold_count + 1], {}).get(token) != user_id
                    ):
                        return -1
                return int(all(self._get(key) == user_id for key in keys[:hold_count]))
            if "local seat_count = #KEYS - 1" in script:
                user_id = argv[0]
                user_set = keys[-1]
                released = 0
                for seat_key, seat_id in zip(keys[:-1], argv[1:], strict=True):
                    if self._get(seat_key) == user_id:
                        self.values.pop(seat_key, None)
                        self.expirations.pop(seat_key, None)
                        self.sets.get(user_set, set()).discard(seat_id)
                        released += 1
                return released
            if "local seat_key = KEYS[1]" in script:
                seat_key, user_set = keys
                user_id, seat_id = argv
                if self._get(seat_key) != user_id:
                    return 0
                self.values.pop(seat_key, None)
                self.expirations.pop(seat_key, None)
                self.sets.get(user_set, set()).discard(seat_id)
                return 1
            if "local queue_ttl" in script:
                queue_key, owners_key, user_token_key, sequence_key, rate_key, admitted_key, _ = (
                    keys
                )
                user_id, token, _, capacity, refill = argv
                if not self._consume_rate(rate_key, float(capacity), float(refill)):
                    return ["rate_limited"]
                existing = self._get(user_token_key)
                if existing and existing in self.sets.get(admitted_key, set()):
                    return ["admitted", existing, 0]
                queue = self.sorted_sets.setdefault(queue_key, [])
                if existing and any(item[1] == existing for item in queue):
                    position = next(i + 1 for i, item in enumerate(queue) if item[1] == existing)
                    return ["queued", existing, position]
                self.counters[sequence_key] = self.counters.get(sequence_key, 0) + 1
                queue.append((self.counters[sequence_key], token))
                self.hashes.setdefault(owners_key, {})[token] = user_id
                self.values[user_token_key] = token
                self.expirations[user_token_key] = time.monotonic() + int(argv[2])
                return ["queued", token, len(queue)]
            if "local admission_rate_key" in script:
                queue_key, queue_owners, admitted_key, admitted_owners, _ = keys
                rate, ttl = map(int, argv)
                queue = self.sorted_sets.setdefault(queue_key, [])
                count = min(rate, len(queue))
                for _, token in queue[:count]:
                    user = self.hashes.get(queue_owners, {}).pop(token, None)
                    if user:
                        self.sets.setdefault(admitted_key, set()).add(token)
                        self.hashes.setdefault(admitted_owners, {})[token] = user
                del queue[:count]
                if count:
                    self.expirations[admitted_key] = time.monotonic() + ttl
                    self.expirations[admitted_owners] = time.monotonic() + ttl
                return count
            if "local token = ARGV[1]" in script and "local rank" in script:
                token, user_id = argv
                queue_key, admitted_key, owners_key, admitted_owners = keys
                if token in self.sets.get(admitted_key, set()):
                    if self.hashes.get(admitted_owners, {}).get(token) == user_id:
                        return ["admitted", 0]
                    return ["not_found", 0]
                if self.hashes.get(owners_key, {}).get(token) != user_id:
                    return ["not_found", 0]
                for position, (_, queued_token) in enumerate(
                    self.sorted_sets.get(queue_key, []), start=1
                ):
                    if queued_token == token:
                        return ["queued", position]
                return ["not_found", 0]
            if "SISMEMBER', KEYS[1]" in script:
                token, user_id = argv
                return int(
                    token in self.sets.get(keys[0], set())
                    and self.hashes.get(keys[1], {}).get(token) == user_id
                )
            raise AssertionError("Unknown Redis script")

    def _consume_rate(self, key: str, capacity: float, refill_rate: float) -> bool:
        now = time.monotonic()
        tokens, updated = self.rate_buckets.get(key, (capacity, now))
        tokens = min(capacity, tokens + max(0.0, now - updated) * refill_rate)
        if tokens < 1:
            self.rate_buckets[key] = (tokens, now)
            return False
        self.rate_buckets[key] = (tokens - 1, now)
        return True


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    from app.api import routes
    from app.core import redis as redis_module

    fake_redis = FakeRedis()
    monkeypatch.setattr(routes, "redis_client", fake_redis)
    monkeypatch.setattr(redis_module, "redis_client", fake_redis)
    app.dependency_overrides[get_session] = override_session
    app.state.test_session_factory = factory
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client:
        yield test_client
    app.dependency_overrides.clear()
    del app.state.test_session_factory
    await engine.dispose()
