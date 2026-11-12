"""Deterministic concurrency tests for :meth:`falcon.asgi.Request.get_media`.

The interleave points are driven by :class:`asyncio.Event` objects rather than
timers, so these tests do not rely on real-world delays to schedule tasks in a
particular order.
"""

import asyncio

import pytest

import falcon
from falcon import media
from falcon import testing
from falcon.asgi import Request

_MEDIA_TYPE = 'application/x-pausing'
_BODY = b'hello world'
_FIRST_CHUNK = _BODY[:4]
_SECOND_CHUNK = _BODY[4:]


class PausingHandler(media.BaseHandler):
    """Media handler that suspends in the middle of reading the body."""

    def __init__(self, *, should_fail: bool = False) -> None:
        self.calls = 0
        self.should_fail = should_fail
        self.entered = asyncio.Event()
        self.proceed = asyncio.Event()

    def serialize(self, media: object, content_type: str) -> bytes:
        return b''

    async def deserialize_async(self, stream, content_type, content_length):
        self.calls += 1
        first = await stream.read(len(_FIRST_CHUNK))
        # Suspend with the stream half-consumed so that concurrent callers and
        # cancellation can be exercised at a well-defined interleave point.
        self.entered.set()
        await self.proceed.wait()
        rest = await stream.read()
        data = first + rest

        if self.should_fail:
            raise falcon.MediaMalformedError(_MEDIA_TYPE)
        return data


def _create_request(handler: media.BaseHandler, *, body: bytes = _BODY) -> Request:
    scope = testing.create_scope(
        headers={'Content-Type': _MEDIA_TYPE},
        content_length=len(body),
    )
    events = [
        {'type': 'http.request', 'body': _FIRST_CHUNK, 'more_body': True},
        {'type': 'http.request', 'body': _SECOND_CHUNK, 'more_body': False},
    ]

    async def receive():
        return events.pop(0)

    options = falcon.RequestOptions()
    options.media_handlers[_MEDIA_TYPE] = handler
    return Request(scope, receive, options=options)


async def test_concurrent_readers_share_single_deserialization():
    handler = PausingHandler()
    req = _create_request(handler)

    first = asyncio.create_task(req.get_media())
    await handler.entered.wait()

    second = asyncio.create_task(req.get_media())
    await asyncio.sleep(0)
    assert not first.done() and not second.done()

    handler.proceed.set()
    first_media, second_media = await asyncio.gather(first, second)

    assert first_media == _BODY
    assert second_media is first_media
    assert handler.calls == 1

    # The cached object keeps being returned without another parse.
    assert await req.get_media() is first_media
    assert handler.calls == 1
    assert req.stream.eof


async def test_concurrent_readers_observe_same_parse_failure():
    handler = PausingHandler(should_fail=True)
    req = _create_request(handler)

    first = asyncio.create_task(req.get_media())
    await handler.entered.wait()

    second = asyncio.create_task(req.get_media())
    await asyncio.sleep(0)

    handler.proceed.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)

    assert all(isinstance(ex, falcon.MediaMalformedError) for ex in outcomes)
    assert outcomes[0] is outcomes[1]
    assert handler.calls == 1

    # The failure is cached: later callers observe the same exception object.
    with pytest.raises(falcon.MediaMalformedError) as info:
        await req.get_media()
    assert info.value is outcomes[0]
    assert handler.calls == 1


async def test_cancelling_one_waiter_keeps_full_body_for_other():
    handler = PausingHandler()
    req = _create_request(handler)

    first = asyncio.create_task(req.get_media())
    await handler.entered.wait()

    second = asyncio.create_task(req.get_media())
    await asyncio.sleep(0)

    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second

    # The shared parse must keep running despite the cancelled waiter.
    assert not first.done()

    handler.proceed.set()
    first_media = await first
    assert first_media == _BODY
    assert handler.calls == 1

    # A reader arriving after the fact gets the complete, cached body rather
    # than an empty or partially read stream.
    assert await req.get_media() is first_media
    assert req.stream.eof


async def test_cancelling_all_waiters_does_not_corrupt_cached_outcome():
    handler = PausingHandler(should_fail=True)
    req = _create_request(handler)

    first = asyncio.create_task(req.get_media())
    await handler.entered.wait()
    second = asyncio.create_task(req.get_media())
    await asyncio.sleep(0)

    first.cancel()
    second.cancel()
    for task in (first, second):
        with pytest.raises(asyncio.CancelledError):
            await task

    # The shielded task finishes the single parse on its own; its failure must
    # still become observable instead of being lost or producing an empty body.
    handler.proceed.set()
    for _ in range(3):
        await asyncio.sleep(0)

    assert req._media_task.done() and not req._media_task.cancelled()
    with pytest.raises(falcon.MediaMalformedError):
        await req.get_media()
    assert handler.calls == 1


async def test_default_when_empty_is_per_call_on_cached_not_found():
    scope = testing.create_scope(headers={'Content-Type': falcon.MEDIA_JSON})

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    req = Request(scope, receive)

    first_default, second_default = await asyncio.gather(
        req.get_media('first'), req.get_media('second')
    )
    assert first_default == 'first'
    assert second_default == 'second'

    # The underlying MediaNotFoundError remains cached and re-raised when no
    # default is requested.
    with pytest.raises(falcon.MediaNotFoundError):
        await req.get_media()

    # And a later call may still supply its own default.
    assert await req.get_media('third') == 'third'


