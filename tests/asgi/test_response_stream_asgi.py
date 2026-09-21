# Regression tests for the ASGI response stream lifecycle: the stream
# must be closed according to the protocol it supports, exactly once,
# whether the response completes normally, iterating or sending fails,
# or the request task is cancelled.

import asyncio

import pytest

from falcon.asgi import App


class SendError(Exception):
    pass


class CloseError(Exception):
    pass


class IterationError(Exception):
    pass


def make_scope():
    return {
        'type': 'http',
        'asgi': {'version': '3.0', 'spec_version': '2.3'},
        'http_version': '1.1',
        'method': 'GET',
        'scheme': 'http',
        'path': '/',
        'query_string': b'',
        'root_path': '',
        'headers': [],
        'client': ('127.0.0.1', 52742),
        'server': ('127.0.0.1', 8000),
    }


async def receive():
    return {'type': 'http.request', 'body': b'', 'more_body': False}


class SendRecorder:
    """ASGI send callable that records events and can simulate failures."""

    def __init__(self, fail_on=None):
        self.events = []
        self._fail_on = fail_on

    async def __call__(self, message):
        self.events.append(message)
        if self._fail_on is not None and self._fail_on(message):
            raise SendError('simulated client disconnect')

    @property
    def body_events(self):
        return [m for m in self.events if m['type'] == 'http.response.body']

    @property
    def eof_count(self):
        return sum(1 for m in self.body_events if not m.get('more_body', False))

    @property
    def body(self):
        return b''.join(m.get('body', b'') for m in self.body_events)


class StreamState:
    """Observable finalization state shared with a stream object."""

    def __init__(self):
        self.close_count = 0
        self.closed = asyncio.Event()

    def finalize(self):
        self.close_count += 1
        self.closed.set()


def observable_generator(state, chunks=(b'one', b'two'), error=None):
    """Create an async generator with an observable finally block."""

    async def gen():
        try:
            for chunk in chunks:
                yield chunk
            if error is not None:
                raise error
        finally:
            state.finalize()

    return gen()


class ObservableAsyncIterator:
    """Async iterator with an observable, optionally failing aclose()."""

    def __init__(self, chunks=(b'one', b'two'), close_error=None):
        self._chunks = list(chunks)
        self.close_error = close_error
        self.state = StreamState()
        self.anext_count = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.anext_count += 1
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def aclose(self):
        self.state.finalize()
        if self.close_error is not None:
            raise self.close_error


class BareAsyncIterator:
    """Async iterator without any closing protocol."""

    def __init__(self, chunks=(b'one', b'two')):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class ObservableAsyncFile:
    """Async file-like object with an observable close()."""

    def __init__(self, chunks=(b'one', b'two')):
        self._chunks = list(chunks)
        self.state = StreamState()

    async def read(self, size=-1):
        if not self._chunks:
            return b''
        return self._chunks.pop(0)

    async def close(self):
        self.state.finalize()


def make_app(stream):
    class Resource:
        async def on_get(self, req, resp):
            resp.stream = stream

    app = App()
    app.add_route('/', Resource())
    return app


async def test_async_generator_closed_on_normal_completion():
    state = StreamState()
    app = make_app(observable_generator(state))
    send = SendRecorder()

    await app(make_scope(), receive, send)

    assert state.close_count == 1
    assert send.body == b'onetwo'
    assert send.eof_count == 1


async def test_async_generator_closed_on_disconnect():
    state = StreamState()
    app = make_app(observable_generator(state))
    send = SendRecorder(fail_on=lambda m: m['type'] == 'http.response.body')

    with pytest.raises(SendError):
        await app(make_scope(), receive, send)

    assert state.close_count == 1
    assert send.eof_count == 0


async def test_async_generator_closed_when_iteration_raises():
    state = StreamState()
    stream = observable_generator(state, error=IterationError('boom'))
    app = make_app(stream)
    send = SendRecorder()

    with pytest.raises(IterationError):
        await app(make_scope(), receive, send)

    assert state.close_count == 1
    assert send.eof_count == 0


