import asyncio
import gc
import time
import weakref

import pytest

import falcon
from falcon import testing
from falcon.asgi import App
from falcon.asgi import SSEOptions
from falcon.asgi import SSEvent
from falcon.asgi.sse import _SSEConnection

HTTP_REQUEST_EVENT = {'type': 'http.request', 'body': b'', 'more_body': False}


class FakeSend:
    """Fake ASGI send callable with controllable backpressure."""

    def __init__(self, block_on=None, stubborn=False):
        self.messages = []
        self.block_on = block_on
        self.stubborn = stubborn
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()
        self.calls = 0

    async def __call__(self, message):
        self.messages.append(message)
        index = self.calls
        self.calls += 1

        if self.block_on is not None and index == self.block_on:
            self.entered.set()
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                if not self.stubborn:
                    raise
                # NOTE: Simulate a send callable that is blocked on a slow
                #   client and therefore ignores cancellation.
                await self.gate.wait()

    @property
    def bodies(self):
        return [
            message.get('body')
            for message in self.messages
            if message['type'] == 'http.response.body'
        ]


class FakeReceive:
    """Fake ASGI receive callable driven by an event barrier."""

    def __init__(self, events=()):
        self._events = list(events)
        self.disconnect_requested = asyncio.Event()
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        if self._events:
            return self._events.pop(0)
        await self.disconnect_requested.wait()
        return {'type': 'http.disconnect'}

    def disconnect(self):
        self.disconnect_requested.set()


class CountingEmitter:
    """Async emitter wrapper that counts aclose() invocations."""

    def __init__(self, gen):
        self._gen = gen
        self.aclose_count = 0

    def __aiter__(self):
        return self._gen

    async def aclose(self):
        self.aclose_count += 1
        await self._gen.aclose()


async def wait_for_frames(send, count, timeout=5.0):
    async def poll():
        while len(send.messages) < count:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout)


def assert_no_lingering_tasks(baseline):
    leftover = asyncio.all_tasks() - baseline
    assert not leftover, f'lingering tasks: {leftover!r}'


def assert_released(connection):
    assert connection._emitter is None
    assert connection._send is None
    assert connection._receive is None


def test_disconnect_while_suspended_at_yield():
    async def main():
        parked = asyncio.Event()
        finalized = asyncio.Event()

        async def gen():
            try:
                yield SSEvent(data=b'one')
                parked.set()
                await asyncio.Event().wait()
                yield SSEvent(data=b'two')
            finally:
                finalized.set()

        emitter = CountingEmitter(gen())
        send = FakeSend()
        receive = FakeReceive()
        connection = _SSEConnection(emitter, send, receive)

        baseline = asyncio.all_tasks()
        runner = asyncio.create_task(connection.run())

        # NOTE: The producer is parked inside the generator, in between
        #   two yields, when the client disconnects.
        await asyncio.wait_for(parked.wait(), 5)
        receive.disconnect()
        await asyncio.wait_for(runner, 5)

        assert connection.disconnected
        assert connection.last_error is None
        assert emitter.aclose_count == 1
        assert finalized.is_set()
        assert all(task.done() for task in connection.tasks)
        assert_released(connection)

        # NOTE: No further frames (not even a closing one) are sent once
        #   the client is gone.
        assert send.bodies == [b'data: one\n\n']

        assert_no_lingering_tasks(baseline)

    falcon.async_to_sync(main)


def test_disconnect_during_send_backpressure():
    async def main():
        finalized = asyncio.Event()

        async def gen():
            try:
                yield SSEvent(data=b'one')
                yield SSEvent(data=b'two')
                yield SSEvent(data=b'three')
            finally:
                finalized.set()

        emitter = CountingEmitter(gen())
        send = FakeSend(block_on=1)
        receive = FakeReceive()
        connection = _SSEConnection(emitter, send, receive)

        baseline = asyncio.all_tasks()
        runner = asyncio.create_task(connection.run())

        # NOTE: The producer is blocked inside send() (backpressure) when
        #   the client disconnects.
        await asyncio.wait_for(send.entered.wait(), 5)
        receive.disconnect()
        await asyncio.wait_for(runner, 5)

        assert connection.disconnected
        assert connection.last_error is None
        assert emitter.aclose_count == 1
        assert finalized.is_set()
        assert all(task.done() for task in connection.tasks)
        assert_released(connection)
        assert_no_lingering_tasks(baseline)

    falcon.async_to_sync(main)


