from __future__ import annotations

import gzip
import io
import zlib

import pytest

import falcon
from falcon.util import gzip as gzip_util

DATA = gzip.compress(b'ab\ncd\n') + gzip.compress(b'ef')


class ClosingAsyncRaw:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_is_gzip_content_encoding():
    assert gzip_util.is_gzip_content_encoding(None) is False
    assert gzip_util.is_gzip_content_encoding('GZIP') is True
    assert gzip_util.is_gzip_content_encoding('x-gzip') is True
    assert gzip_util.is_gzip_content_encoding('identity, gzip') is True

    with pytest.raises(falcon.HTTPUnsupportedMediaType):
        gzip_util.is_gzip_content_encoding('deflate')

    with pytest.raises(falcon.HTTPUnsupportedMediaType):
        gzip_util.is_gzip_content_encoding('gzip, gzip')


def test_request_options_validation():
    options = falcon.RequestOptions()

    with pytest.raises(ValueError):
        options.max_gzip_decompressed_bytes = -1

    with pytest.raises(ValueError):
        options.max_gzip_expansion_ratio = -1

    with pytest.raises(ValueError):
        options.max_gzip_expansion_ratio = True


def test_decoder_byte_limit():
    decoder = gzip_util.GzipDecoder(1, 0, len(DATA))
    with pytest.raises(falcon.GzipExpansionLimitError) as info:
        decoder.feed(DATA, final=True)
    assert info.value.status == falcon.HTTP_413


def test_gzip_error_defaults():
    assert falcon.GzipDecodingError().status == falcon.HTTP_400
    assert falcon.GzipExpansionLimitError().status == falcon.HTTP_413


def test_decoder_ratio_limit_and_cached_error():
    bomb = gzip.compress(b'x' * 10_000, compresslevel=9)
    decoder = gzip_util.GzipDecoder(2**31, 1, len(bomb))
    with pytest.raises(falcon.GzipExpansionLimitError):
        decoder.feed(bomb)
    with pytest.raises(falcon.GzipExpansionLimitError):
        decoder.feed(b'x')


def test_decoder_rejects_truncation_and_junk():
    with pytest.raises(falcon.GzipDecodingError):
        gzip_util.GzipDecoder(2**31, 0, 0).feed(b'', final=True)

    with pytest.raises(falcon.GzipDecodingError):
        gzip_util.GzipDecoder(2**31, 0, len(DATA) - 1).feed(DATA[:-1], final=True)

    with pytest.raises(falcon.GzipDecodingError):
        gzip_util.GzipDecoder(2**31, 0, len(DATA) + 1).feed(DATA + b'x', final=True)


def test_decoder_error_and_limit_branches():
    def decoder_with_zlib(zlib_state):
        decoder = gzip_util.GzipDecoder(2**31, 0, 1)
        decoder._compressed_size = 1
        decoder._zlib = zlib_state
        return decoder

    decoder = gzip_util.GzipDecoder(2**31, 0, len(DATA))
    decoder._zlib = object()
    with pytest.raises(AttributeError):
        decoder.feed(DATA, final=True)

    truncated_member = gzip.compress(b'ab')[:-9]
    decoder = gzip_util.GzipDecoder(2**31, 0, len(truncated_member))
    decoder.feed(truncated_member)
    with pytest.raises(falcon.GzipDecodingError):
        decoder.feed(b'', final=True)

    decoder = gzip_util.GzipDecoder(2**31, 0, len(DATA))
    decoder.feed(DATA, final=True)
    assert decoder.finalized is True
    with pytest.raises(falcon.GzipDecodingError):
        decoder.feed(b'x', final=True)

    decoder = gzip_util.GzipDecoder(2**31, 0, len(DATA))
    decoder.feed(DATA, final=True)
    decoder._member_input = b'abc'
    with pytest.raises(falcon.GzipDecodingError):
        decoder.feed(b'd')

    class FailingFinish:
        eof = True
        unused_data = b''

        def decompress(self, data):
            return b''

        def flush(self):
            raise zlib.error('boom')

    class TruncatedFinish(FailingFinish):
        eof = False

    class TruncatedAfterFinish:
        eof = True
        unused_data = b''

        def decompress(self, data):
            self.eof = True
            return b''

        def flush(self):
            self.eof = False
            return b''

    class FailingFinalize:
        eof = False

        def flush(self):
            raise zlib.error('boom')

    class OversizedFinalize:
        eof = True

        def flush(self):
            return b'x'

    class OutputtingFinalize(OversizedFinalize):
        pass

    with pytest.raises(falcon.GzipDecodingError):
        decoder_with_zlib(FailingFinish()).feed(b'x')

    with pytest.raises(falcon.GzipDecodingError):
        decoder_with_zlib(TruncatedFinish()).feed(b'', final=True)

    with pytest.raises(falcon.GzipDecodingError):
        decoder_with_zlib(TruncatedAfterFinish()).feed(b'x')

    with pytest.raises(falcon.GzipDecodingError):
        decoder_with_zlib(FailingFinalize()).feed(b'', final=True)

    decoder = decoder_with_zlib(OversizedFinalize())
    decoder._output_size = 2**31
    with pytest.raises(falcon.GzipExpansionLimitError):
        decoder.feed(b'', final=True)

    decoder = decoder_with_zlib(OutputtingFinalize())
    assert decoder.feed(b'', final=True) == b'x'

    trailer_limit = gzip.compress(b'ab')
    decoder = gzip_util.GzipDecoder(2**31, 0, len(trailer_limit))
    decoder._output_size = 2**31 - 1
    with pytest.raises(falcon.GzipExpansionLimitError):
        decoder.feed(trailer_limit, final=True)


