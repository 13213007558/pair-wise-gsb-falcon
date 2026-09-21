import asyncio

import pytest

from falcon import testing
import falcon.asgi


class ClientDisconnectedError(Exception):
    """Simulates the ASGI server failing once the client is gone."""


class StreamCloseError(Exception):
    """Raised by stream doubles when their cleanup fails."""


class IterationError(Exception):
    """Raised by stream doubles when iteration itself fails."""


class AsyncCleanup:
    """Observable asynchronous cleanup barrier for stream doubles.

    When 'use_barrier' is enabled, the cleanup only completes once the
    'release' event is set, making it possible to verify that Falcon
    actually awaits the stream's closing protocol instead of relying on
    garbage collection or event loop shutdown.
    """

    def __init__(self, *, use_barrier=False, error=None):
        self.count = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._use_barrier = use_barrier
        self._error = error

    async def __call__(self):
        self.count += 1
        self.started.set()

        if self._use_barrier:
            await self.release.wait()

        if self._error is not None:
            raise self._error


class AsyncGeneratorStream:
    """An async generator performing observable cleanup in 'finally'."""

    def __init__(self, chunks=(b'one', b'two', b'three'), cleanup=None, gate=None):
        self.cleanup = cleanup or AsyncCleanup()
        self._chunks = chunks
        self._gate = gate
        self.yielded = 0
        self.stream = self._emit()

    async def _emit(self):
        try:
            for index, chunk in enumerate(self._chunks):
                if index and self._gate is not None:
                    await self._gate.wait()

                if isinstance(chunk, BaseException):
                    raise chunk

                self.yielded += 1
                yield chunk
        finally:
            await self.cleanup()


class WrappedAsyncGenerator:
    """An async generator wrapper exposing an observable aclose()."""

    def __init__(self, chunks=(b'one', b'two')):
        self.aclose_count = 0
        self.iterated = 0
        self._chunks = chunks
        self._generator = self._emit()

    async def _emit(self):
        for chunk in self._chunks:
            self.iterated += 1
            yield chunk

    def __aiter__(self):
        return self._generator

    async def aclose(self):
        self.aclose_count += 1
        await self._generator.aclose()


class AsyncIteratorStream:
    """An async iterator implementing the aclose() closing protocol."""

    def __init__(self, chunks=(b'one', b'two', b'three'), cleanup=None, gate=None):
        self.cleanup = cleanup or AsyncCleanup()
        self._chunks = list(chunks)
        self._gate = gate
        self.iterated = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration

        if self.iterated and self._gate is not None:
            await self._gate.wait()

        self.iterated += 1
        chunk = self._chunks.pop(0)
        if isinstance(chunk, BaseException):
            raise chunk

        return chunk

    async def aclose(self):
        await self.cleanup()


class NoCloseAsyncIterator:
    """A valid async iterator without any closing method."""

    def __init__(self, chunks=(b'one', b'two')):
        self._chunks = list(chunks)
        self.exhausted = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            self.exhausted = True
            raise StopAsyncIteration

        return self._chunks.pop(0)


class AsyncFileLikeStream:
    """A file-like async stream implementing read() and close()."""

    def __init__(self, chunks=(b'one', b'two'), cleanup=None):
        self.cleanup = cleanup or AsyncCleanup()
        self._chunks = list(chunks)

    async def read(self, size=-1):
        if not self._chunks:
            return b''

        return self._chunks.pop(0)

    async def close(self):
        await self.cleanup()


class StreamResource:
    def __init__(self, stream):
        self.stream = stream

    async def on_get(self, req, resp):
        resp.stream = self.stream


def create_app(stream):
    app = falcon.asgi.App()
    app.add_route('/', StreamResource(stream))
    return app


class AsgiClient:
    """Minimal harness driving ASGI events directly, without a server."""

    def __init__(self, app, send_hook=None):
        self.app = app
        self.send_hook = send_hook
        self.sent = []

    async def request(self, path='/'):
        scope = testing.create_scope(path=path)

        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        async def send(message):
            self.sent.append(message)
            if self.send_hook is not None:
                await self.send_hook(message)

        await self.app(scope, receive, send)
        return self.sent


