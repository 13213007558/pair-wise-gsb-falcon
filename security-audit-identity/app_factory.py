"""WSGI/ASGI demo apps for the identity-bleed reproduction.

Each app wires the authentication middleware *before* the audit middleware,
so an authentication rejection short-circuits the request stack before the
audit component's ``process_request()`` runs.
"""

from __future__ import annotations

from typing import List
from typing import Optional

import falcon

from secure_middleware import SecureAuditMiddleware
from vulnerable_middleware import AuthMiddleware
from vulnerable_middleware import CallRecorder
from vulnerable_middleware import CleanShortCircuitMiddleware
from vulnerable_middleware import StaleAuditMiddleware


class BusinessError(Exception):
    """Domain error that a registered error handler converts into HTTP 409."""


def _convert_business_error(
    req: falcon.Request, resp: falcon.Response, ex: Exception, params: dict
) -> None:
    resp.status = falcon.HTTP_409
    resp.text = 'business rule violation (converted by error handler)'


async def _convert_business_error_async(
    req: falcon.asgi.Request,
    resp: falcon.asgi.Response,
    ex: Exception,
    params: dict,
) -> None:
    resp.status = falcon.HTTP_409
    resp.text = 'business rule violation (converted by error handler)'


class _ResourceBase:
    def _identity(self, req: falcon.Request) -> str:
        identity = req.context.get('identity')
        assert identity is not None, 'auth middleware must have run'
        return identity


class ProtectedResource(_ResourceBase):
    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        resp.text = f'hello {self._identity(req)}'


class AsyncProtectedResource(_ResourceBase):
    async def on_get(
        self, req: falcon.asgi.Request, resp: falcon.asgi.Response
    ) -> None:
        resp.text = f'hello {self._identity(req)}'


class ConvertedFailureResource(_ResourceBase):
    """Raises a domain error; the app converts it to a normal HTTP response."""

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        raise BusinessError(f'{self._identity(req)} hit a business rule')


class AsyncConvertedFailureResource(_ResourceBase):
    async def on_get(
        self, req: falcon.asgi.Request, resp: falcon.asgi.Response
    ) -> None:
        raise BusinessError(f'{self._identity(req)} hit a business rule')


class RawFailureResource(_ResourceBase):
    """Raises a plain exception with no dedicated handler (generic 500)."""

    def on_get(self, req: falcon.Request, resp: falcon.Response) -> None:
        raise RuntimeError(f'{self._identity(req)} crashed the responder')


class AsyncRawFailureResource(_ResourceBase):
    async def on_get(
        self, req: falcon.asgi.Request, resp: falcon.asgi.Response
    ) -> None:
        raise RuntimeError(f'{self._identity(req)} crashed the responder')


def _middleware(recorder: CallRecorder, secure: bool) -> List[object]:
    audit = (
        SecureAuditMiddleware(recorder)
        if secure
        else StaleAuditMiddleware(recorder)
    )
    # Request-leg order matters: auth first, then a clean (non-exception)
    # short-circuit component, then the audit whose process_request may be
    # skipped by either of the two upstream components.
    return [
        AuthMiddleware(recorder),
        CleanShortCircuitMiddleware(recorder, 'soft'),
        audit,
    ]


def _routes() -> List[tuple]:
    return [
        ('/ok', ProtectedResource, AsyncProtectedResource),
        ('/boom', ConvertedFailureResource, AsyncConvertedFailureResource),
        ('/fail', RawFailureResource, AsyncRawFailureResource),
        ('/deny', ProtectedResource, AsyncProtectedResource),
        ('/soft-deny', ProtectedResource, AsyncProtectedResource),
    ]


def create_app(
    flavor: str = 'wsgi',
    independent_middleware: bool = True,
    secure: bool = False,
    recorder: Optional[CallRecorder] = None,
) -> object:
    """Build a demo app.

    Args:
        flavor: ``'wsgi'`` or ``'asgi'``.
        independent_middleware: Value of the homonymous Falcon option.
        secure: When ``True``, use the request-scoped audit middleware;
            otherwise use the vulnerable instance-state version.
        recorder: Shared call recorder; a fresh one is created when omitted.
    """

    recorder = recorder or CallRecorder()
    middleware = _middleware(recorder, secure)

    if flavor == 'wsgi':
        app = falcon.App(
            middleware=middleware,
            independent_middleware=independent_middleware,
        )
        app.add_error_handler(BusinessError, _convert_business_error)
        for path, sync_resource, _ in _routes():
            app.add_route(path, sync_resource())
    elif flavor == 'asgi':
        app = falcon.asgi.App(
            middleware=middleware,
            independent_middleware=independent_middleware,
        )
        app.add_error_handler(BusinessError, _convert_business_error_async)
        for path, _, async_resource in _routes():
            app.add_route(path, async_resource())
    else:
        raise ValueError(f'unknown flavor: {flavor!r}')

    return app
