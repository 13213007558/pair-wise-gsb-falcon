"""Tests for reversing named routes via url_for()."""

from datetime import datetime
from datetime import timezone
import uuid

import pytest

from falcon import testing
from falcon.routing import converters
from falcon.routing.compiled import UnacceptableRouteError

_TEST_UUID = uuid.uuid4()
_TEST_DT = datetime(2024, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
_TEST_DT_STR = '2024-03-04T05%3A06%3A07%2B0000'


def _make_resource(asgi, captured, marker='ok'):
    if asgi:

        class AsyncResource:
            async def on_get(self, req, resp, **kwargs):
                captured['kwargs'] = kwargs
                captured['marker'] = marker
                resp.text = marker

            async def on_get_item(self, req, resp, **kwargs):
                captured['kwargs'] = kwargs
                captured['marker'] = marker + ':item'
                resp.text = marker + ':item'

        return AsyncResource()

    class Resource:
        def on_get(self, req, resp, **kwargs):
            captured['kwargs'] = kwargs
            captured['marker'] = marker
            resp.text = marker

        def on_get_item(self, req, resp, **kwargs):
            captured['kwargs'] = kwargs
            captured['marker'] = marker + ':item'
            resp.text = marker + ':item'

    return Resource()


@pytest.fixture
def app(asgi, util):
    return util.create_app(asgi)


@pytest.fixture
def client(app):
    return testing.TestClient(app)


@pytest.fixture
def captured():
    return {}


class TestRoundTrip:
    @pytest.mark.parametrize(
        'template,params,expected_path,expected_params',
        [
            (
                '/things/{tid:int}',
                {'tid': 42},
                '/things/42',
                {'tid': 42},
            ),
            (
                '/things/{tid:int(3)}',
                {'tid': 42},
                '/things/042',
                {'tid': 42},
            ),
            (
                '/things/{tid:int(min=1, max=100)}',
                {'tid': 42},
                '/things/42',
                {'tid': 42},
            ),
            (
                '/floats/{value:float}',
                {'value': 2.5},
                '/floats/2.5',
                {'value': 2.5},
            ),
            (
                '/items/{iid:uuid}',
                {'iid': _TEST_UUID},
                f'/items/{_TEST_UUID}',
                {'iid': _TEST_UUID},
            ),
            (
                '/logs/{ts:dt}',
                {'ts': _TEST_DT},
                '/logs/' + _TEST_DT_STR,
                {'ts': _TEST_DT},
            ),
            (
                '/files/{rest:path}',
                {'rest': 'a/b/c'},
                '/files/a/b/c',
                {'rest': 'a/b/c'},
            ),
            (
                '/users/{uname}',
                {'uname': 'alice'},
                '/users/alice',
                {'uname': 'alice'},
            ),
            (
                '/files/{fname}.{ext}',
                {'fname': 'report', 'ext': 'pdf'},
                '/files/report.pdf',
                {'fname': 'report', 'ext': 'pdf'},
            ),
        ],
    )
    def test_round_trip(
        self,
        asgi,
        app,
        client,
        captured,
        template,
        params,
        expected_path,
        expected_params,
    ):
        resource = _make_resource(asgi, captured)
        app.add_route(template, resource, name='route')

        path = app.url_for('route', **params)
        assert path == expected_path

        response = client.simulate_get(path)
        assert response.status_code == 200
        assert captured['kwargs'] == expected_params

    def test_round_trip_dt_custom_format(self, asgi, app, client, captured):
        resource = _make_resource(asgi, captured)
        app.add_route('/logs/{day:dt("%Y-%m-%d")}', resource, name='logs')

        path = app.url_for('logs', day=_TEST_DT)
        assert path == '/logs/2024-03-04'

        response = client.simulate_get(path)
        assert response.status_code == 200
        assert captured['kwargs'] == {'day': datetime(2024, 3, 4)}

    def test_field_named_name(self, asgi, app, client, captured):
        resource = _make_resource(asgi, captured)
        app.add_route('/users/{name}', resource, name='user')

        assert app.url_for('user', name='alice') == '/users/alice'

        response = client.simulate_get('/users/alice')
        assert response.status_code == 200
        assert captured['kwargs'] == {'name': 'alice'}

    def test_adjacent_fields(self, asgi, app, client, captured):
        resource = _make_resource(asgi, captured)
        app.add_route('/combo/{a}{b}', resource, name='combo')

        path = app.url_for('combo', a='x', b='y')
        assert path == '/combo/xy'

        response = client.simulate_get(path)
        assert response.status_code == 200
        assert captured['kwargs'] == {'a': 'x', 'b': 'y'}


class TestEncoding:
    def test_reserved_characters_are_encoded(self, asgi, app, client, captured):
        resource = _make_resource(asgi, captured)
        app.add_route('/users/{uname}', resource, name='user')

        value = 'c d%e?f#g&h=iü'
        path = app.url_for('user', uname=value)
        assert path == '/users/c%20d%25e%3Ff%23g%26h%3Di%C3%BC'

        response = client.simulate_get(path)
        assert response.status_code == 200
        assert captured['kwargs'] == {'uname': value}

    def test_slash_is_encoded(self, app):
        resource = _make_resource(False, {})
        app.add_route('/users/{uname}', resource, name='user')

        # A slash can not be carried by a single-segment field, so it is
        # percent-encoded; the path converter should be used instead
        # when slashes are meaningful.
        assert app.url_for('user', uname='a/b') == '/users/a%2Fb'

    def test_path_converter_segments(self, asgi, app, client, captured):
        resource = _make_resource(asgi, captured)
        app.add_route('/files/{rest:path}', resource, name='file')

        # A '/' always denotes a segment separator; each segment is
        # percent-encoded individually.
        assert app.url_for('file', rest='a/b/c') == '/files/a/b/c'
        assert app.url_for('file', rest=['a', 'b', 'c']) == '/files/a/b/c'
        assert app.url_for('file', rest='a/%/ü') == '/files/a/%25/%C3%BC'
        assert app.url_for('file', rest='') == '/files/'

        response = client.simulate_get('/files/a/%25/%C3%BC')
        assert response.status_code == 200
        assert captured['kwargs'] == {'rest': 'a/%/ü'}

        response = client.simulate_get('/files/')
        assert response.status_code == 200
        assert captured['kwargs'] == {'rest': ''}


class TestCustomConverters:
    def test_custom_converter_with_reverse(self, asgi, app, client, captured):
        class ReversingConverter(converters.BaseConverter):
            def convert(self, value):
                return value[::-1]

            def reverse(self, value):
                return value[::-1]

        app.router_options.converters['rev'] = ReversingConverter
        resource = _make_resource(asgi, captured)
        app.add_route('/pal/{word:rev}', resource, name='pal')

        assert app.url_for('pal', word='abc') == '/pal/cba'

        response = client.simulate_get('/pal/cba')
        assert response.status_code == 200
        assert captured['kwargs'] == {'word': 'abc'}

    def test_custom_converter_without_reverse(self, asgi, app):
        class NonReversingConverter(converters.BaseConverter):
            def convert(self, value):
                return value

        app.router_options.converters['norev'] = NonReversingConverter
        resource = _make_resource(asgi, {})
        app.add_route('/things/{thing:norev}', resource, name='thing')

        with pytest.raises(NotImplementedError, match='NonReversingConverter'):
            app.url_for('thing', thing='abc')

    def test_regex_converter_does_not_reverse(self, asgi, app):
        resource = _make_resource(asgi, {})
        app.add_route('/tokens/{token:re("[a-z]+")}', resource, name='token')

        with pytest.raises(NotImplementedError, match='RegexConverter'):
            app.url_for('token', token='abc')


class TestNameIndex:
    def test_unknown_name(self, app):
        with pytest.raises(ValueError, match='No route is registered'):
            app.url_for('nope')

    def test_missing_param(self, asgi, app):
        resource = _make_resource(asgi, {})
        app.add_route('/a/{x}/b/{y}', resource, name='ab')

        with pytest.raises(ValueError, match='Missing'):
            app.url_for('ab', x=1)

    def test_extra_param(self, asgi, app):
        resource = _make_resource(asgi, {})
        app.add_route('/a/{x}/b/{y}', resource, name='ab')

        with pytest.raises(ValueError, match='Unexpected'):
            app.url_for('ab', x=1, y=2, z=3)

    @pytest.mark.parametrize('bad_name', ['', 42])
    def test_invalid_name(self, asgi, app, bad_name):
        resource = _make_resource(asgi, {})

        with pytest.raises(ValueError, match='non-empty string'):
            app.add_route('/things', resource, name=bad_name)

    def test_duplicate_name_rejected(self, asgi, app, client):
        app.add_route('/foo', _make_resource(asgi, {}), name='thing')

        with pytest.raises(ValueError, match='already registered'):
            app.add_route('/bar', _make_resource(asgi, {}), name='thing')

        # The name index still points to the first route, and the
        # conflicting route was never added.
        assert app.url_for('thing') == '/foo'
        assert client.simulate_get('/bar').status_code == 404

    def test_failed_registration_preserves_name_index(self, asgi, app):
        app.add_route('/foo/{fid}', _make_resource(asgi, {}), name='foo')

        with pytest.raises(UnacceptableRouteError):
            app.add_route('/foo/{other}', _make_resource(asgi, {}), name='bar')

        with pytest.raises(ValueError, match='No route is registered'):
            app.url_for('bar')

        with pytest.raises(UnacceptableRouteError):
            app.add_route('/baz/{x:nope}', _make_resource(asgi, {}), name='baz')

        with pytest.raises(ValueError, match='No route is registered'):
            app.url_for('baz')

        assert app.url_for('foo', fid=1) == '/foo/1'

    def test_override_without_name_invalidates_stale_name(self, asgi, app):
        app.add_route('/foo', _make_resource(asgi, {}), name='foo')
        app.add_route('/foo', _make_resource(asgi, {}))

        with pytest.raises(ValueError, match='No route is registered'):
            app.url_for('foo')

    def test_override_with_new_name_invalidates_stale_name(self, asgi, app):
        app.add_route('/foo', _make_resource(asgi, {}), name='old')
        app.add_route('/foo', _make_resource(asgi, {}), name='new')

        with pytest.raises(ValueError, match='No route is registered'):
            app.url_for('old')

        assert app.url_for('new') == '/foo'

    def test_reregister_same_template_and_name(self, asgi, app, client):
        first = {}
        second = {}
        app.add_route('/foo', _make_resource(asgi, first, 'first'), name='foo')
        app.add_route('/foo', _make_resource(asgi, second, 'second'), name='foo')

        assert app.url_for('foo') == '/foo'

        response = client.simulate_get('/foo')
        assert response.status_code == 200
        assert second.get('marker') == 'second'
        assert 'marker' not in first


class TestAppIntegration:
    def test_suffixed_responder(self, asgi, app, client, captured):
        resource = _make_resource(asgi, captured)
        app.add_route('/items/{item_id:int}', resource, suffix='item', name='item')

        path = app.url_for('item', item_id=3)
        assert path == '/items/3'

        response = client.simulate_get(path)
        assert response.status_code == 200
        assert captured['marker'] == 'ok:item'
        assert captured['kwargs'] == {'item_id': 3}

    def test_mounted_app(self, asgi, app, client, captured):
        resource = _make_resource(asgi, captured)
        app.add_route('/things/{tid:int}', resource, name='thing')

        # The generated path is relative to the root of the application;
        # any prefix the app is mounted under is not included.
        path = app.url_for('thing', tid=7)
        assert path == '/things/7'

        response = client.simulate_get(path, root_path='/mounted/api')
        assert response.status_code == 200
        assert captured['kwargs'] == {'tid': 7}

    def test_custom_router_not_supported(self, asgi, util):
        class CustomRouter:
            def add_route(self, uri_template, resource, **kwargs):
                pass

            def find(self, uri):
                pass

        app = util.create_app(asgi, router=CustomRouter())

        with pytest.raises(NotImplementedError, match='does not support'):
            app.url_for('thing')


class TestConverterReverse:
    def test_int(self):
        converter = converters.IntConverter()
        assert converter.reverse(42) == '42'
        assert converter.reverse(-7) == '-7'

        with pytest.raises(ValueError):
            converter.reverse('abc')

    def test_int_num_digits(self):
        converter = converters.IntConverter(3)
        assert converter.reverse(42) == '042'
        assert converter.reverse(123) == '123'

        with pytest.raises(ValueError):
            converter.reverse(1000)

    def test_int_min_max(self):
        converter = converters.IntConverter(min=10, max=99)
        assert converter.reverse(42) == '42'

        with pytest.raises(ValueError):
            converter.reverse(5)

        with pytest.raises(ValueError):
            converter.reverse(100)

    def test_float(self):
        converter = converters.FloatConverter()
        assert converter.reverse(2.5) == '2.5'

        with pytest.raises(ValueError):
            converter.reverse('abc')

        with pytest.raises(ValueError):
            converter.reverse(float('nan'))

        with pytest.raises(ValueError):
            converter.reverse(float('inf'))

        assert converters.FloatConverter(finite=False).reverse(float('inf')) == 'inf'

    def test_float_min_max(self):
        converter = converters.FloatConverter(min=0.0, max=1.0)
        assert converter.reverse(0.5) == '0.5'

        with pytest.raises(ValueError):
            converter.reverse(2.0)

    def test_datetime(self):
        converter = converters.DateTimeConverter()
        assert converter.reverse(_TEST_DT) == '2024-03-04T05:06:07+0000'

        converter = converters.DateTimeConverter('%Y/%m/%d')
        assert converter.reverse(_TEST_DT) == '2024/03/04'

        with pytest.raises(TypeError):
            converter.reverse('2024-03-04')

    def test_uuid(self):
        converter = converters.UUIDConverter()
        assert converter.reverse(_TEST_UUID) == str(_TEST_UUID)

        sans_hyphens = str(_TEST_UUID).replace('-', '')
        assert converter.reverse(sans_hyphens) == str(_TEST_UUID)

        with pytest.raises(ValueError):
            converter.reverse('not-a-uuid')

    def test_path(self):
        converter = converters.PathConverter()
        assert converter.reverse('a/b/c') == 'a/b/c'
        assert converter.reverse(['a', 'b', 'c']) == 'a/b/c'

    def test_base_converter_does_not_reverse(self):
        class MinimalConverter(converters.BaseConverter):
            def convert(self, value):
                return value

        with pytest.raises(NotImplementedError, match='MinimalConverter'):
            MinimalConverter().reverse('abc')