def body_events(sent):
    return [message for message in sent if message['type'] == 'http.response.body']


def assert_eof_sent(sent):
    # NOTE: The terminal body event carries no 'more_body' flag, and it
    #   must be sent exactly once.
    assert sent[-1] == {'type': 'http.response.body'}
    assert body_events(sent).count(sent[-1]) == 1


def assert_no_eof(sent):
    assert all(message.get('more_body') for message in body_events(sent))


def disconnect_after(bodies):
    bodies_seen = 0

    async def send_hook(message):
        nonlocal bodies_seen
        if message['type'] == 'http.response.body':
            bodies_seen += 1
            if bodies_seen > bodies:
                raise ClientDisconnectedError('client disconnected')

    return send_hook


class TestStreamLifecycle:
    async def test_async_generator_finalized_on_completion(self):
        cleanup = AsyncCleanup()
        stream = AsyncGeneratorStream(cleanup=cleanup)
        client = AsgiClient(create_app(stream.stream))

        sent = await client.request()

        assert cleanup.count == 1
        assert stream.yielded == 3
        assert [m['body'] for m in body_events(sent)[:-1]] == [
            b'one',
            b'two',
            b'three',
        ]
        assert_eof_sent(sent)

    async def test_async_generator_finalized_on_client_disconnect(self):
        cleanup = AsyncCleanup()
        stream = AsyncGeneratorStream(cleanup=cleanup)
        client = AsgiClient(create_app(stream.stream), disconnect_after(1))

        with pytest.raises(ClientDisconnectedError):
            await client.request()

        # NOTE: The generator's finally block must run promptly, even
        #   though the async for loop was aborted by the failed send.
        assert cleanup.count == 1
        assert stream.yielded == 2
        assert_no_eof(client.sent)

    async def test_async_generator_finalized_on_iteration_error(self):
        cleanup = AsyncCleanup()
        stream = AsyncGeneratorStream(
            chunks=[b'one', IterationError('iteration failed')],
            cleanup=cleanup,
        )
        client = AsgiClient(create_app(stream.stream))

        with pytest.raises(IterationError):
            await client.request()

        assert cleanup.count == 1
        assert_no_eof(client.sent)

    async def test_async_iterator_aclose_on_completion(self):
        cleanup = AsyncCleanup()
        stream = AsyncIteratorStream(cleanup=cleanup)
        client = AsgiClient(create_app(stream))

        sent = await client.request()

        assert cleanup.count == 1
        assert stream.iterated == 3
        assert_eof_sent(sent)

    async def test_async_iterator_aclose_on_client_disconnect(self):
        cleanup = AsyncCleanup()
        stream = AsyncIteratorStream(cleanup=cleanup)
        client = AsgiClient(create_app(stream), disconnect_after(1))

        with pytest.raises(ClientDisconnectedError):
            await client.request()

        assert cleanup.count == 1
        assert_no_eof(client.sent)

    async def test_iterator_without_close_method_still_works(self):
        stream = NoCloseAsyncIterator()
        client = AsgiClient(create_app(stream))

        sent = await client.request()

        assert stream.exhausted
        assert [m['body'] for m in body_events(sent)[:-1]] == [b'one', b'two']
        assert_eof_sent(sent)

    async def test_filelike_closed_on_completion(self):
        cleanup = AsyncCleanup()
        stream = AsyncFileLikeStream(cleanup=cleanup)
        client = AsgiClient(create_app(stream))

        sent = await client.request()

        assert cleanup.count == 1
        assert_eof_sent(sent)

    async def test_filelike_closed_on_client_disconnect(self):
        cleanup = AsyncCleanup()
        stream = AsyncFileLikeStream(cleanup=cleanup)
        client = AsgiClient(create_app(stream), disconnect_after(0))

        with pytest.raises(ClientDisconnectedError):
            await client.request()

        assert cleanup.count == 1
        assert_no_eof(client.sent)

    async def test_iterator_closed_when_response_start_fails(self):
        cleanup = AsyncCleanup()
        stream = AsyncIteratorStream(cleanup=cleanup)

        async def send_hook(message):
            if message['type'] == 'http.response.start':
                raise ClientDisconnectedError('client disconnected')

        client = AsgiClient(create_app(stream), send_hook)

        with pytest.raises(ClientDisconnectedError):
            await client.request()

        # NOTE: The stream was never iterated, yet the already created
        #   resource must still be released.
        assert stream.iterated == 0
        assert cleanup.count == 1
        assert body_events(client.sent) == []

    async def test_generator_aclose_when_response_start_fails(self):
        stream = WrappedAsyncGenerator()

        async def send_hook(message):
            if message['type'] == 'http.response.start':
                raise ClientDisconnectedError('client disconnected')

        client = AsgiClient(create_app(stream), send_hook)

        with pytest.raises(ClientDisconnectedError):
            await client.request()

        assert stream.iterated == 0
        assert stream.aclose_count == 1
        assert body_events(client.sent) == []

    async def test_iteration_error_preserved_when_close_fails(self):
        cleanup = AsyncCleanup(error=StreamCloseError('close failed'))
        stream = AsyncIteratorStream(
            chunks=[b'one', IterationError('iteration failed')],
            cleanup=cleanup,
        )
        client = AsgiClient(create_app(stream))

        with pytest.raises(IterationError):
            await client.request()

        assert cleanup.count == 1
        assert_no_eof(client.sent)

    async def test_send_error_preserved_when_close_fails(self):
        cleanup = AsyncCleanup(error=StreamCloseError('close failed'))
        stream = AsyncIteratorStream(cleanup=cleanup)
        client = AsgiClient(create_app(stream), disconnect_after(1))

        with pytest.raises(ClientDisconnectedError):
            await client.request()

        assert cleanup.count == 1
        assert_no_eof(client.sent)

    async def test_close_error_propagates_on_success(self):
        cleanup = AsyncCleanup(error=StreamCloseError('close failed'))
        stream = AsyncIteratorStream(cleanup=cleanup)
        client = AsgiClient(create_app(stream))

        # NOTE: On the success path there is no original exception to
        #   preserve, so the close error itself is propagated, and the
        #   terminal body event is not sent.
        with pytest.raises(StreamCloseError):
            await client.request()

        assert cleanup.count == 1
        assert_no_eof(client.sent)

    async def test_cancellation_closes_stream_exactly_once(self):
        cleanup = AsyncCleanup(use_barrier=True)
        gate = asyncio.Event()
        stream = AsyncGeneratorStream(cleanup=cleanup, gate=gate)
        client = AsgiClient(create_app(stream.stream))

        first_body_sent = asyncio.Event()

        async def send_hook(message):
            if message['type'] == 'http.response.body':
                first_body_sent.set()

        client.send_hook = send_hook
        task = asyncio.create_task(client.request())

        await first_body_sent.wait()
        task.cancel()

        # NOTE: Cleanup starts promptly, and the task does not complete
        #   until the asynchronous cleanup barrier is released.
        await cleanup.started.wait()
        assert not task.done()

        cleanup.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert cleanup.count == 1
        assert_no_eof(client.sent)

    async def test_cancellation_preserved_when_close_fails(self):
        cleanup = AsyncCleanup(error=StreamCloseError('close failed'))
        gate = asyncio.Event()
        stream = AsyncIteratorStream(cleanup=cleanup, gate=gate)
        client = AsgiClient(create_app(stream))

        first_body_sent = asyncio.Event()

        async def send_hook(message):
            if message['type'] == 'http.response.body':
                first_body_sent.set()

        client.send_hook = send_hook
        task = asyncio.create_task(client.request())

        await first_body_sent.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert cleanup.count == 1
        assert_no_eof(client.sent)

    async def test_cleanup_awaited_before_response_completes(self):
        cleanup = AsyncCleanup(use_barrier=True)
        stream = AsyncIteratorStream(chunks=[b'one'], cleanup=cleanup)
        client = AsgiClient(create_app(stream))

        task = asyncio.create_task(client.request())

        # NOTE: The response is not finalized (and the terminal body event
        #   is not sent) until the asynchronous cleanup has completed.
        await cleanup.started.wait()
        assert not task.done()
        assert_no_eof(client.sent)

        cleanup.release.set()
        sent = await task

        assert cleanup.count == 1
        assert_eof_sent(sent)
