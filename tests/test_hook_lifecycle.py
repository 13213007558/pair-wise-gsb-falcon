"""Paired WSGI/ASGI tests pinning the terminal state of hooks.

A single request traverses middleware, before hooks, the responder,
after hooks, and (on failure) error handlers. Each test below runs
against both the WSGI and the ASGI app flavor (via the "asgi"
fixture) and asserts, for exactly one request, the precise call
sequence, the resulting response status, and the exception chain
surfaced through the error reporter.
"""

import pytest

import falcon
from falcon import testing


class LifecycleError(Exception):
    """Distinct error type used to trace error-handling paths."""


class Recorder:
    """Append-only event log used to assert the exact call sequence."""

    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)


# --------------------------------------------------------------------
# Hooks
# --------------------------------------------------------------------


def make_before_hook(recorder, name, error=None):
    def action(req, resp, resource, params):
        recorder('before:' + name)
        if error is not None:
            raise error

    return action


def make_after_hook(recorder, name, error=None):
    def action(req, resp, resource):
        recorder('after:' + name)
        if error is not None:
            raise error

    return action


def make_before_hook_async(recorder, name, error=None):
    async def action(req, resp, resource, params):
        recorder('before:' + name)
        if error is not None:
            raise error

    return action


def make_after_hook_async(recorder, name, error=None):
    async def action(req, resp, resource):
        recorder('after:' + name)
        if error is not None:
            raise error

    return action


def make_hook(asgi, recorder, kind, name, error=None, asynchronous=False):
    """Make a hook, optionally as a coroutine function.

    Coroutine hooks are only meaningful for the ASGI flavor; mixing
    sync and async hooks in a single resource exercises the framework's
    support for combining both styles.
    """

    assert kind in ('before', 'after')
    if asynchronous:
        assert asgi
        factory = make_before_hook_async if kind == 'before' else make_after_hook_async
    else:
        factory = make_before_hook if kind == 'before' else make_after_hook
    return factory(recorder, name, error)


# --------------------------------------------------------------------
# Middleware, resources, and error handlers
# --------------------------------------------------------------------


class RecordingMiddleware:
    """Middleware recording each phase, optionally raising from one."""

    def __init__(self, recorder, name='mw', raise_in=None, error=None):
        self._recorder = recorder
        self._name = name
        self._raise_in = raise_in
        self._error = error

    def process_request(self, req, resp):
        self._record('process_request')

    def process_resource(self, req, resp, resource, params):
        self._record('process_resource')

    def process_response(self, req, resp, resource, req_succeeded):
        self._recorder(f'{self._name}.process_response(req_succeeded={req_succeeded})')
        if self._raise_in == 'process_response':
            raise self._error

    def _record(self, phase):
        self._recorder(self._name + '.' + phase)
        if phase == self._raise_in:
            raise self._error


def build_resource(
    asgi, recorder, before_hooks=(), after_hooks=(), error=None, stream=None
):
    """Build a resource class, applying hooks as stacked decorators.

    Hooks are applied in the same order as if they had been stacked as
    decorators above the responder, top-most first.
    """

    if asgi:

        async def on_get(self, req, resp, thing_id):
            recorder('responder')
            if error is not None:
                raise error
            if stream is not None:
                resp.stream = stream
            else:
                resp.text = 'pass'

    else:

        def on_get(self, req, resp, thing_id):
            recorder('responder')
            if error is not None:
                raise error
            if stream is not None:
                resp.stream = stream
            else:
                resp.text = 'pass'

    for hook in reversed(before_hooks):
        on_get = falcon.before(hook)(on_get)

    for hook in reversed(after_hooks):
        on_get = falcon.after(hook)(on_get)

    return type('LifecycleResource', (), {'on_get': on_get})


def make_error_handler(recorder, status=falcon.HTTP_500, raise_error=None):
    def handler(req, resp, ex, params):
        recorder('error_handler:' + type(ex).__name__)
        if raise_error is not None:
            raise raise_error from ex
        resp.status = status
        resp.text = 'handled'

    return handler


