import gzip
import io
import json

import pytest

import falcon
from falcon import errors
from falcon import testing
import falcon.asgi
from falcon.util.decompression import DEFAULT_MAX_DECOMPRESSED_BODY_SIZE
from falcon.util.decompression import DEFAULT_MAX_DECOMPRESSION_RATIO
from falcon.util.decompression import GzipDecoder

DOC = {'message': 'compress me! ' * 64, 'items': list(range(64))}
DOC_BODY = json.dumps(DOC).encode()
DOC_GZ = gzip.compress(DOC_BODY)

# NOTE: Highly compressible body (a decompression "bomb" candidate).
BOMB_BODY = b'x' * (32 * 1024 * 1024)
BOMB_GZ = gzip.compress(BOMB_BODY)

LINES_BODY = b'first line\nsecond line\nthird (no newline)'
LINES_GZ = gzip.compress(LINES_BODY)

MULTI_MEMBER_GZ = gzip.compress(b'{"first": 1,') + gzip.compress(b'"second": 2}')


def _make_client(asgi, resource, **req_options):
    app_cls = falcon.asgi.App if asgi else falcon.App
    app = app_cls()
    for name, value in req_options.items():
        setattr(app.req_options, name, value)
    app.add_route('/', resource)
    client = testing.TestClient(app)
    client.resource = resource
    return client


class MediaResource:
    def on_post(self, req, resp):
        self.captured_content_length = req.content_length
        self.captured_media = req.get_media()
        self.captured_media_cached = req.get_media()
        resp.media = self.captured_media


class MediaResourceAsync:
    async def on_post(self, req, resp):
        self.captured_content_length = req.content_length
        self.captured_media = await req.get_media()
        self.captured_media_cached = await req.get_media()
        resp.media = self.captured_media


class MediaErrorResource:
    def on_post(self, req, resp):
        with pytest.raises(self.expected_error) as err1:
            req.get_media()
        with pytest.raises(self.expected_error) as err2:
            req.get_media()

        # NOTE: The error is cached; the body is not consumed again.
        assert err1.value is err2.value
        self.captured_error = err1.value
        raise err1.value


class MediaErrorResourceAsync:
    async def on_post(self, req, resp):
        with pytest.raises(self.expected_error) as err1:
            await req.get_media()
        with pytest.raises(self.expected_error) as err2:
            await req.get_media()

        assert err1.value is err2.value
        self.captured_error = err1.value
        raise err1.value


class StreamResource:
    def on_post_read(self, req, resp):
        self.captured_body = req.bounded_stream.read()

    def on_post_read_chunks(self, req, resp):
        chunks = []
        while True:
            chunk = req.bounded_stream.read(5)
            if not chunk:
                break
            chunks.append(chunk)
        self.captured_chunks = chunks

    def on_post_readline(self, req, resp):
        lines = []
        while True:
            line = req.bounded_stream.readline()
            if not line:
                break
            lines.append(line)
        self.captured_lines = lines

    def on_post_iterate(self, req, resp):
        self.captured_chunks = list(req.bounded_stream)


class StreamResourceAsync:
    async def on_post_read(self, req, resp):
        self.captured_body = await req.bounded_stream.read()

    async def on_post_read_chunks(self, req, resp):
        chunks = []
        while True:
            chunk = await req.bounded_stream.read(5)
            if not chunk:
                break
            chunks.append(chunk)
        self.captured_chunks = chunks

    async def on_post_readline(self, req, resp):
        lines = []
        while True:
            line = await req.bounded_stream.readline()
            if not line:
                break
            lines.append(line)
        self.captured_lines = lines

    async def on_post_iterate(self, req, resp):
        chunks = []
        async for chunk in req.bounded_stream:
            chunks.append(chunk)
        self.captured_chunks = chunks


@pytest.fixture()
def media_resource(asgi):
    return MediaResourceAsync() if asgi else MediaResource()


@pytest.fixture()
def media_error_resource(asgi):
    resource = MediaErrorResourceAsync() if asgi else MediaErrorResource()
    resource.expected_error = errors.ContentDecompressionError
    return resource


@pytest.fixture()
def stream_resource(asgi):
    return StreamResourceAsync() if asgi else StreamResource()


