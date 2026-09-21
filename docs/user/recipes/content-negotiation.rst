.. _content_negotiation_recipe:

Content Negotiation of Responses
=================================

When a resource is able to provide the same data in several Internet media
types, decorate it with :func:`falcon.provides` to opt in to proactive
content negotiation based on the request ``Accept`` header. Falcon picks the
best declared type, taking into account the quality values (``q``), the
specificity of media ranges versus wildcards, ``q=0`` exclusions, and the
declaration order for equally acceptable types. If no declared type is
acceptable to the client, a ``406 Not Acceptable`` response is returned.

The following example negotiates between the built-in JSON handler and a
small ``text/csv`` handler that only relies on the Python standard library.
The CSV handler is only registered for responses, which leaves request
``Content-Type`` handling unaffected.

.. literalinclude:: ../../../examples/recipes/content_negotiation.py
    :language: python

Requesting ``/sensors`` with ``Accept: application/json`` yields the JSON
representation, while ``Accept: text/csv`` yields the CSV one. Sending no
``Accept`` header (equivalent to ``*/*``) selects the first declared type,
``application/json``. When the negotiation takes place, Falcon merges
``Vary: Accept`` into the response headers.

If a responder explicitly sets
:attr:`resp.content_type <falcon.Response.content_type>`, that value takes
precedence and is sent as is, without triggering negotiation for that
response.

See :ref:`content-type-negotiation` for the full reference.
