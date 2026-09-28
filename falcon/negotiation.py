"""Response representation negotiation based on the Accept header."""

from __future__ import annotations

from typing import Callable, TYPE_CHECKING, TypeVar

from falcon.errors import HTTPNotAcceptable

if TYPE_CHECKING:
    from falcon.request import Request
    from falcon.response import Response

_PROVIDES_ATTR = '_falcon_provides'

_ResourceT = TypeVar('_ResourceT')


def provides(*media_types: str) -> Callable[[_ResourceT], _ResourceT]:
    """Decorate a resource with the media types its responses may use.

    The decorated resource opts in to proactive content negotiation for
    responses represented via the :attr:`~falcon.Response.media` attribute.
    Before the response body is serialized, Falcon selects the best match
    between the declared media types and the request's ``Accept`` header,
    taking into account quality values (``q``), media-range specificity,
    and explicit ``q=0`` exclusions. Equally acceptable types are resolved
    in the declaration order.

    If none of the declared media types is acceptable to the client, or no
    :ref:`media handler <custom_media_handlers>` is registered for any of
    the acceptable types, an instance of :class:`~falcon.HTTPNotAcceptable`
    is raised before serialization is attempted.

    A responder (or response middleware) that explicitly assigns a value to
    :attr:`~falcon.Response.content_type` opts out of the automatic
    negotiation for that response; the assigned value is preserved as is.

    When negotiation takes place, the ``Accept`` field name is merged into
    the response's ``Vary`` header, preserving any pre-existing values.

    A missing ``Accept`` header is equivalent to ``*/*``, in which case the
    first declared media type is selected.

    Request deserialization (based on the ``Content-Type`` request header)
    is not affected by this declaration.

    Args:
        media_types (str): One or more Internet media types, in the order of
            server preference for the case of equally acceptable types.

    .. versionadded:: 4.4
    """

    def decorate(resource: _ResourceT) -> _ResourceT:
        if not media_types:
            raise TypeError('at least one media type must be provided')

        for media_type in media_types:
            if not isinstance(media_type, str) or not media_type.strip():
                raise TypeError('media types must be non-empty strings')

        setattr(resource, _PROVIDES_ATTR, tuple(media_types))
        return resource

    return decorate


def negotiate_response(req: Request, resp: Response, resource: object) -> None:
    """Select the response media type declared by the resource, if any.

    Negotiation is only attempted when the resource has been decorated with
    :func:`provides`, the responder has not explicitly set
    :attr:`~falcon.Response.content_type`, and there is a
    :attr:`~falcon.Response.media` object to serialize. The selection is
    made purely from headers, so HEAD requests and bodiless response
    statuses do not trigger serialization.

    Args:
        req: Instance of ``falcon.Request``.
        resp: Instance of ``falcon.Response``.
        resource: The routed resource object.

    Raises:
        HTTPNotAcceptable: If no declared media type with a registered
            handler is acceptable to the client.
    """

    media_types = getattr(resource, _PROVIDES_ATTR, None)
    if not media_types:
        return

    # NOTE: An explicit content_type overrides the automatic negotiation, and
    #   must not be silently rewritten.
    if resp.content_type is not None:
        return

    # NOTE: There is no media representation to negotiate for text/data/
    #   stream responses, empty bodies, or statuses such as 204.
    if resp._media is None:
        return

    handlers = resp.options.media_handlers

    # NOTE: Only types with a registered handler can actually be produced, so
    #   that 406 is raised up front instead of attempting a failing
    #   serialization and then switching to another format.
    candidates = [
        media_type
        for media_type in media_types
        if handlers._resolve(
            media_type, resp.options.default_media_type, raise_not_found=False
        )[0]
        is not None
    ]

    preferred = req.client_prefers(candidates)
    if preferred is None:
        raise HTTPNotAcceptable(
            description=(
                'None of the supported media types ({0}) is acceptable per '
                'the request Accept header.'.format(', '.join(media_types))
            )
        )

    resp.content_type = preferred

    # NOTE: Merge Vary: Accept while preserving existing values and avoiding
    #   duplicates.
    vary = resp.vary
    if vary is None:
        resp.vary = ('Accept',)
    elif not any(token.strip().lower() == 'accept' for token in str(vary).split(',')):
        resp.append_header('Vary', 'Accept')