def build_client(asgi, util, resource, middleware=(), error_handlers=(), **app_kwargs):
    app = util.create_app(asgi, middleware=middleware, **app_kwargs)
    for exception, handler in error_handlers:
        app.add_error_handler(exception, handler)
    app.add_route('/things/{thing_id}', resource())
    return testing.TestClient(app)


@pytest.fixture
def recorder():
    return Recorder()


@pytest.fixture
def reported():
    return []


@pytest.fixture
def reporter(reported):
    def report(req, error, params, handled):
        reported.append((error, handled))

    return report


# --------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------


def test_success_nested_hooks(asgi, util, recorder):
    resource = build_resource(
        asgi,
        recorder,
        before_hooks=(
            make_hook(asgi, recorder, 'before', 'outer'),
            make_hook(asgi, recorder, 'before', 'inner', asynchronous=asgi),
        ),
        after_hooks=(
            make_hook(asgi, recorder, 'after', 'outer'),
            make_hook(asgi, recorder, 'after', 'inner', asynchronous=asgi),
        ),
    )
    client = build_client(
        asgi, util, resource, middleware=[RecordingMiddleware(recorder)]
    )

    result = client.simulate_get('/things/42')

    assert result.status_code == 200
    assert result.text == 'pass'
    assert recorder.events == [
        'mw.process_request',
        'mw.process_resource',
        'before:outer',
        'before:inner',
        'responder',
        'after:inner',
        'after:outer',
        'mw.process_response(req_succeeded=True)',
    ]


def test_nested_class_and_method_hooks(asgi, util, recorder):
    resource = build_resource(
        asgi,
        recorder,
        before_hooks=(make_hook(asgi, recorder, 'before', 'method'),),
        after_hooks=(make_hook(asgi, recorder, 'after', 'method'),),
    )
    resource = falcon.before(make_hook(asgi, recorder, 'before', 'class'))(resource)
    resource = falcon.after(make_hook(asgi, recorder, 'after', 'class'))(resource)

    client = build_client(asgi, util, resource)
    result = client.simulate_get('/things/42')

    assert result.status_code == 200
    assert recorder.events == [
        'before:class',
        'before:method',
        'responder',
        'after:method',
        'after:class',
    ]


def test_before_hook_raises_http_error(asgi, util, recorder, reporter, reported):
    resource = build_resource(
        asgi,
        recorder,
        before_hooks=(
            make_hook(asgi, recorder, 'before', 'open'),
            make_hook(
                asgi,
                recorder,
                'before',
                'guard',
                error=falcon.HTTPForbidden(title='Nope'),
                asynchronous=asgi,
            ),
        ),
        after_hooks=(make_hook(asgi, recorder, 'after', 'never'),),
    )
    client = build_client(
        asgi, util, resource, middleware=[RecordingMiddleware(recorder)]
    )
    client.app.set_error_reporter(reporter)

    result = client.simulate_get('/things/42')

    assert result.status_code == 403
    assert recorder.events == [
        'mw.process_request',
        'mw.process_resource',
        'before:open',
        'before:guard',
        'mw.process_response(req_succeeded=False)',
    ]
    assert [(type(ex), handled) for ex, handled in reported] == [
        (falcon.HTTPForbidden, True),
    ]


def test_before_hook_raises_generic_error(asgi, util, recorder, reporter, reported):
    error = LifecycleError('before failed')
    resource = build_resource(
        asgi,
        recorder,
        before_hooks=(make_hook(asgi, recorder, 'before', 'guard', error=error),),
        after_hooks=(make_hook(asgi, recorder, 'after', 'never'),),
    )
    client = build_client(
        asgi,
        util,
        resource,
        middleware=[RecordingMiddleware(recorder)],
        error_handlers=[(LifecycleError, make_error_handler(recorder))],
    )
    client.app.set_error_reporter(reporter)

    result = client.simulate_get('/things/42')

    assert result.status_code == 500
    assert result.text == 'handled'
    assert recorder.events == [
        'mw.process_request',
        'mw.process_resource',
        'before:guard',
        'error_handler:LifecycleError',
        'mw.process_response(req_succeeded=False)',
    ]
    # The handler received the exact exception object raised by the hook.
    assert reported == [(error, True)]


