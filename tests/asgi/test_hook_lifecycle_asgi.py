"""ASGI-specific hook terminal states: disconnect and send failures.

These tests drive the ASGI app directly (without the test client) in
order to simulate a client disconnecting mid-stream, and to simulate
servers that raise from send() once the connection is gone. In both
cases the framework must not misreport the disconnect as a server-side
500 error.
"""

import pytest

import falcon
from falcon import asgi
from falcon import testing


class Recorder:
    """Append-only event log used to assert the exact call sequence."""

    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)


class RecordingMiddleware:
    def __init__(self, recorder):
        self._recorder = recorder

    async def process_request(self, req, resp):
        self._recorder('mw.process_request')

    async def process_resource(self, req, resp, resource, params):
        self._recorder('mw.process_resource')

    async def process_response(self, req, resp, resource, req_succeeded):
        self._recorder(f'mw.process_response(req_succeeded={req_succeeded})')


def make_streaming_resource(recorder, chunks=(b'one', b'two', b'three')):
    async def after_hook(req, resp, resource):
        recorder('after:tail')

    class StreamResource:
        @falcon.after(after_hook)
        async def on_get(self, req, resp):
            recorder('responder')

            async def stream():
                for chunk in chunks:
                    recorder('stream:yield')
                    yield chunk

            resp.stream = stream()

    return StreamResource()


def make_error_handler(recorder):
    async def handler(req, resp, ex, params):
        recorder('error_handler:' + type(ex).__name__)
        resp.status = falcon.HTTP_500

    return handler


def make_reporter(recorder):
    def report(req, error, params, handled):
        recorder(f'report:{type(error).__name__}:handled={handled}')

    return report


def create_app(recorder, resource):
    app = asgi.App(middleware=[RecordingMiddleware(recorder)])
    app.add_route('/stream', resource)
    app.add_error_handler(Exception, make_error_handler(recorder))
    app.set_error_reporter(make_reporter(recorder))
    return app


def request_event():
    return {'type': 'http.request', 'body': b'', 'more_body': False}


async def drive(app, receive_events, send_hook=None):
    """Drive the app directly, collecting the sent ASGI messages.

    Once receive_events is exhausted, receive() keeps emitting
    http.disconnect events, as a real server would.
    """

    events = list(receive_events)
    sent = []

    async def receive():
        if events:
            return events.pop(0)
        return {'type': 'http.disconnect'}

    async def send(message):
        sent.append(message)
        if send_hook is not None:
            send_hook(message)

    scope = testing.create_scope(path='/stream')
    await app(scope, receive, send)
    return sent


async def test_disconnect_before_first_request_event():
    recorder = Recorder()
    resource = make_streaming_resource(recorder)
    app = create_app(recorder, resource)

    sent = await drive(app, [{'type': 'http.disconnect'}])

    # The app bails out immediately; no middleware or hook ever runs.
    assert sent == []
    assert recorder.events == []


async def test_disconnect_during_streaming():
    recorder = Recorder()
    resource = make_streaming_resource(recorder)
    app = create_app(recorder, resource)

    # NOTE: After the first event, receive() only emits http.disconnect,
    # and send() acts as a no-op, per the ASGI spec.
    sent = await drive(app, [request_event()])

    # The after hook had already completed when the responder returned;
    # the disconnect does not turn into an error of any kind.
    assert recorder.events == [
        'mw.process_request',
        'mw.process_resource',
        'responder',
        'after:tail',
        'mw.process_response(req_succeeded=True)',
        'stream:yield',
        'stream:yield',
        'stream:yield',
    ]
    assert [message['type'] for message in sent] == [
        'http.response.start',
        'http.response.body',
        'http.response.body',
        'http.response.body',
        'http.response.body',
    ]
    assert sent[0]['status'] == 200


async def test_send_raises_after_response_started():
    recorder = Recorder()
    resource = make_streaming_resource(recorder)
    app = create_app(recorder, resource)

    class DisconnectError(Exception):
        pass

    def send_hook(message):
        # Simulate a server that raises from send() once the client is
        # gone (the response start and the first chunk went through).
        if message['type'] == 'http.response.body':
            raise DisconnectError('client is gone')

    events = [request_event()]
    sent = []

    async def receive():
        if events:
            return events.pop(0)
        return {'type': 'http.disconnect'}

    async def send(message):
        sent.append(message)
        send_hook(message)

    scope = testing.create_scope(path='/stream')

    # The exception raised by send() propagates to the server unchanged;
    # since the response has already started (200), the framework does
    # not and cannot convert it into a 500 error response.
    with pytest.raises(DisconnectError):
        await app(scope, receive, send)

    assert sent[0]['type'] == 'http.response.start'
    assert sent[0]['status'] == 200
    assert recorder.events == [
        'mw.process_request',
        'mw.process_resource',
        'responder',
        'after:tail',
        'mw.process_response(req_succeeded=True)',
        'stream:yield',
    ]