def _add_stream_routes(client, resource):
    client.app.add_route('/read', resource, suffix='read')
    client.app.add_route('/read-chunks', resource, suffix='read_chunks')
    client.app.add_route('/readline', resource, suffix='readline')
    client.app.add_route('/iterate', resource, suffix='iterate')


class CountingStream:
    """WSGI input stream wrapper that counts the bytes pulled from it."""

    def __init__(self, data):
        self._stream = io.BytesIO(data)
        self.bytes_read = 0

    def read(self, size=-1):
        data = self._stream.read(size)
        self.bytes_read += len(data)
        return data


class CountingReceive:
    """ASGI receive wrapper that counts the events pulled from it."""

    def __init__(self, emitter):
        self._emitter = emitter
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        return await self._emitter.emit()


def _make_wsgi_request(body, encoding='gzip', **req_options):
    env = testing.create_environ(
        method='POST',
        body=body,
        headers={
            'Content-Type': 'application/json',
            'Content-Encoding': encoding,
        },
    )
    stream = CountingStream(body)
    env['wsgi.input'] = stream
    req = falcon.Request(env)
    for name, value in req_options.items():
        setattr(req.options, name, value)
    return req, stream


def _make_asgi_request(body, chunk_size=4096, encoding='gzip', **req_options):
    scope = testing.create_scope(
        method='POST',
        headers={
            'Content-Type': 'application/json',
            'Content-Encoding': encoding,
        },
        content_length=len(body),
    )
    emitter = testing.ASGIRequestEventEmitter(body=body, chunk_size=chunk_size)
    emitter._emit_empty_chunks = False
    receive = CountingReceive(emitter)
    req = falcon.asgi.Request(scope, receive)
    for name, value in req_options.items():
        setattr(req.options, name, value)
    return req, emitter, receive


class TestGzipMedia:
    def test_media_roundtrip(self, asgi, media_resource):
        client = _make_client(asgi, media_resource)
        result = client.simulate_post(
            '/',
            body=DOC_GZ,
            headers={'Content-Type': 'application/json', 'Content-Encoding': 'gzip'},
        )

        assert result.status == falcon.HTTP_200
        assert result.json == DOC
        assert media_resource.captured_media == DOC
        # NOTE: get_media() caches the result of the first call.
        assert media_resource.captured_media_cached is media_resource.captured_media
        # NOTE: Content-Length still describes the compressed body on
        #   the wire, and the (larger) decompressed document is not
        #   truncated to it.
        assert media_resource.captured_content_length == len(DOC_GZ)
        assert len(DOC_BODY) > len(DOC_GZ)

    @pytest.mark.parametrize('encoding', ['gzip', 'GZip', 'x-gzip', 'X-GZIP'])
    def test_encoding_aliases(self, asgi, media_resource, encoding):
        client = _make_client(asgi, media_resource)
        result = client.simulate_post(
            '/',
            body=DOC_GZ,
            headers={'Content-Type': 'application/json', 'Content-Encoding': encoding},
        )
        assert result.status == falcon.HTTP_200
        assert result.json == DOC

    @pytest.mark.parametrize('encoding', ['identity', 'Identity', 'identity,identity'])
    def test_identity_passthrough(self, asgi, media_resource, encoding):
        client = _make_client(asgi, media_resource)
        result = client.simulate_post(
            '/',
            body=DOC_BODY,
            headers={'Content-Type': 'application/json', 'Content-Encoding': encoding},
        )
        assert result.status == falcon.HTTP_200
        assert result.json == DOC

    def test_multi_member(self, asgi, media_resource):
        client = _make_client(asgi, media_resource)
        result = client.simulate_post(
            '/',
            body=MULTI_MEMBER_GZ,
            headers={'Content-Type': 'application/json', 'Content-Encoding': 'gzip'},
        )
        assert result.status == falcon.HTTP_200
        assert result.json == {'first': 1, 'second': 2}

    def test_empty_gzip_member(self, asgi, media_resource):
        client = _make_client(asgi, media_resource)
        result = client.simulate_post(
            '/',
            body=gzip.compress(b''),
            headers={'Content-Type': 'application/json', 'Content-Encoding': 'gzip'},
        )
        # NOTE: A valid gzip member decompressing to an empty body is
        #   surfaced to the media handler as an empty body.
        assert result.status == falcon.HTTP_400
        assert result.json['title'] == 'Invalid JSON'

    def test_empty_body(self, asgi, media_resource):
        client = _make_client(asgi, media_resource)
        result = client.simulate_post(
            '/',
            body=b'',
            headers={'Content-Type': 'application/json', 'Content-Encoding': 'gzip'},
        )
        assert result.status == falcon.HTTP_400
        assert result.json['title'] == 'Content decompression failed'

    def test_urlencoded_form(self, asgi):
        class FormResource:
            def on_post(self, req, resp):
                resp.media = req.get_media()

        class FormResourceAsync:
            async def on_post(self, req, resp):
                resp.media = await req.get_media()

        resource = FormResourceAsync() if asgi else FormResource()
        client = _make_client(asgi, resource)
        body = gzip.compress(b'foo=1&bar=2')
        result = client.simulate_post(
            '/',
            body=body,
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'Content-Encoding': 'gzip',
            },
        )
        assert result.status == falcon.HTTP_200
        assert result.json == {'foo': '1', 'bar': '2'}