def test_disconnect_with_stubborn_send_is_bounded():
    async def main():
        options = SSEOptions()
        options.cancel_timeout = 0.2

        async def gen():
            yield SSEvent(data=b'one')
            yield SSEvent(data=b'two')

        emitter = CountingEmitter(gen())
        send = FakeSend(block_on=1, stubborn=True)
        receive = FakeReceive()
        connection = _SSEConnection(emitter, send, receive, options=options)

        runner = asyncio.create_task(connection.run())
        await asyncio.wait_for(send.entered.wait(), 5)

        receive.disconnect()
        start = time.monotonic()
        await asyncio.wait_for(runner, 5)
        elapsed = time.monotonic() - start

        # NOTE: The slow client could not hold up the cancellation path
        #   indefinitely; cleanup gave up waiting after cancel_timeout.
        assert elapsed >= options.cancel_timeout
        assert elapsed < options.cancel_timeout * 5
        assert connection.disconnected
        assert_released(connection)

        # NOTE: The producer task is still stuck in the stubborn send, so
        #   the emitter could not be closed deterministically.
        assert emitter.aclose_count == 0
        assert not all(task.done() for task in connection.tasks)

        # NOTE: Once the client unblocks, the remaining task finishes.
        send.gate.set()
        await asyncio.wait_for(
            asyncio.gather(*connection.tasks, return_exceptions=True), 5
        )
        assert all(task.done() for task in connection.tasks)

    falcon.async_to_sync(main)


def test_disconnect_at_heartbeat():
    async def main():
        options = SSEOptions()
        options.heartbeat_interval = 0.01

        finalized = asyncio.Event()

        async def gen():
            try:
                await asyncio.sleep(3600)
                yield SSEvent(data=b'never')
            finally:
                finalized.set()

        emitter = CountingEmitter(gen())
        send = FakeSend()
        receive = FakeReceive()
        connection = _SSEConnection(emitter, send, receive, options=options)

        baseline = asyncio.all_tasks()
        runner = asyncio.create_task(connection.run())

        # NOTE: The emitter is idle; only heartbeat frames are flowing
        #   when the client disconnects.
        await wait_for_frames(send, 2)
        assert send.bodies == [b': ping\n\n', b': ping\n\n']

        receive.disconnect()
        await asyncio.wait_for(runner, 5)

        assert connection.disconnected
        assert connection.last_error is None
        assert emitter.aclose_count == 1
        assert finalized.is_set()
        assert all(task.done() for task in connection.tasks)
        assert_released(connection)

        # NOTE: The heartbeat task was stopped as well.
        frame_count = len(send.messages)
        await asyncio.sleep(0.05)
        assert len(send.messages) == frame_count

        assert_no_lingering_tasks(baseline)

    falcon.async_to_sync(main)


def test_generator_exception_is_recorded_and_raised():
    async def main():
        async def gen():
            yield SSEvent(data=b'one')
            raise RuntimeError('boom')

        emitter = CountingEmitter(gen())
        send = FakeSend()
        # NOTE: The watcher first receives a non-disconnect event, which
        #   it must ignore while waiting for a disconnect.
        receive = FakeReceive(events=[{'type': 'http.request', 'body': b''}])
        connection = _SSEConnection(emitter, send, receive)

        baseline = asyncio.all_tasks()
        with pytest.raises(RuntimeError, match='boom'):
            await connection.run()

        assert isinstance(connection.last_error, RuntimeError)
        assert str(connection.last_error) == 'boom'
        assert not connection.disconnected
        assert emitter.aclose_count == 1
        assert all(task.done() for task in connection.tasks)
        assert_released(connection)

        # NOTE: No closing frame is sent when the emitter fails.
        assert send.bodies == [b'data: one\n\n']

        assert_no_lingering_tasks(baseline)

    falcon.async_to_sync(main)


def test_disconnect_while_generator_parked_before_error():
    async def main():
        raise_gate = asyncio.Event()
        finalized = asyncio.Event()

        async def gen():
            try:
                yield SSEvent(data=b'one')
                await raise_gate.wait()
                raise RuntimeError('boom')
            finally:
                finalized.set()

        emitter = CountingEmitter(gen())
        send = FakeSend()
        receive = FakeReceive()
        connection = _SSEConnection(emitter, send, receive)

        baseline = asyncio.all_tasks()
        runner = asyncio.create_task(connection.run())

        await wait_for_frames(send, 1)
        # NOTE: Disconnect while the generator is parked right before it
        #   would raise; the disconnect wins and the error never happens.
        receive.disconnect()
        await asyncio.wait_for(runner, 5)
        raise_gate.set()

        assert connection.disconnected
        assert connection.last_error is None
        assert emitter.aclose_count == 1
        assert finalized.is_set()
        assert all(task.done() for task in connection.tasks)
        assert_released(connection)
        assert_no_lingering_tasks(baseline)

    falcon.async_to_sync(main)


