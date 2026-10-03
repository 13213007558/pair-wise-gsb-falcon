from inspect import iscoroutinefunction

import pytest

import falcon
from falcon import testing
import falcon.asgi
from falcon.asgi._ws_subprotocols import WebSocketSubprotocolDispatcher

WS_FORBIDDEN = 3403  # 403 + 3000


class EchoResource:
    def __init__(self, name, subprotocol=None):
        self.name = name
        self.subprotocol = subprotocol
        self.requests = []
        self.params = None

    async def on_websocket(self, req, ws, **params):
        self.requests.append(req)
        self.params = params
        await ws.accept(subprotocol=self.subprotocol)
        message = await ws.receive_text()
        await ws.send_text(f'{self.name}:{message}')


class PlainWsResource:
    def __init__(self):
        self.called = False

    async def on_websocket(self, req, ws):
        self.called = True
        await ws.accept()
        await ws.send_text('plain')


class HttpResource:
    async def on_get(self, req, resp):
        resp.text = 'http-ok'


@pytest.fixture
def conductor():
    app = falcon.asgi.App()
    return testing.ASGIConductor(app)


def make_dispatcher(subprotocols, **kwargs):
    return WebSocketSubprotocolDispatcher(subprotocols, **kwargs)


@pytest.mark.parametrize(
    'client_order',
    [['chat.v1', 'chat.v2'], ['chat.v2', 'chat.v1']],
    ids=['v1-first', 'v2-first'],
)
async def test_server_priority_regardless_of_client_order(conductor, client_order):
    r1 = EchoResource('v1', subprotocol='chat.v1')
    r2 = EchoResource('v2', subprotocol='chat.v2')
    conductor.app.add_websocket_route('/ws/{room}', {'chat.v2': r2, 'chat.v1': r1})

    async with conductor.simulate_ws('/ws/lobby', subprotocols=client_order) as ws:
        assert ws.subprotocol == 'chat.v2'
        await ws.send_text('hello')
        assert await ws.receive_text() == 'v2:hello'

    assert r1.requests == []
    assert len(r2.requests) == 1
    assert r2.params == {'room': 'lobby'}


async def test_server_priority_single_match(conductor):
    r1 = EchoResource('v1', subprotocol='chat.v1')
    r2 = EchoResource('v2', subprotocol='chat.v2')
    conductor.app.add_websocket_route('/ws', {'chat.v2': r2, 'chat.v1': r1})

    async with conductor.simulate_ws('/ws', subprotocols=['chat.v1']) as ws:
        assert ws.subprotocol == 'chat.v1'
        await ws.send_text('ping')
        assert await ws.receive_text() == 'v1:ping'

    assert r2.requests == []
    assert len(r1.requests) == 1


async def test_no_common_subprotocol_rejected(conductor):
    r1 = EchoResource('v1', subprotocol='chat.v1')
    conductor.app.add_websocket_route('/ws', {'chat.v1': r1})

    with pytest.raises(falcon.WebSocketDisconnected) as excinfo:
        async with conductor.simulate_ws('/ws', subprotocols=['xmpp']):
            pass

    assert excinfo.value.code == WS_FORBIDDEN
    assert r1.requests == []


async def test_no_subprotocols_offered_rejected(conductor):
    r1 = EchoResource('v1', subprotocol='chat.v1')
    conductor.app.add_websocket_route('/ws', {'chat.v1': r1})

    with pytest.raises(falcon.WebSocketDisconnected) as excinfo:
        async with conductor.simulate_ws('/ws'):
            pass

    assert excinfo.value.code == WS_FORBIDDEN
    assert r1.requests == []


async def test_fallback_no_common_subprotocol(conductor):
    primary = EchoResource('primary', subprotocol='chat.v1')
    fallback = EchoResource('fallback')
    conductor.app.add_websocket_route('/ws', {'chat.v1': primary}, fallback=fallback)

    async with conductor.simulate_ws('/ws', subprotocols=['xmpp']) as ws:
        assert ws.subprotocol is None
        await ws.send_text('hi')
        assert await ws.receive_text() == 'fallback:hi'

    assert primary.requests == []
    assert len(fallback.requests) == 1


