.. _media:

Media
=====

Falcon allows for easy and customizable internet media type handling. By
default Falcon only enables handlers for JSON and HTML (URL-encoded and
multipart) forms. However, additional handlers can be configured through the
:any:`falcon.RequestOptions` and :any:`falcon.ResponseOptions` objects
specified on your :any:`falcon.App`.

.. note::

    WebSocket media is handled differently from regular HTTP requests. For
    information regarding WebSocket media handlers, please
    see: :ref:`ws_media_handlers` in the WebSocket section.

Usage
-----

Zero configuration is needed if you're creating a JSON API. Simply use
:meth:`~falcon.Request.get_media()` and :attr:`~falcon.Response.media` (WSGI)
, or :meth:`~falcon.asgi.Request.get_media()` and
:attr:`~falcon.asgi.Response.media` (ASGI) to let Falcon
do the heavy lifting for you.

.. tab-set::

    .. tab-item:: WSGI

        .. code:: python

            import falcon


            class EchoResource:
                def on_post(self, req, resp):
                    # Deserialize the request body based on the Content-Type
                    #   header in the request, or the default media type
                    #   when the Content-Type header is generic ('*/*') or
                    #   missing.
                    obj = req.get_media()

                    message = obj.get('message')

                    # The framework will look for a media handler that matches
                    #   the response's Content-Type header, or fall back to the
                    #   default media type (typically JSON) when the app does
                    #   not explicitly set the Content-Type header.
                    resp.media = {'message': message}
                    resp.status = falcon.HTTP_200

    .. tab-item:: ASGI

        .. code:: python

            import falcon


            class EchoResource:
                async def on_post(self, req, resp):
                    # Deserialize the request body. Note that the ASGI version
                    #   of this method must be awaited.
                    obj = await req.get_media()

                    message = obj.get('message')

                    # The framework will look for a media handler that matches
                    #   the response's Content-Type header, or fall back to the
                    #   default media type (typically JSON) when the app does
                    #   not explicitly set the Content-Type header.
                    resp.media = {'message': message}
                    resp.status = falcon.HTTP_200

.. warning::

    Once :meth:`falcon.Request.get_media()` or
    :meth:`falcon.asgi.Request.get_media()` is called on a request, it will
    consume the request's body stream. To avoid unnecessary overhead, Falcon
    will only process request media the first time it is referenced. Subsequent
    interactions will use a cached object.

.. _media_validation:

Validating Media
----------------

Falcon currently only provides a JSON Schema media validator; however,
JSON Schema is very versatile and can be used to validate any deserialized
media type that JSON also supports (i.e. dicts, lists, etc).

.. autofunction:: falcon.media.validators.jsonschema.validate

If JSON Schema does not meet your needs, a custom validator may be
implemented in a similar manner to the one above.

.. _content-type-negotiation:

Content-Type Negotiation
------------------------

Request deserialization and response serialization are independent. The
request body is always decoded according to its ``Content-Type`` header (see
:ref:`media`), and the response representation may be negotiated based on the
request ``Accept`` header.

A resource opts in to response content negotiation by declaring the Internet
media types it is able to produce with the :func:`~falcon.provides`
decorator. When the responder sets :attr:`~falcon.Response.media`, Falcon
selects the best match between the declared types and the request ``Accept``
header before the body is serialized.

.. code:: python

    import falcon

    @falcon.provides('application/json', 'application/msgpack')
    class SensorResource:
        def on_get(self, req: Request, resp: Response) -> None:
            resp.media = {'temperature': 21.5}

Selection follows :meth:`Request.client_prefers <falcon.Request.client_prefers>`
and :func:`falcon.util.mediatypes.best_match`:

* Quality values (``q``) are taken into account.
* More specific media ranges (exact ``type/subtype``) take precedence over
  less specific ones (``type/*`` and ``*/*``).
* A media type explicitly given a quality of zero (``q=0``) is excluded.
* When several types are equally acceptable, the declaration order of the
  arguments to :func:`~falcon.provides` is used as a stable tie-breaker.

