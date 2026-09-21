import asyncio

import pytest

import falcon
from falcon import testing
from falcon.asgi_spec import EventType
from falcon.testing import helpers


class LifecycleError(Exception):
    """Base class for errors raised by lifecycle test fixtures."""


class BeforeHookError(LifecycleError):
    """Raised by a before hook in the lifecycle test fixtures."""


class ResourceError(LifecycleError):
    """Raised by the resource in the lifecycle test fixtures."""


class AfterHookError(LifecycleError):
    """Raised by an after hook in the lifecycle test fixtures."""


class MiddlewareError(LifecycleError):
    """Raised by middleware in the lifecycle test fixtures."""


class StreamError(LifecycleError):
    """Raised after a streamed response has already started."""


class ClientGoneError(Exception):
    """Raised by a test server after its client has disconnected."""


class SendError(Exception):
    """Raised by the test ASGI sender after response start."""


class Trace:
    """Record the terminal state of one request without shared state."""

    def __init__(self, mode):
        self.mode = mode
        self.events = []
        self.reports = []
        self.handlers = []
        self.stream = None

    def record(self, event):
        """Append a lifecycle event to this request's trace."""
        self.events.append(event)

    def report(self, req, ex, params, handled):
        """Record an error reporter invocation for this request."""
        self.reports.append((type(ex).__name__, handled, ex))


def make_cause(trace, label):
    """Create a distinct cause for the selected terminal exception."""
    return RuntimeError(f'{trace.mode}:{label}')


def invoke_named_hook(trace, name):
    """Record one hook and raise its configured terminal error."""
    trace.record(name)

    if trace.mode == 'before_error' and name == 'class_before_outer':
        raise BeforeHookError(name) from make_cause(trace, name)

    if trace.mode == 'after_error' and name == 'method_after_inner':
        raise AfterHookError(name) from make_cause(trace, name)


def make_hook(trace, name, asgi, force_sync=False):
    """Create one hook bound to this request's trace."""

    def hook(req, resp, resource, params=None):
        invoke_named_hook(trace, name)

    async def hook_async(req, resp, resource, params=None):
        invoke_named_hook(trace, name)

    return hook if not asgi or force_sync else hook_async


class SyncLifecycleStream:
    """Synchronous response stream with an explicit post-start failure."""

    def __init__(self, trace, failure_type):
        self._trace = trace
        self._failure_type = failure_type
        self._started = False
        self.closed = False

    def __iter__(self):
        """Return the WSGI response iterator."""
        return self

    def __next__(self):
        """Yield one chunk and then enter the configured terminal state."""
        if not self._started:
            self._started = True
            self._trace.record('stream:first')
            return b'first'

        if self._failure_type is StreamError:
            raise StreamError('stream') from make_cause(self._trace, 'stream')

        raise ClientGoneError('client disconnected') from ConnectionResetError()

    def close(self):
        """Record that the WSGI server closed the stream."""
        self.closed = True
        self._trace.record('stream:close')


class AsyncLifecycleStream:
    """Asynchronous response stream with explicit terminal transitions."""

    def __init__(self, trace, req):
        self._trace = trace
        self._req = req
        self._state = 'first'
        self.closed = False

    def __aiter__(self):
        """Return the ASGI response iterator."""
        return self

    async def __anext__(self):
        """Yield one chunk and then enter the configured terminal state."""
        if self._state == 'first':
            self._state = self._trace.mode
            self._trace.record('stream:first')
            return b'first'

        if self._state == 'stream_error':
            raise StreamError('stream') from make_cause(self._trace, 'stream')

        if self._state == 'disconnect':
            event = await self._req._receive()
            while event['type'] != EventType.HTTP_DISCONNECT:
                event = await self._req._receive()
            assert event['type'] == EventType.HTTP_DISCONNECT
            self._trace.record('stream:disconnect')
            self._state = 'disconnected'
            return b''

        if self._state == 'cancel':
            await asyncio.sleep(3600)

        raise StopAsyncIteration

    async def close(self):
        """Record that the ASGI server closed the stream."""
        self.closed = True
        self._trace.record('stream:close')


