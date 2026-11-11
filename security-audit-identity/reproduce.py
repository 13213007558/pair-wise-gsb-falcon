"""Reproduce the audit identity-bleed and verify the secure variant.

Run directly (uses falcon.testing, no network or open port required)::

    python reproduce.py

For every combination of WSGI/ASGI and ``independent_middleware`` in
{True, False} it drives the same interleaved scenario:

1. ``alice`` succeeds on ``/ok``            (warm the instance attribute).
2. an unauthenticated request hits ``/deny`` (auth rejects in
   ``process_request`` -> exception-based request short-circuit).
3. ``soft`` hits ``/soft-deny`` (a component sets ``resp.complete=True``
   without raising -> clean request short-circuit).
4. ``bob`` triggers a converted error on ``/boom`` (resource-stage failure
   turned into HTTP 409 by a registered error handler).
5. ``alice`` triggers a raw failure on ``/fail`` (unhandled -> 500).
6. ``bob`` hits ``/deny`` with the ``bad`` identity (another short-circuit).
7. ``alice`` succeeds again (control request proving state flips back).

The event trace shows the exact middleware call order, and the audit table
shows which principal each response was attributed to.
"""

from __future__ import annotations

import io
import logging
from typing import List
from typing import Tuple

from falcon import testing

from app_factory import create_app
from vulnerable_middleware import CallRecorder

# The /fail scenarios intentionally raise. WSGI reports them through the
# environ's wsgi.errors stream and ASGI through the 'falcon' logger; silence
# both so the expected tracebacks do not obscure the middleware traces.
logging.getLogger('falcon').disabled = True
_SILENT_ENV = {'wsgi.errors': io.StringIO()}

# (label, path, identity header or None, failure kind)
SCENARIO: List[Tuple[str, str, object, str]] = [
    ('1 alice success', '/ok', 'alice', 'none'),
    (
        '2 anonymous denied (exception short-circuit)',
        '/deny',
        None,
        'short-circuit',
    ),
    (
        '3 soft clean short-circuit (resp.complete)',
        '/soft-deny',
        'soft',
        'clean-short-circuit',
    ),
    (
        '4 bob converted 409',
        '/boom',
        'bob',
        'converted-exception',
    ),
    ('5 alice raw 500', '/fail', 'alice', 'raw-exception'),
    (
        '6 bob bad-identity denied (exception short-circuit)',
        '/deny',
        'bad',
        'short-circuit',
    ),
    ('7 alice success again', '/ok', 'alice', 'none'),
]

# Requests on which no downstream request/resource middleware should run.
SHORT_CIRCUIT_PATHS = ('/deny', '/soft-deny')


def _headers(identity: object) -> dict:
    return {'X-Identity': identity} if identity is not None else {}


def run_case(flavor: str, independent: bool, secure: bool) -> CallRecorder:
    recorder = CallRecorder()
    app = create_app(
        flavor=flavor,
        independent_middleware=independent,
        secure=secure,
        recorder=recorder,
    )
    client = testing.TestClient(app)

    print('=' * 78)
    label = 'SECURE' if secure else 'VULNERABLE'
    print(
        f'{label} | flavor={flavor.upper()} | '
        f'independent_middleware={independent}'
    )
    print('=' * 78)

    for case_label, path, identity, _kind in SCENARIO:
        result = client.simulate_get(
            path, headers=_headers(identity), extras=dict(_SILENT_ENV)
        )
        print(f'\n--- {case_label}: GET {path} -> {result.status}')
        # Events produced by this single request only.
        for event in recorder.events:
            print(f'    {event}')
        recorder.events.clear()

    print('\n  Audit trail (one row per process_response):')
    print(
        '  {:<45} {:<20} {}'.format(
            'path', 'attributed_identity', 'req_succeeded'
        )
    )
    for entry in recorder.audit_entries:
        identity = entry.get('attributed_identity')
        if 'identity_established' in entry:
            shown = identity if entry['identity_established'] else (
                '<not established>'
            )
        else:
            shown = repr(identity)
        print(
            '  {:<45} {:<20} {}'.format(
                entry['path'], str(shown), entry['req_succeeded']
            )
        )
    print()
    return recorder


def _attribution_for_path(recorder: CallRecorder, path: str) -> object:
    """Return how the last audit entry for ``path`` attributed identity."""
    matches = [e for e in recorder.audit_entries if e['path'] == path]
    if not matches:
        return '<no process_response ran>'
    entry = matches[-1]
    if 'identity_established' in entry:
        if entry['identity_established']:
            return entry['attributed_identity']
        return '<not established>'
    return entry['attributed_identity']


def main() -> None:
    findings = []
    for flavor in ('wsgi', 'asgi'):
        for independent in (True, False):
            vulnerable = run_case(flavor, independent, secure=False)
            secure = run_case(flavor, independent, secure=True)

            for index, (
                _label,
                path,
                _identity,
                kind,
            ) in enumerate(SCENARIO):
                if path not in SHORT_CIRCUIT_PATHS:
                    continue
                v_match = next(
                    (e for e in vulnerable.audit_entries
                     if e['path'] == path),
                    None,
                )
                s_match = next(
                    (e for e in secure.audit_entries
                     if e['path'] == path),
                    None,
                )
                findings.append(
                    (
                        flavor,
                        independent,
                        SCENARIO[index][0],
                        kind,
                        _attribution_for_path(vulnerable, path),
                        None if v_match is None else v_match['req_succeeded'],
                        _attribution_for_path(secure, path),
                        None if s_match is None else s_match['req_succeeded'],
                    )
                )

    print('#' * 78)
    print('SUMMARY: attribution of the short-circuited requests')
    print('#' * 78)
    print(
        '{:<5} {:<18} {:<46} {:<22} {:<18} {:<20} {}'.format(
            'flavor',
            'independent',
            'scenario',
            'kind',
            'vuln attribution',
            'secure attribution',
            'succeeded v/s',
        )
    )
    for (
        flavor,
        independent,
        scenario,
        kind,
        vuln_identity,
        vuln_ok,
        secure_identity,
        secure_ok,
    ) in findings:
        print(
            '{:<5} {:<18} {:<46} {:<22} {:<18} {:<20} {}/{}'.format(
                flavor,
                str(independent),
                scenario,
                kind,
                str(vuln_identity),
                str(secure_identity),
                vuln_ok,
                secure_ok,
            )
        )


if __name__ == '__main__':
    main()
