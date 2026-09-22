from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timezone
from functools import partial
import io
import os
from pathlib import Path
import re
from re import Pattern
from typing import Any, ClassVar, IO, TYPE_CHECKING

import falcon
from falcon.typing import ReadableIO
from falcon.util import ETag

if TYPE_CHECKING:
    from falcon import asgi
    from falcon import Request
    from falcon import Response


def _open_file(file_path: str | Path) -> tuple[io.BufferedReader, os.stat_result]:
    """Open a file for a static file request and read file stat.

    Args:
        file_path (Union[str, Path]): Path to the file to open.
    Returns:
        tuple: Tuple of (BufferedReader, stat_result).
    """
    fh: io.BufferedReader | None = None
    try:
        fh = io.open(file_path, 'rb')  # noqa: UP020
        st = os.fstat(fh.fileno())
    except OSError:
        if fh is not None:
            fh.close()
        raise falcon.HTTPNotFound()
    return fh, st


def _invalid_range() -> falcon.HTTPInvalidHeader:
    """Build the standard error for a malformed Range header."""
    return falcon.HTTPInvalidHeader(
        'It must be a range formatted according to RFC 9110.',
        'Range',
        href='https://www.rfc-editor.org/rfc/rfc9110.html',
        href_text='HTTP Range Requests',
    )


