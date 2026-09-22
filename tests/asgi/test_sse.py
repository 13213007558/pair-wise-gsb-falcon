import asyncio
import json
import sys
import time

import pytest

import falcon
from falcon import testing
from falcon.asgi import App
from falcon.asgi import SSEvent


def test_no_events():
    class Emitter:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class SomeResource:
        async def on_get(self, req, resp):
            self._called = True
            resp.sse = Emitter()

            # NOTE(vytas): Test explicitly referencing property since it is
            # normally optimized away by operating directly on the private
            # attribute in hot App.__call__ code paths.
            assert resp.sse is not None

    resource = SomeResource()

    app = App()
    app.add_route('/', resource)

    client = testing.TestClient(app)
    client.simulate_get()

    assert resource._called


def test_single_event():
    class SomeResource:
        async def on_get(self, req, resp):
            async def emitter():
                yield

            resp.sse = emitter()

        async def on_post(self, req, resp):
            async def emitter():
                yield SSEvent()

            resp.sse = emitter()

    resource = SomeResource()

    app = App()
    app.add_route('/', resource)

    client = testing.TestClient(app)

    result = client.simulate_get()
    assert result.text == ': ping\n\n'

    result = client.simulate_post()
    assert result.text == ': ping\n\n'


# TODO(vytas): Remove later during the CPython 3.15 dev cycle.
#   See also: https://github.com/python/cpython/issues/143939.
@pytest.mark.skipif(
    sys.version_info[:4] == (3, 15, 0, 'alpha'),
    reason='Raises "cannot reuse already awaited coroutine", CPython bug gh-143939',
)
def test_multiple_events():
    expected_result_text = (
        'data: ketchup\n'
        '\n'
        'event: condiment\n'
        'data: mustard\n'
        '\n'
        'event: condiment\n'
        'id: 1234\n'
        'data: mayo\n'
        '\n'
        'event: topping\n'
        'id: 5678\n'
        'retry: 100\n'
        'data: onions\n'
        '\n'
        ': Serve with chips.\n'
        'retry: 100\n'
        'data: guacamole \u1f951\n'
        '\n'
        'retry: 100\n'
        'data: {"condiment": "salsa"}\n'
        '\n'
    )

    class SomeResource:
        async def on_get(self, req, resp):
            async def emitter():
                for event in [
                    SSEvent(data=b'ketchup'),
                    SSEvent(data=b'mustard', event='condiment'),
                    SSEvent(data=b'mayo', event='condiment', event_id='1234'),
                    SSEvent(
                        data=b'onions', event='topping', event_id='5678', retry=100
                    ),
                    SSEvent(
                        text='guacamole \u1f951', retry=100, comment='Serve with chips.'
                    ),
                    SSEvent(json={'condiment': 'salsa'}, retry=100),
                ]:
                    yield event
                    await asyncio.sleep(0.001)

            resp.sse = emitter()

    resource = SomeResource()

    app = App()
    app.add_route('/', resource)

    client = testing.TestClient(app)

    async def _test():
        async with client as conductor:
            # NOTE(kgriffs): Single-shot test will only allow the first
            #   one or two events since a client disconnect will be emitted
            #   into the app immediately.
            result = await conductor.simulate_get()
            assert expected_result_text.startswith(result.text)

            async with conductor.simulate_get_stream() as sr:
                event_count = 0

                result_text = ''

                while True:
                    chunk = (await sr.stream.read()).decode()

                    if not chunk:
                        continue

                    result_text += chunk
                    event_count += len(chunk.strip().split('\n\n'))

                    if 'salsa' in chunk:
                        break

                assert not (await sr.stream.read())

                assert event_count == 6
                assert result_text == expected_result_text

    falcon.async_to_sync(_test)