Only the media types that have a registered
:ref:`media handler <custom_media_handlers>` are offered during negotiation.
If the client cannot accept any of them, Falcon raises
:class:`~falcon.HTTPNotAcceptable` before attempting to serialize, so that no
representation is rendered only to be discarded.

A missing ``Accept`` header is treated as ``*/*``, in which case the first
declared media type is selected.

Negotiation does not change how the request ``Content-Type`` is handled:
``req.media`` continues to deserialize the request body according to its own
``Content-Type``, independently of the response representation. Error
responses are serialized by the configured error serializer (see
:meth:`~falcon.App.set_error_serializer`) and are not restricted to the
resource's declared types.

Overrides and Special Cases
~~~~~~~~~~~~~~~~~~~~~~~~~~~

* If the responder (or response middleware) explicitly assigns a value to
  :attr:`~falcon.Response.content_type`, the automatic negotiation is skipped
  for that response and the assigned value is preserved as is. Falcon does
  not overwrite, "correct", or reinterpret an explicit content type.
* When Falcon performs the negotiation, it merges ``Accept`` into the
  response ``Vary`` header, preserving any values that were already present
  and avoiding duplicates.
* Negotiation only applies to responses represented via
  :attr:`~falcon.Response.media`. It is skipped for ``text``, ``data`` and
  ``stream`` responses, as well as when there is no body to serialize, e.g.
  for ``204 No Content`` responses.
* ``HEAD`` requests still negotiate the response content type (the response
  headers should describe the representation that a ``GET`` would produce),
  but the negotiation itself never serializes the body. No representation is
  rendered as part of choosing the content type.

.. autofunction:: falcon.provides

See :ref:`content_negotiation_recipe` for a complete example that uses the
built-in JSON handler together with a standard-library ``text/csv`` handler.

For more specialized needs, negotiation may also be implemented from scratch
using middleware, for example by setting
:attr:`~falcon.Response.content_type` from the value of the ``Accept`` header.


Exception Handling
------------------

Version 3 of Falcon updated how the handling of exceptions raised by handlers behaves:

*  Falcon lets the media handler try to deserialized an empty body. For the media types
   that don't allow empty bodies as a valid value, such as ``JSON``, an instance of
   :class:`falcon.MediaNotFoundError` should be raised. By default, this error
   will be rendered as a ``400 Bad Request`` response to the client.
   This exception may be suppressed by passing a value to the ``default_when_empty``
   argument when calling :meth:`Request.get_media`. In this case, this value will
   be returned by the call.
*  If a handler encounters an error while parsing a non-empty body, an instance of
   :class:`falcon.MediaMalformedError` should be raised. The original exception, if any,
   is stored in the ``__cause__`` attribute of the raised instance. By default, this
   error will be rendered as a ``400 Bad Request`` response to the client.

If any exception was raised by the handler while parsing the body, all subsequent invocations
of :meth:`Request.get_media` or :attr:`Request.media` will result in a re-raise of the same
exception, unless the exception was a :class:`falcon.MediaNotFoundError` and a default value
is passed to the ``default_when_empty`` attribute of the current invocation.

External handlers should update their logic to align to the internal Falcon handlers.

.. _custom_media_handlers:

Replacing the Default Handlers
------------------------------

By default, the framework installs :class:`falcon.media.JSONHandler`,
:class:`falcon.media.URLEncodedFormHandler`, and
:class:`falcon.media.MultipartFormHandler` for the ``application/json``,
``application/x-www-form-urlencoded``, and ``multipart/form-data`` media types,
respectively.

When creating your App object you can either add or completely replace all of
the handlers. For example, let's say you want to write an API that sends and
receives `MessagePack <https://msgpack.org/>`_. We can easily do this by telling
our Falcon API that we want a default media type of ``application/msgpack``, and
then creating a new :class:`~falcon.media.Handlers` object to map that media
type to an appropriate handler.

The following example demonstrates how to replace the default handlers. Because
Falcon provides a :class:`~.falcon.media.MessagePackHandler` that is not enabled
by default, we use it in our examples below. However, you can always substitute
a :ref:`custom media handler <custom-media-handler-type>` as needed.

.. code:: python

    import falcon
    from falcon import media


    handlers = media.Handlers({
        falcon.MEDIA_MSGPACK: media.MessagePackHandler(),
    })

    app = falcon.App(media_type=falcon.MEDIA_MSGPACK)

    app.req_options.media_handlers = handlers
    app.resp_options.media_handlers = handlers

Alternatively, you can simply update the existing
:class:`~falcon.media.Handlers` object to retain the default handlers:

.. code-block:: python

    import falcon
    from falcon import media


    extra_handlers = {
        falcon.MEDIA_MSGPACK: media.MessagePackHandler(),
    }

    app = falcon.App()

    app.req_options.media_handlers.update(extra_handlers)
    app.resp_options.media_handlers.update(extra_handlers)

The ``falcon`` module provides a number of constants for common media types.
See also: :ref:`media_type_constants`.

.. _note_json_handler:

.. note::

    The configured :class:`falcon.Response` JSON handler is also used to serialize
    :class:`falcon.HTTPError` and the ``json`` attribute of :class:`falcon.asgi.SSEvent`.
    The JSON handler configured in :class:`falcon.Request` is used by
    :meth:`falcon.Request.get_param_as_json` to deserialize query params.

    Therefore, when implementing a custom handler for the JSON media type, it is required
    that the sync interface methods, meaning
    :meth:`falcon.media.BaseHandler.serialize` and :meth:`falcon.media.BaseHandler.deserialize`,
    are implemented even in ``ASGI`` applications. The default JSON handler,
    :class:`falcon.media.JSONHandler`, already implements the methods required to
    work with both types of applications.


Supported Handler Types
-----------------------

.. autoclass:: falcon.media.JSONHandler
    :no-members:

.. autoclass:: falcon.media.MessagePackHandler
    :no-members:

.. autoclass:: falcon.media.MultipartFormHandler
    :no-members:

.. autoclass:: falcon.media.URLEncodedFormHandler
    :no-members:

.. _custom-media-handler-type:

Custom Handler Type
-------------------

If Falcon doesn't have an Internet media type handler that supports your
use case, you can easily implement your own using the abstract base class
provided by Falcon and documented below.

In general ``WSGI`` applications only use the sync methods, while
``ASGI`` applications only use the async one.
The JSON handled is an exception to this, since it's used also by
other parts of the framework, not only in the media handling.
See the :ref:`note above<note_json_handler>` for more details.

.. autoclass:: falcon.media.BaseHandler
    :members:
    :member-order: bysource

.. tip::
    In order to use your custom media handler in a :ref:`Falcon app <app>`,
    you'll have to add an instance of your class to the app's media handlers
    (specified in :attr:`RequestOptions <falcon.RequestOptions.media_handlers>`
    and :attr:`ResponseOptions<falcon.ResponseOptions.media_handlers>`,
    respectively).

    See also: :ref:`custom_media_handlers`.


Handlers Mapping
----------------

.. autoclass:: falcon.media.Handlers
    :members:


.. _media_type_constants:

Media Type Constants
--------------------

The ``falcon`` module provides a number of constants for
common media type strings, including the following:

.. code:: python

    falcon.MEDIA_JSON
    falcon.MEDIA_MSGPACK
    falcon.MEDIA_MULTIPART
    falcon.MEDIA_URLENCODED
    falcon.MEDIA_CSV
    falcon.MEDIA_PARQUET
    falcon.MEDIA_YAML
    falcon.MEDIA_XML
    falcon.MEDIA_HTML
    falcon.MEDIA_JS
    falcon.MEDIA_TEXT
    falcon.MEDIA_JPEG
    falcon.MEDIA_PNG
    falcon.MEDIA_GIF