class TestGzipStream:
    def test_read(self, asgi, stream_resource):
        client = _make_client(asgi, stream_resource)
        _add_stream_routes(client, stream_resource)
        result = client.simulate_post(
            '/read', body=DOC_GZ, headers={'Content-Encoding': 'gzip'}
        )
        assert result.status == falcon.HTTP_200
        assert stream_resource.captured_body == DOC_BODY

    def test_read_chunks(self, asgi, stream_resource):
        client = _make_client(asgi, stream_resource)
        _add_stream_routes(client, stream_resource)
        result = client.simulate_post(
            '/read-chunks', body=DOC_GZ, headers={'Content-Encoding': 'gzip'}
        )
        assert result.status == falcon.HTTP_200
        assert b''.join(stream_resource.captured_chunks) == DOC_BODY
        assert all(len(chunk) <= 5 for chunk in stream_resource.captured_chunks)

    def test_readline(self, asgi, stream_resource):
        client = _make_client(asgi, stream_resource)
        _add_stream_routes(client, stream_resource)
        result = client.simulate_post(
            '/readline', body=LINES_GZ, headers={'Content-Encoding': 'gzip'}
        )
        assert result.status == falcon.HTTP_200
        assert stream_resource.captured_lines == [
            b'first line\n',
            b'second line\n',
            b'third (no newline)',
        ]

    def test_iterate(self, asgi, stream_resource):
        client = _make_client(asgi, stream_resource)
        _add_stream_routes(client, stream_resource)
        result = client.simulate_post(
            '/iterate', body=LINES_GZ, headers={'Content-Encoding': 'gzip'}
        )
        assert result.status == falcon.HTTP_200
        assert b''.join(stream_resource.captured_chunks) == LINES_BODY

    async def test_byte_by_byte(self, asgi):
        if asgi:
            req, _, _ = _make_asgi_request(DOC_GZ, chunk_size=1)
            assert await req.bounded_stream.read() == DOC_BODY
        else:
            from falcon.stream import BoundedStream
            from falcon.stream import DecompressedStream

            stream = DecompressedStream(
                BoundedStream(io.BytesIO(DOC_GZ), len(DOC_GZ)), chunk_size=1
            )
            assert stream.read() == DOC_BODY