# TODO(vytas): Remove later during the CPython 3.15 dev cycle.
#   See also: https://github.com/python/cpython/issues/143939.
@pytest.mark.skipif(
    sys.version_info[:4] == (3, 15, 0, 'alpha'),
    reason='Raises "cannot reuse already awaited coroutine", CPython bug gh-143939',
)
def test_multiple_events_early_disconnect():
    class SomeResource:
        async def on_get(self, req, resp):
            async def emitter():
                while True:
                    yield SSEvent(data=b'whassup')
                    await asyncio.sleep(0.01)

            resp.sse = emitter()

    resource = SomeResource()

    app = App()
    app.add_route('/', resource)

    async def _test():
        conductor = testing.ASGIConductor(app)
        result = await conductor.simulate_get()
        assert 'data: whassup' in result.text

        async with testing.ASGIConductor(app) as conductor:
            # NOTE(vytas): Using the get_stream() alias.
            async with conductor.get_stream() as sr:
                event_count = 0

                result_text = ''

                while event_count < 5:
                    chunk = (await sr.stream.read()).decode()
                    if not chunk:
                        continue

                    result_text += chunk
                    event_count += len(chunk.strip().split('\n\n'))

                assert result_text.startswith('data: whassup\n\n' * 5)
                assert event_count == 5

    falcon.async_to_sync(_test)


class TestSerializeJson:
    @pytest.fixture
    def client(self):
        class SomeResource:
            async def on_get(self, req, resp):
                async def emitter():
                    yield SSEvent(json={'foo': 'bar'})
                    yield SSEvent(json={'bar': 'baz'})

                resp.sse = emitter()

        resource = SomeResource()

        app = App()
        app.add_route('/', resource)

        client = testing.TestClient(app)
        return client

    def test_use_media_handler_dumps(self, client):
        h = client.app.resp_options.media_handlers[falcon.MEDIA_JSON]
        h._dumps = lambda x: json.dumps(x).upper()

        result = client.simulate_get()
        assert result.text == 'data: {"FOO": "BAR"}\n\ndata: {"BAR": "BAZ"}\n\n'

    def test_no_json_media_handler(self, client):
        for h in list(client.app.resp_options.media_handlers):
            if 'json' in h.casefold():
                client.app.resp_options.media_handlers.pop(h)

        result = client.simulate_get()
        assert result.text == ('data: {"foo": "bar"}\n\ndata: {"bar": "baz"}\n\n')


def test_invalid_event_values():
    with pytest.raises(TypeError):
        SSEvent(data='notbytes')

    with pytest.raises(TypeError):
        SSEvent(data=12345)

    with pytest.raises(TypeError):
        SSEvent(data=0)

    with pytest.raises(TypeError):
        SSEvent(text=b'notbytes')

    with pytest.raises(TypeError):
        SSEvent(text=23455)

    with pytest.raises(TypeError):
        SSEvent(text=0)

    with pytest.raises(TypeError):
        SSEvent(json=set()).serialize()

    with pytest.raises(TypeError):
        SSEvent(event=b'name')

    with pytest.raises(TypeError):
        SSEvent(event=1234)

    with pytest.raises(TypeError):
        SSEvent(event=0)

    with pytest.raises(TypeError):
        SSEvent(event_id=b'idbytes')

    with pytest.raises(TypeError):
        SSEvent(event_id=52085)

    with pytest.raises(TypeError):
        SSEvent(event_id=0)

    with pytest.raises(TypeError):
        SSEvent(retry='5808.25')

    with pytest.raises(TypeError):
        SSEvent(retry='')

    with pytest.raises(TypeError):
        SSEvent(retry=5808.25)

    with pytest.raises(TypeError):
        SSEvent(comment=b'somebytes')

    with pytest.raises(TypeError):
        SSEvent(comment=1234)

    with pytest.raises(TypeError):
        SSEvent(comment=0)


def test_non_iterable():
    class SomeResource:
        async def on_get(self, req, resp):
            async def emitter():
                yield

            resp.sse = emitter

    resource = SomeResource()

    app = App()
    app.add_route('/', resource)

    client = testing.TestClient(app)

    with pytest.raises(TypeError):
        client.simulate_get()


# ----------------------------------------------------------------------
# SSE pump teardown: disconnect points, backpressure, and task cleanup
# ----------------------------------------------------------------------


