from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any

from redis.asyncio import Redis
from taskiq import AckableMessage
from taskiq_redis import RedisStreamBroker


logger = logging.getLogger(__name__)


class ReclaimingRedisStreamBroker(RedisStreamBroker):
    """Reclaim stale pending messages even when no new stream entries arrive."""

    async def listen(self) -> AsyncGenerator[AckableMessage, None]:
        async with Redis(connection_pool=self.connection_pool) as redis_conn:
            while True:
                for stream in [self.queue_name, *self.additional_streams.keys()]:
                    for msg_id, message in await self._reclaim_pending(redis_conn, stream):
                        yield AckableMessage(
                            data=message[b"data"],
                            ack=self._ack_generator(id=msg_id, queue_name=stream),
                        )

                fetched = await redis_conn.xreadgroup(
                    self.consumer_group_name,
                    self.consumer_name,
                    {
                        self.queue_name: ">",
                        **self.additional_streams,
                    },
                    block=self.block,
                    noack=False,
                    count=self.count,
                )
                for stream, messages in fetched or []:
                    for msg_id, message in messages:
                        yield AckableMessage(
                            data=message[b"data"],
                            ack=self._ack_generator(id=msg_id, queue_name=stream),
                        )

    async def _reclaim_pending(
        self,
        redis_conn: Any,
        stream: str,
    ) -> list[tuple[str, dict[bytes, bytes]]]:
        lock = redis_conn.lock(
            f"autoclaim:{self.consumer_group_name}:{stream}",
            timeout=self.unacknowledged_lock_timeout,
        )
        if not await lock.acquire(blocking=False):
            return []
        try:
            result = await redis_conn.xautoclaim(
                name=stream,
                groupname=self.consumer_group_name,
                consumername=self.consumer_name,
                min_idle_time=self.idle_timeout,
                count=self.unacknowledged_batch_size,
            )
            return result[1]
        finally:
            try:
                await lock.release()
            except Exception:
                logger.warning(
                    "Could not release pending-message reclaim lock for %s",
                    stream,
                    exc_info=True,
                )