async def test_fallback_no_subprotocols_offered(conductor):
    primary = EchoResource('primary', subprotocol='chat.v1')
    fallback = EchoResource('fallback')
    conductor.app.add_websocket_route('/ws', {'chat.v1': primary}, fallback=fallback)

    async with conductor.simulate_ws('/ws') as ws:
        assert ws.subprotocol is None
        await ws.send_text('hi')
        assert await ws.receive_text() == 'fallback:hi'

    assert primary.requests == []
    assert len(fallback.requests) == 1


async def test_fallback_not_used_when_subprotocol_matches(conductor):
    primary = EchoResource('primary', subprotocol='chat.v1')
    fallback = EchoResource('fallback')
    conductor.app.add_websocket_route('/ws', {'chat.v1': primary}, fallback=fallback)

    async with conductor.simulate_ws('/ws', subprotocols=['chat.v1']) as ws:
        assert ws.subprotocol == 'chat.v1'
        await ws.send_text('hi')
        assert await ws.receive_text() == 'primary:hi'

    assert fallback.requests == []
    assert fallback.requests == []


def test_empty_subprotocols_rejected(conductor):
    with pytest.raises(ValueError, match='At least one'):
        conductor.app.add_websocket_route('/ws', {})


@pytest.mark.parametrize('subprotocols', [42, 'chat.v1', object()])
def test_subprotocols_wrong_type_rejected(conductor, subprotocols):
    with pytest.raises(TypeError):
        conductor.app.add_websocket_route('/ws', subprotocols)


@pytest.mark.parametrize(
    'item', [('chat.v1',), 42, ('chat.v1', EchoResource('a'), 'x')]
)
def test_invalid_subprotocol_pair_rejected(conductor, item):
    with pytest.raises(TypeError):
        conductor.app.add_websocket_route('/ws', [item])


@pytest.mark.parametrize('name', ['', ' ', 'has space', 'tab\tchar', 42, None])
def test_invalid_subprotocol_name_rejected(conductor, name):
    with pytest.raises(ValueError):
        conductor.app.add_websocket_route('/ws', [(name, EchoResource('a'))])


def test_duplicate_subprotocol_rejected(conductor):
    with pytest.raises(ValueError, match='more than one'):
        conductor.app.add_websocket_route(
            '/ws',
            [
                ('chat.v1', EchoResource('a')),
                ('chat.v1', EchoResource('b')),
            ],
        )


def test_resource_missing_responder_rejected(conductor):
    class NotAResource:
        pass

    with pytest.raises(ValueError, match='on_websocket'):
        conductor.app.add_websocket_route('/ws', {'chat.v1': NotAResource()})


def test_fallback_missing_responder_rejected(conductor):
    class NotAResource:
        pass

    with pytest.raises(ValueError, match='on_websocket'):
        conductor.app.add_websocket_route(
            '/ws', {'chat.v1': EchoResource('a')}, fallback=NotAResource()
        )


def test_responder_not_callable_rejected(conductor):
    class WeirdResource:
        on_websocket = 42

    with pytest.raises(ValueError, match='not'):
        conductor.app.add_websocket_route('/ws', {'chat.v1': WeirdResource()})


def test_sync_responder_rejected(conductor, util):
    class SyncResource:
        def on_websocket(self, req, ws):
            pass

    with util.disable_asgi_non_coroutine_wrapping():
        with pytest.raises(TypeError, match='async coroutine'):
            conductor.app.add_websocket_route('/ws', {'chat.v1': SyncResource()})


def test_sync_responder_wrapped_when_enabled():
    class SyncResource:
        def on_websocket(self, req, ws):
            pass

    dispatcher = make_dispatcher({'chat.v1': SyncResource()})
    __, responder = dispatcher.select(['chat.v1'])
    assert iscoroutinefunction(responder)


def test_duplicate_route_registration_rejected(conductor):
    conductor.app.add_websocket_route('/ws', {'chat.v1': EchoResource('a')})

    with pytest.raises(falcon.CompatibilityError):
        conductor.app.add_websocket_route('/ws', {'chat.v2': EchoResource('b')})


def test_conflict_with_plain_websocket_route_rejected(conductor):
    conductor.app.add_route('/ws', PlainWsResource())

    with pytest.raises(falcon.CompatibilityError):
        conductor.app.add_websocket_route('/ws', {'chat.v1': EchoResource('a')})