class _FakeASGIConnection:
    """Fake ASGI send/receive pair with controllable backpressure."""

    def __init__(self, send_credits=None, stubborn_send=False, receive_messages=()):
        self.sent = []
        self.disconnect = asyncio.Event()
        self.send_blocked = asyncio.Event()
        self.resume = asyncio.Event()
        self._send_credits = send_credits
        self._stubborn_send = stubborn_send
        self._receive_messages = list(receive_messages)
        self._request_received = False

    async def receive(self):
        if not self._request_received:
            self._request_received = True
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        if self._receive_messages:
            return self._receive_messages.pop(0)

        await self.disconnect.wait()
        return {'type': 'http.disconnect'}

    async def send(self, message):
        if message['type'] != 'http.response.body' or self._send_credits is None:
            self.sent.append(message)
            return

        if self._send_credits > 0:
            self._send_credits -= 1
        else:
            self.send_blocked.set()
            try:
                await self.resume.wait()
            except asyncio.CancelledError:
                if not self._stubborn_send:
                    raise

                # NOTE: Simulate a misbehaving server that keeps
                #   blocking even when cancelled.
                await self.resume.wait()
                raise

        self.sent.append(message)

    @property
    def bodies(self):
        return [
            message['body']
            for message in self.sent
            if message['type'] == 'http.response.body' and 'body' in message
        ]

    @property
    def eof_sent(self):
        return any(
            message['type'] == 'http.response.body' and not message.get('more_body')
            for message in self.sent
        )


class _TrackedEmitter:
    """Async iterator wrapping an async generator, counting aclose()."""

    def __init__(self, agen):
        self._agen = agen
        self.aclose_count = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._agen.__anext__()

    async def aclose(self):
        self.aclose_count += 1
        await self._agen.aclose()


class _SSEResource:
    def __init__(self, emitter, terminal=None, heartbeat=None):
        self.emitter = emitter
        self.terminal = terminal
        self.heartbeat = heartbeat
        self.resp = None

    async def on_get(self, req, resp):
        self.resp = resp
        resp.sse = self.emitter
        resp.sse_terminal = self.terminal
        resp.sse_heartbeat = self.heartbeat


def _background_tasks():
    return {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}


def _start_request(app, path, conn):
    scope = testing.create_scope(path=path)
    return asyncio.ensure_future(app(scope, conn.receive, conn.send))


def _make_app(resource, path='/sse'):
    app = App()
    app.add_route(path, resource)
    return app


def test_disconnect_at_yield():
    async def _test():
        conn = _FakeASGIConnection(send_credits=1)

        async def emitter():
            while True:
                yield SSEvent(data=b'event')

        tracked = _TrackedEmitter(emitter())
        resource = _SSEResource(tracked)
        app = _make_app(resource)

        before = _background_tasks()
        request = _start_request(app, '/sse', conn)

        # NOTE: With the writer blocked by backpressure and the frame
        #   queue full, the producer is paused on put(); in other words,
        #   the generator is suspended at a yield point.
        await asyncio.wait_for(conn.send_blocked.wait(), timeout=1)
        await asyncio.sleep(0.05)

        conn.disconnect.set()
        await asyncio.wait_for(request, timeout=1)

        assert conn.bodies == [SSEvent(data=b'event').serialize()]
        assert not conn.eof_sent
        assert tracked.aclose_count == 1
        assert _background_tasks() == before

    falcon.async_to_sync(_test)


def test_disconnect_at_send():
    async def _test():
        conn = _FakeASGIConnection(
            send_credits=0,
            receive_messages=[
                {'type': 'http.request', 'body': b'', 'more_body': False}
            ],
        )
        never = asyncio.Event()

        async def emitter():
            yield SSEvent(data=b'first')
            await never.wait()
            yield SSEvent(data=b'second')

        tracked = _TrackedEmitter(emitter())
        resource = _SSEResource(tracked)
        app = _make_app(resource)

        before = _background_tasks()
        request = _start_request(app, '/sse', conn)

        # NOTE: The writer is now blocked inside send() by the
        #   backpressure gate, with the first frame still unsent.
        await asyncio.wait_for(conn.send_blocked.wait(), timeout=1)

        conn.disconnect.set()
        await asyncio.wait_for(request, timeout=1)

        assert conn.bodies == []
        assert not conn.eof_sent
        assert tracked.aclose_count == 1
        assert _background_tasks() == before

    falcon.async_to_sync(_test)


