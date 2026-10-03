import pytest

import falcon
from falcon import testing
import falcon.asgi
from falcon.routing.compiled import UnacceptableRouteError


class CloseCode:
    BAD_REQUEST = 3400
    FORBIDDEN = 3403
    NOT_FOUND = 3404


class EchoResource:
    """Resource that records its handshakes and echoes one message."""

    def __init__(self, name):
        self.name = name
        self.handshakes = []

    async def on_websocket(self, req, ws, **params):
        self.handshakes.append((ws.subprotocol, params))
        await ws.accept()
        message = await ws.receive_text()
        await ws.send_text(f'{self.name}:{message}')


@pytest.fixture
def app():
    return falcon.asgi.App()


@pytest.fixture
def conductor(app):
    return testing.ASGIConductor(app)


@pytest.mark.parametrize(
    'subprotocols',
    [
        ['chat.v1', 'chat.v2'],
        ['chat.v2', 'chat.v1'],
    ],
)
async def test_server_priority_overrides_client_order(app, conductor, subprotocols):
    v1 = EchoResource('v1')
    v2 = EchoResource('v2')
    app.add_websocket_route('/chat', v1, subprotocols='chat.v1')
    app.add_websocket_route('/chat', v2, subprotocols='chat.v2')

    async with conductor as client:
        async with client.simulate_ws('/chat', subprotocols=subprotocols) as ws:
            assert ws.subprotocol == 'chat.v1'
            await ws.send_text('hello')
            assert await ws.receive_text() == 'v1:hello'

    # The resource registered first wins regardless of the order in
    #   which the client advertised its candidates, and the other resource
    #   is never invoked.
    assert v1.handshakes == [('chat.v1', {})]
    assert not v2.handshakes


async def test_unsupported_client_candidates_are_skipped(app, conductor):
    v1 = EchoResource('v1')
    v2 = EchoResource('v2')
    app.add_websocket_route('/chat', v1, subprotocols='chat.v1')
    app.add_websocket_route('/chat', v2, subprotocols=['chat.v2', 'chat.v2.1'])

    async with conductor as client:
        async with client.simulate_ws(
            '/chat', subprotocols=['unknown', 'chat.v2']
        ) as ws:
            assert ws.subprotocol == 'chat.v2'
            await ws.send_text('hello')
            assert await ws.receive_text() == 'v2:hello'

    assert not v1.handshakes
    assert v2.handshakes == [('chat.v2', {})]


@pytest.mark.parametrize('subprotocols', [None, [], ['chat.v2']])
async def test_no_common_subprotocol_rejected(app, conductor, subprotocols):
    resource = EchoResource('v1')
    app.add_websocket_route('/chat', resource, subprotocols='chat.v1')

    kwargs = {} if subprotocols is None else {'subprotocols': subprotocols}
    async with conductor as client:
        with pytest.raises(falcon.WebSocketDisconnected) as exc_info:
            async with client.simulate_ws('/chat', **kwargs):
                pass

    assert exc_info.value.code == CloseCode.FORBIDDEN
    assert not resource.handshakes


@pytest.mark.parametrize('subprotocols', [None, ['chat.v2']])
async def test_fallback_used_when_no_subprotocol_matches(app, conductor, subprotocols):
    resource = EchoResource('v1')
    fallback = EchoResource('fallback')
    app.add_websocket_route('/chat', resource, subprotocols='chat.v1')
    app.add_websocket_route('/chat', fallback)

    kwargs = {} if subprotocols is None else {'subprotocols': subprotocols}
    async with conductor as client:
        async with client.simulate_ws('/chat', **kwargs) as ws:
            assert ws.subprotocol is None
            await ws.send_text('hello')
            assert await ws.receive_text() == 'fallback:hello'

    assert not resource.handshakes
    assert fallback.handshakes == [(None, {})]


