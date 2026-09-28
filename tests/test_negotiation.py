import pytest

import falcon
from falcon import media
from falcon import testing


@pytest.fixture(scope='session')
def msgpack():
    return pytest.importorskip('msgpack')


@falcon.provides('application/json', 'application/msgpack')
class NegotiatedResource:
    def on_get(self, req, resp):
        resp.media = {'answer': 42}

    def on_head(self, req, resp):
        resp.media = {'answer': 42}

    def on_delete(self, req, resp):
        resp.status = falcon.HTTP_204


@falcon.provides('application/json', 'application/msgpack')
class OverrideResource:
    def on_get(self, req, resp):
        resp.content_type = falcon.MEDIA_JSON
        resp.media = {'answer': 42}


@falcon.provides('application/json', 'application/msgpack')
class ErrorResource:
    def on_get(self, req, resp):
        raise falcon.HTTPBadRequest(title='Bad Request')


class InputResource:
    def on_post(self, req, resp):
        resp.media = req.get_media()


@falcon.provides('application/json', 'application/msgpack')
class MediaInputResource:
    def on_post(self, req, resp):
        resp.media = req.get_media()


@falcon.provides('application/json', 'application/cbor')
class PartialHandlerResource:
    def on_get(self, req, resp):
        resp.media = {'answer': 42}


class UndeclaredResource:
    def on_get(self, req, resp):
        resp.media = {'answer': 42}


@falcon.provides('application/json')
class TextResource:
    def on_get(self, req, resp):
        resp.content_type = falcon.MEDIA_HTML
        resp.text = '<p>hello</p>'


def create_client(resource, *, negotiate=True):
    app = falcon.App()
    app.add_route('/', resource)
    msgpack_handler = media.MessagePackHandler()
    app.req_options.media_handlers[falcon.MEDIA_MSGPACK] = msgpack_handler
    app.resp_options.media_handlers[falcon.MEDIA_MSGPACK] = msgpack_handler
    return testing.TestClient(app)


def test_weighted_selection(msgpack):
    client = create_client(NegotiatedResource())
    result = client.simulate_get(
        '/',
        headers={'Accept': 'application/json;q=0.5, application/msgpack;q=0.9'},
    )
    assert result.status_code == 200
    assert result.headers['Content-Type'] == 'application/msgpack'
    assert msgpack.loads(result.content) == {'answer': 42}


def test_specific_type_precedes_wildcard(msgpack):
    client = create_client(NegotiatedResource())
    result = client.simulate_get(
        '/',
        headers={'Accept': 'application/*;q=0.5, application/msgpack;q=0.9'},
    )
    assert result.headers['Content-Type'] == 'application/msgpack'
    assert msgpack.loads(result.content) == {'answer': 42}


def test_stable_tie_break_order(msgpack):
    client = create_client(NegotiatedResource())
    result = client.simulate_get('/', headers={'Accept': 'application/*'})
    assert result.headers['Content-Type'] == 'application/json'
    assert result.json == {'answer': 42}


def test_explicit_q_zero_exclusion(msgpack):
    client = create_client(NegotiatedResource())
    result = client.simulate_get(
        '/',
        headers={'Accept': 'application/msgpack;q=0, application/*;q=0.5'},
    )
    assert result.status_code == 200
    assert result.headers['Content-Type'] == 'application/json'
    assert result.json == {'answer': 42}


def test_all_excluded_returns_406(msgpack):
    client = create_client(NegotiatedResource())
    result = client.simulate_get(
        '/',
        headers={'Accept': 'application/json;q=0, application/msgpack;q=0'},
    )
    assert result.status_code == 406
    assert result.content == b''


def test_unrelated_type_returns_406():
    req = falcon.Request(testing.create_environ(headers={'Accept': 'text/html'}))
    resp = falcon.Response(options=falcon.ResponseOptions())
    resp.media = {'answer': 42}

    with pytest.raises(falcon.HTTPNotAcceptable) as exc_info:
        falcon.negotiation.negotiate_response(req, resp, NegotiatedResource())

    assert 'application/json' in exc_info.value.description
    assert 'application/msgpack' in exc_info.value.description


def test_malformed_accept_returns_406(msgpack):
    client = create_client(NegotiatedResource())
    result = client.simulate_get(
        '/', headers={'Accept': 'application/json;q=not-a-number'}
    )
    assert result.status_code == 406


