.. _hooks:

Hooks
=====

Falcon supports *before* and *after* hooks. You install a hook simply by
applying one of the decorators below, either to an individual responder or
to an entire resource.

For example, consider this hook that validates a POST request for
an image resource:

.. code:: python

    def validate_image_type(req, resp, resource, params):
        if req.content_type not in ALLOWED_IMAGE_TYPES:
            msg = 'Image type not allowed. Must be PNG, JPEG, or GIF'
            raise falcon.HTTPBadRequest(title='Bad request', description=msg)

You would attach this hook to an ``on_post`` responder like so:

.. code:: python

    @falcon.before(validate_image_type)
    def on_post(self, req, resp):
        pass

Or, suppose you had a hook that you would like to apply to *all*
responders for a given resource. In that case, you would simply
decorate the resource class:

.. code:: python

    def extract_project_id(req, resp, resource, params):
        params['project_id'] = None
        if req.content_type == falcon.MEDIA_JSON:
            params['project_id'] = req.get_media().get('projectId')

    @falcon.before(extract_project_id)
    class Message:
        def on_post(self, req, resp, project_id):
            pass

        def on_get(self, req, resp, project_id):
            pass

In addition to just running your logic before all responders of this
resource, the ``extract_project_id`` hook also injects the `project_id`
parameter -- see :func:`falcon.before` for a more elaborate explanation.

.. note::
    When decorating an entire resource class, all method names that resemble
    responders, including *suffix*\ed (see also :meth:`~falcon.App.add_route`)
    ones, are decorated. If, for instance, a method is called ``on_get_items``,
    but it is not meant for handling ``GET`` requests under a route with the
    *suffix* ``items``, the easiest workaround for preventing the hook function
    from being applied to the method is renaming it not to clash with the
    responder pattern.

Note also that you can pass additional arguments to your hook function
as needed:

.. code:: python

    def validate_image_type(req, resp, resource, params, allowed_types):
        if req.content_type not in allowed_types:
            msg = 'Image type not allowed.'
            raise falcon.HTTPBadRequest(title='Bad request', description=msg)

    @falcon.before(validate_image_type, ['image/png'])
    def on_post(self, req, resp):
        pass

Falcon supports using any callable as a hook. This allows for using a class
instead of a function:

.. code:: python

    class Authorize:
        def __init__(self, roles):
            self._roles = roles

        def __call__(self, req, resp, resource, params):
            pass

    @falcon.before(Authorize(['admin']))
    def on_post(self, req, resp):
        pass


Falcon :ref:`middleware components <middleware>` can also be used to insert
logic before and after requests. However, unlike hooks,
:ref:`middleware components <middleware>` are triggered **globally** for all
requests.

.. Tip::
    In order to pass data from a hook function to a resource function
    use the ``req.context`` and ``resp.context`` objects. These context objects
    are intended to hold request and response data specific to your app as it
    passes through the framework.

You can also create your own decorators to wrap responder methods without needing to use hooks.

Here's an example of a custom decorator that logs request/response information:

.. code:: python

    def log_access(responder):
        @functools.wraps(responder)
        def wrapper(resource, req, resp, *args, **kwargs) -> None:
            # Call the original responder
            responder(resource, req, resp, *args, **kwargs)

            # Log after the responder has been called
            logging.info(
                f'[{type(resource).__name__}] {req.method} {req.relative_uri}'
                f' => HTTP {resp.status_code}'
            )

        return wrapper

    class HelloResource:
        @log_access
        def on_get(self, req, resp) -> None:
            resp.media = {"Hello": "World"}

In this example, the ``log_access`` decorator will log information about each
request after the responder completes.

.. note::
    The ``@functools.wraps(responder)`` ensures that the wrapper function preserves the original method's name and attributes.

You can also create parameterized decorators:

.. code:: python

    def log_access(level=logging.INFO):
        def decorator(responder):
            @functools.wraps(responder)
            def wrapper(resource, req, resp, *args, **kwargs) -> None:
                responder(resource, req, resp, *args, **kwargs)

                logging.log(
                    level,
                    f'[{type(resource).__name__}] {req.method} {req.relative_uri}'
                    f' => HTTP {resp.status_code}'
                )

            return wrapper

        return decorator

    class HelloResource:
        @log_access(level=logging.DEBUG)
        def on_get(self, req, resp) -> None:
            resp.media = {"Hello": "World"}

.. _hook_execution_order:

Hook Execution Order and Terminal States
----------------------------------------

When multiple hooks are stacked on a single responder, *before* hooks
execute top-down (the outermost decorator first), while *after* hooks
execute in the reverse order, innermost first::

    @falcon.before(trace_request)   # runs first
    @falcon.before(check_auth)      # runs second
    @falcon.after(annotate)         # runs fourth
    @falcon.after(audit)            # runs third
    def on_get(self, req, resp):
        ...

Class-level hooks wrap the responder outside of any method-level hooks,
so a class-level *before* hook runs before method-level ones, and a
class-level *after* hook runs last.

Each request resolves to exactly one terminal state for its hooks:

* If a *before* hook raises, the responder and the *after* hooks are
  skipped, and the exception is passed to the app's
  :ref:`error handlers <errors>`.
* If the responder raises, the *after* hooks are skipped in the same
  manner.
* If an *after* hook raises, any remaining *after* hooks for that
  responder are skipped, and the exception is passed to the error
  handlers.

For streaming responses (i.e., when
:attr:`resp.stream <falcon.Response.stream>` or
:attr:`resp.sse <falcon.asgi.Response.sse>` is set), an *after* hook is
considered complete as soon as the responder returns; it does **not**
wait for the response body to finish streaming, and it is not invoked
again afterwards. By the time the body is streamed, the response status
and headers have already been sent, so an exception raised while
iterating the stream can no longer be converted into an error response;
it propagates to the application server instead.

Similarly, when an ASGI client disconnects in the middle of a streamed
response, the framework does not treat the disconnect as an error: per
the ASGI spec, ``send()`` simply becomes a no-op, and no 500 response
is synthesized.

Before Hooks
------------

.. autofunction:: falcon.before

After Hooks
-----------

.. autofunction:: falcon.after

Configuration of Hooks
----------------------

.. autodata:: falcon.hooks.decorate_on_request
