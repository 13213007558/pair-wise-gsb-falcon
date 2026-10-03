# Copyright 2024 by Federico Caselli
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
"""Public Falcon type alias definitions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from collections.abc import Iterable
from collections.abc import Mapping
from typing import Optional, Protocol, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from falcon.asgi import SSEvent

__all__ = (
    'Headers',
    'ReadableIO',
    'AsyncReadableIO',
    'SSEEmitter',
    'WebSocketSubprotocolMap',
)

Headers = dict[str, str]
"""Mutable headers dictionary used by :class:`~falcon.Response` objects.

(Note that the :attr:`req.headers <falcon.Request.headers>` property is
annotated as a read-only mapping instead of this type.)

.. versionadded:: 4.0
"""


# WSGI
class ReadableIO(Protocol):
    """File-like protocol that defines only a read method.

    .. versionadded:: 4.0
    """

    def read(self, n: int | None = ..., /) -> bytes: ...


# ASGI
class AsyncReadableIO(Protocol):
    """Async file-like protocol that defines only a read method, and is iterable.

    .. versionadded:: 4.0
    """

    async def read(self, n: int | None = ..., /) -> bytes: ...
    def __aiter__(self) -> AsyncIterator[bytes]: ...


SSEEmitter = AsyncIterator[Optional['SSEvent']]
"""Async generator or iterator over Server-Sent Events
(instances of :class:`falcon.asgi.SSEvent`).

.. versionadded:: 4.0
"""

WebSocketSubprotocolMap = Union[
    Mapping[str, object],
    Iterable[tuple[str, object]],
]
"""Mapping of WebSocket subprotocol names to resources.

Used by :meth:`falcon.asgi.App.add_websocket_route` to register a
WebSocket route that dispatches connections to different resources
according to the negotiated subprotocol. May also be given as an
iterable of ``(name, resource)`` pairs; in either case, the iteration
order defines the server's preference during subprotocol negotiation.

.. versionadded:: 4.4
"""