def test_disconnect_at_heartbeat():
    async def _test():
        conn = _FakeASGIConnection(send_credits=2)
        never = asyncio.Event()

        async def emitter():
            await never.wait()
            yield SSEvent(data=b'unreachable')

        tracked = _TrackedEmitter(emitter())
        resource = _SSEResource(tracked, heartbeat=0.01)
        app = _make_app(resource)

        before = _background_tasks()
        request = _start_request(app, '/sse', conn)

        # NOTE: The emitter never produces anything, so the only
        #   frames in flight are heartbeat pings; wait until the
        #   writer is blocked sending a third one. While the pump is
        #   running, it comprises the request task plus the producer,
        #   writer, watcher, and heartbeat tasks.
        await asyncio.wait_for(conn.send_blocked.wait(), timeout=1)
        assert len(_background_tasks() - before) == 5

        conn.disconnect.set()
        await asyncio.wait_for(request, timeout=1)

        assert conn.bodies == [b': ping\n\n', b': ping\n\n']
        assert not conn.eof_sent
        assert tracked.aclose_count == 1
        assert resource.resp.sse_heartbeat == 0.01
        assert _background_tasks() == before

    falcon.async_to_sync(_test)


def test_generator_error_teardown():
    boom = RuntimeError('emitter went boom')

    async def _test():
        conn = _FakeASGIConnection()

        async def emitter():
            yield SSEvent(data=b'first')
            await asyncio.sleep(0.01)
            raise boom

        tracked = _TrackedEmitter(emitter())
        resource = _SSEResource(tracked)
        app = _make_app(resource)

        before = _background_tasks()
        scope = testing.create_scope(path='/sse')

        with pytest.raises(RuntimeError) as excinfo:
            await app(scope, conn.receive, conn.send)

        # NOTE: The last (and only) exception surfaced is the one
        #   raised by the emitter, and all related tasks were stopped.
        assert excinfo.value is boom
        assert conn.bodies == [SSEvent(data=b'first').serialize()]
        assert not conn.eof_sent
        assert tracked.aclose_count == 1
        assert _background_tasks() == before

    falcon.async_to_sync(_test)


def test_normal_completion_sends_terminal_frame():
    async def _test():
        conn = _FakeASGIConnection(send_credits=1)

        async def emitter():
            yield SSEvent(data=b'e1')
            yield SSEvent(data=b'e2')

        tracked = _TrackedEmitter(emitter())
        resource = _SSEResource(tracked, terminal=SSEvent(event='end', data=b'bye'))
        app = _make_app(resource)

        before = _background_tasks()
        request = _start_request(app, '/sse', conn)

        # NOTE: Hold the writer back until the producer has enqueued
        #   all frames (events, terminal frame, and the sentinel),
        #   then let the queue drain.
        await asyncio.wait_for(conn.send_blocked.wait(), timeout=1)
        await asyncio.sleep(0.05)
        conn.resume.set()
        await asyncio.wait_for(request, timeout=1)

        assert conn.bodies == [
            SSEvent(data=b'e1').serialize(),
            SSEvent(data=b'e2').serialize(),
            SSEvent(event='end', data=b'bye').serialize(),
        ]
        assert conn.eof_sent
        assert tracked.aclose_count == 1

        # NOTE: Normal completion releases all references held by
        #   the response.
        assert resource.resp.sse is None
        assert resource.resp.sse_terminal is None
        assert _background_tasks() == before

    falcon.async_to_sync(_test)


def test_terminal_frame_raw_bytes():
    async def _test():
        conn = _FakeASGIConnection()

        async def emitter():
            yield SSEvent(data=b'e1')

        resource = _SSEResource(_TrackedEmitter(emitter()), terminal=b': done\n\n')
        app = _make_app(resource)

        before = _background_tasks()
        await _start_request(app, '/sse', conn)

        assert conn.bodies == [SSEvent(data=b'e1').serialize(), b': done\n\n']
        assert conn.eof_sent
        assert _background_tasks() == before

    falcon.async_to_sync(_test)


def test_concurrent_producers_write_frames_in_order():
    async def _test():
        conn = _FakeASGIConnection()

        async def emitter():
            for i in range(5):
                yield SSEvent(data=f'event-{i}'.encode())
                await asyncio.sleep(0.015)

        tracked = _TrackedEmitter(emitter())
        resource = _SSEResource(tracked, terminal=SSEvent(event='end'), heartbeat=0.005)
        app = _make_app(resource)

        before = _background_tasks()
        await _start_request(app, '/sse', conn)

        # NOTE: Emitter frames and heartbeat pings race through the
        #   same queue, but the single writer sends them one by one,
        #   in the exact order they were enqueued.
        data_frames = [b for b in conn.bodies if b.startswith(b'data:')]
        assert data_frames == [f'data: event-{i}\n\n'.encode() for i in range(5)]
        assert conn.bodies[-1] == SSEvent(event='end').serialize()
        assert b': ping\n\n' in conn.bodies
        assert all(b.endswith(b'\n\n') for b in conn.bodies)
        assert conn.eof_sent
        assert tracked.aclose_count == 1
        assert _background_tasks() == before

    falcon.async_to_sync(_test)