def test_conflict_with_plain_websocket_route_rejected_reversed(conductor):
    conductor.app.add_websocket_route('/ws', {'chat.v1': EchoResource('a')})

    with pytest.raises(falcon.CompatibilityError):
        conductor.app.add_route('/ws', PlainWsResource())


async def test_plain_websocket_route_unaffected(conductor):
    plain = PlainWsResource()
    conductor.app.add_route('/ws', plain)

    async with conductor.simulate_ws('/ws') as ws:
        assert await ws.receive_text() == 'plain'

    assert plain.called


async def test_suffixed_responders(conductor):
    class SuffixedResource:
        def __init__(self):
            self.called = False

        async def on_websocket_chat(self, req, ws):
            self.called = True
            await ws.accept()
            await ws.send_text('suffixed')

    resource = SuffixedResource()
    conductor.app.add_websocket_route('/ws', {'chat.v1': resource}, suffix='chat')

    async with conductor.simulate_ws('/ws', subprotocols=['chat.v1']) as ws:
        assert await ws.receive_text() == 'suffixed'

    assert resource.called
    assert resource.called


async def test_resource_middleware_receives_selected_resource():
    seen = {}

    class RecordingMiddleware:
        async def process_request_ws(self, req, ws):
            seen['request'] = True

        async def process_resource_ws(self, req, ws, resource, params):
            seen['resource'] = resource
            seen['params'] = params

    app = falcon.asgi.App(middleware=RecordingMiddleware())
    r1 = EchoResource('v1', subprotocol='chat.v1')
    r2 = EchoResource('v2', subprotocol='chat.v2')
    app.add_websocket_route('/ws/{room:int}', {'chat.v1': r1, 'chat.v2': r2})
    conductor = testing.ASGIConductor(app)

    async with conductor.simulate_ws('/ws/7', subprotocols=['chat.v2']) as ws:
        await ws.send_text('hi')
        assert await ws.receive_text() == 'v2:hi'

    assert seen['request']
    assert seen['resource'] is r2
    assert seen['params'] == {'room': 7}
    assert r2.params == {'room': 7}


async def test_middleware_error_closes_connection():
    class RejectingMiddleware:
        async def process_resource_ws(self, req, ws, resource, params):
            raise falcon.HTTPForbidden()

    app = falcon.asgi.App(middleware=RejectingMiddleware())
    r1 = EchoResource('v1', subprotocol='chat.v1')
    app.add_websocket_route('/ws', {'chat.v1': r1})
    conductor = testing.ASGIConductor(app)

    with pytest.raises(falcon.WebSocketDisconnected) as excinfo:
        async with conductor.simulate_ws('/ws', subprotocols=['chat.v1']):
            pass

    assert excinfo.value.code == WS_FORBIDDEN
    assert r1.requests == []


async def test_error_handler_receives_params(conductor):
    class Boom(Exception):
        pass

    class FailingResource:
        async def on_websocket(self, req, ws, **params):
            raise Boom()

    captured = {}

    async def handle_boom(req, resp, ex, params, ws):
        captured['params'] = params
        raise falcon.HTTPForbidden()

    conductor.app.add_websocket_route('/ws/{room:int}', {'chat.v1': FailingResource()})
    conductor.app.add_error_handler(Boom, handle_boom)

    with pytest.raises(falcon.WebSocketDisconnected) as excinfo:
        async with conductor.simulate_ws('/ws/3', subprotocols=['chat.v1']):
            pass

    assert excinfo.value.code == WS_FORBIDDEN
    assert captured['params'] == {'room': 3}


@pytest.mark.parametrize('http_first', [True, False])
async def test_http_and_websocket_share_path(http_first):
    app = falcon.asgi.App()
    http_resource = HttpResource()
    ws_resource = EchoResource('v1', subprotocol='chat.v1')

    if http_first:
        app.add_route('/ws', http_resource)
        app.add_websocket_route('/ws', {'chat.v1': ws_resource})
    else:
        app.add_websocket_route('/ws', {'chat.v1': ws_resource})
        app.add_route('/ws', http_resource)

    conductor = testing.ASGIConductor(app)

    resp = await conductor.simulate_get('/ws')
    assert resp.status_code == 200
    assert resp.text == 'http-ok'

    async with conductor.simulate_ws('/ws', subprotocols=['chat.v1']) as ws:
        assert ws.subprotocol == 'chat.v1'
        await ws.send_text('hello')
        assert await ws.receive_text() == 'v1:hello'


