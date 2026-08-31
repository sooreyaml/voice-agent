from __future__ import annotations

from app.runtime_state import MemoryRuntimeState, RedisRuntimeState


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.operations = []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.operations.append((name, args, kwargs))
            return self

        return queue

    def execute(self):
        return [
            getattr(self.redis, name)(*args, **kwargs)
            for name, args, kwargs in self.operations
        ]


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.zsets = {}

    def set(self, key, value, *, ex=None, nx=False, xx=False):
        if nx and key in self.values:
            return False
        if xx and key not in self.values:
            return False
        self.values[key] = value
        return True

    def delete(self, key):
        return int(self.values.pop(key, None) is not None)

    def incr(self, key):
        value = int(self.values.get(key, 0)) + 1
        self.values[key] = value
        return value

    def expire(self, _key, _seconds):
        return True

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)
        return len(mapping)

    def zrem(self, key, member):
        return int(self.zsets.setdefault(key, {}).pop(member, None) is not None)

    def zremrangebyscore(self, key, _minimum, maximum):
        members = self.zsets.setdefault(key, {})
        removed = [
            member for member, score in members.items() if score <= float(maximum)
        ]
        for member in removed:
            members.pop(member)
        return len(removed)

    def zcard(self, key):
        return len(self.zsets.setdefault(key, {}))

    def pipeline(self, transaction=True):
        assert transaction is True
        return FakePipeline(self)

    def close(self):
        return None


def _exercise_shared_state(first, second):
    assert first.claim_webhook("delivery-1") is True
    assert second.claim_webhook("delivery-1") is False
    first.release_webhook("delivery-1")
    assert second.claim_webhook("delivery-1") is True
    second.complete_webhook("delivery-1")
    assert first.claim_webhook("delivery-1") is False

    assert first.check_api_key_rate_limit("key-1", 2) is None
    assert second.check_api_key_rate_limit("key-1", 2) is None
    assert first.check_api_key_rate_limit("key-1", 2) is not None

    first.register_call("call-1", "org-1")
    assert second.active_call_count() == 1
    second.heartbeat_call("call-1", "org-1")
    first.finish_call("call-1")
    assert second.active_call_count() == 0


def test_memory_runtime_state_contract():
    state = MemoryRuntimeState()
    _exercise_shared_state(state, state)


def test_redis_runtime_state_is_shared_between_replicas():
    redis = FakeRedis()
    _exercise_shared_state(RedisRuntimeState(redis), RedisRuntimeState(redis))