def test_responder_raises(asgi, util, recorder, reporter, reported):
    error = LifecycleError('responder failed')
    resource = build_resource(
        asgi,
        recorder,
        before_hooks=(make_hook(asgi, recorder, 'before', 'open'),),
        after_hooks=(make_hook(asgi, recorder, 'after', 'never'),),
        error=error,
    )
    client = build_client(
        asgi,
        util,
        resource,
        middleware=[RecordingMiddleware(recorder)],
        error_handlers=[(LifecycleError, make_error_handler(recorder))],
    )
    client.app.set_error_reporter(reporter)

    result = client.simulate_get('/things/42')

    assert result.status_code == 500
    assert recorder.events == [
        'mw.process_request',
        'mw.process_resource',
        'before:open',
        'responder',
        'error_handler:LifecycleError',
        'mw.process_response(req_succeeded=False)',
    ]
    assert reported == [(error, True)]


def test_after_hook_raises(asgi, util, recorder, reporter, reported):
    error = LifecycleError('after failed')
    resource = build_resource(
        asgi,
        recorder,
        after_hooks=(
            make_hook(asgi, recorder, 'after', 'outer'),
            make_hook(asgi, recorder, 'after', 'inner', error=error),
        ),
    )
    client = build_client(
        asgi,
        util,
        resource,
        middleware=[RecordingMiddleware(recorder)],
        error_handlers=[(LifecycleError, make_error_handler(recorder))],
    )
    client.app.set_error_reporter(reporter)

    result = client.simulate_get('/things/42')

    assert result.status_code == 500
    # The inner after hook runs first; its failure prevents the outer one.
    assert recorder.events == [
        'mw.process_request',
        'mw.process_resource',
        'responder',
        'after:inner',
        'error_handler:LifecycleError',
        'mw.process_response(req_succeeded=False)',
    ]
    assert reported == [(error, True)]


def test_middleware_process_request_raises(asgi, util, recorder, reporter, reported):
    error = LifecycleError('process_request failed')
    middleware = RecordingMiddleware(recorder, raise_in='process_request', error=error)
    resource = build_resource(
        asgi,
        recorder,
        before_hooks=(make_hook(asgi, recorder, 'before', 'never'),),
        after_hooks=(make_hook(asgi, recorder, 'after', 'never'),),
    )
    client = build_client(
        asgi,
        util,
        resource,
        middleware=[middleware],
        error_handlers=[(LifecycleError, make_error_handler(recorder))],
    )
    client.app.set_error_reporter(reporter)

    result = client.simulate_get('/things/42')

    assert result.status_code == 500
    # With the default independent middleware, process_response() is
    # invoked even though process_request() failed.
    assert recorder.events == [
        'mw.process_request',
        'error_handler:LifecycleError',
        'mw.process_response(req_succeeded=False)',
    ]
    assert reported == [(error, True)]


def test_middleware_process_request_raises_dependent(
    asgi, util, recorder, reporter, reported
):
    error = LifecycleError('process_request failed')
    middleware = RecordingMiddleware(recorder, raise_in='process_request', error=error)
    resource = build_resource(
        asgi,
        recorder,
        before_hooks=(make_hook(asgi, recorder, 'before', 'never'),),
        after_hooks=(make_hook(asgi, recorder, 'after', 'never'),),
    )
    client = build_client(
        asgi,
        util,
        resource,
        middleware=[middleware],
        error_handlers=[(LifecycleError, make_error_handler(recorder))],
        independent_middleware=False,
    )
    client.app.set_error_reporter(reporter)

    result = client.simulate_get('/things/42')

    assert result.status_code == 500
    # Dependent middleware never reached the responder, so its
    # process_response() is not queued either.
    assert recorder.events == [
        'mw.process_request',
        'error_handler:LifecycleError',
    ]
    assert reported == [(error, True)]


