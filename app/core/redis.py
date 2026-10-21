from redis.asyncio import Redis, from_url

from app.core.config import settings

redis_client: Redis = from_url(  # type: ignore[no-untyped-call]
    settings.redis_url, decode_responses=True
)

ACQUIRE_HOLD_SCRIPT = """
local seat_key = KEYS[1]
local user_seats_key = KEYS[2]
local rate_key = KEYS[3]
local admitted_key = KEYS[4]
local admitted_owners_key = KEYS[5]
local user_id = ARGV[1]
local ttl = tonumber(ARGV[2])
local seat_limit = tonumber(ARGV[3])
local seat_prefix = ARGV[4]
local capacity = tonumber(ARGV[6])
local refill_rate = tonumber(ARGV[7])
local queue_enabled = ARGV[8] == '1'
local admission_token = ARGV[9]
local time = redis.call('TIME')
local now = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)
local state = redis.call('HMGET', rate_key, 'tokens', 'updated_at')
local tokens = tonumber(state[1]) or capacity
local updated_at = tonumber(state[2]) or now
tokens = math.min(capacity, tokens + math.max(0, now - updated_at) * refill_rate / 1000)
if tokens < 1 then
    redis.call('HSET', rate_key, 'tokens', tokens, 'updated_at', now)
    return -2
end
redis.call('HSET', rate_key, 'tokens', tokens - 1, 'updated_at', now)
redis.call('PEXPIRE', rate_key, math.ceil(capacity / refill_rate * 2000))
if queue_enabled and not (
    redis.call('SISMEMBER', admitted_key, admission_token) == 1
    and redis.call('HGET', admitted_owners_key, admission_token) == user_id
) then
    return -3
end
if redis.call('EXISTS', seat_key) == 1 then
    return 0
end
local seats = redis.call('SMEMBERS', user_seats_key)
for _, seat_id in ipairs(seats) do
    if redis.call('EXISTS', seat_prefix .. seat_id) == 0 then
        redis.call('SREM', user_seats_key, seat_id)
    end
end
if redis.call('SCARD', user_seats_key) >= seat_limit then
    return -1
end
local acquired = redis.call('SET', seat_key, user_id, 'NX', 'EX', ttl)
if not acquired then
    return 0
end
redis.call('SADD', user_seats_key, ARGV[5])
redis.call('EXPIRE', user_seats_key, ttl)
return 1
"""

VERIFY_HOLDS_SCRIPT = """
local user_id = ARGV[1]
local queue_enabled = ARGV[2] == '1'
local admission_token = ARGV[3]
local hold_count = #KEYS - 2
if queue_enabled and not (
    redis.call('SISMEMBER', KEYS[hold_count + 1], admission_token) == 1
    and redis.call('HGET', KEYS[hold_count + 2], admission_token) == user_id
) then
    return -1
end
for index = 1, hold_count do
    if redis.call('GET', KEYS[index]) ~= user_id then
        return 0
    end
end
return 1
"""

RELEASE_HOLD_SCRIPT = """
local seat_key = KEYS[1]
local user_seats_key = KEYS[2]
local user_id = ARGV[1]
local seat_id = ARGV[2]
if redis.call('GET', seat_key) ~= user_id then
    return 0
end
redis.call('DEL', seat_key)
redis.call('SREM', user_seats_key, seat_id)
return 1
"""

RELEASE_CONFIRMED_HOLDS_SCRIPT = """
local user_id = ARGV[1]
local seat_count = #KEYS - 1
local user_seats_key = KEYS[seat_count + 1]
local released = 0
for index = 1, seat_count do
    local seat_key = KEYS[index]
    local seat_id = ARGV[index + 1]
    if redis.call('GET', seat_key) == user_id then
        redis.call('DEL', seat_key)
        redis.call('SREM', user_seats_key, seat_id)
        released = released + 1
    end
end
return released
"""

HOLD_TTL_SECONDS = 300
MAX_HELD_SEATS_PER_SHOW = 6


def hold_key(show_id: int, seat_id: int) -> str:
    return f"hold:{show_id}:{seat_id}"


def user_holds_key(show_id: int, user_id: str) -> str:
    return f"seatlock:user-holds:{show_id}:{user_id}"


async def eval_script(script: str, numkeys: int, *args: str) -> int:
    from collections.abc import Awaitable
    from typing import cast

    result = cast(
        Awaitable[object],
        redis_client.eval(script, numkeys, *args),
    )
    return cast(int, await result)