async def test_http_middleware_sees_http_resource():
    seen = {}

    class RecordingMiddleware:
        async def process_resource(self, req, resp, resource, params):
            seen['resource'] = resource

    app = falcon.asgi.App(middleware=RecordingMiddleware())
    http_resource = HttpResource()
    app.add_route('/ws', http_resource)
    app.add_websocket_route('/ws', {'chat.v1': EchoResource('v1')})
    conductor = testing.ASGIConductor(app)

    resp = await conductor.simulate_get('/ws')
    assert resp.status_code == 200
    assert seen['resource'] is http_resource


async def test_http_request_to_ws_only_route_is_405(conductor):
    conductor.app.add_websocket_route('/ws', {'chat.v1': EchoResource('v1')})

    resp = await conductor.simulate_get('/ws')
    assert resp.status_code == 405


async def test_multiple_routes_are_independent(conductor):
    r1 = EchoResource('one', subprotocol='a.v1')
    r2 = EchoResource('two', subprotocol='b.v1')
    conductor.app.add_websocket_route('/one', {'a.v1': r1})
    conductor.app.add_websocket_route('/two', {'b.v1': r2})

    async with conductor.simulate_ws('/one', subprotocols=['a.v1']) as ws:
        await ws.send_text('x')
        assert await ws.receive_text() == 'one:x'

    async with conductor.simulate_ws('/two', subprotocols=['b.v1']) as ws:
        await ws.send_text('y')
        assert await ws.receive_text() == 'two:y'

    assert r2.requests and not r1.params
    assert r2.requests and not r1.params


def test_dispatcher_select_server_priority():
    r1 = EchoResource('v1')
    r2 = EchoResource('v2')
    dispatcher = make_dispatcher({'chat.v2': r2, 'chat.v1': r1})

    assert dispatcher.select(['chat.v1', 'chat.v2'])[0] is r2
    assert dispatcher.select(['chat.v2', 'chat.v1'])[0] is r2
    assert dispatcher.select(['chat.v1'])[0] is r1
    assert dispatcher.subprotocols == ('chat.v2', 'chat.v1')


def test_dispatcher_select_fallback_and_reject():
    dispatcher = make_dispatcher({'chat.v1': EchoResource('v1')})

    with pytest.raises(falcon.HTTPForbidden):
        dispatcher.select(['xmpp'])

    fallback = EchoResource('fallback')
    dispatcher = make_dispatcher({'chat.v1': EchoResource('v1')}, fallback=fallback)
    assert dispatcher.select(['xmpp'])[0] is fallback
    assert dispatcher.select([])[0] is fallback


def test_dispatcher_attribute_delegation():
    dispatcher = make_dispatcher({'chat.v1': EchoResource('v1')})

    with pytest.raises(AttributeError):
        dispatcher.bogus_attribute

    with pytest.raises(AttributeError):
        dispatcher.on_get

    assert callable(dispatcher.on_websocket)

    http_resource = HttpResource()
    dispatcher.http_resource = http_resource
    assert dispatcher.http_resource is http_resource
    assert dispatcher.on_get == http_resource.on_get


def test_dispatcher_suffixed_placeholder():
    class SuffixedResource:
        async def on_websocket_chat(self, req, ws):
            pass

    dispatcher = make_dispatcher({'chat.v1': SuffixedResource()}, suffix='chat')

    assert callable(dispatcher.on_websocket_chat)

    with pytest.raises(AttributeError):
        dispatcher.on_websocket


async def test_dispatcher_placeholder_raises():
    dispatcher = make_dispatcher({'chat.v1': EchoResource('v1')})

    with pytest.raises(RuntimeError):
        await dispatcher._on_websocket_placeholder(None, None)


def test_create_scope_ws_subprotocols():
    scope = testing.create_scope_ws('/ws', subprotocols=['chat.v1', 'chat.v2'])
    assert scope['subprotocols'] == ['chat.v1', 'chat.v2']