async def test_async_generator_closed_on_cancellation():
    state = StreamState()
    app = make_app(observable_generator(state))

    started = asyncio.Event()
    gate = asyncio.Event()

    async def blocking_send(message):
        if message['type'] == 'http.response.body':
            started.set()
            await gate.wait()

    task = asyncio.create_task(app(make_scope(), receive, blocking_send))
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert state.close_count == 1


async def test_async_iterator_closed_on_normal_completion():
    stream = ObservableAsyncIterator()
    app = make_app(stream)
    send = SendRecorder()

    await app(make_scope(), receive, send)

    assert stream.state.close_count == 1
    assert send.body == b'onetwo'
    assert send.eof_count == 1


async def test_async_iterator_closed_before_first_iteration():
    # NOTE: The stream must be closed even when sending the response
    #   start event fails before the stream was ever iterated over.
    stream = ObservableAsyncIterator()
    app = make_app(stream)
    send = SendRecorder(fail_on=lambda m: m['type'] == 'http.response.start')

    with pytest.raises(SendError):
        await app(make_scope(), receive, send)

    assert stream.anext_count == 0
    assert stream.state.close_count == 1
    assert send.eof_count == 0


async def test_async_iterator_without_close_protocol():
    stream = BareAsyncIterator()
    app = make_app(stream)
    send = SendRecorder()

    await app(make_scope(), receive, send)

    assert send.body == b'onetwo'
    assert send.eof_count == 1


async def test_file_like_stream_closed_on_normal_completion():
    stream = ObservableAsyncFile()
    app = make_app(stream)
    send = SendRecorder()

    await app(make_scope(), receive, send)

    assert stream.state.close_count == 1
    assert send.body == b'onetwo'
    assert send.eof_count == 1


async def test_file_like_stream_closed_on_disconnect():
    stream = ObservableAsyncFile()
    app = make_app(stream)
    send = SendRecorder(fail_on=lambda m: m['type'] == 'http.response.body')

    with pytest.raises(SendError):
        await app(make_scope(), receive, send)

    assert stream.state.close_count == 1
    assert send.eof_count == 0


async def test_close_error_suppressed_when_send_fails():
    stream = ObservableAsyncIterator(close_error=CloseError('close boom'))
    app = make_app(stream)
    send = SendRecorder(fail_on=lambda m: m['type'] == 'http.response.body')

    with pytest.raises(SendError):
        await app(make_scope(), receive, send)

    assert stream.state.close_count == 1
    assert send.eof_count == 0


async def test_close_error_suppressed_when_iteration_raises():
    class FaultyIterator(ObservableAsyncIterator):
        async def __anext__(self):
            raise IterationError('iter boom')

    stream = FaultyIterator(close_error=CloseError('close boom'))
    app = make_app(stream)
    send = SendRecorder()

    with pytest.raises(IterationError):
        await app(make_scope(), receive, send)

    assert stream.state.close_count == 1
    assert send.eof_count == 0


async def test_close_error_suppressed_when_cancelled():
    stream = ObservableAsyncIterator(close_error=CloseError('close boom'))
    app = make_app(stream)

    started = asyncio.Event()
    gate = asyncio.Event()

    async def blocking_send(message):
        if message['type'] == 'http.response.body':
            started.set()
            await gate.wait()

    task = asyncio.create_task(app(make_scope(), receive, blocking_send))
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stream.state.close_count == 1


async def test_close_error_propagated_when_streaming_succeeded():
    stream = ObservableAsyncIterator(close_error=CloseError('close boom'))
    app = make_app(stream)
    send = SendRecorder()

    with pytest.raises(CloseError):
        await app(make_scope(), receive, send)

    assert stream.state.close_count == 1
    assert send.eof_count == 0


async def test_response_callbacks_scheduled_after_streaming():
    state = StreamState()
    callback_done = asyncio.Event()

    async def callback():
        callback_done.set()

    class Resource:
        async def on_get(self, req, resp):
            resp.stream = observable_generator(state)
            resp.schedule(callback)

    app = App()
    app.add_route('/', Resource())
    send = SendRecorder()

    await app(make_scope(), receive, send)

    await asyncio.wait_for(callback_done.wait(), timeout=1)
    assert state.close_count == 1
    assert send.eof_count == 1
