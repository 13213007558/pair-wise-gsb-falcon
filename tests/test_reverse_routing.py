from datetime import datetime
from datetime import timezone
import uuid

import pytest

import falcon
from falcon import testing
from falcon.routing import converters
from falcon.routing import DefaultRouter
from falcon.routing.compiled import UnacceptableRouteError

_TEST_UUID = uuid.uuid4()
_TEST_DT = datetime(2024, 2, 29, 13, 45, 30, tzinfo=timezone.utc)


class CapturingResource:
    def __init__(self):
        self.captured_kwargs = None

    def on_get(self, req, resp, **kwargs):
        self.captured_kwargs = kwargs
        resp.media = {'kwargs': {k: str(v) for k, v in kwargs.items()}}


class SuffixedResource:
    def __init__(self):
        self.suffix_hit = None

    def on_get_item(self, req, resp, name):
        self.suffix_hit = 'item'
        resp.media = {'suffix': 'item', 'name': name}

    def on_get_collection(self, req, resp):
        self.suffix_hit = 'collection'
        resp.media = {'suffix': 'collection'}


class UpperConverter(converters.BaseConverter):
    def convert(self, value):
        return value.upper()

    def reverse(self, value):
        return str(value).lower()


@pytest.fixture()
def router():
    return DefaultRouter()


@pytest.fixture()
def client(asgi, util):
    return testing.TestClient(util.create_app(asgi))


class TestUrlFor:
    def test_static_route(self, router):
        router.add_route('/about', CapturingResource(), name='about')

        assert router.url_for('about') == '/about'

    def test_unknown_name(self, router):
        router.add_route('/about', CapturingResource(), name='about')

        with pytest.raises(KeyError, match='nope'):
            router.url_for('nope')

    def test_missing_params(self, router):
        router.add_route('/a/{x}/b/{y}', CapturingResource(), name='ab')

        with pytest.raises(ValueError, match='Missing.*x, y'):
            router.url_for('ab')

        with pytest.raises(ValueError, match='Missing.*y'):
            router.url_for('ab', x=1)

    def test_extra_params(self, router):
        router.add_route('/a/{x}', CapturingResource(), name='a')

        with pytest.raises(ValueError, match='Unexpected.*bogus'):
            router.url_for('a', x=1, bogus=2)

    @pytest.mark.parametrize('name', ['', 42, None.__class__])
    def test_invalid_name(self, router, name):
        with pytest.raises(ValueError, match='non-empty strings'):
            router.add_route('/a', CapturingResource(), name=name)

    def test_complex_segment(self, router):
        resource = CapturingResource()
        router.add_route(
            '/repos/{org}/{repo}/compare/{usr0}:{branch0}...{usr1}:{branch1}',
            resource,
            name='compare',
        )

        path = router.url_for(
            'compare',
            org='falconry',
            repo='falcon',
            usr0='alice',
            branch0='main',
            usr1='bob',
            branch1='dev',
        )

        assert path == '/repos/falconry/falcon/compare/alice:main...bob:dev'

        _, _, params, _ = router.find(path)
        assert params == {
            'org': 'falconry',
            'repo': 'falcon',
            'usr0': 'alice',
            'branch0': 'main',
            'usr1': 'bob',
            'branch1': 'dev',
        }


