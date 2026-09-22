# Copyright 2013 by Rackspace Hosting, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""WSGI BoundedStream class."""

from __future__ import annotations

import io
from typing import BinaryIO, Callable, TypeVar, Union

from falcon import errors
from falcon.typing import ReadableIO
from falcon.util import deprecated
from falcon.util.decompression import GzipDecoder

__all__ = ('BoundedStream', 'DecompressedStream')


Result = TypeVar('Result', bound=Union[bytes, list[bytes]])


class BoundedStream(io.IOBase):
    """Wrap *wsgi.input* streams to make them more robust.

    ``socket._fileobject`` and ``io.BufferedReader`` are sometimes used
    to implement *wsgi.input*. However, app developers are often burned
    by the fact that the `read()` method for these objects block
    indefinitely if either no size is passed, or a size greater than
    the request's content length is passed to the method.

    This class normalizes *wsgi.input* behavior between WSGI servers
    by implementing non-blocking behavior for the cases mentioned
    above. The caller is not allowed to read more than the number of
    bytes specified by the Content-Length header in the request.

    Args:
        stream: Instance of ``socket._fileobject`` from
            ``environ['wsgi.input']``
        stream_len: Expected content length of the stream.
    """

    def __init__(self, stream: BinaryIO, stream_len: int) -> None:
        self.stream = stream
        self.stream_len = stream_len

        self._bytes_remaining = self.stream_len

    def __iter__(self) -> BoundedStream:
        return self

    def __next__(self) -> bytes:
        return next(self.stream)

    next = __next__

    def _read(self, size: int | None, target: Callable[[int], Result]) -> Result:
        """Proxy reads to the underlying stream.

        Args:
            size (int): Maximum number of bytes to read. Will be
                coerced, if None or -1, to the number of remaining bytes
                in the stream. Will likewise be coerced if greater than
                the number of remaining bytes, to avoid making a
                blocking call to the wrapped stream.
            target (callable): Once `size` has been fixed up, this function
                will be called to actually do the work.

        Returns:
            bytes: Data read from the stream, as returned by `target`.

        """

        # NOTE(kgriffs): Default to reading all remaining bytes if the
        # size is not specified or is out of bounds. This behaves
        # similarly to the IO streams passed in by non-wsgiref servers.
        if size is None or size == -1 or size > self._bytes_remaining:
            size = self._bytes_remaining

        self._bytes_remaining -= size
        return target(size)

    def readable(self) -> bool:
        """Return ``True`` always."""
        return True

    def seekable(self) -> bool:
        """Return ``False`` always."""
        return False

    def writable(self) -> bool:
        """Return ``False`` always."""
        return False

    def read(self, size: int | None = None) -> bytes:
        """Read from the stream.

        Args:
            size (int): Maximum number of bytes/characters to read.
                Defaults to reading until EOF.

        Returns:
            bytes: Data read from the stream.

        """

        return self._read(size, self.stream.read)

    def readline(self, limit: int | None = None) -> bytes:
        """Read a line from the stream.

        Args:
            limit (int): Maximum number of bytes/characters to read.
                Defaults to reading until EOF.

        Returns:
            bytes: Data read from the stream.

        """

        return self._read(limit, self.stream.readline)

    def readlines(self, hint: int | None = None) -> list[bytes]:
        """Read lines from the stream.

        Args:
            hint (int): Maximum number of bytes/characters to read.
                Defaults to reading until EOF.

        Returns:
            bytes: Data read from the stream.

        """

        return self._read(hint, self.stream.readlines)

    def write(self, data: bytes) -> None:
        """Raise OSError always; writing is not supported."""

        raise OSError('Stream is not writeable')

    def exhaust(self, chunk_size: int = 64 * 1024) -> None:
        """Exhaust the stream.

        This consumes all the data left until the limit is reached.

        Args:
            chunk_size (int): The size for a chunk (default: 64 KB).
                It will read the chunk until the stream is exhausted.
        """
        while True:
            chunk = self.read(chunk_size)
            if not chunk:
                break

    @property
    def eof(self) -> bool:
        """``True`` if there is no more data to read from the stream,
        otherwise ``False``.
        """  # noqa: D205
        return self._bytes_remaining <= 0

    @property
    # NOTE(caselit): Deprecated long ago. Warns since 4.0.
    @deprecated(
        'Use `eof` instead. (This compatibility alias will be removed in Falcon 5.0.)',
        is_property=True,
    )
    def is_exhausted(self) -> bool:
        """Deprecated alias for `eof`."""
        return self.eof


# NOTE(kgriffs): Alias for backwards-compat
Body = BoundedStream


