"""Who is listening: the open streams of each user, and delivering an event to that user's streams only."""

import asyncio
from collections import defaultdict


class TooManyStreams(Exception):
    pass


class Hub:
    """Kept in memory: run the service as a single process, or the streams of one process miss the
    events another one receives."""

    # Events waiting for a slow browser; beyond this the oldest are dropped instead of using memory.
    QUEUE_SIZE = 100

    def __init__(self, max_per_user: int, max_total: int):
        self.max_per_user = max_per_user
        self.max_total = max_total
        self._streams: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def open(self, email: str) -> asyncio.Queue:
        if self.count() >= self.max_total or len(self._streams[email]) >= self.max_per_user:
            raise TooManyStreams()
        queue: asyncio.Queue = asyncio.Queue(self.QUEUE_SIZE)
        self._streams[email].add(queue)
        return queue

    def close(self, email: str, queue: asyncio.Queue) -> None:
        streams = self._streams.get(email)
        if streams is None:
            return
        streams.discard(queue)
        if not streams:
            del self._streams[email]

    def publish(self, email: str, kind: str, event: dict) -> int:
        """Puts the event on every stream of that user; returns how many got it."""
        streams = self._streams.get(email, set())
        for queue in streams:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait((kind, event))
        return len(streams)

    def count(self, email: str | None = None) -> int:
        if email is not None:
            return len(self._streams.get(email, ()))
        return sum(len(streams) for streams in self._streams.values())
