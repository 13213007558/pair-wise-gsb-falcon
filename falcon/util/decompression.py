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

"""Request body decompression with decompression bomb protection."""

from __future__ import annotations

import zlib

from falcon import errors

__all__ = (
    'DEFAULT_MAX_DECOMPRESSED_BODY_SIZE',
    'DEFAULT_MAX_DECOMPRESSION_RATIO',
    'GzipDecoder',
)

DEFAULT_MAX_DECOMPRESSED_BODY_SIZE = 32 * 1024 * 1024
"""Default maximum size of a decompressed request body (32 MiB)."""

DEFAULT_MAX_DECOMPRESSION_RATIO = 100.0
"""Default maximum ratio of decompressed to compressed request body bytes."""


class GzipDecoder:
    """Incrementally decompress a gzip stream while enforcing limits.

    This decoder wraps :class:`zlib.decompressobj` and accepts the
    compressed request body in arbitrarily-sized chunks via
    :meth:`decompress`. Concatenated gzip members (as produced, for
    example, by appending one gzip file to another) are decoded
    transparently, one after another.

    Decompression bombs are guarded against in two ways: the total
    number of decompressed bytes may be capped via *max_size*, and the
    ratio of decompressed to compressed bytes may be capped via
    *max_ratio*. Both limits are enforced incrementally: as soon as the
    decompressed output exceeds a limit,
    :class:`~falcon.errors.DecompressedContentTooLarge` is raised, and
    the caller can stop pulling data from the underlying stream.

    Corrupt gzip data, a failed CRC check, a truncated stream, and
    trailing garbage bytes all result in
    :class:`~falcon.errors.ContentDecompressionError`.

    Note:
        This class is not thread-safe; it is designed to decode a single
        request body.

    Keyword Args:
        max_size (int): Maximum number of decompressed bytes to emit
            before raising
            :class:`~falcon.errors.DecompressedContentTooLarge`
            (default `None`, i.e. no limit).
        max_ratio (float): Maximum allowed ratio of decompressed to
            compressed bytes before raising
            :class:`~falcon.errors.DecompressedContentTooLarge`
            (default `None`, i.e. no limit).
    """

    def __init__(
        self, max_size: int | None = None, max_ratio: float | None = None
    ) -> None:
        self._max_size = max_size
        self._max_ratio = max_ratio

        self._decompressor = zlib.decompressobj(wbits=31)
        self._compressed_count = 0
        self._decompressed_count = 0
        self._members_completed = 0
        self._member_started = False

    def decompress(self, data: bytes) -> bytes:
        """Decompress the next chunk of compressed data.

        Args:
            data (bytes): The next chunk of the compressed request body.

        Returns:
            bytes: The decompressed output, which may be empty if more
            input is required to make progress.

        Raises:
            ContentDecompressionError: The gzip stream is corrupt.
            DecompressedContentTooLarge: A configured limit was exceeded.
        """
        self._compressed_count += len(data)

        chunks = []
        buf = data
        while buf:
            self._member_started = True
            chunks.append(self._decompress_member(buf))

            if self._decompressor.eof:
                # NOTE: The current gzip member ended; unused_data holds
                #   any bytes past its end, which either form the next
                #   concatenated member, or trailing garbage that the new
                #   decompressor will reject.
                buf = self._decompressor.unused_data
                self._decompressor = zlib.decompressobj(wbits=31)
                self._members_completed += 1
                self._member_started = False
            else:
                # NOTE: Output was capped by max_length; keep consuming
                #   the rest of the input before asking for more.
                buf = self._decompressor.unconsumed_tail

        return b''.join(chunks)

    def flush(self) -> bytes:
        """Finalize decoding once the input stream is exhausted.

        Returns:
            bytes: Any remaining decompressed output.

        Raises:
            ContentDecompressionError: The gzip stream is truncated, or
                no gzip data was received at all.
            DecompressedContentTooLarge: A configured limit was exceeded.
        """
        tail = self._decompressor.flush()
        self._decompressed_count += len(tail)
        self._check_limits()

        if self._member_started:
            # NOTE: The final gzip member did not terminate cleanly
            #   before the input stream was exhausted.
            raise errors.ContentDecompressionError(
                description='The compressed request body is truncated.'
            )

        if not self._members_completed:
            raise errors.ContentDecompressionError(
                description='The compressed request body is empty or truncated.'
            )

        return tail

    def _decompress_member(self, data: bytes) -> bytes:
        max_length = self._get_max_length()
        try:
            chunk = self._decompressor.decompress(data, max_length)
        except zlib.error as ex:
            raise errors.ContentDecompressionError(
                description='The compressed request body is corrupt: {0}.'.format(ex)
            ) from ex

        self._decompressed_count += len(chunk)
        self._check_limits()
        return chunk

    def _get_max_length(self) -> int:
        # NOTE: zlib treats a max_length of 0 as "no limit", so the
        #   allowance is always clamped to at least 1 byte; emitting even
        #   a single byte past the allowance trips the limit check below.
        limit = None
        if self._max_size is not None:
            limit = self._max_size - self._decompressed_count + 1
        if self._max_ratio is not None:
            ratio_limit = (
                int(self._max_ratio * self._compressed_count)
                - self._decompressed_count
                + 1
            )
            limit = ratio_limit if limit is None else min(limit, ratio_limit)
        if limit is None:
            return 0
        return max(limit, 1)

    def _check_limits(self) -> None:
        if self._max_size is not None and self._decompressed_count > self._max_size:
            raise errors.DecompressedContentTooLarge(
                description='The decompressed request body exceeds the maximum '
                'allowed size of {0} bytes.'.format(self._max_size)
            )
        if (
            self._max_ratio is not None
            and self._decompressed_count > self._max_ratio * self._compressed_count
        ):
            raise errors.DecompressedContentTooLarge(
                description='The decompressed request body exceeds the maximum '
                'allowed decompression ratio of {0}.'.format(self._max_ratio)
            )