def test_error_raised_during_cancellation_is_harvested():
    async def main():
        async def gen():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError('boom during cancel')
            yield

        emitter = CountingEmitter(gen())
        send = FakeSend()
        receive = FakeReceive()
        connection = _SSEConnection(emitter, send, receive)

        baseline = asyncio.all_tasks()
        runner = asyncio.create_task(connection.run())
        await asyncio.sleep(0.01)

        # NOTE: The disconnect and the generator error (raised from the
        #   cancellation point) coexist; the error must still surface.
        receive.disconnect()
        with pytest.raises(RuntimeError, match='boom during cancel'):
            await asyncio.wait_for(runner, 5)

        assert isinstance(connection.last_error, RuntimeError)
        assert connection.disconnected
        assert emitter.aclose_count == 1
        assert all(task.done() for task in connection.tasks)
        assert_released(connection)
        assert_no_lingering_tasks(baseline)

    falcon.async_to_sync(main)


def test_concurrent_producers_write_frames_in_order():
    async def main():
        options = SSEOptions()
        options.heartbeat_interval = 0.001
        options.terminator = SSEvent(comment='end')

        async def gen():
            for index in range(30):
                yield SSEvent(data=f'data-{index:02d}'.encode())
                await asyncio.sleep(0.001)

        emitter = CountingEmitter(gen())
        send = FakeSend()
        receive = FakeReceive()
        connection = _SSEConnection(emitter, send, receive, options=options)

        baseline = asyncio.all_tasks()
        await connection.run()

        bodies = send.bodies
        event_frames = [body for body in bodies if body]

        # NOTE: Every frame written by the concurrent producers (emitter
        #   and heartbeat) is complete and well-formed.
        assert all(frame.endswith(b'\n\n') for frame in event_frames)

        # NOTE: Event frames arrive in emission order.
        data_frames = [frame for frame in event_frames if frame.startswith(b'data: ')]
        assert data_frames == [
            f'data: data-{index:02d}\n\n'.encode() for index in range(30)
        ]

        # NOTE: Heartbeat frames interleave but never corrupt a frame.
        assert b': ping\n\n' in event_frames

        # NOTE: The terminator is the final event frame, followed only by
        #   the closing frame.
        assert event_frames[-1] == b': end\n\n'
        assert send.messages[-1] == {'type': 'http.response.body'}

        assert not connection.disconnected
        assert connection.last_error is None
        assert emitter.aclose_count == 1
        assert all(task.done() for task in connection.tasks)
        assert_released(connection)
        assert_no_lingering_tasks(baseline)

    falcon.async_to_sync(main)


@pytest.mark.parametrize(
    'terminator,expected',
    [
        (None, None),
        (SSEvent(text='fin'), b'data: fin\n\n'),
        (b'data: raw\n\n', b'data: raw\n\n'),
    ],
    ids=['none', 'ssevent', 'bytes'],
)
def test_normal_completion_sends_terminator(terminator, expected):
    async def main():
        options = SSEOptions()
        options.terminator = terminator

        async def gen():
            yield SSEvent(data=b'one')
            yield SSEvent(data=b'two')

        emitter = CountingEmitter(gen())
        send = FakeSend()
        receive = FakeReceive()
        connection = _SSEConnection(emitter, send, receive, options=options)

        baseline = asyncio.all_tasks()
        await connection.run()

        bodies = [b'data: one\n\n', b'data: two\n\n']
        if expected is not None:
            bodies.append(expected)
        assert send.bodies == bodies + [None]
        assert send.messages[-1] == {'type': 'http.response.body'}

        assert not connection.disconnected
        assert connection.last_error is None
        assert emitter.aclose_count == 1
        assert all(task.done() for task in connection.tasks)
        assert_released(connection)
        assert_no_lingering_tasks(baseline)

    falcon.async_to_sync(main)


def test_invalid_terminator_type():
    options = SSEOptions()
    options.terminator = 'data: nope\n\n'

    async def gen():
        yield SSEvent(data=b'one')

    with pytest.raises(TypeError, match='terminator'):
        _SSEConnection(gen(), FakeSend(), FakeReceive(), options=options)


