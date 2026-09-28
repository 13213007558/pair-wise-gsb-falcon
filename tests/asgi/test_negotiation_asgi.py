import pytest

import falcon
from falcon import media
from falcon import testing
import falcon.asgi


@pytest.fixture(scope='session')
def msgpack():
    return pytest.importorskip('msgpack')


@falcon.provides('application/json', 'application/msgpack')
class NegotiatedResource:
    async def on_get(self, req, resp):
        resp.media = {'answer': 42}

    async def on_head(self, req, resp):
        resp.media = {'answer': 42}

    async def on_delete(self, req, resp):
        resp.status = falcon.HTTP_204


@falcon.provides('application/json', 'application/msgpack')
class OverrideResource:
    async def on_get(self, req, resp):
        resp.content_type = falcon.MEDIA_JSON
        resp.media = {'answer': 42}


@falcon.provides('application/json', 'application/msgpack')
class ErrorResource:
    async def on_get(self, req, resp):
        raise falcon.HTTPBadRequest(title='Bad Request')


@falcon.provides('application/json', 'application/msgpack')
class InputResource:
    async def on_post(self, req, resp):
        resp.media = await req.get_media()


@falcon.provides('application/json', 'application/cbor')
class PartialHandlerResource:
    async def on_get(self, req, resp):
        resp.media = {'answer': 42}


class UndeclaredResource:
    async def on_get(self, req, resp):
        resp.media = {'answer': 42}


def create_client(resource):
    app = falcon.asgi.App()
    app.add_route('/', resource)
    msgpack_handler = media.MessagePackHandler()
    app.req_options.media_handlers[falcon.MEDIA_MSGPACK] = msgpack_handler
    app.resp_options.media_handlers[falcon.MEDIA_MSGPACK] = msgpack_handler
    return testing.TestClient(app)


async def test_weighted_selection(msgpack):
    client = create_client(NegotiatedResource())
    async with client as async_client:
        result = await async_client.simulate_get(
            '/',
            headers={'Accept': 'application/json;q=0.5, application/msgpack;q=0.9'},
        )
    assert result.status_code == 200
    assert result.headers['Content-Type'] == 'application/msgpack'
    assert msgpack.loads(result.content) == {'answer': 42}


async def test_specific_type_precedes_wildcard(msgpack):
    client = create_client(NegotiatedResource())
    async with client as async_client:
        result = await async_client.simulate_get(
            '/',
            headers={'Accept': 'application/*;q=0.5, application/msgpack;q=0.9'},
        )
    assert result.headers['Content-Type'] == 'application/msgpack'
    assert msgpack.loads(result.content) == {'answer': 42}


async def test_stable_tie_break_order_and_default_accept(msgpack):
    client = create_client(NegotiatedResource())
    async with client as async_client:
        result = await async_client.simulate_get(
            '/', headers={'Accept': 'application/*'}
        )
        assert result.headers['Content-Type'] == 'application/json'
        assert result.json == {'answer': 42}

        result = await async_client.simulate_get('/')
        assert result.headers['Content-Type'] == 'application/json'
        assert result.headers['Vary'] == 'Accept'


async def test_q_zero_exclusion_and_406(msgpack):
    client = create_client(NegotiatedResource())
    async with client as async_client:
        result = await async_client.simulate_get(
            '/',
            headers={'Accept': 'application/msgpack;q=0, application/*;q=0.5'},
        )
        assert result.status_code == 200
        assert result.headers['Content-Type'] == 'application/json'

        result = await async_client.simulate_get(
            '/',
            headers={'Accept': 'application/json;q=0, application/msgpack;q=0'},
        )
        assert result.status_code == 406
        assert result.content == b''


async def test_explicit_content_type_override(msgpack):
    client = create_client(OverrideResource())
    async with client as async_client:
        result = await async_client.simulate_get(
            '/', headers={'Accept': 'application/msgpack'}
        )
    assert result.status_code == 200
    assert result.headers['Content-Type'] == 'application/json'
    assert 'Vary' not in result.headers
    assert result.json == {'answer': 42}


async def test_missing_handler_type_is_not_offered(msgpack):
    client = create_client(PartialHandlerResource())
    async with client as async_client:
        result = await async_client.simulate_get(
            '/', headers={'Accept': 'application/cbor'}
        )
        assert result.status_code == 406

        result = await async_client.simulate_get(
            '/', headers={'Accept': 'application/cbor, application/json;q=0.5'}
        )
        assert result.status_code == 200
        assert result.headers['Content-Type'] == 'application/json'


async def test_head_does_not_serialize_body(msgpack):
    client = create_client(NegotiatedResource())
    async with client as async_client:
        result = await async_client.simulate_head(
            '/', headers={'Accept': 'application/msgpack'}
        )
    assert result.status_code == 200
    assert result.headers['Content-Type'] == 'application/msgpack'
    assert result.headers['Vary'] == 'Accept'
    assert result.content == b''


async def test_204_skips_negotiation(msgpack):
    client = create_client(NegotiatedResource())
    async with client as async_client:
        result = await async_client.simulate_delete(
            '/', headers={'Accept': 'text/html'}
        )
    assert result.status_code == 204
    assert 'Content-Type' not in result.headers
    assert 'Vary' not in result.headers
    assert result.content == b''


async def test_vary_is_merged_and_deduplicated(msgpack):
    @falcon.provides('application/json', 'application/msgpack')
    class Resource:
        async def on_get(self, req, resp):
            resp.append_header('Vary', 'Accept-Encoding')
            resp.media = {'answer': 42}

    client = create_client(Resource())
    async with client as async_client:
        result = await async_client.simulate_get(
            '/', headers={'Accept': 'application/msgpack'}
        )
    assert result.headers['Vary'] == 'Accept-Encoding, Accept'


async def test_error_response_not_restricted_by_declaration(msgpack):
    client = create_client(ErrorResource())
    async with client as async_client:
        result = await async_client.simulate_get('/', headers={'Accept': 'text/html'})
        assert result.status_code == 400
        assert result.content == b''

        result = await async_client.simulate_get(
            '/', headers={'Accept': 'application/msgpack'}
        )
        assert result.status_code == 400
        assert result.headers['Content-Type'] == falcon.MEDIA_MSGPACK
        assert msgpack.loads(result.content)['title'] == 'Bad Request'


async def test_request_content_type_unaffected(msgpack):
    client = create_client(InputResource())
    async with client as async_client:
        result = await async_client.simulate_post(
            '/',
            body=b'{"answer": 42}',
            headers={
                'Content-Type': falcon.MEDIA_JSON,
                'Accept': 'application/msgpack',
            },
        )
        assert result.status_code == 200
        assert result.headers['Content-Type'] == 'application/msgpack'
        assert msgpack.loads(result.content) == {'answer': 42}

        result = await async_client.simulate_post(
            '/',
            body=msgpack.dumps({'answer': 7}),
            headers={
                'Content-Type': falcon.MEDIA_MSGPACK,
                'Accept': 'application/json',
            },
        )
        assert result.status_code == 200
        assert result.headers['Content-Type'] == 'application/json'
        assert result.json == {'answer': 7}


async def test_undeclared_resource_keeps_default_behavior():
    client = create_client(UndeclaredResource())
    async with client as async_client:
        result = await async_client.simulate_get('/', headers={'Accept': 'text/html'})
    assert result.status_code == 200
    assert result.headers['Content-Type'] == falcon.MEDIA_JSON
    assert 'Vary' not in result.headers
