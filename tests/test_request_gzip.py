from __future__ import annotations

import gzip
import io

import pytest

import falcon
from falcon import testing

EXPECTED = b'line one\nline two\n' + bytes(range(256)) + b'\nlast line\n'
GZIPPED = gzip.compress(b'prefix\n', compresslevel=9) + gzip.compress(
    EXPECTED, compresslevel=9
)
MEDIA_EXPECTED = {'ok': True, 'names': ['gzip', 'member']}
MEDIA_GZIPPED = gzip.compress(b'{"ok":true,"names":[', compresslevel=9)
MEDIA_GZIPPED += gzip.compress(b'"gzip","member"]}', compresslevel=9)


def _corrupt_crc(data: bytes) -> bytes:
    corrupted = bytearray(data)
    corrupted[-1] ^= 1
    return bytes(corrupted)


class CountingSyncRaw:
    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)
        self.calls = 0
        self.bytes_read = 0

    def read(self, size: int) -> bytes:
        self.calls += 1
        data = self._buffer.read(size)
        self.bytes_read += len(data)
        return data


class CountingAsyncRaw:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.calls = 0
        self.bytes_read = 0

    async def read(self, size: int | None = None) -> bytes:
        self.calls += 1
        if self._data:
            data = self._data[:size]
            self._data = self._data[len(data) :]
            self.bytes_read += len(data)
            return data
        return b''


class OneByteSyncRaw(CountingSyncRaw):
    def read(self, size: int) -> bytes:
        return super().read(1)


class OneByteAsyncRaw(CountingAsyncRaw):
    async def read(self, size: int | None = None) -> bytes:
        return await super().read(1)


def _options(**kwargs):
    options = falcon.RequestOptions()
    for name, value in kwargs.items():
        setattr(options, name, value)
    return options


def _wsgi_request(data, headers=None, options=None, raw=None):
    env = testing.create_environ(body=data, method='POST', headers=headers or {})
    env['wsgi.input'] = raw or CountingSyncRaw(data)
    return falcon.Request(env, options=options)


async def _asgi_request(data, headers=None, options=None, raw=None):
    request = testing.create_asgi_req(
        body=b'',
        method='POST',
        headers=headers or {},
        content_length=len(data),
        options=options,
    )
    request._stream = raw or CountingAsyncRaw(data)
    return request


async def _request(data, asgi, headers=None, options=None, raw=None):
    if asgi:
        return await _asgi_request(data, headers, options, raw)
    return _wsgi_request(data, headers, options, raw)


async def _read(request, mode, asgi):
    if mode == 'read':
        if asgi:
            return await request.stream.read()
        return request.stream.read()

    if mode == 'readline':
        chunks = []
        while True:
            chunk = (
                await request.stream.readline() if asgi else request.stream.readline()
            )
            chunks.append(chunk)
            if not chunk:
                return b''.join(chunks)

    chunks = []
    if asgi:
        async for chunk in request.stream:
            chunks.append(chunk)
    else:
        for chunk in request.stream:
            chunks.append(chunk)
    return b''.join(chunks)


async def _get_media(request, asgi):
    if asgi:
        return await request.get_media()
    return request.get_media()


@pytest.mark.parametrize('asgi', [False, True])
@pytest.mark.parametrize('mode', ['read', 'readline', 'chunks'])
async def test_gzip_corpus(asgi, mode):
    headers = {'Content-Encoding': 'gzip'}
    request = await _request(GZIPPED, asgi, headers)

    result = await _read(request, mode, asgi)

    assert result == b'prefix\n' + EXPECTED
    assert request.content_length == len(GZIPPED)


def test_wsgi_raw_stream_assignment_remains_supported():
    request = testing.create_req()
    raw = object()
    request._raw_stream = raw
    assert request._raw_stream is raw


@pytest.mark.parametrize('asgi', [False, True])
async def test_gzip_media_corpus(asgi):
    request = await _request(
        MEDIA_GZIPPED,
        asgi,
        {'Content-Type': 'application/json', 'Content-Encoding': 'gzip'},
    )

    result = await _get_media(request, asgi)

    assert result == MEDIA_EXPECTED
    assert request.content_length == len(MEDIA_GZIPPED)


