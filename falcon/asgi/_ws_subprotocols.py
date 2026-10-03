"""Helpers for routing WebSocket connections by subprotocol (ASGI)."""

from __future__ import annotations

from collections.abc import Iterable
from inspect import iscoroutinefunction

from falcon._typing import AsgiResponderWsCallable
from falcon._typing import Resource
from falcon.errors import HTTPForbidden
from falcon.routing.compiled import UnacceptableRouteError
from falcon.util.misc import is_python_func
from falcon.util.sync import _should_wrap_non_coroutines
from falcon.util.sync import wrap_sync_to_async

__all__: tuple[str, ...] = ()


def websocket_responder_name(suffix: str | None) -> str:
    """Return the WebSocket responder name for the given route suffix."""
    return 'on_websocket_' + suffix if suffix else 'on_websocket'


class WebSocketSubprotocolMap:
    """Map WebSocket subprotocols to resources for a single URI template.

    This class is used internally by :class:`falcon.asgi.App` to implement
    :meth:`~falcon.asgi.App.add_websocket_route`.
    """

    __slots__ = ('_entries', '_fallback')

    def __init__(self) -> None:
        # Registration order defines the server preference when the
        #   client offers more than one supported subprotocol.
        self._entries: list[tuple[str, Resource, AsgiResponderWsCallable]] = []
        self._fallback: tuple[Resource, AsgiResponderWsCallable] | None = None

    def add(
        self,
        uri_template: str,
        resource: Resource,
        subprotocols: str | Iterable[str] | None,
        suffix: str | None,
    ) -> None:
        """Register a resource for one or more subprotocols.

        Args:
            uri_template (str): The URI template being registered (only used
                in error messages).
            resource: The resource instance to register.
            subprotocols: A single subprotocol name, an iterable of names,
                or `None` to register the resource as the fallback used
                when no advertised subprotocol matches.
            suffix (str): Optional responder name suffix, as accepted by
                :meth:`falcon.asgi.App.add_websocket_route`.

        Raises:
            UnacceptableRouteError: A subprotocol or the fallback was
                already registered for this URI template, or the resource
                does not implement the expected responder.
            TypeError: The responder is not an async coroutine function.
            ValueError: An invalid subprotocol name was given.
        """
        responder = self._require_responder(resource, suffix, uri_template)

        if subprotocols is None:
            if self._fallback is not None:
                raise UnacceptableRouteError(
                    'A fallback WebSocket resource was already registered '
                    'for {}.'.format(uri_template)
                )
            self._fallback = (resource, responder)
            return

        names: tuple[str, ...]
        if isinstance(subprotocols, str):
            names = (subprotocols,)
        else:
            names = tuple(subprotocols)

        if not names:
            raise ValueError(
                'subprotocols may not be empty; pass None instead to '
                'register a fallback resource.'
            )

        existing = {name for name, __, ___ in self._entries}
        for name in names:
            if not isinstance(name, str) or not name:
                raise ValueError(
                    'WebSocket subprotocol names must be non-empty strings.'
                )
            if name in existing:
                raise UnacceptableRouteError(
                    'The WebSocket subprotocol {!r} was already registered '
                    'for {}.'.format(name, uri_template)
                )
            existing.add(name)

        for name in names:
            self._entries.append((name, resource, responder))

    def resolve(
        self, client_subprotocols: tuple[str, ...]
    ) -> tuple[AsgiResponderWsCallable, Resource, str | None]:
        """Select a resource for the subprotocols offered by the client.

        When the client offers more than one supported subprotocol, the
        resource that was registered first (i.e., the one preferred by the
        server) is selected, regardless of the order in which the client
        advertised its candidates.

        Args:
            client_subprotocols (tuple[str, ...]): Subprotocol names
                advertised by the client in the WebSocket handshake.

        Returns:
            tuple: A 3-member tuple consisting of the selected
            `on_websocket()` responder, its resource, and the negotiated
            subprotocol name (or `None` when the fallback resource was
            selected).

        Raises:
            HTTPForbidden: No common subprotocol was found and no fallback
                resource was registered.
        """
        offered = frozenset(client_subprotocols)
        for name, resource, responder in self._entries:
            if name in offered:
                return responder, resource, name

        if self._fallback is not None:
            resource, responder = self._fallback
            return responder, resource, None

        raise HTTPForbidden(
            description='The client did not offer any WebSocket subprotocol '
            'supported by this resource.'
        )

    @staticmethod
    def _require_responder(
        resource: Resource, suffix: str | None, uri_template: str
    ) -> AsgiResponderWsCallable:
        responder_name = websocket_responder_name(suffix)
        responder = getattr(resource, responder_name, None)

        if not callable(responder):
            raise UnacceptableRouteError(
                'The resource does not implement a {}() responder, as '
                'required in order to be added as a WebSocket route for '
                '{}.'.format(responder_name, uri_template)
            )

        # This check mirrors CompiledRouter._require_coroutine_responders().
        if not iscoroutinefunction(responder) and is_python_func(responder):
            if _should_wrap_non_coroutines():
                responder = wrap_sync_to_async(responder)
            else:
                msg = (
                    'The {} responder must be a non-blocking '
                    'async coroutine (i.e., defined using async def) to '
                    'avoid blocking the main request thread.'
                )
                msg = msg.format(responder)
                raise TypeError(msg)

        return responder