JOIN_QUEUE_SCRIPT = """
local queue_key = KEYS[1]
local owners_key = KEYS[2]
local user_token_key = KEYS[3]
local sequence_key = KEYS[4]
local rate_key = KEYS[5]
local admitted_key = KEYS[6]
local admitted_owners_key = KEYS[7]
local user_id = ARGV[1]
local token = ARGV[2]
local queue_ttl = tonumber(ARGV[3])
local capacity = tonumber(ARGV[4])
local refill_rate = tonumber(ARGV[5])
local time = redis.call('TIME')
local now = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)
local state = redis.call('HMGET', rate_key, 'tokens', 'updated_at')
local tokens = tonumber(state[1]) or capacity
local updated_at = tonumber(state[2]) or now
tokens = math.min(capacity, tokens + math.max(0, now - updated_at) * refill_rate / 1000)
if tokens < 1 then
    redis.call('HSET', rate_key, 'tokens', tokens, 'updated_at', now)
    return {'rate_limited'}
end
redis.call('HSET', rate_key, 'tokens', tokens - 1, 'updated_at', now)
redis.call('PEXPIRE', rate_key, math.ceil(capacity / refill_rate * 2000))
local existing = redis.call('GET', user_token_key)
if existing then
    if redis.call('SISMEMBER', admitted_key, existing) == 1 then
        return {'admitted', existing, 0}
    end
    local rank = redis.call('ZRANK', queue_key, existing)
    if rank then
        return {'queued', existing, rank + 1}
    end
end
local score = redis.call('INCR', sequence_key)
redis.call('ZADD', queue_key, score, token)
redis.call('HSET', owners_key, token, user_id)
redis.call('SET', user_token_key, token, 'EX', queue_ttl)
return {'queued', token, redis.call('ZCARD', queue_key)}
"""

ADMIT_QUEUE_SCRIPT = """
local queue_key = KEYS[1]
local queue_owners_key = KEYS[2]
local admitted_key = KEYS[3]
local admitted_owners_key = KEYS[4]
local admission_rate_key = KEYS[5]
local rate = tonumber(ARGV[1])
local admitted_ttl = tonumber(ARGV[2])
local time = redis.call('TIME')
local now = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)
local state = redis.call('HMGET', admission_rate_key, 'tokens', 'updated_at')
local tokens = tonumber(state[1]) or rate
local updated_at = tonumber(state[2]) or now
tokens = math.min(rate, tokens + math.max(0, now - updated_at) * rate / 1000)
local count = math.min(math.floor(tokens), redis.call('ZCARD', queue_key))
redis.call('HSET', admission_rate_key, 'tokens', tokens - count, 'updated_at', now)
redis.call('PEXPIRE', admission_rate_key, math.ceil(rate * 2000))
if count <= 0 then
    return 0
end
local next_tokens = redis.call('ZRANGE', queue_key, 0, count - 1)
for _, token in ipairs(next_tokens) do
    local user_id = redis.call('HGET', queue_owners_key, token)
    redis.call('ZREM', queue_key, token)
    redis.call('HDEL', queue_owners_key, token)
    if user_id then
        redis.call('SADD', admitted_key, token)
        redis.call('HSET', admitted_owners_key, token, user_id)
    end
end
redis.call('EXPIRE', admitted_key, admitted_ttl)
redis.call('EXPIRE', admitted_owners_key, admitted_ttl)
return count
"""

QUEUE_STATUS_SCRIPT = """
local token = ARGV[1]
local user_id = ARGV[2]
if redis.call('SISMEMBER', KEYS[2], token) == 1 then
    if redis.call('HGET', KEYS[4], token) == user_id then
        return {'admitted', 0}
    end
    return {'not_found', 0}
end
if redis.call('HGET', KEYS[3], token) ~= user_id then
    return {'not_found', 0}
end
local rank = redis.call('ZRANK', KEYS[1], token)
if rank then
    return {'queued', rank + 1}
end
return {'not_found', 0}
"""

ADMISSION_CHECK_SCRIPT = """
if redis.call('SISMEMBER', KEYS[1], ARGV[1]) == 1
    and redis.call('HGET', KEYS[2], ARGV[1]) == ARGV[2] then
    return 1
end
return 0
"""

QUEUE_TTL_SECONDS = 86_400
ADMISSION_TTL_SECONDS = 600


def queue_keys(show_id: int) -> tuple[str, str, str, str, str, str]:
    prefix = f"seatlock:queue:{show_id}"
    return (
        f"{prefix}:waiting",
        f"{prefix}:waiting-owners",
        f"{prefix}:sequence",
        f"{prefix}:admitted",
        f"{prefix}:admitted-owners",
        f"{prefix}:admission-rate",
    )


def queue_user_token_key(show_id: int, user_id: str) -> str:
    return f"seatlock:queue:{show_id}:user:{user_id}"


def join_rate_key(show_id: int, user_id: str) -> str:
    return f"seatlock:rate:join:{show_id}:{user_id}"


def hold_rate_key(show_id: int, user_id: str) -> str:
    return f"seatlock:rate:hold:{show_id}:{user_id}"


async def eval_raw(script: str, numkeys: int, *args: str) -> object:
    from collections.abc import Awaitable
    from typing import cast

    result = cast(Awaitable[object], redis_client.eval(script, numkeys, *args))
    return await result
