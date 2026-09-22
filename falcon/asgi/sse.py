"""Helpers for orchestrating Server-Sent Events (SSE) connections."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from falcon.asgi_spec import EventType

from .structures import SSEvent

if TYPE_CHECKING:
    from falcon._typing import AsgiReceive
    from falcon._typing import AsgiSend
    from falcon.media import BaseHandler
    from falcon.typing import SSEEmitter

__all__ = ('SSEOptions',)


_HEARTBEAT_BODY: bytes = SSEvent().serialize()


class SSEOptions:
    """Defines a set of configurable Server-Sent Events (SSE) options.

    An instance of this class is exposed via
    :attr:`falcon.asgi.App.sse_options` for configuring the behavior of
    SSE connections.
    """

    __slots__ = [
        'cancel_timeout',
        'heartbeat_interval',
        'terminator',
    ]

    cancel_timeout: float
    """Maximum time, in seconds, to wait for SSE tasks to stop.

    When a client disconnects, all tasks related to the connection are
    cancelled. A task that is blocked on a slow client (i.e., backpressure)
    and that ignores cancellation must not hold up cleanup indefinitely,
    so waiting for the cancelled tasks to finish is bounded by this
    timeout (default ``5.0``).
    """

    heartbeat_interval: float | None
    """Interval, in seconds, between heartbeat frames.

    When no event has been emitted for this long, a heartbeat (a
    comment-only ``ping`` frame) is sent in order to keep the connection
    alive and to surface dead clients. Must be a positive number when
    set. Heartbeats are disabled by default (default ``None``).
    """

    terminator: SSEvent | bytes | None
    """Final event frame to send when the emitter completes normally.

    May be an :class:`~falcon.asgi.SSEvent` or a raw byte string. When
    set, the terminator is sent as the last event frame, just before the
    response body is closed (default ``None``, meaning no terminator
    frame is sent).
    """

    def __init__(self) -> None:
        self.heartbeat_interval = None
        self.terminator = None
        self.cancel_timeout = 5.0


class _SSEConnection:
    """Orchestrates the tasks and frames of a single SSE connection.

    A connection spawns one task per concurrent producer (the event
    emitter itself, plus an optional heartbeat), along with a task
    watching for client disconnects. Frame writes from the concurrent
    producers are serialized through a lock so that frames are always
    written in order, and cleanup is bounded so that a slow client
    exercising backpressure cannot block cancellation indefinitely.

    Note:
        This class is not part of Falcon's public interface, and it may
        change without warning.
    """

    def __init__(
        self,
        emitter: SSEEmitter,
        send: AsgiSend,
        receive: AsgiReceive,
        *,
        json_handler: BaseHandler | None = None,
        options: SSEOptions | None = None,
    ) -> None:
        if options is None:
            options = SSEOptions()

        terminator = options.terminator
        if terminator is None or isinstance(terminator, bytes):
            terminator_body = terminator
        elif isinstance(terminator, SSEvent):
            terminator_body = terminator.serialize(json_handler)
        else:
            raise TypeError(
                'SSEOptions.terminator must be an SSEvent, a byte string, or None'
            )

        self._emitter: SSEEmitter | None = emitter
        self._send: AsgiSend | None = send
        self._receive: AsgiReceive | None = receive
        self._json_handler = json_handler

        self._heartbeat_interval = options.heartbeat_interval
        self._cancel_timeout = options.cancel_timeout
        self._terminator_body = terminator_body

        self._write_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._last_error: BaseException | None = None
        self._disconnected = False

    @property
    def tasks(self) -> frozenset[asyncio.Task[None]]:
        """Snapshot of the tasks spawned for this connection."""
        return frozenset(self._tasks)

    @property
    def last_error(self) -> BaseException | None:
        """The last exception raised while streaming events, if any."""
        return self._last_error

    @property
    def disconnected(self) -> bool:
        """Whether the client disconnected before the stream completed."""
        return self._disconnected

    async def run(self) -> None:
        """Stream events to the client until completion or disconnect."""
        emitter = self._emitter
        send = self._send
        receive = self._receive
        if emitter is None or send is None or receive is None:
            raise RuntimeError('this SSE connection has already been closed')

        producer = asyncio.create_task(
            self._produce(emitter, send), name='falcon.sse.producer'
        )
        watcher = asyncio.create_task(
            self._watch_disconnect(receive), name='falcon.sse.watcher'
        )
        heartbeat = None
        self._tasks = {producer, watcher}

        interval = self._heartbeat_interval
        if interval is not None:
            heartbeat = asyncio.create_task(
                self._heartbeat(send, interval), name='falcon.sse.heartbeat'
            )
            self._tasks.add(heartbeat)

        try:
            done, _ = await asyncio.wait(
                self._tasks, return_when=asyncio.FIRST_COMPLETED
            )

            if producer in done:
                error = producer.exception()
                if error is None:
                    # NOTE: Stop the heartbeat first so that the
                    #   terminator remains the final event frame.
                    if heartbeat is not None:
                        heartbeat.cancel()

                    if self._terminator_body is not None:
                        await send(
                            {
                                'type': EventType.HTTP_RESPONSE_BODY,
                                'body': self._terminator_body,
                                'more_body': True,
                            }
                        )

                    await send({'type': EventType.HTTP_RESPONSE_BODY})
                else:
                    self._last_error = error
            else:
                # NOTE: The disconnect watcher finished first (or,
                #   pathologically, a producer failed while waiting on a
                #   blocked send); the client is gone either way.
                self._disconnected = True
        finally:
            await self._shutdown(producer, emitter)

        if self._last_error is not None:
            raise self._last_error

    async def _produce(self, emitter: SSEEmitter, send: AsgiSend) -> None:
        async for event in emitter:
            if not event:
                event = SSEvent()

            # NOTE: According to the ASGI spec, once the client
            #   disconnects, send() acts as a no-op. The connection
            #   state itself is tracked by the disconnect watcher.
            await self._write_frame(send, event.serialize(self._json_handler))

    async def _heartbeat(self, send: AsgiSend, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            await self._write_frame(send, _HEARTBEAT_BODY)

    async def _watch_disconnect(self, receive: AsgiReceive) -> None:
        while True:
            message = await receive()
            if message['type'] == EventType.HTTP_DISCONNECT:
                return

    async def _write_frame(self, send: AsgiSend, body: bytes) -> None:
        # NOTE: The emitter and the heartbeat act as concurrent
        #   producers; serialize their writes so that event frames are
        #   always sent in order and never interleave.
        async with self._write_lock:
            await send(
                {
                    'type': EventType.HTTP_RESPONSE_BODY,
                    'body': body,
                    'more_body': True,
                }
            )

    async def _shutdown(
        self, producer: asyncio.Task[None], emitter: SSEEmitter
    ) -> None:
        for task in self._tasks:
            task.cancel()

        # NOTE: A send() callable that is blocked on a slow client may
        #   ignore cancellation, so waiting for the cancelled tasks to
        #   finish is bounded in order to not hold up cleanup
        #   indefinitely.
        await asyncio.wait(self._tasks, timeout=self._cancel_timeout)

        # NOTE: Release all references to request-scoped objects so that
        #   nothing leaks across requests even if the connection object
        #   itself is retained.
        self._emitter = None
        self._send = None
        self._receive = None

        # NOTE: Surface (and retrieve) exceptions from any task that
        #   failed while the connection was winding down, e.g., when a
        #   disconnect and an emitter error happen at the same time.
        for task in self._tasks:
            if not task.done() or task.cancelled():
                continue
            ex = task.exception()
            if ex is not None and self._last_error is None:
                self._last_error = ex

        # NOTE: An async generator cannot be closed while it is still
        #   running; if the producer task somehow outlived the bounded
        #   wait above, leave the emitter to the garbage collector.
        if producer.done():
            aclose = getattr(emitter, 'aclose', None)
            if aclose is not None:
                await aclose()