def make_resource(trace, asgi):
    """Create the nested resource and method/class-level hooks."""

    @falcon.after(make_hook(trace, 'class_after_outer', asgi))
    @falcon.after(make_hook(trace, 'class_after_inner', asgi, True))
    @falcon.before(make_hook(trace, 'class_before_outer', asgi))
    @falcon.before(make_hook(trace, 'class_before_inner', asgi))
    class AsyncLifecycleResource:
        """Asynchronous resource with nested hooks."""

        @falcon.before(make_hook(trace, 'method_before_outer', asgi))
        @falcon.before(make_hook(trace, 'method_before_inner', asgi, True))
        @falcon.after(make_hook(trace, 'method_after_outer', asgi))
        @falcon.after(make_hook(trace, 'method_after_inner', asgi, True))
        async def on_get(self, req, resp):
            """Run the asynchronous resource terminal action."""
            trace.record('resource')

            if trace.mode == 'resource_error':
                raise ResourceError('resource') from make_cause(trace, 'resource')

            if trace.mode in {'stream_error', 'send_error', 'disconnect', 'cancel'}:
                trace.stream = AsyncLifecycleStream(trace, req)
                resp.stream = trace.stream
            else:
                resp.text = 'created'

    @falcon.after(make_hook(trace, 'class_after_outer', asgi))
    @falcon.after(make_hook(trace, 'class_after_inner', asgi, True))
    @falcon.before(make_hook(trace, 'class_before_outer', asgi))
    @falcon.before(make_hook(trace, 'class_before_inner', asgi))
    class WSGILifecycleResource:
        """Synchronous resource with nested hooks."""

        @falcon.before(make_hook(trace, 'method_before_outer', asgi))
        @falcon.before(make_hook(trace, 'method_before_inner', asgi, True))
        @falcon.after(make_hook(trace, 'method_after_outer', asgi))
        @falcon.after(make_hook(trace, 'method_after_inner', asgi, True))
        def on_get(self, req, resp):
            """Run the synchronous resource terminal action."""
            trace.record('resource')

            if trace.mode == 'resource_error':
                raise ResourceError('resource') from make_cause(trace, 'resource')

            if trace.mode in {'stream_error', 'disconnect'}:
                failure_type = (
                    StreamError if trace.mode == 'stream_error' else ClientGoneError
                )
                trace.stream = SyncLifecycleStream(trace, failure_type)
                resp.stream = trace.stream
            else:
                resp.text = 'created'

    return AsyncLifecycleResource() if asgi else WSGILifecycleResource()


class SyncTrackingMiddleware:
    """Synchronous middleware that logs every stage it passes through."""

    def __init__(self, name, trace):
        self._name = name
        self._trace = trace

    def process_request(self, req, resp):
        """Log the request middleware call or raise its configured error."""
        self._trace.record(f'{self._name}:request')
        if self._trace.mode == 'middleware_request_error' and self._name == 'C':
            raise MiddlewareError('request') from make_cause(
                self._trace, 'middleware-request'
            )

    def process_resource(self, req, resp, resource, params):
        """Log the resource middleware call."""
        self._trace.record(f'{self._name}:resource')

    def process_response(self, req, resp, resource, req_succeeded):
        """Log the response middleware call or raise its configured error."""
        self._trace.record(f'{self._name}:response')
        if self._trace.mode == 'middleware_response_error' and self._name == 'C':
            raise MiddlewareError('response') from make_cause(
                self._trace, 'middleware-response'
            )


class MixedTrackingMiddleware:
    """Asynchronous middleware mixed into the same ASGI middleware stack."""

    def __init__(self, name, trace):
        self._name = name
        self._trace = trace

    async def process_request_async(self, req, resp):
        """Log the asynchronous request middleware call."""
        self._trace.record(f'{self._name}:request')

    async def process_resource_async(self, req, resp, resource, params):
        """Log the asynchronous resource middleware call."""
        self._trace.record(f'{self._name}:resource')

    async def process_response_async(self, req, resp, resource, req_succeeded):
        """Log the asynchronous response middleware call."""
        self._trace.record(f'{self._name}:response')


def handle_lifecycle_error(req, resp, ex, params, trace, status):
    """Handle lifecycle errors with a deterministic response status."""
    trace.handlers.append((type(ex).__name__, status))
    resp.status = status
    resp.text = f'handled:{type(ex).__name__}'


def create_app(trace, asgi):
    """Create a WSGI or ASGI app with independent middleware and hooks."""
    if asgi:
        middleware = [
            SyncTrackingMiddleware('A', trace),
            MixedTrackingMiddleware('B', trace),
            SyncTrackingMiddleware('C', trace),
        ]
        app = falcon.asgi.App(
            middleware=middleware,
            independent_middleware=True,
        )
    else:
        middleware = [SyncTrackingMiddleware(name, trace) for name in 'ABC']
        app = falcon.App(middleware=middleware, independent_middleware=True)

    app.add_route('/', make_resource(trace, asgi))
    app.set_error_reporter(trace.report)

    def handle_generic(req, resp, ex, params):
        handle_lifecycle_error(req, resp, ex, params, trace, falcon.HTTP_422)

    def handle_resource(req, resp, ex, params):
        handle_lifecycle_error(req, resp, ex, params, trace, falcon.HTTP_418)

    async def handle_generic_async(req, resp, ex, params):
        handle_lifecycle_error(req, resp, ex, params, trace, falcon.HTTP_422)

    async def handle_resource_async(req, resp, ex, params):
        handle_lifecycle_error(req, resp, ex, params, trace, falcon.HTTP_418)

    generic = handle_generic_async if asgi else handle_generic
    specific = handle_resource_async if asgi else handle_resource
    app.add_error_handler(LifecycleError, generic)
    app.add_error_handler(ResourceError, specific)
    return app