def test_sync_stream_interfaces():
    stream = gzip_util.SyncGzipStream(io.BytesIO(DATA), 2**31, 0, len(DATA))

    assert stream.readable() is True
    assert stream.seekable() is False
    assert stream.writable() is False
    assert stream.read(3) == b'ab\n'
    assert stream.readall() == b'cd\nef'
    assert stream.eof is True
    assert stream.read(1) == b''
    stream.close()
    assert stream.closed is True
    assert stream.read(0) == b''

    stream = gzip_util.SyncGzipStream(io.BytesIO(DATA), 2**31, 0, len(DATA))
    assert stream.read(2**63) == b'ab\ncd\nef'


def test_sync_stream_line_and_buffered_interfaces():
    stream = gzip_util.SyncGzipStream(io.BytesIO(DATA), 2**31, 0, len(DATA))

    assert stream.readline(3) == b'ab\n'
    assert stream.read_until(b'\n') == b'cd'
    stream.read(1)
    assert stream.peek(1) == b'e'
    delimited = stream.delimit(b'f')
    assert delimited.read() == b'e'

    destination = io.BytesIO()
    stream.pipe(destination)
    assert destination.getvalue() == b'f'
    stream.pipe_until(b'\n', destination)
    assert list(stream) == []
    with pytest.raises(StopIteration):
        next(stream)
    assert stream.readlines() == []
    stream.exhaust()


def test_sync_stream_iteration():
    stream = gzip_util.SyncGzipStream(io.BytesIO(DATA), 2**31, 0, len(DATA))
    assert next(stream) == b'ab\ncd\nef'
    stream.exhaust()

    large_data = gzip.compress(b'x' * 70_000)
    stream = gzip_util.SyncGzipStream(io.BytesIO(large_data), 2**31, 0, len(large_data))
    stream.exhaust(chunk_size=4096)


async def test_async_stream_interfaces():
    class Raw:
        def __init__(self):
            self._data = DATA

        async def read(self, size=-1):
            data = self._data[:size]
            self._data = self._data[len(data) :]
            return data

    raw = Raw()
    stream = gzip_util.AsyncGzipStream(raw, 2**31, 0, len(DATA))

    assert stream.readable() is True
    assert stream.seekable() is False
    assert stream.writable() is False
    assert await stream.read(0) == b''
    assert await stream.readline(3) == b'ab\n'
    assert await stream.readall() == b'cd\nef'
    assert stream.eof is True
    assert await stream.readline(0) == b''
    assert [chunk async for chunk in stream] == []
    await stream.exhaust()


async def test_async_stream_partial_read_and_readline():
    class Raw:
        def __init__(self):
            self._data = DATA

        async def read(self, size=-1):
            data = self._data[:size]
            self._data = self._data[len(data) :]
            return data

    stream = gzip_util.AsyncGzipStream(Raw(), 2**31, 0, len(DATA))
    assert await stream.read(2) == b'ab'
    assert await stream.read() == b'\ncd\nef'

    stream = gzip_util.AsyncGzipStream(Raw(), 2**31, 0, len(DATA))
    assert await stream.readline(1) == b'a'
    assert await stream.readline() == b'b\n'
    assert await stream.readline(100) == b'cd\n'
    await stream.exhaust()


async def test_async_stream_close_and_iteration():
    class Raw(ClosingAsyncRaw):
        async def read(self, size=-1):
            return b''

    raw = Raw()
    stream = gzip_util.AsyncGzipStream(raw, 2**31, 0, 0)
    stream.close()

    assert raw.closed is True
    assert stream.closed is True
    with pytest.raises(falcon.OperationNotAllowed):
        await stream.read(1)

    iterator = stream.__aiter__()
    with pytest.raises(falcon.OperationNotAllowed):
        await iterator.__anext__()