async def test_fallback_not_used_when_subprotocol_matches(app, conductor):
    resource = EchoResource('v1')
    fallback = EchoResource('fallback')
    app.add_websocket_route('/chat', resource, subprotocols='chat.v1')
    app.add_websocket_route('/chat', fallback)

    async with conductor as client:
        async with client.simulate_ws('/chat', subprotocols=['chat.v1']) as ws:
            assert ws.subprotocol == 'chat.v1'
            await ws.send_text('hello')
            assert await ws.receive_text() == 'v1:hello'

    assert resource.handshakes == [('chat.v1', {})]
    assert not fallback.handshakes


def test_duplicate_subprotocol_registration(app):
    app.add_websocket_route(
        '/chat', EchoResource('v1'), subprotocols=['chat.v1', 'chat.v2']
    )

    with pytest.raises(UnacceptableRouteError, match='chat.v2'):
        app.add_websocket_route(
            '/chat', EchoResource('v2'), subprotocols=['chat.v3', 'chat.v2']
        )


def test_duplicate_subprotocol_within_single_call(app):
    with pytest.raises(UnacceptableRouteError, match='chat.v1'):
        app.add_websocket_route(
            '/chat', EchoResource('v1'), subprotocols=['chat.v1', 'chat.v1']
        )


def test_duplicate_fallback_registration(app):
    app.add_websocket_route('/chat', EchoResource('a'))

    with pytest.raises(UnacceptableRouteError, match='fallback'):
        app.add_websocket_route('/chat', EchoResource('b'))


def test_empty_subprotocols(app):
    with pytest.raises(ValueError, match='may not be empty'):
        app.add_websocket_route('/chat', EchoResource('v1'), subprotocols=[])


@pytest.mark.parametrize('name', ['', 42, None])
def test_invalid_subprotocol_name(app, name):
    with pytest.raises(ValueError, match='non-empty strings'):
        app.add_websocket_route('/chat', EchoResource('v1'), subprotocols=[name])


def test_invalid_uri_template_type(app):
    with pytest.raises(TypeError, match='not a string'):
        app.add_websocket_route(42, EchoResource('v1'), subprotocols='v1')


@pytest.mark.parametrize('template', ['no-slash', '/double//slash'])
def test_invalid_uri_template(app, template):
    with pytest.raises(ValueError, match='uri_template'):
        app.add_websocket_route(template, EchoResource('v1'), subprotocols='v1')


def test_missing_websocket_responder(app):
    class Resource:
        pass

    with pytest.raises(UnacceptableRouteError, match='on_websocket'):
        app.add_websocket_route('/chat', Resource(), subprotocols='v1')


def test_missing_suffixed_websocket_responder(app):
    class Resource:
        async def on_websocket(self, req, ws):
            pass

    with pytest.raises(UnacceptableRouteError, match='on_websocket_foo'):
        app.add_websocket_route('/chat', Resource(), subprotocols='v1', suffix='foo')


def test_sync_websocket_responder_wrapped(app):
    class Resource:
        def on_websocket(self, req, ws):
            pass

    # The test session enables FALCON_ASGI_WRAP_NON_COROUTINES, so a
    #   synchronous responder is wrapped rather than rejected.
    app.add_websocket_route('/chat', Resource(), subprotocols='v1')


def test_sync_websocket_responder_rejected(app, util):
    class Resource:
        def on_websocket(self, req, ws):
            pass

    with util.disable_asgi_non_coroutine_wrapping():
        with pytest.raises(TypeError, match='must be a non-blocking'):
            app.add_websocket_route('/chat', Resource(), subprotocols='v1')


async def test_callable_object_responder(app, conductor):
    class Responder:
        async def __call__(self, req, ws, **params):
            await ws.accept()
            await ws.send_text('called')

    class Resource:
        on_websocket = Responder()

    app.add_websocket_route('/chat', Resource(), subprotocols='v1')

    async with conductor as client:
        async with client.simulate_ws('/chat', subprotocols=['v1']) as ws:
            assert await ws.receive_text() == 'called'


def test_conflict_with_existing_plain_websocket_route(app):
    class Resource:
        async def on_websocket(self, req, ws):
            pass

    app.add_route('/chat', Resource())

    with pytest.raises(UnacceptableRouteError, match='on_websocket'):
        app.add_websocket_route('/chat', EchoResource('v1'), subprotocols='v1')