@pytest.mark.parametrize('asgi', [False, True])
async def test_gzip_media_success_is_cached_without_re_reading(asgi):
    raw = OneByteAsyncRaw(MEDIA_GZIPPED) if asgi else OneByteSyncRaw(MEDIA_GZIPPED)
    request = await _request(
        MEDIA_GZIPPED,
        asgi,
        {'Content-Type': 'application/json', 'Content-Encoding': 'gzip'},
        raw=raw,
    )

    assert await _get_media(request, asgi) == MEDIA_EXPECTED
    assert raw.bytes_read == len(MEDIA_GZIPPED)
    assert await _get_media(request, asgi) == MEDIA_EXPECTED
    assert raw.bytes_read == len(MEDIA_GZIPPED)


@pytest.mark.parametrize('asgi', [False, True])
def test_gzip_multipart_media(asgi):
    boundary = 'test-boundary'
    body = (
        f'--{boundary}\r\n'
        'Content-Disposition: form-data; name="message"\r\n'
        '\r\n'
        'hello multipart\r\n'
        f'--{boundary}--\r\n'
    ).encode()

    class SyncFormResource:
        def on_post(self, req, resp):
            form = req.get_media()
            for part in form:
                resp.media = {'message': part.text}

    class AsyncFormResource:
        async def on_post(self, req, resp):
            form = await req.get_media()
            async for part in form:
                resp.media = {'message': await part.get_text()}

    app = falcon.asgi.App() if asgi else falcon.App()
    app.add_route('/', AsyncFormResource() if asgi else SyncFormResource())
    client = testing.TestClient(app)

    result = client.simulate_post(
        '/',
        body=gzip.compress(body),
        headers={
            'Content-Type': f'multipart/form-data; boundary={boundary}',
            'Content-Encoding': 'gzip',
        },
        asgi_chunk_size=1,
    )

    assert result.status_code == 200
    assert result.json == {'message': 'hello multipart'}


@pytest.mark.parametrize('asgi', [False, True])
async def test_gzip_media_error_is_cached_without_re_reading(asgi):
    body = gzip.compress(b'{"ok":true}')[:-1]
    raw = OneByteAsyncRaw(body) if asgi else OneByteSyncRaw(body)
    request = await _request(
        body,
        asgi,
        {'Content-Type': 'application/json', 'Content-Encoding': 'gzip'},
        raw=raw,
    )

    with pytest.raises(falcon.GzipDecodingError):
        await _get_media(request, asgi)
    consumed_after_first_error = raw.bytes_read

    with pytest.raises(falcon.GzipDecodingError):
        await _get_media(request, asgi)

    assert raw.bytes_read == consumed_after_first_error


@pytest.mark.parametrize('asgi', [False, True])
async def test_gzip_one_byte_input(asgi):
    raw = OneByteAsyncRaw(GZIPPED) if asgi else OneByteSyncRaw(GZIPPED)
    request = await _request(GZIPPED, asgi, {'Content-Encoding': 'gzip'}, raw=raw)

    result = await _read(request, 'chunks', asgi)

    assert result == b'prefix\n' + EXPECTED
    assert raw.bytes_read == len(GZIPPED)


@pytest.mark.parametrize('asgi', [False, True])
@pytest.mark.parametrize(
    ('corpus', 'error_type'),
    [
        (b'', falcon.GzipDecodingError),
        (b'not gzip', falcon.GzipDecodingError),
        (gzip.compress(b'valid')[:-1], falcon.GzipDecodingError),
        (gzip.compress(b'valid')[:-8], falcon.GzipDecodingError),
        (gzip.compress(b'valid') + b'\x00', falcon.GzipDecodingError),
    ],
)
async def test_gzip_invalid_corpus(asgi, corpus, error_type):
    request = await _request(corpus, asgi, {'Content-Encoding': 'gzip'})

    with pytest.raises(error_type):
        await _read(request, 'read', asgi)


@pytest.mark.parametrize('asgi', [False, True])
async def test_gzip_bad_crc(asgi):
    request = await _request(
        _corrupt_crc(gzip.compress(b'valid')),
        asgi,
        {'Content-Encoding': 'gzip'},
    )

    with pytest.raises(falcon.GzipDecodingError):
        await _read(request, 'read', asgi)


