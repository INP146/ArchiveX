import asyncio

from archivex import queue_broker


class FakeLock:
    def __init__(self) -> None:
        self.released = False

    async def acquire(self, *, blocking: bool) -> bool:
        assert blocking is False
        return True

    async def release(self) -> None:
        self.released = True


class FakeRedis:
    instances = []

    def __init__(self, *, connection_pool) -> None:
        self.connection_pool = connection_pool
        self.lock_instance = FakeLock()
        self.read_calls = 0
        self.claim_calls = 0
        self.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None

    def lock(self, name, *, timeout):
        assert name == "autoclaim:archivex-workers:archivex:media"
        assert timeout == 30
        return self.lock_instance

    async def xautoclaim(self, **kwargs):
        self.claim_calls += 1
        assert kwargs == {
            "name": "archivex:media",
            "groupname": "archivex-workers",
            "consumername": "worker-1",
            "min_idle_time": 360_000,
            "count": 100,
        }
        if self.claim_calls == 1:
            return ["0-0", [("1-0", {b"data": b"stale-task"})], []]
        return ["0-0", [], []]

    async def xreadgroup(self, *args, **kwargs):
        self.read_calls += 1
        return []


def test_broker_reclaims_pending_message_before_waiting_for_new_entries(monkeypatch) -> None:
    FakeRedis.instances = []
    monkeypatch.setattr(queue_broker, "Redis", FakeRedis)
    broker = queue_broker.ReclaimingRedisStreamBroker(
        "redis://example.test/0",
        queue_name="archivex:media",
        consumer_group_name="archivex-workers",
        consumer_name="worker-1",
        idle_timeout=360_000,
        unacknowledged_lock_timeout=30,
    )

    async def receive_one():
        listener = broker.listen()
        message = await anext(listener)
        await listener.aclose()
        return message

    message = asyncio.run(receive_one())

    redis = FakeRedis.instances[0]
    assert message.data == b"stale-task"
    assert redis.claim_calls == 1
    assert redis.read_calls == 0
    assert redis.lock_instance.released is True