def test_slow_client_cannot_block_cancellation(monkeypatch):
    monkeypatch.setattr('falcon.asgi._sse.CLEANUP_TIMEOUT', 0.05)

    async def _test():
        conn = _FakeASGIConnection(send_credits=0, stubborn_send=True)
        never = asyncio.Event()

        async def emitter():
            yield SSEvent(data=b'first')
            await never.wait()
            yield SSEvent(data=b'second')

        tracked = _TrackedEmitter(emitter())
        resource = _SSEResource(tracked)
        app = _make_app(resource)

        before = _background_tasks()
        request = _start_request(app, '/sse', conn)
        await asyncio.wait_for(conn.send_blocked.wait(), timeout=1)

        started = time.monotonic()
        conn.disconnect.set()
        await asyncio.wait_for(request, timeout=2)
        elapsed = time.monotonic() - started

        # NOTE: The stubborn send() ignores cancellation, but teardown
        #   must nevertheless complete within the configured bound
        #   rather than waiting on the slow client indefinitely.
        assert elapsed < 1.0
        assert tracked.aclose_count == 1
        assert not conn.eof_sent

        # NOTE: Release the stuck send and verify nothing leaks.
        conn.resume.set()
        await asyncio.sleep(0.05)
        assert _background_tasks() == before

    falcon.async_to_sync(_test)


def test_emitter_close_error_does_not_mask_disconnect():
    class StubbornEmitter:
        def __init__(self):
            self.aclose_count = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

        async def aclose(self):
            self.aclose_count += 1
            raise ValueError('cannot close')

    async def _test():
        conn = _FakeASGIConnection()
        emitter = StubbornEmitter()
        resource = _SSEResource(emitter)
        app = _make_app(resource)

        before = _background_tasks()
        request = _start_request(app, '/sse', conn)
        await asyncio.sleep(0.05)

        conn.disconnect.set()

        # NOTE: The aclose() error must not mask the disconnect; the
        #   request simply ends.
        await asyncio.wait_for(request, timeout=1)

        assert emitter.aclose_count == 1
        assert _background_tasks() == before

    falcon.async_to_sync(_test)


def test_no_cross_request_leaks():
    async def _test():
        conns = [_FakeASGIConnection(), _FakeASGIConnection()]

        async def emitter_one():
            for i in range(3):
                yield SSEvent(data=f'one-{i}'.encode())
                await asyncio.sleep(0.001)

        async def emitter_two():
            for i in range(2):
                yield SSEvent(data=f'two-{i}'.encode())
                await asyncio.sleep(0.001)

        tracked_one = _TrackedEmitter(emitter_one())
        tracked_two = _TrackedEmitter(emitter_two())
        resource_one = _SSEResource(tracked_one, terminal=SSEvent(event='end-one'))
        resource_two = _SSEResource(tracked_two, terminal=SSEvent(event='end-two'))

        app = App()
        app.add_route('/one', resource_one)
        app.add_route('/two', resource_two)

        before = _background_tasks()
        await asyncio.gather(
            _start_request(app, '/one', conns[0]),
            _start_request(app, '/two', conns[1]),
        )

        assert conns[0].bodies == [
            b'data: one-0\n\n',
            b'data: one-1\n\n',
            b'data: one-2\n\n',
            SSEvent(event='end-one').serialize(),
        ]
        assert conns[1].bodies == [
            b'data: two-0\n\n',
            b'data: two-1\n\n',
            SSEvent(event='end-two').serialize(),
        ]
        assert conns[0].eof_sent
        assert conns[1].eof_sent
        assert tracked_one.aclose_count == 1
        assert tracked_two.aclose_count == 1
        assert resource_one.resp.sse is None
        assert resource_two.resp.sse is None
        assert _background_tasks() == before

    falcon.async_to_sync(_test)