def prefix_events():
    """Return the shared middleware events up to nested hooks."""
    return [
        'A:request',
        'B:request',
        'C:request',
        'A:resource',
        'B:resource',
        'C:resource',
    ]


def hook_events(after_index=None, before_index=4, include_resource=True):
    """Return nested hook events through the requested terminal hook."""
    before_events = [
        'class_before_outer',
        'class_before_inner',
        'method_before_outer',
        'method_before_inner',
    ]
    events = before_events[:before_index]
    if include_resource:
        events.append('resource')
    after_events = [
        'method_after_inner',
        'method_after_outer',
        'class_after_inner',
        'class_after_outer',
    ]
    if after_index is not None:
        events.extend(after_events[:after_index])
    return events


def complete_events(after_index=4, **kwargs):
    """Return all request, hook, and response middleware events."""
    return prefix_events() + hook_events(after_index, **kwargs) + response_events()


def streaming_events(*terminal_events):
    """Return events through middleware and post-start stream termination."""
    return complete_events() + list(terminal_events)


def response_events(include_all=True):
    """Return response middleware events in reverse registration order."""
    events = ['C:response']
    if include_all:
        events.extend(['B:response', 'A:response'])
    return events


def assert_report(trace, report_name, handled, cause_label):
    """Assert one terminal report and its exception chain."""
    assert len(trace.reports) == 1
    name, was_handled, error = trace.reports[0]
    assert (name, was_handled) == (report_name, handled)
    if cause_label:
        assert str(error.__cause__) == f'{trace.mode}:{cause_label}'
    else:
        assert error.__cause__ is None


@pytest.mark.parametrize('asgi', [False, True], ids=['wsgi', 'asgi'])
@pytest.mark.parametrize(
    ('mode', 'report_name', 'status_code', 'expected_events', 'cause_label'),
    [
        (
            'nested',
            None,
            200,
            complete_events(),
            None,
        ),
        (
            'before_error',
            'BeforeHookError',
            422,
            complete_events(
                after_index=None,
                before_index=1,
                include_resource=False,
            ),
            'class_before_outer',
        ),
        (
            'resource_error',
            'ResourceError',
            418,
            complete_events(after_index=None),
            'resource',
        ),
        (
            'after_error',
            'AfterHookError',
            422,
            complete_events(after_index=1),
            'method_after_inner',
        ),
        (
            'middleware_request_error',
            'MiddlewareError',
            422,
            ['A:request', 'B:request', 'C:request'] + response_events(),
            'middleware-request',
        ),
        (
            'middleware_response_error',
            'MiddlewareError',
            422,
            complete_events(),
            'middleware-response',
        ),
    ],
    ids=[
        'nested',
        'before-hook',
        'resource',
        'after-hook',
        'middleware-request',
        'middleware-response',
    ],
)
def test_lifecycle_before_sending(
    asgi,
    mode,
    report_name,
    status_code,
    expected_events,
    cause_label,
):
    trace = Trace(mode)
    app = create_app(trace, asgi)

    result = testing.simulate_get(app, '/')

    assert result.status_code == status_code
    assert trace.events == expected_events

    if report_name:
        assert len(trace.handlers) == 1
        handled_name, handled_status = trace.handlers[0]
        assert handled_name == report_name
        assert handled_status.startswith(str(status_code))
        assert_report(trace, report_name, True, cause_label)
        assert result.text == f'handled:{report_name}'
    else:
        assert trace.handlers == []
        assert trace.reports == []
        assert result.text == 'created'


@pytest.mark.parametrize('asgi', [False, True], ids=['wsgi', 'asgi'])
def test_repeated_nested_requests_do_not_deduplicate_hooks(asgi):
    trace = Trace('nested')
    app = create_app(trace, asgi)

    first = testing.simulate_get(app, '/')
    second = testing.simulate_get(app, '/')

    expected_events = complete_events() * 2
    assert first.status_code == second.status_code == 200
    assert trace.events == expected_events
    assert trace.handlers == []
    assert trace.reports == []