def test_conflict_with_later_plain_websocket_route(app):
    app.add_websocket_route('/chat', EchoResource('v1'), subprotocols='v1')

    class Resource:
        async def on_websocket(self, req, ws):
            pass

    with pytest.raises(UnacceptableRouteError, match='on_websocket'):
        app.add_route('/chat', Resource())


def test_conflict_with_suffixed_plain_websocket_route(app):
    class Resource:
        async def on_websocket_foo(self, req, ws):
            pass

    app.add_route('/chat', Resource(), suffix='foo')

    with pytest.raises(UnacceptableRouteError, match='on_websocket'):
        app.add_websocket_route('/chat', EchoResource('v1'), subprotocols='v1')


async def test_reregistering_http_only_route_clears_conflict(app, conductor):
    class WsResource:
        async def on_websocket(self, req, ws):
            pass

    class HttpResource:
        async def on_get(self, req, resp):
            resp.media = {'ok': True}

    app.add_route('/chat', WsResource())
    app.add_route('/chat', HttpResource())

    resource = EchoResource('v1')
    app.add_websocket_route('/chat', resource, subprotocols='v1')

    async with conductor as client:
        async with client.simulate_ws('/chat', subprotocols=['v1']) as ws:
            await ws.send_text('hello')
            assert await ws.receive_text() == 'v1:hello'

        resp = await client.simulate_get('/chat')
        assert resp.json == {'ok': True}


async def test_suffix(app, conductor):
    class Resource:
        def __init__(self):
            self.called = False

        async def on_websocket_foo(self, req, ws):
            self.called = True
            await ws.accept()
            await ws.send_text('suffixed')

    resource = Resource()
    app.add_websocket_route('/chat', resource, subprotocols='v1', suffix='foo')

    async with conductor as client:
        async with client.simulate_ws('/chat', subprotocols=['v1']) as ws:
            assert await ws.receive_text() == 'suffixed'

    assert resource.called


async def test_converter_params(app, conductor):
    resource = EchoResource('v1')
    app.add_websocket_route('/chat/{room}/{count:int}', resource, subprotocols='v1')

    async with conductor as client:
        async with client.simulate_ws('/chat/lobby/3', subprotocols=['v1']) as ws:
            await ws.send_text('hello')
            assert await ws.receive_text() == 'v1:hello'

    assert resource.handshakes == [('v1', {'room': 'lobby', 'count': 3})]


async def test_http_route_on_same_template(app, conductor):
    class HttpResource:
        async def on_get(self, req, resp, room):
            resp.media = {'room': room}

    resource = EchoResource('v1')
    app.add_websocket_route('/chat/{room}', resource, subprotocols='v1')
    app.add_route('/chat/{room}', HttpResource())

    async with conductor as client:
        resp = await client.simulate_get('/chat/lobby')
        assert resp.status_code == 200
        assert resp.json == {'room': 'lobby'}

        async with client.simulate_ws('/chat/lobby', subprotocols=['v1']) as ws:
            await ws.send_text('hello')
            assert await ws.receive_text() == 'v1:hello'

    assert resource.handshakes == [('v1', {'room': 'lobby'})]


async def test_http_route_registered_first(app, conductor):
    class HttpResource:
        async def on_get(self, req, resp, room):
            resp.media = {'room': room}

    app.add_route('/chat/{room}', HttpResource())
    app.add_websocket_route('/chat/{room}', EchoResource('v1'), subprotocols='v1')

    async with conductor as client:
        resp = await client.simulate_get('/chat/lobby')
        assert resp.status_code == 200

        async with client.simulate_ws('/chat/lobby', subprotocols=['v1']) as ws:
            await ws.send_text('hello')
            assert await ws.receive_text() == 'v1:hello'


async def test_http_request_to_websocket_only_template(app, conductor):
    app.add_websocket_route('/chat', EchoResource('v1'), subprotocols='v1')

    async with conductor as client:
        resp = await client.simulate_get('/chat')
        assert resp.status_code == 405