def test_emitter_without_aclose():
    class PlainEmitter:
        async def __aiter__(self):
            yield SSEvent(data=b'one')

    async def main():
        send = FakeSend()
        connection = _SSEConnection(PlainEmitter(), send, FakeReceive())
        await connection.run()

        assert send.bodies == [b'data: one\n\n', None]
        assert connection.last_error is None
        assert_released(connection)

    falcon.async_to_sync(main)


def test_run_twice_raises():
    async def main():
        async def gen():
            yield SSEvent(data=b'one')

        connection = _SSEConnection(gen(), FakeSend(), FakeReceive())
        await connection.run()

        with pytest.raises(RuntimeError, match='already been closed'):
            await connection.run()

    falcon.async_to_sync(main)


def test_cancelling_run_cleans_up():
    async def main():
        finalized = asyncio.Event()

        async def gen():
            try:
                yield SSEvent(data=b'one')
                yield SSEvent(data=b'two')
            finally:
                finalized.set()

        emitter = CountingEmitter(gen())
        send = FakeSend(block_on=0)
        receive = FakeReceive()
        connection = _SSEConnection(emitter, send, receive)

        baseline = asyncio.all_tasks()
        runner = asyncio.create_task(connection.run())

        # NOTE: Cancel the connection itself (e.g., server shutdown)
        #   while the producer is blocked on a slow client.
        await asyncio.wait_for(send.entered.wait(), 5)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

        assert emitter.aclose_count == 1
        assert finalized.is_set()
        assert all(task.done() for task in connection.tasks)
        assert_released(connection)
        assert_no_lingering_tasks(baseline)

    falcon.async_to_sync(main)


def test_app_sse_options_defaults():
    app = App()
    assert isinstance(app.sse_options, SSEOptions)
    assert app.sse_options.heartbeat_interval is None
    assert app.sse_options.terminator is None
    assert app.sse_options.cancel_timeout > 0


def test_app_normal_completion_with_terminator():
    class SomeResource:
        async def on_get(self, req, resp):
            async def emitter():
                yield SSEvent(data=b'one')
                yield SSEvent(data=b'two')

            resp.sse = emitter()

    async def main():
        app = App()
        app.sse_options.terminator = SSEvent(comment='fin')
        app.add_route('/', SomeResource())

        send = FakeSend()
        receive = FakeReceive(events=[HTTP_REQUEST_EVENT])
        baseline = asyncio.all_tasks()

        await app(testing.create_scope(), receive, send)

        assert send.messages[0]['type'] == 'http.response.start'
        headers = dict(send.messages[0]['headers'])
        assert headers[b'content-type'] == b'text/event-stream'
        assert send.bodies == [
            b'data: one\n\n',
            b'data: two\n\n',
            b': fin\n\n',
            None,
        ]
        assert_no_lingering_tasks(baseline)

    falcon.async_to_sync(main)


def test_app_disconnect_and_no_cross_request_leak():
    emitters = []

    class SomeResource:
        async def on_get(self, req, resp):
            async def gen():
                yield SSEvent(data=b'x')
                if not self.finite:
                    await asyncio.sleep(3600)

            emitter = CountingEmitter(gen())
            emitters.append(emitter)
            resp.sse = emitter

        finite = False

    async def main():
        app = App()
        resource = SomeResource()
        app.add_route('/', resource)

        baseline = asyncio.all_tasks()

        # NOTE: First request is cut short by a client disconnect.
        send1 = FakeSend()
        receive1 = FakeReceive(events=[HTTP_REQUEST_EVENT])
        task1 = asyncio.create_task(app(testing.create_scope(), receive1, send1))
        await wait_for_frames(send1, 1)
        receive1.disconnect()
        await asyncio.wait_for(task1, 5)
        assert emitters[0].aclose_count == 1

        # NOTE: Second request runs to completion, unaffected by the first.
        send2 = FakeSend()
        receive2 = FakeReceive(events=[HTTP_REQUEST_EVENT])
        resource.finite = True
        await app(testing.create_scope(), receive2, send2)
        assert emitters[1].aclose_count == 1
        assert send2.bodies == [b'data: x\n\n', None]

        assert_no_lingering_tasks(baseline)

        # NOTE: Nothing from either request is retained by the app.
        refs = [weakref.ref(emitter) for emitter in emitters]
        emitters.clear()
        gc.collect()
        assert all(ref() is None for ref in refs)

    falcon.async_to_sync(main)
