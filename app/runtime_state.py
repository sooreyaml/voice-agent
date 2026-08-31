"""Shared, short-lived runtime coordination.

Redis is used in deployed environments so webhook idempotency, API-key limits,
and active-call heartbeats remain correct across API replicas. Development uses
the same interface with an in-memory implementation and no external service.
Durable queues and business data continue to live in PostgreSQL.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from typing import Any, Protocol

from .settings import Settings

WEBHOOK_DEDUPE_TTL_SECONDS = 24 * 60 * 60
WEBHOOK_CLAIM_TTL_SECONDS = 60
ACTIVE_CALL_TTL_SECONDS = 45
RATE_WINDOW_SECONDS = 60
KEY_PREFIX = "callagent"


class RuntimeState(Protocol):
    backend: str

    def claim_webhook(self, delivery_id: str) -> bool: ...

    def complete_webhook(self, delivery_id: str) -> None: ...

    def release_webhook(self, delivery_id: str) -> None: ...

    def check_api_key_rate_limit(self, key_id: str, limit: int) -> int | None: ...

    def register_call(self, call_id: str, organization_id: str) -> None: ...

    def heartbeat_call(self, call_id: str, organization_id: str) -> None: ...

    def finish_call(self, call_id: str) -> None: ...

    def active_call_count(self) -> int: ...

    def close(self) -> None: ...


class MemoryRuntimeState:
    """Single-process development implementation with Redis-equivalent rules."""

    backend = "memory"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._webhooks: dict[str, float] = {}
        self._rate_windows: dict[tuple[str, int], int] = {}
        self._calls: dict[str, tuple[str, float]] = {}

    def claim_webhook(self, delivery_id: str) -> bool:
        now = time.time()
        with self._lock:
            self._webhooks = {
                key: expires for key, expires in self._webhooks.items() if expires > now
            }
            if delivery_id in self._webhooks:
                return False
            self._webhooks[delivery_id] = now + WEBHOOK_CLAIM_TTL_SECONDS
            return True

    def complete_webhook(self, delivery_id: str) -> None:
        with self._lock:
            self._webhooks[delivery_id] = time.time() + WEBHOOK_DEDUPE_TTL_SECONDS

    def release_webhook(self, delivery_id: str) -> None:
        with self._lock:
            self._webhooks.pop(delivery_id, None)

    def check_api_key_rate_limit(self, key_id: str, limit: int) -> int | None:
        if limit <= 0:
            return None
        now = int(time.time())
        window = now // RATE_WINDOW_SECONDS
        window_key = (key_id, window)
        with self._lock:
            self._rate_windows = {
                key: count
                for key, count in self._rate_windows.items()
                if key[1] >= window
            }
            count = self._rate_windows.get(window_key, 0) + 1
            self._rate_windows[window_key] = count
        if count > limit:
            return RATE_WINDOW_SECONDS - (now % RATE_WINDOW_SECONDS)
        return None

    def register_call(self, call_id: str, organization_id: str) -> None:
        self.heartbeat_call(call_id, organization_id)

    def heartbeat_call(self, call_id: str, organization_id: str) -> None:
        with self._lock:
            self._calls[call_id] = (
                organization_id,
                time.time() + ACTIVE_CALL_TTL_SECONDS,
            )

    def finish_call(self, call_id: str) -> None:
        with self._lock:
            self._calls.pop(call_id, None)

    def active_call_count(self) -> int:
        now = time.time()
        with self._lock:
            self._calls = {
                key: value for key, value in self._calls.items() if value[1] > now
            }
            return len(self._calls)

    def close(self) -> None:
        return None


class RedisRuntimeState:
    backend = "redis"

    def __init__(self, client: Any) -> None:
        self._client = client
        self._instance_id = f"{socket.gethostname()}:{os.getpid()}"

    def claim_webhook(self, delivery_id: str) -> bool:
        return bool(
            self._client.set(
                f"{KEY_PREFIX}:webhook:{delivery_id}",
                self._instance_id,
                ex=WEBHOOK_CLAIM_TTL_SECONDS,
                nx=True,
            )
        )

    def complete_webhook(self, delivery_id: str) -> None:
        self._client.set(
            f"{KEY_PREFIX}:webhook:{delivery_id}",
            self._instance_id,
            ex=WEBHOOK_DEDUPE_TTL_SECONDS,
            xx=True,
        )

    def release_webhook(self, delivery_id: str) -> None:
        self._client.delete(f"{KEY_PREFIX}:webhook:{delivery_id}")

    def check_api_key_rate_limit(self, key_id: str, limit: int) -> int | None:
        if limit <= 0:
            return None
        now = int(time.time())
        window = now // RATE_WINDOW_SECONDS
        key = f"{KEY_PREFIX}:api-rate:{key_id}:{window}"
        pipe = self._client.pipeline(transaction=True)
        pipe.incr(key)
        pipe.expire(key, RATE_WINDOW_SECONDS * 2)
        count, _ = pipe.execute()
        if int(count) > limit:
            return RATE_WINDOW_SECONDS - (now % RATE_WINDOW_SECONDS)
        return None

    def register_call(self, call_id: str, organization_id: str) -> None:
        self.heartbeat_call(call_id, organization_id)

    def heartbeat_call(self, call_id: str, organization_id: str) -> None:
        now = int(time.time())
        expires_at = now + ACTIVE_CALL_TTL_SECONDS
        details = json.dumps(
            {
                "organization_id": organization_id,
                "instance_id": self._instance_id,
                "heartbeat_at": now,
            }
        )
        pipe = self._client.pipeline(transaction=True)
        pipe.zadd(f"{KEY_PREFIX}:active-calls", {call_id: expires_at})
        pipe.set(
            f"{KEY_PREFIX}:active-call:{call_id}",
            details,
            ex=ACTIVE_CALL_TTL_SECONDS,
        )
        pipe.execute()

    def finish_call(self, call_id: str) -> None:
        pipe = self._client.pipeline(transaction=True)
        pipe.zrem(f"{KEY_PREFIX}:active-calls", call_id)
        pipe.delete(f"{KEY_PREFIX}:active-call:{call_id}")
        pipe.execute()

    def active_call_count(self) -> int:
        key = f"{KEY_PREFIX}:active-calls"
        pipe = self._client.pipeline(transaction=True)
        pipe.zremrangebyscore(key, "-inf", time.time())
        pipe.zcard(key)
        _, count = pipe.execute()
        return int(count)

    def close(self) -> None:
        self._client.close()


def build_runtime_state(settings: Settings) -> RuntimeState:
    if not settings.redis_url:
        return MemoryRuntimeState()

    from redis import Redis

    client = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
        health_check_interval=30,
    )
    client.ping()
    return RedisRuntimeState(client)