async def test_plain_websocket_route_unaffected(app, conductor):
    class PlainResource:
        async def on_websocket(self, req, ws):
            assert ws.subprotocol is None
            await ws.accept()
            await ws.send_text('plain')

    app.add_route('/plain', PlainResource())
    app.add_websocket_route('/chat', EchoResource('v1'), subprotocols='v1')

    async with conductor as client:
        async with client.simulate_ws('/plain') as ws:
            assert ws.subprotocol is None
            assert await ws.receive_text() == 'plain'


async def test_websocket_route_not_found(conductor):
    async with conductor as client:
        with pytest.raises(falcon.WebSocketDisconnected) as exc_info:
            async with client.simulate_ws('/nowhere'):
                pass

    assert exc_info.value.code == CloseCode.NOT_FOUND


async def test_resource_middleware_receives_resolved_resource():
    v1 = EchoResource('v1')
    v2 = EchoResource('v2')
    seen = []

    class Middleware:
        async def process_request_ws(self, req, ws):
            seen.append('request')

        async def process_resource_ws(self, req, ws, resource, params):
            seen.append((resource, params))

    app = falcon.asgi.App(middleware=[Middleware()])
    app.add_websocket_route('/chat/{room}', v1, subprotocols='v1')
    app.add_websocket_route('/chat/{room}', v2, subprotocols='v2')

    async with testing.ASGIConductor(app) as client:
        async with client.simulate_ws('/chat/a', subprotocols=['v2']) as ws:
            await ws.send_text('hello')
            assert await ws.receive_text() == 'v2:hello'
        async with client.simulate_ws('/chat/b', subprotocols=['v1']) as ws:
            await ws.send_text('hello')
            assert await ws.receive_text() == 'v1:hello'

    assert seen == [
        'request',
        (v2, {'room': 'a'}),
        'request',
        (v1, {'room': 'b'}),
    ]


async def test_middleware_error_rejects_handshake():
    class Middleware:
        async def process_resource_ws(self, req, ws, resource, params):
            raise falcon.HTTPBadRequest(description='Nope.')

    app = falcon.asgi.App(middleware=[Middleware()])
    resource = EchoResource('v1')
    app.add_websocket_route('/chat', resource, subprotocols='v1')

    async with testing.ASGIConductor(app) as client:
        with pytest.raises(falcon.WebSocketDisconnected) as exc_info:
            async with client.simulate_ws('/chat', subprotocols=['v1']):
                pass

    assert exc_info.value.code == CloseCode.BAD_REQUEST
    assert not resource.handshakes


async def test_error_handler_receives_params():
    class CustomError(Exception):
        pass

    caught = {}

    async def handle_custom(req, resp, ex, params, ws=None):
        caught['resp'] = resp
        caught['ws'] = ws
        caught['params'] = params
        raise falcon.HTTPForbidden()

    class Resource:
        async def on_websocket(self, req, ws, **params):
            raise CustomError()

    app = falcon.asgi.App()
    app.add_error_handler(CustomError, handle_custom)
    app.add_websocket_route('/chat/{room}', Resource(), subprotocols='v1')

    async with testing.ASGIConductor(app) as client:
        with pytest.raises(falcon.WebSocketDisconnected) as exc_info:
            async with client.simulate_ws('/chat/lobby', subprotocols=['v1']):
                pass

    assert exc_info.value.code == CloseCode.FORBIDDEN
    assert caught['resp'] is None
    assert caught['ws'] is not None
    assert caught['params'] == {'room': 'lobby'}


async def test_explicit_accept_subprotocol_is_recorded(app, conductor):
    class Resource:
        def __init__(self):
            self.negotiated = 'unset'

        async def on_websocket(self, req, ws):
            await ws.accept(subprotocol='explicit')
            self.negotiated = ws.subprotocol
            await ws.send_text('done')

    resource = Resource()
    app.add_route('/chat', resource)

    async with conductor as client:
        async with client.simulate_ws('/chat', subprotocols=['explicit']) as ws:
            assert ws.subprotocol == 'explicit'
            assert await ws.receive_text() == 'done'

    assert resource.negotiated == 'explicit'
