"""Streaming gzip request-body decompression helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
import io
from typing import Any, BinaryIO, cast
import zlib

from falcon import errors
from falcon.typing import AsyncReadableIO
from falcon.typing import ReadableIO
from falcon.util.reader import BufferedReader

_GZIP_WINDOW_BITS = 16 + zlib.MAX_WBITS
_DECODED_CHUNK_SIZE = 64 * 1024


def is_gzip_content_encoding(value: str | None) -> bool:
    """Return whether a Content-Encoding value denotes one gzip layer."""

    if value is None:
        return False

    gzip_layers = 0
    for token in value.split(','):
        encoding = token.strip().lower()
        if encoding in ('gzip', 'x-gzip'):
            gzip_layers += 1
            continue

        if encoding == 'identity':
            continue

        raise errors.HTTPUnsupportedMediaType(
            description=f'Unsupported Content-Encoding: {token.strip()!r}.'
        )

    if gzip_layers > 1:
        raise errors.HTTPUnsupportedMediaType(
            description='Multiple gzip Content-Encoding layers are not supported.'
        )

    return bool(gzip_layers)


class GzipDecoder:
    """Incrementally decode concatenated gzip members with expansion limits."""

    __slots__ = (
        '_at_end',
        '_compressed_size',
        '_error',
        '_finalized',
        '_max_expansion_bytes',
        '_max_expansion_ratio',
        '_member_input',
        '_output_size',
        '_raw_size',
        '_zlib',
    )

    def __init__(
        self,
        max_expansion_bytes: int,
        max_expansion_ratio: float | int,
        raw_size: int | None = None,
    ) -> None:
        self._max_expansion_bytes = max_expansion_bytes
        self._max_expansion_ratio = max_expansion_ratio
        self._raw_size = raw_size
        self._compressed_size = 0
        self._output_size = 0
        self._member_input = b''
        self._at_end = False
        self._finalized = False
        self._error: Exception | None = None
        self._zlib = self._new_zlib()

    @staticmethod
    def _new_zlib() -> Any:
        return cast(Any, zlib.decompressobj(_GZIP_WINDOW_BITS))

    def _decoding_error(self, ex: Exception) -> errors.GzipDecodingError:
        error = errors.GzipDecodingError(
            description='The request body could not be decompressed as gzip.'
        )
        error.__cause__ = ex
        return error

    def _expansion_limit_error(self) -> errors.GzipExpansionLimitError:
        if self._output_size > self._max_expansion_bytes:
            description = (
                'The decompressed request body exceeds '
                f'{self._max_expansion_bytes} bytes.'
            )
        else:
            description = (
                'The request body exceeds the maximum gzip expansion ratio of '
                f'{self._max_expansion_ratio}:1.'
            )

        return errors.GzipExpansionLimitError(description=description)

    def _check_limits(self) -> None:
        if self._output_size > self._max_expansion_bytes:
            raise self._expansion_limit_error()

        if self._max_expansion_ratio > 0:
            denominator = (
                self._raw_size if self._raw_size is not None else self._compressed_size
            )
            if self._output_size > denominator * self._max_expansion_ratio:
                raise self._expansion_limit_error()

    def _finish_member(self) -> bytes:
        try:
            output = self._zlib.flush()
        except zlib.error as ex:
            raise self._decoding_error(ex)

        if not self._zlib.eof:
            raise errors.GzipDecodingError(
                description='The gzip request body is truncated.'
            )

        unused = self._zlib.unused_data
        self._member_input = unused
        self._at_end = not unused
        self._zlib = self._new_zlib()
        self._output_size += len(output)
        self._check_limits()
        return cast(bytes, output)

    def _finalize(self, output_chunks: list[bytes]) -> None:
        if self._at_end:
            return

        if not self._compressed_size:
            raise errors.GzipDecodingError(
                description='The request body is not gzip data.'
            )

        try:
            output = self._zlib.flush()
        except zlib.error as ex:
            raise self._decoding_error(ex)

        if output:
            output_chunks.append(output)
            self._output_size += len(output)
            self._check_limits()

        if not self._zlib.eof:
            raise errors.GzipDecodingError(
                description='The gzip request body is truncated.'
            )

    def _feed_member(self, member_input: bytes, output_chunks: list[bytes]) -> bytes:
        try:
            output = self._zlib.decompress(member_input)
        except zlib.error as ex:
            raise self._decoding_error(ex)

        output_chunks.append(output)
        self._output_size += len(output)
        self._check_limits()

        if self._zlib.eof:
            output_chunks.append(self._finish_member())
            return self._member_input

        return b''

    def feed(self, raw: bytes, final: bool = False) -> bytes:
        """Decode one compressed chunk, optionally validating the final bytes."""

        if self._error is not None:
            raise self._error

        try:
            if raw:
                self._at_end = False

            if self._member_input:
                raw = self._member_input + raw
                self._member_input = b''

            output_chunks: list[bytes] = []
            self._compressed_size += len(raw)
            member_input = raw

            while member_input:
                member_input = self._feed_member(member_input, output_chunks)
                self._member_input = b''

            if final:
                self._finalize(output_chunks)
                self._finalized = True

            return b''.join(output_chunks)
        except Exception as ex:
            self._error = ex
            raise

    @property
    def finalized(self) -> bool:
        """Return whether all compressed members have been validated."""

        return self._finalized


class SyncGzipStream(io.IOBase):
    """Synchronous file-like object that decompresses a gzip request body."""

    def __init__(
        self,
        stream: ReadableIO,
        max_expansion_bytes: int,
        max_expansion_ratio: float | int,
        raw_size: int | None,
    ) -> None:
        self._raw = stream
        self._decoder = GzipDecoder(max_expansion_bytes, max_expansion_ratio, raw_size)
        self._buffer = BufferedReader(self._read_decoded, max_expansion_bytes + 1)
        self._closed = False
        self._eof = False

    def _read_decoded(self, size: int) -> bytes:
        chunks: list[bytes] = []
        received = 0

        while received < size and not self._eof:
            raw = self._raw.read(1)
            output = self._decoder.feed(raw, final=not raw)
            chunks.append(output)
            received += len(output)
            if not raw:
                self._eof = True

        return b''.join(chunks)

    def readable(self) -> bool:
        """Return ``True`` always."""

        return True

    def seekable(self) -> bool:
        """Return ``False`` always."""

        return False

    def writable(self) -> bool:
        """Return ``False`` always."""

        return False

    def read(self, size: int | None = -1) -> bytes:
        """Read up to `size` decompressed bytes, or all remaining bytes."""

        return self._buffer.read(size)

    def readall(self) -> bytes:
        """Read and return all remaining decompressed bytes."""

        return self.read()

    def readline(self, size: int | None = -1) -> bytes:
        """Read one decompressed line."""

        return self._buffer.readline(-1 if size is None else size)

    def readlines(self, hint: int | None = -1) -> list[bytes]:
        """Read all remaining decompressed lines."""

        return self._buffer.readlines(-1 if hint is None else hint)

    def read_until(
        self, delimiter: bytes, size: int = -1, consume_delimiter: bool = False
    ) -> bytes:
        """Read until `delimiter` using the buffered-reader protocol."""

        return self._buffer.read_until(delimiter, size, consume_delimiter)

    def peek(self, size: int = -1) -> bytes:
        """Return decompressed bytes without consuming them."""

        return self._buffer.peek(size)

    def pipe(self, destination: BinaryIO | None = None) -> None:
        """Read and discard or write all remaining decompressed bytes."""

        self._buffer.pipe(destination)

    def pipe_until(
        self,
        delimiter: bytes,
        destination: BinaryIO | None = None,
        consume_delimiter: bool = False,
    ) -> None:
        """Pipe decompressed bytes until `delimiter`."""

        self._buffer.pipe_until(delimiter, destination, consume_delimiter)

    def delimit(self, delimiter: bytes) -> BufferedReader:
        """Return a buffered reader delimited by `delimiter`."""

        return self._buffer.delimit(delimiter)

    def exhaust(self, chunk_size: int = _DECODED_CHUNK_SIZE) -> None:
        """Consume and validate the complete gzip body."""

        chunk = self.read(chunk_size)
        while chunk:
            chunk = self.read(chunk_size)

    def __iter__(self) -> SyncGzipStream:
        return self

    def __next__(self) -> bytes:
        chunk = self.read(_DECODED_CHUNK_SIZE)
        if not chunk:
            raise StopIteration
        return chunk

    next = __next__

    @property
    def eof(self) -> bool:
        """Return whether the decompressed body has been fully consumed."""

        return self._eof and self._decoder.finalized

    @property
    def closed(self) -> bool:
        """Return whether this stream has been closed."""

        return self._closed

    def close(self) -> None:
        """Discard buffered decompressed data and mark the stream closed."""

        self._closed = True


class AsyncGzipStream:
    """Asynchronous file-like object that decompresses a gzip request body."""

    __slots__ = ('_buffer', '_closed', '_decoder', '_eof', '_raw')

    def __init__(
        self,
        stream: AsyncReadableIO,
        max_expansion_bytes: int,
        max_expansion_ratio: float | int,
        raw_size: int | None,
    ) -> None:
        self._raw = stream
        self._decoder = GzipDecoder(max_expansion_bytes, max_expansion_ratio, raw_size)
        self._buffer = b''
        self._eof = False
        self._closed = False

    async def _fill(self, size: int) -> bytes:
        chunks: list[bytes] = []
        received = 0

        while received < size and not self._eof:
            raw = await self._raw.read(1)
            output = self._decoder.feed(raw, final=not raw)
            chunks.append(output)
            received += len(output)
            if not raw:
                self._eof = True

        return b''.join(chunks)

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iter_content()

    async def _iter_content(self) -> AsyncIterator[bytes]:
        if self._closed:
            raise errors.OperationNotAllowed('This stream is closed.')

        while True:
            chunk = await self.read(_DECODED_CHUNK_SIZE)
            if not chunk:
                return
            yield chunk

    async def read(self, size: int | None = None) -> bytes:
        """Read up to `size` decompressed bytes, or all remaining bytes."""

        if self._closed:
            raise errors.OperationNotAllowed('This stream is closed.')

        if size is None or size == -1:
            output_chunks = []
            if self._buffer:
                output_chunks.append(self._buffer)
                self._buffer = b''

            while not self._eof:
                output_chunks.append(await self._fill(_DECODED_CHUNK_SIZE))

            return b''.join(output_chunks)

        if size <= 0:
            return b''

        target_size = size + 1
        while len(self._buffer) < target_size and not self._eof:
            self._buffer += await self._fill(target_size - len(self._buffer))

        output = self._buffer[:size]
        self._buffer = self._buffer[size:]
        return output

    async def readall(self) -> bytes:
        """Read and return all remaining decompressed bytes."""

        return await self.read()

    async def readline(self, limit: int | None = None) -> bytes:
        """Read one decompressed line."""

        if limit is None or limit < 0:
            limit = 2**63

        chunks: list[bytes] = []
        received = 0

        while received < limit:
            newline = self._buffer.find(b'\n')
            if newline >= 0:
                size = min(newline + 1, limit - received)
                chunks.append(self._buffer[:size])
                self._buffer = self._buffer[size:]
                return b''.join(chunks)

            size = min(len(self._buffer), limit - received)
            if size:
                chunks.append(self._buffer[:size])
                received += size
                self._buffer = self._buffer[size:]

            if self._eof:
                break

            self._buffer = await self._fill(1)

        return b''.join(chunks)

    async def exhaust(self) -> None:
        """Consume and validate the complete gzip body."""

        while await self.read(_DECODED_CHUNK_SIZE):
            pass

    def readable(self) -> bool:
        """Return ``True`` always."""

        return True

    def seekable(self) -> bool:
        """Return ``False`` always."""

        return False

    def writable(self) -> bool:
        """Return ``False`` always."""

        return False

    @property
    def eof(self) -> bool:
        """Return whether the decompressed body has been fully consumed."""

        return not self._buffer and self._eof

    @property
    def closed(self) -> bool:
        """Return whether this stream has been closed."""

        return self._closed

    def close(self) -> None:
        """Discard buffered data and prevent further reads."""

        close = getattr(self._raw, 'close', None)
        if close is not None:
            close()
        self._buffer = b''
        self._eof = True
        self._closed = True
