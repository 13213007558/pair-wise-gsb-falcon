"""Secure audit middleware that never trusts cross-request state.

The fix has two parts:

1. Per-request identity is stored on the :class:`falcon.Request` context,
   which Falcon creates fresh for every request, instead of on the
   middleware singleton. Middleware instances live for the whole app
   lifetime and are shared by every concurrent request.
2. An explicit *not established* marker distinguishes "no identity was
   established for this request" from a real principal. The marker is a
   sentinel object, not an empty string, so the audit trail cannot quietly
   relabel an anonymous/denied request as a user named ``''``.

``process_response()`` therefore only attributes an entry when the identity
was established during *this* request's own request leg; otherwise it
records the entry as unauthenticated, keeping the denial auditable.
"""

from __future__ import annotations

from typing import Optional

import falcon

from vulnerable_middleware import CallRecorder

# Sentinel: the identity slot exists, but no principal was established for
# the current request. Do not replace this with '' or None-as-empty-string.
_NO_IDENTITY = object()


class SecureAuditMiddleware:
    """Audit component bound to request-scoped state."""

    def __init__(self, recorder: CallRecorder) -> None:
        self.recorder = recorder

    def _bind(self, req: falcon.Request) -> None:
        # Seed the audit slot on every request. An identity established by an
        # upstream authentication component is taken as-is; otherwise the
        # slot is explicitly marked as not established, rather than silently
        # defaulting to '' or None-as-empty-string.
        identity = req.context.get('identity', _NO_IDENTITY)
        req.context.audit_identity = identity
        self.recorder.event(
            'audit.process_request -> identity='
            + (
                str(identity)
                if identity is not _NO_IDENTITY
                else '<not established>'
            )
        )

    def _write(self, req: falcon.Request, req_succeeded: bool) -> None:
        identity = req.context.get('audit_identity', _NO_IDENTITY)
        established = identity is not _NO_IDENTITY

        entry = {
            'path': req.path,
            'identity_established': established,
            'attributed_identity': identity if established else None,
            'req_succeeded': req_succeeded,
        }
        self.recorder.event(
            'audit.process_response -> req_succeeded='
            f'{req_succeeded}, identity='
            + (str(identity) if established else '<not established>')
        )
        self.recorder.audit(entry)

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