async def test_aclose_media_task_tears_down_inflight_parse():
    handler = PausingHandler()
    req = _create_request(handler)

    reader = asyncio.create_task(req.get_media())
    await handler.entered.wait()

    # Simulate the ASGI App cancelling the request and running its request
    # teardown while get_media() is blocked reading the body.
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader

    task = req._media_task
    await req.aclose_media_task()

    assert task.done() and task.cancelled()


async def test_aclose_media_task_is_a_noop_without_or_after_parse():
    scope = testing.create_scope(headers={'Content-Type': falcon.MEDIA_JSON})

    async def receive():
        return {'type': 'http.request', 'body': b'{"ok": true}', 'more_body': False}

    req = Request(scope, receive)

    # No parsing task has been created yet.
    assert req._media_task is None
    await req.aclose_media_task()

    media_object = await req.get_media()
    assert media_object == {'ok': True}

    # Once the task is done, closing is again a no-op.
    assert req._media_task.done()
    await req.aclose_media_task()


async def test_late_caller_rechecks_cached_outcome_of_finished_task():
    scope = testing.create_scope(headers={'Content-Type': falcon.MEDIA_JSON})

    async def receive():
        return {'type': 'http.request', 'body': b'{"ok": true}', 'more_body': False}

    req = Request(scope, receive)
    first = await req.get_media()
    assert req._media_task.done()

    # The task is already finished; the done() recheck path returns the cached
    # object without creating or awaiting anything new.
    assert await req.get_media() is first


async def test_late_caller_rechecks_cached_not_found_with_own_default():
    scope = testing.create_scope(headers={'Content-Type': falcon.MEDIA_JSON})

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    req = Request(scope, receive)
    assert await req.get_media('first') == 'first'
    assert req._media_task.done()

    # A later call goes through the done() recheck and still gets its own
    # default for the cached empty-body error.
    assert await req.get_media('second') == 'second'
    with pytest.raises(falcon.MediaNotFoundError):
        await req.get_media()


async def test_concurrent_sync_handler_shares_single_deserialization():
    # The default JSON handler uses the optimized synchronous
    # _deserialize_sync protocol, which reads the whole stream first.
    scope = testing.create_scope(
        headers={'Content-Type': falcon.MEDIA_JSON}, content_length=15
    )
    events = [
        {'type': 'http.request', 'body': b'{"a":', 'more_body': True},
        {'type': 'http.request', 'body': b' 1}', 'more_body': False},
    ]

    async def receive():
        return events.pop(0)

    req = Request(scope, receive)
    results = await asyncio.gather(
        req.get_media(), req.get_media(), req.get_media()
    )

    assert results == [{'a': 1}, {'a': 1}, {'a': 1}]
    assert results[0] is results[1] is results[2]
    assert req.stream.eof


async def test_concurrent_handler_with_exhaust_stream_drains_body():
    class PartialHandler(media.BaseHandler):
        exhaust_stream = True

        def serialize(self, media: object, content_type: str) -> bytes:
            return b''

        async def deserialize_async(self, stream, content_type, content_length):
            return await stream.read(2)

    scope = testing.create_scope(
        headers={'Content-Type': _MEDIA_TYPE}, content_length=len(_BODY)
    )
    events = [
        {'type': 'http.request', 'body': _FIRST_CHUNK, 'more_body': True},
        {'type': 'http.request', 'body': _SECOND_CHUNK, 'more_body': False},
    ]

    async def receive():
        return events.pop(0)

    options = falcon.RequestOptions()
    options.media_handlers[_MEDIA_TYPE] = PartialHandler()
    req = Request(scope, receive, options=options)

    first, second = await asyncio.gather(req.get_media(), req.get_media())
    assert first == b'he' and second is first
    assert req.stream.eof


async def test_app_teardown_cancels_inflight_media_task():
    handler = PausingHandler()
    captured = {}

    class MediaResource:
        async def on_post(self, req, resp):
            captured['req'] = req
            await req.get_media()

    app = falcon.asgi.App()
    app.add_route('/', MediaResource())
    app.req_options.media_handlers[_MEDIA_TYPE] = handler

    scope = testing.create_scope(
        method='POST',
        headers={'Content-Type': _MEDIA_TYPE},
        content_length=len(_BODY),
    )
    events = [
        {'type': 'http.request', 'body': _FIRST_CHUNK, 'more_body': True},
        {'type': 'http.request', 'body': _SECOND_CHUNK, 'more_body': False},
    ]

    async def receive():
        return events.pop(0)

    collect = testing.ASGIResponseEventCollector()

    request_task = asyncio.create_task(app(scope, receive, collect))
    await handler.entered.wait()

    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task

    assert request_task.cancelled()
    assert handler.calls == 1

    # The App finally-block must have fully drained the parsing task so that
    # no task is left blocked reading the request body. Depending on how far
    # the cancellation propagated, the parse either completed or was
    # cancelled; either way the task must be done rather than still pending.
    media_task = captured['req']._media_task
    assert media_task.done()
    if not media_task.cancelled():
        media_task.exception()