class TestConverterReverse:
    def test_int(self, router):
        router.add_route('/teams/{tid:int(8)}', CapturingResource(), name='team')

        assert router.url_for('team', tid=12345678) == '/teams/12345678'

        _, _, params, _ = router.find('/teams/12345678')
        assert params == {'tid': 12345678}

    @pytest.mark.parametrize(
        'converter, value',
        [
            (converters.IntConverter(num_digits=8), 123),
            (converters.IntConverter(min=10), 5),
            (converters.IntConverter(max=10), 11),
            (converters.IntConverter(), 'not-an-int'),
        ],
    )
    def test_int_invalid(self, converter, value):
        with pytest.raises(ValueError, match='can not be reversed'):
            converter.reverse(value)

    def test_float(self, router):
        router.add_route(
            '/versions/{v:float(min=3.7)}', CapturingResource(), name='version'
        )

        assert router.url_for('version', v=3.9) == '/versions/3.9'

        _, _, params, _ = router.find('/versions/3.9')
        assert params == {'v': 3.9}

    @pytest.mark.parametrize('value', [float('nan'), float('inf'), 3.6, 'x'])
    def test_float_invalid(self, value):
        converter = converters.FloatConverter(min=3.7)

        with pytest.raises(ValueError, match='can not be reversed'):
            converter.reverse(value)

    def test_uuid(self, router):
        router.add_route('/images/{name:uuid}', CapturingResource(), name='image')

        expected = f'/images/{_TEST_UUID}'
        assert router.url_for('image', name=_TEST_UUID) == expected
        assert (
            router.url_for('image', name=str(_TEST_UUID).replace('-', '')) == expected
        )

        _, _, params, _ = router.find(expected)
        assert params == {'name': _TEST_UUID}

    def test_uuid_invalid(self):
        converter = converters.UUIDConverter()

        with pytest.raises(ValueError):
            converter.reverse('not-a-uuid')

    def test_datetime(self, router):
        router.add_route('/logs/{day:dt("%Y-%m-%d")}', CapturingResource(), name='log')

        assert router.url_for('log', day=_TEST_DT) == '/logs/2024-02-29'

        _, _, params, _ = router.find('/logs/2024-02-29')
        assert params == {'day': datetime(2024, 2, 29)}

    def test_datetime_default_format(self):
        converter = converters.DateTimeConverter()

        text = converter.reverse(_TEST_DT)
        assert converter.convert(text) == _TEST_DT

    def test_base_converter_not_implemented(self):
        class MinimalConverter(converters.BaseConverter):
            def convert(self, value):
                return value

        with pytest.raises(NotImplementedError, match='MinimalConverter'):
            MinimalConverter().reverse('spam')

    def test_regex_converter_not_implemented(self, router):
        router.add_route(
            '/things/{name:re("[a-z]+")}', CapturingResource(), name='thing'
        )

        with pytest.raises(NotImplementedError, match='RegexConverter'):
            router.url_for('thing', name='spam')

    def test_custom_converter(self, router):
        router.options.converters['upper'] = UpperConverter
        router.add_route('/shout/{word:upper}', CapturingResource(), name='shout')

        path = router.url_for('shout', word='HELLO')
        assert path == '/shout/hello'

        _, _, params, _ = router.find(path)
        assert params == {'word': 'HELLO'}


class TestPathConverterReverse:
    def test_string_value(self, router):
        router.add_route('/files/{path:path}', CapturingResource(), name='file')

        assert router.url_for('file', path='a/b/c.txt') == '/files/a/b/c.txt'

        _, _, params, _ = router.find('/files/a/b/c.txt')
        assert params == {'path': 'a/b/c.txt'}

    def test_segment_list_value(self, router):
        router.add_route('/files/{path:path}', CapturingResource(), name='file')

        assert router.url_for('file', path=['a', 'b', 'c.txt']) == '/files/a/b/c.txt'

    def test_segments_encoded_individually(self, router):
        router.add_route('/files/{path:path}', CapturingResource(), name='file')

        # NOTE: A '/' within a single segment is encoded so that it can
        #   not smuggle additional segments into the generated path.
        path = router.url_for('file', path=['a/b', 'c d'])
        assert path == '/files/a%2Fb/c%20d'

        path = router.url_for('file', path='a/b c')
        assert path == '/files/a/b%20c'

    def test_empty_path(self, router):
        router.add_route('/files/{path:path}', CapturingResource(), name='file')

        assert router.url_for('file', path='') == '/files/'

        _, _, params, _ = router.find('/files/')
        assert params == {'path': ''}


class TestEncoding:
    @pytest.mark.parametrize(
        'value, expected',
        [
            ('hello', '/things/hello'),
            ('hello world', '/things/hello%20world'),
            ('caf\u00e9 \u4e2d\u6587', '/things/caf%C3%A9%20%E4%B8%AD%E6%96%87'),
            ('100%', '/things/100%25'),
            ('a?b#c', '/things/a%3Fb%23c'),
            ('a&b=c', '/things/a%26b%3Dc'),
            ('a/b', '/things/a%2Fb'),
            ('a;b:c@d', '/things/a%3Bb%3Ac%40d'),
        ],
    )
    def test_field_value_encoding(self, router, value, expected):
        router.add_route('/things/{name}', CapturingResource(), name='thing')

        assert router.url_for('thing', name=value) == expected

    @pytest.mark.parametrize(
        'value',
        [
            'hello world',
            'caf\u00e9 \u4e2d\u6587',
            '100%',
            'a?b#c',
            'a&b=c',
        ],
    )
    def test_roundtrip(self, client, value):
        resource = CapturingResource()
        client.app.add_route('/things/{name}', resource, name='thing')

        path = client.app.url_for('thing', name=value)
        resp = client.simulate_get(path)

        assert resp.status == falcon.HTTP_200
        assert resource.captured_kwargs == {'name': value}