class TestGzipErrors:
    def test_corrupt_crc(self, asgi, media_error_resource):
        bad = bytearray(DOC_GZ)
        bad[-5] ^= 0xFF
        client = _make_client(asgi, media_error_resource)
        result = client.simulate_post(
            '/',
            body=bytes(bad),
            headers={'Content-Type': 'application/json', 'Content-Encoding': 'gzip'},
        )
        assert result.status == falcon.HTTP_400
        assert result.json['title'] == 'Content decompression failed'
        assert isinstance(
            media_error_resource.captured_error, errors.ContentDecompressionError
        )

    def test_corrupt_deflate_data(self, asgi, media_error_resource):
        bad = bytearray(DOC_GZ)
        bad[len(bad) // 2] ^= 0xFF
        client = _make_client(asgi, media_error_resource)
        result = client.simulate_post(
            '/',
            body=bytes(bad),
            headers={'Content-Type': 'application/json', 'Content-Encoding': 'gzip'},
        )
        assert result.status == falcon.HTTP_400
        assert result.json['title'] == 'Content decompression failed'

    @pytest.mark.parametrize('cut', [1, 8, len(DOC_GZ) // 2])
    def test_truncated(self, asgi, media_error_resource, cut):
        client = _make_client(asgi, media_error_resource)
        result = client.simulate_post(
            '/',
            body=DOC_GZ[:-cut],
            headers={'Content-Type': 'application/json', 'Content-Encoding': 'gzip'},
        )
        assert result.status == falcon.HTTP_400
        assert result.json['title'] == 'Content decompression failed'

    @pytest.mark.parametrize('garbage', [b'\r\n', b'garbage', b'\x1f\x8b'])
    def test_trailing_garbage(self, asgi, media_error_resource, garbage):
        client = _make_client(asgi, media_error_resource)
        result = client.simulate_post(
            '/',
            body=DOC_GZ + garbage,
            headers={'Content-Type': 'application/json', 'Content-Encoding': 'gzip'},
        )
        assert result.status == falcon.HTTP_400
        assert result.json['title'] == 'Content decompression failed'

    def test_not_gzip_at_all(self, asgi, media_error_resource):
        client = _make_client(asgi, media_error_resource)
        result = client.simulate_post(
            '/',
            body=b'{"this is": "plain json"}',
            headers={'Content-Type': 'application/json', 'Content-Encoding': 'gzip'},
        )
        assert result.status == falcon.HTTP_400
        assert result.json['title'] == 'Content decompression failed'

    @pytest.mark.parametrize(
        'encoding',
        ['br', 'deflate', 'compress', 'gzip, br', 'gzip, gzip', 'br, gzip', 'zstd'],
    )
    def test_unsupported_encoding(self, asgi, encoding):
        resource = MediaErrorResourceAsync() if asgi else MediaErrorResource()
        resource.expected_error = errors.UnsupportedContentEncodingError
        client = _make_client(asgi, resource)
        result = client.simulate_post(
            '/',
            body=DOC_GZ,
            headers={'Content-Type': 'application/json', 'Content-Encoding': encoding},
        )
        assert result.status == falcon.HTTP_415
        assert result.json['title'] == 'Unsupported content encoding'

    def test_stream_read_after_error_is_sticky(self):
        req, stream = _make_wsgi_request(
            BOMB_GZ, max_decompressed_body_size=1024, max_decompression_ratio=None
        )
        with pytest.raises(errors.DecompressedContentTooLarge):
            req.bounded_stream.read()
        pulled = stream.bytes_read
        with pytest.raises(errors.DecompressedContentTooLarge):
            req.bounded_stream.read()
        # NOTE: No more data is pulled from the underlying stream once
        #   the limit has been exceeded.
        assert stream.bytes_read == pulled

    async def test_stream_read_after_error_is_sticky_asgi(self):
        req, _, receive = _make_asgi_request(
            BOMB_GZ,
            chunk_size=1024,
            max_decompressed_body_size=1024,
            max_decompression_ratio=None,
        )
        with pytest.raises(errors.DecompressedContentTooLarge):
            await req.stream.read()
        calls = receive.calls
        with pytest.raises(errors.DecompressedContentTooLarge):
            await req.stream.read()
        assert receive.calls == calls


class TestDecompressionLimits:
    def test_default_options(self, asgi):
        app_cls = falcon.asgi.App if asgi else falcon.App
        options = app_cls().req_options
        assert options.max_decompressed_body_size == DEFAULT_MAX_DECOMPRESSED_BODY_SIZE
        assert options.max_decompression_ratio == DEFAULT_MAX_DECOMPRESSION_RATIO

    def test_size_limit_boundary(self, asgi, media_resource):
        client = _make_client(
            asgi,
            media_resource,
            max_decompressed_body_size=len(DOC_BODY),
            max_decompression_ratio=None,
        )
        result = client.simulate_post(
            '/',
            body=DOC_GZ,
            headers={'Content-Type': 'application/json', 'Content-Encoding': 'gzip'},
        )
        assert result.status == falcon.HTTP_200
        assert result.json == DOC

    def test_size_limit_exceeded(self, asgi, media_resource):
        resource = media_resource
        client = _make_client(
            asgi,
            resource,
            max_decompressed_body_size=len(DOC_BODY) - 1,
            max_decompression_ratio=None,
        )
        result = client.simulate_post(
            '/',
            body=DOC_GZ,
            headers={'Content-Type': 'application/json', 'Content-Encoding': 'gzip'},
        )
        assert result.status == falcon.HTTP_413
        assert result.json['title'] == 'Decompressed content too large'

    def test_ratio_limit_exceeded(self, asgi, media_resource):
        client = _make_client(
            asgi,
            media_resource,
            max_decompressed_body_size=None,
            max_decompression_ratio=10.0,
        )
        result = client.simulate_post(
            '/',
            body=BOMB_GZ,
            headers={'Content-Type': 'application/json', 'Content-Encoding': 'gzip'},
        )
        assert result.status == falcon.HTTP_413
        assert result.json['title'] == 'Decompressed content too large'

    def test_ratio_limit_not_exceeded(self, asgi, media_resource):
        client = _make_client(
            asgi,
            media_resource,
            max_decompressed_body_size=None,
            max_decompression_ratio=1000.0,
        )
        result = client.simulate_post(
            '/',
            body=DOC_GZ,
            headers={'Content-Type': 'application/json', 'Content-Encoding': 'gzip'},
        )
        assert result.status == falcon.HTTP_200
        assert result.json == DOC

    def test_bomb_default_limits(self, asgi, media_resource):
        client = _make_client(asgi, media_resource)
        result = client.simulate_post(
            '/',
            body=BOMB_GZ,
            headers={'Content-Type': 'application/json', 'Content-Encoding': 'gzip'},
        )
        assert result.status == falcon.HTTP_413

    def test_size_limit_stops_pulling_wsgi(self):
        req, stream = _make_wsgi_request(
            BOMB_GZ, max_decompressed_body_size=64 * 1024, max_decompression_ratio=None
        )
        with pytest.raises(errors.DecompressedContentTooLarge):
            req.bounded_stream.read()
        # NOTE: The stream stopped pulling compressed data as soon as
        #   the limit was tripped; the bulk of the bomb was never read.
        assert stream.bytes_read < len(BOMB_GZ)

    async def test_size_limit_stops_pulling_asgi(self):
        req, _, receive = _make_asgi_request(
            BOMB_GZ,
            chunk_size=1024,
            max_decompressed_body_size=64 * 1024,
            max_decompression_ratio=None,
        )
        with pytest.raises(errors.DecompressedContentTooLarge):
            await req.stream.read()
        expected_events = -(-len(BOMB_GZ) // 1024)
        assert receive.calls < expected_events

    async def test_ratio_limit_stops_pulling_asgi(self):
        req, _, receive = _make_asgi_request(
            BOMB_GZ,
            chunk_size=1024,
            max_decompressed_body_size=None,
            max_decompression_ratio=10.0,
        )
        with pytest.raises(errors.DecompressedContentTooLarge):
            await req.stream.read()
        expected_events = -(-len(BOMB_GZ) // 1024)
        assert receive.calls < expected_events

    def test_get_media_error_cached_wsgi(self):
        req, stream = _make_wsgi_request(
            BOMB_GZ, max_decompressed_body_size=1024, max_decompression_ratio=None
        )
        with pytest.raises(errors.DecompressedContentTooLarge) as err1:
            req.get_media()
        pulled = stream.bytes_read
        with pytest.raises(errors.DecompressedContentTooLarge) as err2:
            req.get_media()
        assert err1.value is err2.value
        assert stream.bytes_read == pulled

    async def test_get_media_error_cached_asgi(self):
        req, _, receive = _make_asgi_request(
            BOMB_GZ,
            chunk_size=1024,
            max_decompressed_body_size=1024,
            max_decompression_ratio=None,
        )
        with pytest.raises(errors.DecompressedContentTooLarge) as err1:
            await req.get_media()
        calls = receive.calls
        with pytest.raises(errors.DecompressedContentTooLarge) as err2:
            await req.get_media()
        assert err1.value is err2.value
        assert receive.calls == calls


class TestGzipDecoder:
    def test_no_limits(self):
        decoder = GzipDecoder(max_size=None, max_ratio=None)
        assert decoder.decompress(BOMB_GZ) == BOMB_BODY
        assert decoder.flush() == b''

    def test_size_limit_only(self):
        decoder = GzipDecoder(max_size=100, max_ratio=None)
        with pytest.raises(errors.DecompressedContentTooLarge):
            decoder.decompress(gzip.compress(b'x' * 1000))

    def test_ratio_limit_only(self):
        decoder = GzipDecoder(max_size=None, max_ratio=2.0)
        with pytest.raises(errors.DecompressedContentTooLarge):
            decoder.decompress(gzip.compress(b'x' * 1000))

    def test_exact_size_limit(self):
        body = b'x' * 100
        decoder = GzipDecoder(max_size=100, max_ratio=None)
        assert decoder.decompress(gzip.compress(body)) == body
        assert decoder.flush() == b''

    def test_multi_member(self):
        decoder = GzipDecoder()
        assert decoder.decompress(MULTI_MEMBER_GZ) == b'{"first": 1,"second": 2}'
        assert decoder.flush() == b''

    def test_byte_by_byte(self):
        decoder = GzipDecoder()
        out = b''
        for index in range(len(DOC_GZ)):
            out += decoder.decompress(DOC_GZ[index : index + 1])
        out += decoder.flush()
        assert out == DOC_BODY

    def test_truncated(self):
        decoder = GzipDecoder()
        decoder.decompress(DOC_GZ[:-4])
        with pytest.raises(errors.ContentDecompressionError):
            decoder.flush()

    def test_empty(self):
        decoder = GzipDecoder()
        with pytest.raises(errors.ContentDecompressionError):
            decoder.flush()

    def test_corrupt_crc(self):
        bad = bytearray(DOC_GZ)
        bad[-5] ^= 0xFF
        decoder = GzipDecoder()
        with pytest.raises(errors.ContentDecompressionError):
            decoder.decompress(bytes(bad))

    def test_trailing_garbage(self):
        decoder = GzipDecoder()
        with pytest.raises(errors.ContentDecompressionError):
            decoder.decompress(DOC_GZ + b'garbage')

    def test_trailing_partial_member(self):
        decoder = GzipDecoder()
        decoder.decompress(DOC_GZ + b'\x1f')
        with pytest.raises(errors.ContentDecompressionError):
            decoder.flush()


class TestDecompressedStreamWSGI:
    def _make_stream(self, data, **kwargs):
        from falcon.stream import BoundedStream
        from falcon.stream import DecompressedStream

        return DecompressedStream(BoundedStream(io.BytesIO(data), len(data)), **kwargs)

    def test_read_negative_size(self):
        stream = self._make_stream(DOC_GZ)
        assert stream.read(-1) == DOC_BODY
        assert stream.eof

    def test_read_zero(self):
        stream = self._make_stream(DOC_GZ)
        assert stream.read(0) == b''
        assert stream.read() == DOC_BODY

    def test_readline_limit(self):
        stream = self._make_stream(LINES_GZ)
        assert stream.readline(6) == b'first '
        assert stream.readline(5) == b'line\n'
        assert stream.readline(0) == b''
        assert stream.readline() == b'second line\n'
        assert stream.readline() == b'third (no newline)'
        assert stream.readline() == b''

    def test_readlines_hint(self):
        stream = self._make_stream(LINES_GZ)
        assert stream.readlines(hint=11) == [b'first line\n']
        assert stream.readlines() == [b'second line\n', b'third (no newline)']
        assert stream.readlines() == []

    def test_iteration(self):
        stream = self._make_stream(LINES_GZ)
        assert list(stream) == [
            b'first line\n',
            b'second line\n',
            b'third (no newline)',
        ]

    def test_exhaust(self):
        stream = self._make_stream(DOC_GZ)
        assert stream.read(10) == DOC_BODY[:10]
        stream.exhaust()
        assert stream.eof
        assert stream.read() == b''

    def test_exhaust_corrupt_does_not_raise(self):
        bad = bytearray(DOC_GZ)
        bad[-5] ^= 0xFF
        stream = self._make_stream(bytes(bad))
        stream.exhaust()
        assert stream.eof

    def test_io_interface(self):
        stream = self._make_stream(DOC_GZ)
        assert stream.readable()
        assert not stream.seekable()
        assert not stream.writable()
        with pytest.raises(OSError):
            stream.write(b'nope')


class TestDecompressedStreamASGI:
    async def _make_stream(self, data, **kwargs):
        from falcon.asgi.stream import BoundedStream
        from falcon.asgi.stream import DecompressedStream

        emitter = testing.ASGIRequestEventEmitter(body=data, chunk_size=4096)
        emitter._emit_empty_chunks = False
        stream = BoundedStream(emitter, content_length=len(data))
        return DecompressedStream(stream, **kwargs)

    async def test_readall(self):
        stream = await self._make_stream(DOC_GZ)
        assert await stream.readall() == DOC_BODY
        assert stream.eof
        assert await stream.readall() == b''

    async def test_read_negative_size(self):
        stream = await self._make_stream(DOC_GZ)
        assert await stream.read(-1) == DOC_BODY

    async def test_readline_limit(self):
        stream = await self._make_stream(LINES_GZ)
        assert await stream.readline(6) == b'first '
        assert await stream.readline(0) == b''
        assert await stream.readline() == b'line\n'
        assert await stream.readline() == b'second line\n'
        assert await stream.readline() == b'third (no newline)'
        assert await stream.readline() == b''

    async def test_iteration(self):
        stream = await self._make_stream(DOC_GZ, chunk_size=8)
        chunks = [chunk async for chunk in stream]
        assert b''.join(chunks) == DOC_BODY
        assert all(len(chunk) <= 8 for chunk in chunks)

    async def test_exhaust(self):
        stream = await self._make_stream(DOC_GZ)
        await stream.exhaust()
        assert stream.eof
        assert await stream.read() == b''

    async def test_close(self):
        stream = await self._make_stream(DOC_GZ)
        stream.close()
        assert stream.closed
        stream.close()
        with pytest.raises(ValueError):
            await stream.read()
        with pytest.raises(ValueError):
            await stream.readall()
        with pytest.raises(ValueError):
            await stream.readline()
        with pytest.raises(ValueError):
            await stream.exhaust()
        with pytest.raises(ValueError):
            async for chunk in stream:
                pass

    async def test_io_interface(self):
        stream = await self._make_stream(DOC_GZ)
        assert stream.readable()
        assert not stream.seekable()
        assert not stream.writable()

    async def test_disconnect_mid_body(self):
        emitter = testing.ASGIRequestEventEmitter(body=DOC_GZ, chunk_size=16)
        emitter._emit_empty_chunks = False
        first_event = await emitter.emit()
        emitter.disconnect(exhaust_body=False)

        scope = testing.create_scope(
            method='POST',
            headers={'Content-Encoding': 'gzip'},
            content_length=len(DOC_GZ),
        )
        req = falcon.asgi.Request(scope, emitter, first_event=first_event)

        # NOTE: The client disconnected before the gzip stream
        #   terminated cleanly; this surfaces as a truncated stream.
        with pytest.raises(errors.ContentDecompressionError):
            await req.stream.read()

        # NOTE: Cleanup still works afterwards; buffered data is
        #   released without surfacing further decompression errors.
        await req.stream.exhaust()
        req.stream.close()
        assert req.stream.closed


class TestGzipMultipart:
    BODY = (
        b'--BOUNDARY\r\n'
        b'Content-Disposition: form-data; name="text"\r\n\r\n'
        b'hello world\r\n'
        b'--BOUNDARY--\r\n'
    )

    def test_multipart_form(self, asgi):
        if asgi:
            resource = self._AsyncResource()
        else:
            resource = self._Resource()
        client = _make_client(asgi, resource)
        result = client.simulate_post(
            '/',
            body=gzip.compress(self.BODY),
            headers={
                'Content-Type': 'multipart/form-data; boundary=BOUNDARY',
                'Content-Encoding': 'gzip',
            },
        )
        assert result.status == falcon.HTTP_200
        assert resource.captured_parts == {'text': 'hello world'}

    class _Resource:
        def on_post(self, req, resp):
            form = req.get_media()
            self.captured_parts = {part.name: part.text for part in form}

    class _AsyncResource:
        async def on_post(self, req, resp):
            form = await req.get_media()
            self.captured_parts = {}
            async for part in form:
                self.captured_parts[part.name] = await part.text