def test_text_response_skips_negotiation(msgpack):
    client = create_client(TextResource())
    result = client.simulate_get('/', headers={'Accept': 'application/json'})
    assert result.status_code == 200
    assert result.headers['Content-Type'] == falcon.MEDIA_HTML
    assert 'Vary' not in result.headers
    assert result.text == '<p>hello</p>'


def test_missing_accept_uses_first_declared(msgpack):
    client = create_client(NegotiatedResource())
    result = client.simulate_get('/')
    assert result.status_code == 200
    assert result.headers['Content-Type'] == 'application/json'
    assert result.json == {'answer': 42}


def test_explicit_content_type_override(msgpack):
    client = create_client(OverrideResource())
    result = client.simulate_get('/', headers={'Accept': 'application/msgpack'})
    assert result.status_code == 200
    assert result.headers['Content-Type'] == 'application/json'
    assert 'Vary' not in result.headers
    assert result.json == {'answer': 42}


def test_missing_handler_type_is_not_offered(msgpack):
    client = create_client(PartialHandlerResource())
    result = client.simulate_get('/', headers={'Accept': 'application/cbor'})
    assert result.status_code == 406

    result = client.simulate_get(
        '/', headers={'Accept': 'application/cbor, application/json;q=0.5'}
    )
    assert result.status_code == 200
    assert result.headers['Content-Type'] == 'application/json'


def test_head_does_not_serialize_body(msgpack):
    client = create_client(NegotiatedResource())
    result = client.simulate_head('/', headers={'Accept': 'application/msgpack'})
    assert result.status_code == 200
    assert result.headers['Content-Type'] == 'application/msgpack'
    assert result.headers['Vary'] == 'Accept'
    assert result.content == b''


def test_204_skips_negotiation(msgpack):
    client = create_client(NegotiatedResource())
    result = client.simulate_delete('/', headers={'Accept': 'text/html'})
    assert result.status_code == 204
    assert 'Content-Type' not in result.headers
    assert 'Vary' not in result.headers
    assert result.content == b''


def test_vary_is_merged_and_deduplicated(msgpack):
    @falcon.provides('application/json', 'application/msgpack')
    class Resource:
        def on_get(self, req, resp):
            resp.append_header('Vary', 'Accept-Encoding')
            resp.media = {'answer': 42}

    client = create_client(Resource())
    result = client.simulate_get('/', headers={'Accept': 'application/msgpack'})
    assert result.headers['Vary'] == 'Accept-Encoding, Accept'

    @falcon.provides('application/json', 'application/msgpack')
    class AlreadyVaryResource:
        def on_get(self, req, resp):
            resp.vary = ('accept', 'Accept-Encoding')
            resp.media = {'answer': 42}

    client = create_client(AlreadyVaryResource())
    result = client.simulate_get('/', headers={'Accept': 'application/msgpack'})
    assert result.headers['Vary'] == 'accept, Accept-Encoding'


def test_error_response_not_restricted_by_declaration(msgpack):
    client = create_client(ErrorResource())
    result = client.simulate_get('/', headers={'Accept': 'text/html'})
    assert result.status_code == 400
    assert result.content == b''

    result = client.simulate_get('/', headers={'Accept': 'application/msgpack'})
    assert result.status_code == 400
    assert result.headers['Content-Type'] == falcon.MEDIA_MSGPACK
    assert msgpack.loads(result.content)['title'] == 'Bad Request'


def test_request_content_type_unaffected(msgpack):
    client = create_client(MediaInputResource())
    body = b'{"answer": 42}'
    result = client.simulate_post(
        '/',
        body=body,
        headers={
            'Content-Type': falcon.MEDIA_JSON,
            'Accept': 'application/msgpack',
        },
    )
    assert result.status_code == 200
    assert result.headers['Content-Type'] == 'application/msgpack'
    assert msgpack.loads(result.content) == {'answer': 42}

    result = client.simulate_post(
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


def test_undeclared_resource_keeps_default_behavior():
    client = create_client(UndeclaredResource())
    result = client.simulate_get('/', headers={'Accept': 'text/html'})
    assert result.status_code == 200
    assert result.headers['Content-Type'] == falcon.MEDIA_JSON
    assert 'Vary' not in result.headers


def test_provides_requires_media_types():
    with pytest.raises(TypeError):
        falcon.provides()(object())

    with pytest.raises(TypeError):
        falcon.provides('application/json', '')(object())

    with pytest.raises(TypeError):
        falcon.provides('application/json', 42)(object())