class TestRouteNameRegistration:
    def test_duplicate_name(self, router):
        router.add_route('/one', CapturingResource(), name='route')

        with pytest.raises(UnacceptableRouteError, match='already in use'):
            router.add_route('/two', CapturingResource(), name='route')

        # NOTE: The failed registration must not pollute the name index.
        assert router.url_for('route') == '/one'

    def test_failed_registration_does_not_pollute_index(self, router):
        router.add_route('/parents/{pid}', CapturingResource())

        with pytest.raises(UnacceptableRouteError):
            router.add_route('/parents/{oid}', CapturingResource(), name='child')

        with pytest.raises(KeyError):
            router.url_for('child')

    def test_invalid_template_does_not_pollute_index(self, router):
        with pytest.raises(UnacceptableRouteError):
            router.add_route('/bad/{x:bogusconverter}', CapturingResource(), name='bad')

        with pytest.raises(KeyError):
            router.url_for('bad')

    def test_override_with_new_name_invalidates_old_name(self, router):
        router.add_route('/things/{tid}', CapturingResource(), name='old')
        router.add_route('/things/{tid}', CapturingResource(), name='new')

        with pytest.raises(KeyError):
            router.url_for('old', tid=1)

        assert router.url_for('new', tid=1) == '/things/1'

    def test_override_without_name_invalidates_old_name(self, router):
        router.add_route('/things/{tid}', CapturingResource(), name='old')
        router.add_route('/things/{tid}', CapturingResource())

        with pytest.raises(KeyError):
            router.url_for('old', tid=1)

    def test_override_with_same_name(self, router):
        router.add_route('/things/{tid}', CapturingResource(), name='thing')
        router.add_route('/things/{tid}', CapturingResource(), name='thing')

        assert router.url_for('thing', tid=1) == '/things/1'


class TestAppIntegration:
    def test_roundtrip(self, client):
        resource = CapturingResource()
        client.app.add_route(
            '/teams/{tid:int(8)}/members/{name}', resource, name='member'
        )

        path = client.app.url_for('member', tid=12345678, name='kgriffs')
        assert path == '/teams/12345678/members/kgriffs'

        resp = client.simulate_get(path)

        assert resp.status == falcon.HTTP_200
        assert resource.captured_kwargs == {'tid': 12345678, 'name': 'kgriffs'}

    def test_suffixed_responder(self, client):
        resource = SuffixedResource()
        client.app.add_route('/items', resource, suffix='collection', name='items')
        client.app.add_route('/items/{name}', resource, suffix='item', name='item')

        resp = client.simulate_get(client.app.url_for('items'))
        assert resp.json == {'suffix': 'collection'}

        resp = client.simulate_get(client.app.url_for('item', name='spam'))
        assert resp.json == {'suffix': 'item', 'name': 'spam'}

    def test_mounted_prefix(self, client):
        resource = CapturingResource()
        client.app.add_route('/api/v1/things/{tid:int}', resource, name='thing-detail')

        path = client.app.url_for('thing-detail', tid=42)
        assert path == '/api/v1/things/42'

        resp = client.simulate_get(path)
        assert resp.status == falcon.HTTP_200
        assert resource.captured_kwargs == {'tid': 42}

    def test_wsgi_asgi_consistency(self, asgi, util):
        paths = []
        for app_asgi in (False, True):
            app = util.create_app(app_asgi)
            app.add_route('/teams/{tid:int(8)}', CapturingResource(), name='team')
            app.add_route('/files/{path:path}', CapturingResource(), name='file')
            paths.append(
                (
                    app.url_for('team', tid=12345678),
                    app.url_for('file', path='a/b c'),
                )
            )

        assert paths[0] == paths[1]

    def test_custom_router_unsupported(self, util, asgi):
        class CustomRouter:
            def add_route(self, uri_template, resource, **kwargs):
                pass

            def find(self, uri, req=None):
                return None

        app = util.create_app(asgi, router=CustomRouter())

        with pytest.raises(NotImplementedError, match='url_for'):
            app.url_for('anything')