class DecompressedStream(io.IOBase):
    """File-like object that transparently decompresses a gzip request body.

    This class wraps a readable binary stream (normally a
    :class:`BoundedStream` bounding the compressed request body to the
    request's Content-Length) and yields the decompressed bytes, much
    like reading the original, uncompressed body.

    The decompressed length is unknown up front; in particular, the
    request's Content-Length header still refers to the compressed body
    on the wire, and it is not used to truncate the decompressed output.

    To guard against decompression bombs, the amount of decompressed
    data is limited by *max_size* and *max_ratio* (see
    :class:`falcon.util.decompression.GzipDecoder` for the exact
    semantics). When a limit is exceeded,
    :class:`falcon.errors.DecompressedContentTooLarge` is raised, and
    no further data is pulled from the underlying stream. Corrupt,
    truncated, or otherwise invalid gzip data results in
    :class:`falcon.errors.ContentDecompressionError`.

    Args:
        stream: File-like object to read the compressed request body from.

    Keyword Args:
        max_size (int): Maximum number of decompressed bytes to emit
            (default `None`, i.e. no limit).
        max_ratio (float): Maximum allowed ratio of decompressed to
            compressed bytes (default `None`, i.e. no limit).
        chunk_size (int): Maximum number of compressed bytes to pull
            from the underlying stream at a time (default: 16 KiB).
    """

    def __init__(
        self,
        stream: ReadableIO,
        max_size: int | None = None,
        max_ratio: float | None = None,
        chunk_size: int = 16 * 1024,
    ) -> None:
        self._stream = stream
        self._decoder = GzipDecoder(max_size, max_ratio)
        self._chunk_size = chunk_size
        self._buffer = bytearray()
        self._eof = False
        self._decode_error: Exception | None = None

    def __iter__(self) -> DecompressedStream:
        return self

    def __next__(self) -> bytes:
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    next = __next__

    def _fill_more(self) -> None:
        if self._decode_error is not None:
            raise self._decode_error

        chunk = self._stream.read(self._chunk_size)
        try:
            if chunk:
                self._buffer += self._decoder.decompress(chunk)
            else:
                self._eof = True
                self._buffer += self._decoder.flush()
        except (
            errors.ContentDecompressionError,
            errors.DecompressedContentTooLarge,
        ) as ex:
            # NOTE: Make the error sticky so that repeated reads keep
            #   failing without pulling any more data from the
            #   underlying stream.
            self._decode_error = ex
            raise

    def _fill(self, size: int | None) -> None:
        while not self._eof and (size is None or len(self._buffer) < size):
            self._fill_more()

    def readable(self) -> bool:
        """Return `True` always."""
        return True

    def seekable(self) -> bool:
        """Return `False` always."""
        return False

    def writable(self) -> bool:
        """Return `False` always."""
        return False

    def write(self, data: bytes) -> None:
        """Raise OSError always; writing is not supported."""

        raise OSError('Stream is not writeable')

    def read(self, size: int | None = None) -> bytes:
        """Read from the stream.

        Args:
            size (int): Maximum number of (decompressed) bytes to read.
                Defaults to reading until EOF.

        Returns:
            bytes: Decompressed data read from the stream.
        """
        if size is None or size < 0:
            self._fill(None)
            data = bytes(self._buffer)
            self._buffer.clear()
            return data

        self._fill(size)
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def readline(self, limit: int | None = None) -> bytes:
        """Read a line of decompressed data from the stream.

        Args:
            limit (int): Maximum number of bytes to read.
                Defaults to reading until a newline or EOF.

        Returns:
            bytes: Decompressed data read from the stream, including
            the trailing newline character, if any.
        """
        if limit is not None and limit <= 0:
            return b''

        while True:
            end = len(self._buffer) if limit is None else min(len(self._buffer), limit)
            pos = self._buffer.find(b'\n', 0, end)
            if pos >= 0:
                pos += 1
                line = bytes(self._buffer[:pos])
                del self._buffer[:pos]
                return line

            if limit is not None and len(self._buffer) >= limit:
                line = bytes(self._buffer[:limit])
                del self._buffer[:limit]
                return line

            if self._eof:
                line = bytes(self._buffer)
                self._buffer.clear()
                return line

            self._fill_more()

    def readlines(self, hint: int | None = None) -> list[bytes]:
        """Read lines of decompressed data from the stream.

        Args:
            hint (int): Maximum number of bytes to read.
                Defaults to reading until EOF.

        Returns:
            list[bytes]: Lines of decompressed data read from the stream.
        """
        lines = []
        total = 0
        while True:
            line = self.readline()
            if not line:
                break
            lines.append(line)
            total += len(line)
            if hint is not None and hint > 0 and total >= hint:
                break
        return lines

    def exhaust(self, chunk_size: int = 64 * 1024) -> None:
        """Exhaust the stream, discarding any remaining data.

        The remaining compressed data is drained directly from the
        underlying stream without decoding it, so that this method never
        raises a decompression error.

        Args:
            chunk_size (int): The size for a chunk (default: 64 KB).
        """
        self._buffer.clear()
        self._eof = True
        while self._stream.read(chunk_size):
            pass

    @property
    def eof(self) -> bool:
        """`True` if there is no more data to read from the stream,
        otherwise `False`.
        """  # noqa: D205
        return self._eof and not self._buffer