@pytest.mark.parametrize('asgi', [False, True])
@pytest.mark.parametrize(
    ('encoding', 'status'),
    [
        ('deflate', 415),
        ('gzip, gzip', 415),
        ('br', 415),
        ('identity, gzip', 200),
    ],
)
def test_content_encoding_support(asgi, encoding, status):
    class SyncResource:
        def on_post(self, req, resp):
            resp.data = req.bounded_stream.read()

    class AsyncResource:
        async def on_post(self, req, resp):
            resp.data = await req.stream.read()

    app = falcon.asgi.App() if asgi else falcon.App()
    resource = AsyncResource() if asgi else SyncResource()
    app.add_route('/', resource)
    client = testing.TestClient(app)
    body = gzip.compress(b'valid')

    result = client.simulate_post(
        '/',
        body=body,
        headers={'Content-Encoding': encoding},
        asgi_chunk_size=1,
    )

    assert result.status_code == status
    if status == 200:
        assert result.content == b'valid'


@pytest.mark.parametrize('asgi', [False, True])
async def test_gzip_byte_limit_boundary(asgi):
    data = gzip.compress(b'abc')
    options = _options(max_gzip_decompressed_bytes=2)
    request = await _request(data, asgi, {'Content-Encoding': 'gzip'}, options)

    with pytest.raises(falcon.GzipExpansionLimitError) as info:
        await _read(request, 'read', asgi)

    assert info.value.status == falcon.HTTP_413


@pytest.mark.parametrize('asgi', [False, True])
async def test_gzip_byte_limit_allows_exact_boundary(asgi):
    data = gzip.compress(b'abc')
    options = _options(max_gzip_decompressed_bytes=3)
    request = await _request(data, asgi, {'Content-Encoding': 'gzip'}, options)

    assert await _read(request, 'read', asgi) == b'abc'


@pytest.mark.parametrize('asgi', [False, True])
async def test_gzip_ratio_limit_and_stops_pulling(asgi):
    data = gzip.compress(b'x' * 10000, compresslevel=9)
    options = _options(max_gzip_decompressed_bytes=0, max_gzip_expansion_ratio=10)
    raw = CountingAsyncRaw(data) if asgi else CountingSyncRaw(data)
    request = await _request(data, asgi, {'Content-Encoding': 'gzip'}, options, raw)

    with pytest.raises(falcon.GzipExpansionLimitError):
        await _read(request, 'chunks', asgi)

    assert raw.bytes_read < len(data)


class MediaResource:
    def on_post(self, req, resp):
        first = req.get_media()
        second = req.get_media()
        assert first is second
        resp.media = first


class AsyncMediaResource:
    async def on_post(self, req, resp):
        first = await req.get_media()
        second = await req.get_media()
        assert first is second
        resp.media = first


class InvalidMediaResource:
    def on_post(self, req, resp):
        req.get_media()


class AsyncInvalidMediaResource:
    async def on_post(self, req, resp):
        await req.get_media()


@pytest.mark.parametrize('asgi', [False, True])
def test_gzip_media_cache(asgi):
    app = falcon.asgi.App() if asgi else falcon.App()
    resource = AsyncMediaResource() if asgi else MediaResource()
    app.add_route('/', resource)
    client = testing.TestClient(app)
    body = gzip.compress(b'{"ok":true}')

    result = client.simulate_post(
        '/',
        body=body,
        headers={'Content-Type': 'application/json', 'Content-Encoding': 'gzip'},
        asgi_chunk_size=1,
    )

    assert result.status_code == 200
    assert result.json == {'ok': True}


@pytest.mark.parametrize('asgi', [False, True])
@pytest.mark.parametrize(
    'body',
    [
        gzip.compress(b'{'),
        gzip.compress(b'{}') + b'\x00',
        _corrupt_crc(gzip.compress(b'{}')),
    ],
)
def test_gzip_invalid_media_is_not_cached_as_complete(asgi, body):
    app = falcon.asgi.App() if asgi else falcon.App()
    resource = AsyncInvalidMediaResource() if asgi else InvalidMediaResource()
    app.add_route('/', resource)
    client = testing.TestClient(app)

    result = client.simulate_post(
        '/',
        body=bytes(body),
        headers={'Content-Type': 'application/json', 'Content-Encoding': 'gzip'},
        asgi_chunk_size=1,
    )

    assert result.status_code == 400
