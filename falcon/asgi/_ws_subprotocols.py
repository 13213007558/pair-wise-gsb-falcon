"""WebSocket subprotocol-based routing support.

This module is private to the framework; the public interface is
:meth:`falcon.asgi.App.add_websocket_route`.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from inspect import iscoroutinefunction
from typing import Any, cast, TYPE_CHECKING

from falcon.errors import HTTPForbidden
from falcon.util.misc import is_python_func
from falcon.util.sync import _should_wrap_non_coroutines
from falcon.util.sync import wrap_sync_to_async

if TYPE_CHECKING:
    from falcon._typing import AsgiResponderWsCallable
    from falcon._typing import Resource
    from falcon.typing import WebSocketSubprotocolMap

__all__ = ('WebSocketSubprotocolDispatcher', 'defines_websocket_responder')


def _ws_responder_name(suffix: str | None) -> str:
    return 'on_websocket' + ('_' + suffix if suffix else '')


def defines_websocket_responder(resource: Resource, suffix: str | None = None) -> bool:
    """Check whether a resource defines a WebSocket responder.

    Args:
        resource: The resource object to inspect.
        suffix (str): Optional responder name suffix, as passed to
            :meth:`~falcon.asgi.App.add_route` (default ``None``).

    Returns:
        bool: ``True`` if the resource implements ``on_websocket()`` (or
        ``on_websocket_{suffix}()`` when a suffix is given).
    """
    return callable(getattr(resource, _ws_responder_name(suffix), None))


class WebSocketSubprotocolDispatcher:
    """Dispatch WebSocket connections to resources by subprotocol.

    This class is used internally to implement
    :meth:`falcon.asgi.App.add_websocket_route`; it is not meant to be
    instantiated directly.

    The dispatcher poses as a route resource so that the router can match
    the URI template as usual. When a WebSocket request arrives, the app
    calls :meth:`~.select` with the subprotocols offered by the client in
    order to obtain the actual resource and responder to use.
    """

    def __init__(
        self,
        subprotocols: WebSocketSubprotocolMap,
        fallback: Resource | None = None,
        suffix: str | None = None,
    ) -> None:
        self._suffix = suffix
        self._ws_responder_name = _ws_responder_name(suffix)
        self._http_resource: Resource | None = None
        self._responders: dict[str, tuple[Resource, AsgiResponderWsCallable]] = {}
        self._fallback: tuple[Resource, AsgiResponderWsCallable] | None = None

        for name, resource in self._normalize(subprotocols):
            if name in self._responders:
                raise ValueError(
                    'The WebSocket subprotocol {!r} is mapped to more than '
                    'one resource for the same route.'.format(name)
                )
            self._responders[name] = (resource, self._resolve_responder(resource))

        if fallback is not None:
            self._fallback = (fallback, self._resolve_responder(fallback))

    def __getattr__(self, name: str) -> Any:
        # __getattr__ is only invoked when normal attribute lookup fails.
        # It is used here to expose a placeholder WebSocket responder to
        # the router, and to delegate any HTTP responders (on_get(),
        # on_post(), etc.) to the HTTP resource, if any, that shares this
        # route.
        ws_responder_name = self.__dict__.get('_ws_responder_name')
        if ws_responder_name is not None and name == ws_responder_name:
            return self._on_websocket_placeholder

        if name.startswith('on_'):
            http_resource = self.__dict__.get('_http_resource')
            if http_resource is not None:
                return getattr(http_resource, name)

        raise AttributeError(
            '{!r} object has no attribute {!r}'.format(type(self).__name__, name)
        )

    @property
    def http_resource(self) -> Resource | None:
        """The HTTP resource sharing this route, if any (or ``None``)."""
        return self._http_resource

    @http_resource.setter
    def http_resource(self, resource: Resource | None) -> None:
        self._http_resource = resource

    @property
    def subprotocols(self) -> tuple[str, ...]:
        """The registered subprotocols, in order of server preference."""
        return tuple(self._responders)

    def select(
        self, client_subprotocols: Iterable[str]
    ) -> tuple[Resource, AsgiResponderWsCallable]:
        """Choose a resource according to the offered subprotocols.

        The first registered subprotocol (in order of server preference)
        that is also offered by the client wins. If no common subprotocol
        is found, the explicitly configured fallback resource is used, if
        any; otherwise the handshake is rejected by raising
        :class:`~falcon.HTTPForbidden`.

        Args:
            client_subprotocols: Subprotocol names advertised by the
                client in the WebSocket handshake.

        Returns:
            tuple: A two-member tuple of the selected resource and its
            WebSocket responder.

        Raises:
            HTTPForbidden: No common subprotocol was found, and no
                fallback resource was configured.
        """
        offered = frozenset(client_subprotocols)

        for name, pair in self._responders.items():
            if name in offered:
                return pair

        if self._fallback is not None:
            return self._fallback

        raise HTTPForbidden(
            description='None of the WebSocket subprotocols offered by the '
            'client are supported by this route.'
        )

    async def _on_websocket_placeholder(self, req: Any, ws: Any, **params: Any) -> None:
        raise RuntimeError(
            'The WebSocket subprotocol dispatcher placeholder responder '
            'was invoked directly; this indicates a bug in the framework.'
        )

    def _resolve_responder(self, resource: Resource) -> AsgiResponderWsCallable:
        try:
            responder = getattr(resource, self._ws_responder_name)
        except AttributeError:
            raise ValueError(
                'The resource {!r} does not implement the required {}() '
                'responder.'.format(resource, self._ws_responder_name)
            ) from None

        if not callable(responder):
            raise ValueError(
                'The {} attribute of the resource {!r} is not callable.'.format(
                    self._ws_responder_name, resource
                )
            )

        if not iscoroutinefunction(responder) and is_python_func(responder):
            if _should_wrap_non_coroutines():
                responder = wrap_sync_to_async(responder)
            else:
                raise TypeError(
                    'The {} responder of the resource {!r} must be a '
                    'non-blocking async coroutine (i.e., defined using '
                    'async def) to avoid blocking the main request '
                    'thread.'.format(self._ws_responder_name, resource)
                )

        return cast('AsgiResponderWsCallable', responder)

    @staticmethod
    def _normalize(
        subprotocols: WebSocketSubprotocolMap,
    ) -> list[tuple[str, Resource]]:
        items: Iterable[tuple[str, Resource]]
        if isinstance(subprotocols, Mapping):
            items = subprotocols.items()
        elif isinstance(subprotocols, Iterable) and not isinstance(subprotocols, str):
            items = subprotocols
        else:
            raise TypeError(
                'WebSocket subprotocols must be specified as a mapping of '
                'subprotocol names to resources, or as an iterable of '
                '(name, resource) pairs.'
            )

        pairs = []
        for item in items:
            try:
                name, resource = item
            except (TypeError, ValueError) as ex:
                raise TypeError(
                    'Each WebSocket subprotocol mapping must be a '
                    '(name, resource) pair; got {!r}.'.format(item)
                ) from ex

            if (
                not isinstance(name, str)
                or not name
                or any(char.isspace() for char in name)
            ):
                raise ValueError(
                    'WebSocket subprotocol names must be non-empty strings '
                    'without whitespace; got {!r}.'.format(name)
                )

            pairs.append((name, resource))

        if not pairs:
            raise ValueError('At least one WebSocket subprotocol must be specified.')

        return pairs