def test_middleware_process_response_raises(asgi, util, recorder, reporter, reported):
    error = LifecycleError('process_response failed')
    middleware = RecordingMiddleware(recorder, raise_in='process_response', error=error)
    resource = build_resource(
        asgi,
        recorder,
        before_hooks=(make_hook(asgi, recorder, 'before', 'open'),),
        after_hooks=(make_hook(asgi, recorder, 'after', 'tail'),),
    )
    client = build_client(
        asgi,
        util,
        resource,
        middleware=[middleware],
        error_handlers=[(LifecycleError, make_error_handler(recorder))],
    )
    client.app.set_error_reporter(reporter)

    result = client.simulate_get('/things/42')

    assert result.status_code == 500
    assert recorder.events == [
        'mw.process_request',
        'mw.process_resource',
        'before:open',
        'responder',
        'after:tail',
        'mw.process_response(req_succeeded=True)',
        'error_handler:LifecycleError',
    ]
    assert reported == [(error, True)]


def test_error_handler_priority(asgi, util, recorder):
    error = LifecycleError('responder failed')
    resource = build_resource(asgi, recorder, error=error)
    client = build_client(
        asgi,
        util,
        resource,
        error_handlers=[
            (Exception, make_error_handler(recorder, status=falcon.HTTP_500)),
            (
                LifecycleError,
                make_error_handler(recorder, status=falcon.HTTP_503),
            ),
        ],
    )

    result = client.simulate_get('/things/42')

    # The most specific handler wins, regardless of registration order.
    assert result.status_code == 503
    assert recorder.events == ['responder', 'error_handler:LifecycleError']


def test_error_handler_raises_http_error_chain(
    asgi, util, recorder, reporter, reported
):
    root_cause = LifecycleError('responder failed')
    resource = build_resource(asgi, recorder, error=root_cause)
    handler = make_error_handler(
        recorder, raise_error=falcon.HTTPBadRequest(title='Wrapped')
    )
    client = build_client(
        asgi, util, resource, error_handlers=[(LifecycleError, handler)]
    )
    client.app.set_error_reporter(reporter)

    result = client.simulate_get('/things/42')

    assert result.status_code == 400
    assert recorder.events == ['responder', 'error_handler:LifecycleError']

    # Both the original error and the HTTPError raised by the handler are
    # reported as handled, and the exception chain links them together.
    assert [handled for _, handled in reported] == [True, True]
    original, http_error = (ex for ex, _ in reported)
    assert original is root_cause
    assert isinstance(http_error, falcon.HTTPBadRequest)
    assert http_error.__cause__ is root_cause


def test_stream_after_hook_completes_before_streaming(asgi, util, recorder):
    if asgi:

        async def stream():
            recorder('stream:consume')
            yield b'data'

    else:

        def stream():
            recorder('stream:consume')
            yield b'data'

    resource = build_resource(
        asgi,
        recorder,
        after_hooks=(make_hook(asgi, recorder, 'after', 'tail'),),
        stream=stream(),
    )
    client = build_client(asgi, util, resource)

    result = client.simulate_get('/things/42')

    assert result.status_code == 200
    assert result.content == b'data'
    # The after hook completes when the responder returns; it does not
    # wait for the stream to be consumed.
    assert recorder.events == ['responder', 'after:tail', 'stream:consume']


def test_stream_raises_after_response_started(asgi, util, recorder, reporter, reported):
    error = LifecycleError('stream failed')
    if asgi:

        async def stream():
            recorder('stream:consume')
            yield b'chunk'
            raise error

    else:

        def stream():
            recorder('stream:consume')
            yield b'chunk'
            raise error

    resource = build_resource(
        asgi,
        recorder,
        after_hooks=(make_hook(asgi, recorder, 'after', 'tail'),),
        stream=stream(),
    )
    client = build_client(
        asgi,
        util,
        resource,
        error_handlers=[(LifecycleError, make_error_handler(recorder))],
    )
    client.app.set_error_reporter(reporter)

    with pytest.raises(LifecycleError) as excinfo:
        client.simulate_get('/things/42')

    # The response had already started (200), so the error can no longer
    # be converted into an error response: it propagates to the server
    # unchanged, without involving error handlers or the reporter.
    assert excinfo.value is error
    assert recorder.events == ['responder', 'after:tail', 'stream:consume']
    assert reported == []