@pytest.mark.parametrize(
    ('mode', 'error_type', 'cause_type', 'cause_label'),
    [
        ('stream_error', StreamError, RuntimeError, 'stream'),
        ('disconnect', ClientGoneError, ConnectionResetError, None),
    ],
    ids=['stream-error-after-start', 'client-disconnect-after-start'],
)
def test_wsgi_terminal_states_after_response_started(
    mode, error_type, cause_type, cause_label
):
    trace = Trace(mode)
    app = create_app(trace, asgi=False)
    start_response = testing.StartResponseMock()
    env = helpers.create_environ()
    response = app(env, start_response)

    assert next(response) == b'first'
    assert start_response.status == '200 OK'

    with pytest.raises(error_type) as error_info:
        next(response)

    response.close()

    assert start_response.call_count == 1
    assert trace.events == streaming_events('stream:first', 'stream:close')
    assert trace.handlers == []
    assert trace.reports == []
    assert isinstance(error_info.value.__cause__, cause_type)
    if cause_label:
        assert str(error_info.value.__cause__) == f'{mode}:{cause_label}'
    assert trace.stream.closed


class ASGILifecycleSender:
    """Collect ASGI events and optionally fail after response start."""

    def __init__(self, mode):
        self.mode = mode
        self.events = []
        self.status = None

    async def __call__(self, event):
        """Record a send event or fail the terminal EOF send."""
        self.events.append(event)

        if event['type'] == EventType.HTTP_RESPONSE_START:
            self.status = event['status']

        if (
            self.mode == 'send_error'
            and event['type'] == EventType.HTTP_RESPONSE_BODY
            and not event.get('more_body', False)
        ):
            raise SendError('send') from RuntimeError('send_error:send')


async def invoke_asgi_stream(trace, mode):
    """Invoke the ASGI stream scenario and return its task and sender."""
    app = create_app(trace, asgi=True)
    scope = helpers.create_scope()
    emitter = helpers.ASGIRequestEventEmitter(b'')
    sender = ASGILifecycleSender(mode)
    task = asyncio.create_task(app(scope, emitter.emit, sender))

    while not sender.events:
        await asyncio.sleep(0)

    if mode == 'disconnect':
        while not any(event.get('body') == b'first' for event in sender.events):
            await asyncio.sleep(0)
        emitter.disconnect()

    return task, sender


async def test_asgi_stream_error_after_response_started():
    trace = Trace('stream_error')
    task, sender = await invoke_asgi_stream(trace, 'stream_error')

    with pytest.raises(StreamError) as error_info:
        await task

    assert sender.status == 200
    assert [event['type'] for event in sender.events] == [
        EventType.HTTP_RESPONSE_START,
        EventType.HTTP_RESPONSE_BODY,
    ]
    assert trace.events == streaming_events('stream:first', 'stream:close')
    assert trace.handlers == []
    assert trace.reports == []
    assert str(error_info.value.__cause__) == 'stream_error:stream'
    assert trace.stream.closed


async def test_asgi_send_error_after_response_started():
    trace = Trace('send_error')
    task, sender = await invoke_asgi_stream(trace, 'send_error')

    with pytest.raises(SendError) as error_info:
        await task

    assert sender.status == 200
    assert [event['type'] for event in sender.events] == [
        EventType.HTTP_RESPONSE_START,
        EventType.HTTP_RESPONSE_BODY,
        EventType.HTTP_RESPONSE_BODY,
    ]
    assert trace.events == streaming_events('stream:first', 'stream:close')
    assert trace.handlers == []
    assert trace.reports == []
    assert str(error_info.value.__cause__) == 'send_error:send'
    assert trace.stream.closed


async def test_asgi_disconnect_after_response_started():
    trace = Trace('disconnect')
    task, sender = await invoke_asgi_stream(trace, 'disconnect')

    await task

    assert sender.status == 200
    assert [event['type'] for event in sender.events] == [
        EventType.HTTP_RESPONSE_START,
        EventType.HTTP_RESPONSE_BODY,
        EventType.HTTP_RESPONSE_BODY,
        EventType.HTTP_RESPONSE_BODY,
    ]
    assert trace.events == streaming_events(
        'stream:first',
        'stream:disconnect',
        'stream:close',
    )
    assert trace.handlers == []
    assert trace.reports == []
    assert trace.stream.closed


async def test_asgi_cancellation_is_not_reported_as_server_error():
    trace = Trace('cancel')
    task, sender = await invoke_asgi_stream(trace, 'cancel')

    while sender.status != 200:
        await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert trace.events == streaming_events('stream:first', 'stream:close')
    assert trace.handlers == []
    assert trace.reports == []
    assert trace.stream.closed


async def test_asgi_immediate_disconnect_runs_no_hooks():
    trace = Trace('disconnect')
    app = create_app(trace, asgi=True)
    scope = helpers.create_scope()
    emitter = helpers.ASGIRequestEventEmitter(b'', disconnect_at=0)
    sender = ASGILifecycleSender('disconnect')

    await app(scope, emitter.emit, sender)

    assert sender.events == []
    assert trace.events == []
    assert trace.handlers == []
    assert trace.reports == []
