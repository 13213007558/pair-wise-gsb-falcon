"""Vulnerable middleware pair used to reproduce cross-request identity bleed.

The audit component here commits the classic mistake of keeping per-request
state on the *middleware instance* (``self.current_identity``) and assuming
its own ``process_request()`` has always run before ``process_response()``.
Falcon does not guarantee that ordering, and the stale instance attribute then
causes a rejected request to be attributed to the principal of the previous
request.

Both sync (WSGI) and ``*_async`` (ASGI) variants are provided so the same
components can be exercised on either flavor.
"""

from __future__ import annotations

from typing import List
from typing import Optional

import falcon


class CallRecorder:
    """Collect ordered middleware/resource events and audit entries."""

    def __init__(self) -> None:
        self.events: List[str] = []
        self.audit_entries: List[dict] = []

    def event(self, message: str) -> None:
        self.events.append(message)

    def audit(self, entry: dict) -> None:
        self.audit_entries.append(entry)

    def reset(self) -> None:
        self.events.clear()
        self.audit_entries.clear()


class AuthMiddleware:
    """Authenticate via the ``X-Identity`` header.

    A request carrying ``X-Identity: bad`` (or no identity at all) is rejected
    from ``process_request()`` before routing ever happens.
    """

    def __init__(self, recorder: CallRecorder) -> None:
        self.recorder = recorder

    def _authenticate(self, req: falcon.Request) -> str:
        identity = req.get_header('X-Identity')
        if not identity or identity == 'bad':
            self.recorder.event(
                f'auth.process_request -> REJECT identity={identity!r}'
            )
            raise falcon.HTTPUnauthorized(
                title='Authentication required',
                description='Missing or invalid X-Identity header.',
            )
        req.context.identity = identity
        req.context.auth_passed = True
        self.recorder.event(
            f'auth.process_request -> accept identity={identity}'
        )
        return identity

    def process_request(
        self, req: falcon.Request, resp: falcon.Response
    ) -> None:
        self._authenticate(req)

    async def process_request_async(
        self, req: falcon.asgi.Request, resp: falcon.asgi.Response
    ) -> None:
        self._authenticate(req)


class CleanShortCircuitMiddleware:
    """Short-circuit without raising, by setting ``resp.complete = True``.

    Unlike an exception in ``process_request()``, this path does not enter
    Falcon's exception handling at all, so it short-circuits the request leg
    under *both* values of ``independent_middleware`` while the response leg
    still runs.
    """

    def __init__(self, recorder: CallRecorder, header_value: str) -> None:
        self.recorder = recorder
        self.header_value = header_value

    def _maybe_short_circuit(
        self, req: falcon.Request, resp: falcon.Response
    ) -> None:
        if req.get_header('X-Identity') == self.header_value:
            self.recorder.event(
                f'shortcircuit.process_request -> resp.complete=True '
                f'identity={self.header_value!r}'
            )
            resp.status = falcon.HTTP_403
            resp.text = 'clean short-circuit'
            resp.complete = True

    def process_request(
        self, req: falcon.Request, resp: falcon.Response
    ) -> None:
        self._maybe_short_circuit(req, resp)

    async def process_request_async(
        self, req: falcon.asgi.Request, resp: falcon.asgi.Response
    ) -> None:
        self._maybe_short_circuit(req, resp)


class StaleAuditMiddleware:
    """Audit middleware that keeps request identity on the instance.

    This is the unsafe pattern under investigation. ``process_request()``
    stores the principal on ``self`` and ``process_response()`` trusts that
    attribute blindly. When ``process_request()`` is skipped (an upstream
    component short-circuited the request stack), the attribute still holds
    the *previous* request's principal.
    """

    def __init__(self, recorder: CallRecorder) -> None:
        self.recorder = recorder
        # Mutable cross-request state on a singleton middleware object.
        self.current_identity: Optional[str] = None

    def _bind(self, req: falcon.Request) -> None:
        identity = req.context.get('identity')
        self.recorder.event(f'audit.process_request -> identity={identity}')
        self.current_identity = identity

    def _write(self, req: falcon.Request, req_succeeded: bool) -> None:
        self.recorder.event(
            f'audit.process_response -> req_succeeded={req_succeeded}, '
            f'read identity={self.current_identity!r}'
        )
        self.recorder.audit(
            {
                'path': req.path,
                'attributed_identity': self.current_identity,
                'req_succeeded': req_succeeded,
            }
        )

    def process_request(
        self, req: falcon.Request, resp: falcon.Response
    ) -> None:
        self._bind(req)

    def process_response(
        self,
        req: falcon.Request,
        resp: falcon.Response,
        resource: Optional[object],
        req_succeeded: bool,
    ) -> None:
        self._write(req, req_succeeded)

    async def process_request_async(
        self, req: falcon.asgi.Request, resp: falcon.asgi.Response
    ) -> None:
        self._bind(req)

    async def process_response_async(
        self,
        req: falcon.asgi.Request,
        resp: falcon.asgi.Response,
        resource: Optional[object],
        req_succeeded: bool,
    ) -> None:
        self._write(req, req_succeeded)
