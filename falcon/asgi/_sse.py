"""Helpers for streaming Server-Sent Events (SSE) to the client."""

from __future__ import annotations

import asyncio
from typing import Any

from falcon._typing import AsgiReceive
from falcon._typing import AsgiSend
from falcon.asgi_spec import EventType
from falcon.media import BaseHandler
from falcon.typing import SSEEmitter

from .structures import SSEvent

__all__ = ()

# NOTE: Default upper bound on the number of serialized event frames that
#   may be buffered between the emitter and the ASGI server. Once the queue
#   is full, the emitter is paused until the client catches up, providing
#   backpressure instead of unbounded buffering.
QUEUE_MAXSIZE = 8

# NOTE: Default upper bound, in seconds, on how long request teardown may
#   wait for cancelled SSE tasks and for the emitter to close. This ensures
#   that a slow client or a stubborn emitter cannot block the cancellation
#   path indefinitely.
CLEANUP_TIMEOUT = 5.0

# NOTE: Pre-serialized "ping" comment frame, used both for falsy events
#   yielded by the emitter and for heartbeat keepalives.
_PING = SSEvent().serialize()

# NOTE: Sentinel enqueued by the producer to signal that the emitter has
#   been exhausted and that all remaining frames have been queued.
_EXHAUSTED = object()


async def pump_events(  # noqa: C901
    send: AsgiSend,
    receive: AsgiReceive,
    emitter: SSEEmitter,
    sse_handler: BaseHandler | None,
    *,
    queue_maxsize: int,
    cleanup_timeout: float,
    terminal_frame: bytes | None = None,
    heartbeat_interval: float | None = None,
) -> None:
    """Stream serialized SSE frames to the client until completion.

    The emitter is pumped by a dedicated producer task, while a single
    writer task sends the queued frames to the client in the order they
    were produced. An optional heartbeat task enqueues "ping" frames
    whenever the emitter has been idle for too long, and a watcher task
    waits for the client to disconnect. Because all frames pass through a
    single bounded queue to a single writer, concurrent producers can
    never interleave bytes or write frames out of order.

    When the client disconnects, or when one of the tasks fails, all
    remaining tasks are cancelled and awaited, but only up to
    ``cleanup_timeout`` seconds, so that a slow client or a stubborn
    emitter cannot block request teardown indefinitely. The emitter is
    then closed exactly once (when it provides an ``aclose()`` method).

    Args:
        send: ASGI send callable.
        receive: ASGI receive callable.
        emitter: Async iterator or generator yielding
            :class:`~falcon.asgi.SSEvent` instances (or ``None`` to emit
            a "ping" comment frame).
        sse_handler: Media handler to use for serializing events to JSON,
            or ``None`` to use the default handler.

    Keyword Args:
        terminal_frame (bytes): Pre-serialized frame to send after the
            emitter has been exhausted normally, just before the final
            empty body chunk (default ``None``, meaning no terminal frame
            is sent). The terminal frame is not sent when the client
            disconnects or an error occurs.
        heartbeat_interval (float): Interval, in seconds, after which a
            "ping" frame is enqueued if the emitter has not produced any
            event in the meantime (default ``None``, meaning no heartbeat
            frames are emitted).
        queue_maxsize (int): Maximum number of serialized frames to buffer
            between the producers and the writer.
        cleanup_timeout (float): Maximum number of seconds to wait for
            cancelled tasks and for the emitter to close during teardown.

    Raises:
        Exception: Any error raised by the emitter, ``receive()``, or
            ``send()`` is re-raised once all related tasks have been
            stopped.
    """
    queue: asyncio.Queue[Any] = asyncio.Queue(queue_maxsize)
    activity = asyncio.Event()

    async def watch_disconnect() -> None:
        while True:
            message = await receive()
            if message['type'] == EventType.HTTP_DISCONNECT:
                return

    async def produce() -> None:
        async for event in emitter:
            frame = _PING if not event else event.serialize(sse_handler)
            await queue.put(frame)
            activity.set()

        if terminal_frame is not None:
            await queue.put(terminal_frame)

        await queue.put(_EXHAUSTED)

    async def heartbeat() -> None:
        while True:
            try:
                await asyncio.wait_for(activity.wait(), heartbeat_interval)
                activity.clear()
            except asyncio.TimeoutError:
                await queue.put(_PING)

    async def write() -> None:
        while True:
            frame = await queue.get()
            if frame is _EXHAUSTED:
                return

            await send(
                {
                    'type': EventType.HTTP_RESPONSE_BODY,
                    'body': frame,
                    'more_body': True,
                }
            )

    producer = asyncio.create_task(produce())
    writer = asyncio.create_task(write())
    watcher = asyncio.create_task(watch_disconnect())
    tasks = [producer, writer, watcher]
    if heartbeat_interval:
        tasks.append(asyncio.create_task(heartbeat()))

    error: BaseException | None = None
    disconnected = False

    try:
        pending = set(tasks)
        while True:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )

            # NOTE: Iterate in a deterministic order so that when several
            #   tasks complete together, an emitter error takes precedence
            #   over, say, a send() error.
            for task in tasks:
                if task in done:
                    ex = task.exception()
                    if ex is not None:
                        error = ex
                        break

            if error is not None or watcher in done:
                disconnected = error is None
                break

            if writer in done:
                # NOTE: The writer only returns after consuming the
                #   sentinel, i.e., once all queued frames (including the
                #   terminal frame, if any) have been sent.
                break

            # NOTE: Otherwise, the producer simply finished enqueueing all
            #   frames; keep waiting for the writer to drain the queue or
            #   for the client to disconnect.
    finally:
        for task in tasks:
            task.cancel()

        # NOTE: Wait for the cancelled tasks to finish, but only up to a
        #   point: a slow client or a stubborn emitter must not be able to
        #   block request teardown indefinitely.
        await asyncio.wait(tasks, timeout=cleanup_timeout)

        # NOTE: Close the emitter exactly once so that it can release any
        #   resources it is holding. When the emitter is an async
        #   generator that has already been exhausted (or that propagated
        #   an error), aclose() is effectively a no-op.
        aclose = getattr(emitter, 'aclose', None)
        if aclose is not None:
            try:
                await asyncio.wait_for(aclose(), cleanup_timeout)
            except Exception:
                # NOTE: Teardown is best-effort; the original outcome
                #   (normal completion, disconnect, or error) takes
                #   precedence over cleanup errors.
                pass

    if error is not None:
        raise error

    if not disconnected:
        await send({'type': EventType.HTTP_RESPONSE_BODY})