def _parse_byte_range_spec(
    spec: str, size: int
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Parse one byte range specification."""
    first, separator, last = spec.partition('-')
    try:
        if not first:
            if not separator or not last:
                raise ValueError()
            suffix_length = int(last)
            if suffix_length <= 0:
                raise ValueError()
            return (
                (max(0, size - suffix_length), size - 1),
                (-suffix_length, -1),
            )

        start = int(first)
        raw_end = -1 if not last else int(last)
    except ValueError as ex:
        raise _invalid_range() from ex

    end = size - 1 if raw_end == -1 else min(raw_end, size - 1)
    return (start, end), (start, raw_end)


def _merge_byte_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping satisfiable byte ranges."""
    ranges.sort()
    merged_ranges: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged_ranges and start <= merged_ranges[-1][1]:
            previous_start, previous_end = merged_ranges[-1]
            merged_ranges[-1] = (previous_start, max(previous_end, end))
        else:
            merged_ranges.append((start, end))
    return merged_ranges


def _parse_byte_ranges(header_value: str, size: int) -> list[tuple[int, int]] | None:
    """Parse and normalize the satisfiable byte ranges from a Range header."""
    unit, separator, range_spec = header_value.partition('=')
    if separator != '=':
        raise _invalid_range()
    if unit != 'bytes':
        return None
    range_spec = range_spec.strip()
    if not range_spec:
        raise _invalid_range()

    if size == 0:
        return None

    range_specs = [spec.strip() for spec in range_spec.split(',')]
    if any(not spec for spec in range_specs):
        raise _invalid_range()

    ranges: list[tuple[int, int]] = []
    raw_ranges: list[tuple[int, int]] = []

    for spec in range_specs:
        range_value, raw_range = _parse_byte_range_spec(spec, size)
        start, end = range_value
        raw_end = raw_range[1]
        if raw_end != -1 and raw_end < start:
            raise _invalid_range()
        raw_ranges.append((start, raw_end))
        if end >= start:
            ranges.append((start, end))

    if not ranges:
        raise falcon.HTTPRangeNotSatisfiable(size)

    if len(raw_ranges) > 1 and any(end == -1 for __, end in raw_ranges):
        raise _invalid_range()

    return _merge_byte_ranges(ranges)


def _set_range(
    fh: io.BufferedReader,
    st: os.stat_result,
    req_range: tuple[int, int] | None,
) -> tuple[ReadableIO, int, tuple[int, int, int] | None, str | None]:
    """Process a file handle for a ranged request."""
    size = st.st_size
    if req_range is None:
        return fh, size, None, None

    start, end = req_range
    if size == 0:
        # NOTE(tipabu): Ignore Range headers for zero-byte files; just serve
        #   the empty body since Content-Range can't be used to express a
        #   zero-byte body.
        return fh, 0, None, None

    if start >= size:
        raise falcon.HTTPRangeNotSatisfiable(size)

    fh.seek(start)
    if end == -1:
        # NOTE(vytas): Wrap in order to prevent sendfile from being used, as
        #   its implementation was found to be buggy in many popular WSGI
        #   servers for open files with a non-zero offset.
        length = size - start
        content_range = (start, size - 1, size)
        return _BoundedFile(fh, length), length, content_range, None

    end = min(end, size - 1)
    length = end - start + 1
    return _BoundedFile(fh, length), length, (start, end, size), None


def _set_ranges(
    fh: io.BufferedReader,
    st: os.stat_result,
    ranges: list[tuple[int, int]],
    content_type: str,
    etag: str,
) -> tuple[ReadableIO, int, tuple[int, int, int] | None, str | None]:
    """Create a single-range or multipart response stream."""
    if len(ranges) == 1:
        return _set_range(fh, st, ranges[0])

    opaque_etag = etag[1:-1] if etag.startswith('"') and etag.endswith('"') else etag
    boundary = f'falcon-{opaque_etag}'
    stream = _MultiRangeFile(fh, ranges, st.st_size, content_type, boundary)
    multipart_type = f'multipart/byteranges; boundary={boundary}'
    return stream, stream.length, None, multipart_type


def _is_not_modified(
    req: falcon.Request, current_etag: ETag, last_modified: datetime
) -> bool:
    """Check whether the requested resource can be served with 304 Not Modified."""

    # NOTE(Cycloctane): RFC 9110 Section 13.1.3: A recipient MUST ignore
    #   If-Modified-Since if the request contains an If-None-Match header
    #   field. See also:
    #   https://www.rfc-editor.org/rfc/rfc9110#section-13.1.3-5
    #   https://www.rfc-editor.org/rfc/rfc9110#section-13.2.2
    if req.if_none_match is not None:
        return (len(req.if_none_match) == 1 and req.if_none_match[0] == '*') or any(
            current_etag == etag for etag in req.if_none_match if etag != '*'
        )

    if req.if_modified_since is not None:
        return last_modified <= req.if_modified_since

    return False


def _if_range_matches(
    req: falcon.Request, current_etag: ETag, last_modified: datetime
) -> bool:
    """Check whether an If-Range validator selects the current representation."""
    if_range = req.if_range
    if if_range is None:
        return True

    if_range = if_range.strip()
    if if_range.startswith(('"', 'W/"', 'w/"')):
        requested_etag = ETag.loads(if_range)
        return current_etag.strong_compare(requested_etag)

    try:
        validator_date = req.get_header_as_datetime('If-Range')
    except falcon.HTTPInvalidHeader:
        return False
    if validator_date is None:
        return False
    return last_modified.replace(microsecond=0) == validator_date


class _BoundedFile:
    """Wrap a file to only allow part of it to be read.

    Args:
        fh: The file object to wrap. Should be opened in binary mode,
            and already seeked to an appropriate position. The object must
            expose a ``.close()`` method.
        length (int): Number of bytes that may be read.
    """

    def __init__(self, fh: IO[bytes], length: int) -> None:
        self.fh = fh
        self.close = fh.close
        self.remaining = length

    def read(self, size: int | None = -1) -> bytes:
        """Read the underlying file object, within the specified bounds."""
        if size is None or size < 0:
            size = self.remaining
        else:
            size = min(size, self.remaining)
        data = self.fh.read(size)
        self.remaining -= len(data)
        return data


class _FileSegment:
    """Read one bounded, lazily seeked segment of a file."""

    def __init__(self, fh: IO[bytes], start: int, length: int) -> None:
        self.fh = fh
        self.start = start
        self.remaining = length
        self._positioned = False

    def read(self, size: int) -> bytes:
        """Read the next chunk from this segment."""
        if not self._positioned:
            self.fh.seek(self.start)
            self._positioned = True

        data = self.fh.read(min(size, self.remaining))
        self.remaining -= len(data)
        return data


class _MultiRangeFile:
    """Stream multiple byte ranges as a multipart/byteranges response."""

    def __init__(
        self,
        fh: IO[bytes],
        ranges: list[tuple[int, int]],
        size: int,
        content_type: str,
        boundary: str,
    ) -> None:
        self.fh = fh
        self.close = fh.close
        self.length = self._calculate_length(ranges, size, content_type, boundary)
        self.remaining = self.length
        self._segments = self._build_segments(fh, ranges, size, content_type, boundary)

    @staticmethod
    def _header(
        range_value: tuple[int, int],
        size: int,
        content_type: str,
        boundary: str,
        first: bool,
    ) -> bytes:
        start, end = range_value
        prefix = '' if first else '\r\n'
        return (
            f'{prefix}--{boundary}\r\n'
            f'Content-Type: {content_type}\r\n'
            f'Content-Range: bytes {start}-{end}/{size}\r\n\r\n'
        ).encode('ascii')

    def _build_segments(
        self,
        fh: IO[bytes],
        ranges: list[tuple[int, int]],
        size: int,
        content_type: str,
        boundary: str,
    ) -> list[tuple[_FileSegment | bytes, int]]:
        segments: list[tuple[_FileSegment | bytes, int]] = []
        for index, range_value in enumerate(ranges):
            header = self._header(range_value, size, content_type, boundary, index == 0)
            segments.append((header, len(header)))
            start, end = range_value
            segments.append((_FileSegment(fh, start, end - start + 1), end - start + 1))
        footer = f'\r\n--{boundary}--\r\n'.encode('ascii')
        segments.append((footer, len(footer)))
        return segments

    def _calculate_length(
        self,
        ranges: list[tuple[int, int]],
        size: int,
        content_type: str,
        boundary: str,
    ) -> int:
        length = len(f'\r\n--{boundary}--\r\n'.encode('ascii'))
        for index, range_value in enumerate(ranges):
            length += len(
                self._header(range_value, size, content_type, boundary, index == 0)
            )
            start, end = range_value
            length += end - start + 1
        return length

    def read(self, size: int | None = -1) -> bytes:
        """Read the next multipart segment, bounded by its declared length."""
        if size is None or size < 0:
            size = self.remaining
        else:
            size = min(size, self.remaining)

        data = b''
        while size > 0:
            while self._segments and self._segments[0][1] == 0:
                self._segments.pop(0)
            source, segment_remaining = self._segments[0]
            read_size = min(size, segment_remaining)
            if isinstance(source, bytes):
                chunk = source[:read_size]
                self._segments[0] = (source[read_size:], segment_remaining - len(chunk))
            else:
                chunk = source.read(read_size)
                self._segments[0] = (source, segment_remaining - len(chunk))

            if not chunk:
                self.fh.close()
                raise OSError('unexpected end of file while reading byte range')

            data += chunk
            size -= len(chunk)
            self.remaining -= len(chunk)

        return data


class StaticRoute:
    """Represent a static route with conditional and range request support.

    Conditional requests take precedence over range processing. A matching
    ``If-None-Match`` value (using the weak comparison function) or a current
    ``If-Modified-Since`` date results in ``304 Not Modified``, even when the
    request also contains ``Range``. When range processing is selected, an
    ``If-Range`` entity-tag must match using the strong comparison function,
    while an HTTP-date is compared at second precision; a failed or invalid
    validator causes the complete representation to be returned with ``200``.

    Satisfiable byte ranges return ``206 Partial Content``. Multiple ranges are
    merged when they overlap and otherwise rendered as
    ``multipart/byteranges``; unsatisfiable syntactically valid ranges return
    ``416 Range Not Satisfiable``. ``HEAD`` responses preserve the status and
    representation metadata that ``GET`` would select, including
    ``Content-Length``, without sending a body. Range offsets apply to the
    selected representation bytes, so responses with ``Content-Encoding`` must
    ensure that their ETag and length describe those encoded bytes.

    Args:
        prefix (str): The path prefix to match for this route. If the
            path in the requested URI starts with this string, the remainder
            of the path will be appended to the source directory to
            determine the file to serve. This is done in a secure manner
            to prevent an attacker from requesting a file outside the
            specified directory.

            Note that static routes are matched in LIFO order, and are only
            attempted after checking dynamic routes and sinks.

        directory (Union[str, pathlib.Path]): The source directory from which to
            serve files. Must be an absolute path.
        downloadable (bool): Set to ``True`` to include a
            Content-Disposition header in the response. The "filename"
            directive is simply set to the name of the requested file.
        fallback_filename (str): Fallback filename used when the requested file
            is not found. Can be a relative path inside the prefix folder or
            any valid absolute path.

            Note:
                If the fallback file is served instead of the requested file,
                the response Content-Type header, as well as the
                Content-Disposition header (provided it was requested with the
                `downloadable` parameter described above), are derived from the
                fallback filename, as opposed to the requested filename.
    """

    # NOTE(kgriffs): Don't allow control characters and reserved chars
    _DISALLOWED_CHARS_PATTERN: ClassVar[Pattern[str]] = re.compile(
        '[\x00-\x1f\x80-\x9f\ufffd~?<>:*|\'"]'
    )

    # NOTE(vytas): Match the behavior of the underlying os.path.normpath.
    _DISALLOWED_NORMALIZED_PREFIXES: ClassVar[tuple[str, ...]] = (
        '..' + os.path.sep,
        os.path.sep,
    )

    # NOTE(kgriffs): If somehow an executable code exploit is triggerable, this
    # minimizes how much can be included in the payload.
    _MAX_NON_PREFIXED_LEN: ClassVar[int] = 512

    def __init__(
        self,
        prefix: str,
        directory: str | Path,
        downloadable: bool = False,
        fallback_filename: str | None = None,
    ) -> None:
        if not prefix.startswith('/'):
            raise ValueError("prefix must start with '/'")

        self._directory = os.path.normpath(directory)
        if not os.path.isabs(self._directory):
            raise ValueError('directory must be an absolute path')

        if fallback_filename is None:
            self._fallback_filename = None
        else:
            self._fallback_filename = os.path.normpath(
                os.path.join(self._directory, fallback_filename)
            )
            if not os.path.isfile(self._fallback_filename):
                raise ValueError('fallback_filename is not a file')

        # NOTE(kgriffs): Ensure it ends with a path separator to ensure
        # we only match on the complete segment. Don't raise an error
        # because most people won't expect to have to append a slash.
        if not prefix.endswith('/'):
            prefix += '/'

        self._prefix = prefix
        self._downloadable = downloadable

    def match(self, path: str) -> bool:
        """Check whether the given path matches this route."""
        if self._fallback_filename is None:
            return path.startswith(self._prefix)
        return path.startswith(self._prefix) or path == self._prefix[:-1]

    def _resolve_file(
        self, req: Request
    ) -> tuple[io.BufferedReader, os.stat_result, str]:
        """Resolve and open the file represented by this static route."""
        without_prefix = req.path[len(self._prefix) :]
        if (
            not (without_prefix or self._fallback_filename is not None)
            or without_prefix.strip().rstrip('.') != without_prefix
            or self._DISALLOWED_CHARS_PATTERN.search(without_prefix)
            or '\\' in without_prefix
            or '//' in without_prefix
            or len(without_prefix) > self._MAX_NON_PREFIXED_LEN
        ):
            raise falcon.HTTPNotFound()

        normalized = os.path.normpath(without_prefix)
        if normalized.startswith(self._DISALLOWED_NORMALIZED_PREFIXES):
            raise falcon.HTTPNotFound()

        file_path = os.path.join(self._directory, normalized)
        if '..' in file_path or not file_path.startswith(self._directory):
            raise falcon.HTTPNotFound()

        if self._fallback_filename is None:
            fh, st = _open_file(file_path)
            return fh, st, file_path

        try:
            fh, st = _open_file(file_path)
        except falcon.HTTPNotFound:
            fh, st = _open_file(self._fallback_filename)
            file_path = self._fallback_filename
        return fh, st, file_path

    def _set_response_stream(
        self,
        req: Request,
        resp: Response,
        fh: io.BufferedReader,
        st: os.stat_result,
        file_path: str,
        content_type: str,
        current_etag: ETag,
        last_modified: datetime,
    ) -> None:
        """Select and configure a complete or ranged response stream."""
        ranges: list[tuple[int, int]] | None = None
        range_header = req.get_header('Range')
        if range_header is not None and _if_range_matches(
            req, current_etag, last_modified
        ):
            ranges = _parse_byte_ranges(range_header, st.st_size)

        try:
            if ranges is None:
                stream, length, content_range, multipart_type = _set_range(fh, st, None)
            else:
                stream, length, content_range, multipart_type = _set_ranges(
                    fh, st, ranges, content_type, current_etag.dumps()
                )
        except OSError:
            fh.close()
            raise falcon.HTTPNotFound()
        except falcon.HTTPError:
            fh.close()
            raise

        resp.set_stream(stream, length)
        resp.content_type = multipart_type or content_type
        if self._downloadable:
            resp.downloadable_as = os.path.basename(file_path)
        if content_range or multipart_type:
            resp.status = falcon.HTTP_206
            if content_range:
                resp.content_range = content_range

    def __call__(self, req: Request, resp: Response, **kw: Any) -> None:
        """Resource responder for this route."""
        assert not kw
        if req.method == 'OPTIONS':
            # it's likely a CORS request. Set the allow header to the appropriate value.
            resp.set_header('Allow', 'GET')
            resp.set_header('Content-Length', '0')
            return

        fh, st, file_path = self._resolve_file(req)

        opaque_etag = f'{int(st.st_mtime):x}-{st.st_size:x}'
        current_etag = ETag(opaque_etag)
        resp.etag = current_etag.dumps()

        last_modified = datetime.fromtimestamp(st.st_mtime, timezone.utc)
        # NOTE(vytas): Strip the microsecond part because that is not reflected
        #   in HTTP date, and when the client passes a previous value via
        #   If-Modified-Since, it will look as if our copy is ostensibly newer.
        last_modified = last_modified.replace(microsecond=0)
        resp.last_modified = last_modified
        resp.accept_ranges = 'bytes'

        if _is_not_modified(req, current_etag, last_modified):
            fh.close()
            resp.status = falcon.HTTP_304
            return

        suffix = os.path.splitext(file_path)[1]
        content_type = resp.options.static_media_types.get(
            suffix, 'application/octet-stream'
        )
        self._set_response_stream(
            req, resp, fh, st, file_path, content_type, current_etag, last_modified
        )


class StaticRouteAsync(StaticRoute):
    """Subclass of StaticRoute with modifications to support ASGI apps."""

    async def __call__(  # type: ignore[override]
        self,
        req: asgi.Request,
        resp: asgi.Response,
        ws: asgi.WebSocket | None = None,
        **kw: Any,
    ) -> None:
        if ws is not None:
            raise falcon.HTTPBadRequest()

        super().__call__(req, resp, **kw)
        if resp.stream is not None:  # None when in an option request
            # NOTE(kgriffs): Fixup resp.stream so that it is non-blocking
            resp.stream = _AsyncFileReader(resp.stream)  # type: ignore[assignment,arg-type]


class _AsyncFileReader:
    """Adapts a standard file I/O object so that reads are non-blocking."""

    def __init__(self, file: IO[bytes]) -> None:
        self._file = file
        self._loop = asyncio.get_running_loop()

    async def read(self, size: int = -1) -> bytes:
        return await self._loop.run_in_executor(None, partial(self._file.read, size))

    async def close(self) -> None:
        await self._loop.run_in_executor(None, self._file.close)
