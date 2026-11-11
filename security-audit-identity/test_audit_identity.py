"""Assert the identity-bleed behavior and the secure variant's guarantees.

Run from this directory (or the repository root) with::

    pytest test_audit_identity.py -v
"""

from __future__ import annotations

import io
import logging

import pytest
from falcon import testing

from app_factory import create_app
from vulnerable_middleware import CallRecorder

logging.getLogger('falcon').disabled = True

FLAVORS = ('wsgi', 'asgi')
MODES = (True, False)


def _drive(app, recorder) -> CallRecorder:
    client = testing.TestClient(app)
    # Route WSGI error output away from the test stream; the /fail request
    # raises intentionally.
    silent_errors = io.StringIO()
    client.simulate_get(
        '/ok',
        headers={'X-Identity': 'alice'},
        extras={'wsgi.errors': silent_errors},
    )
    client.simulate_get('/deny', extras={'wsgi.errors': silent_errors})
    client.simulate_get(
        '/soft-deny',
        headers={'X-Identity': 'soft'},
        extras={'wsgi.errors': silent_errors},
    )
    client.simulate_get(
        '/boom',
        headers={'X-Identity': 'bob'},
        extras={'wsgi.errors': silent_errors},
    )
    client.simulate_get(
        '/fail',
        headers={'X-Identity': 'alice'},
        extras={'wsgi.errors': silent_errors},
    )
    return recorder


def _build(flavor, independent, secure):
    recorder = CallRecorder()
    app = create_app(
        flavor=flavor,
        independent_middleware=independent,
        secure=secure,
        recorder=recorder,
    )
    return app, recorder


@pytest.mark.parametrize('flavor', FLAVORS)
@pytest.mark.parametrize('independent', MODES)
def test_vulnerable_bleds_previous_identity_on_exception_short_circuit(
    flavor, independent
):
    app, recorder = _build(flavor, independent, secure=False)
    _drive(app, recorder)
    deny_entries = [e for e in recorder.audit_entries if e['path'] == '/deny']

    if independent:
        # process_response still runs and reads the stale instance attribute.
        assert len(deny_entries) == 1
        assert deny_entries[0]['attributed_identity'] == 'alice'
        assert deny_entries[0]['req_succeeded'] is False
    else:
        # Dependent mode skips the response leg entirely: no audit row at all,
        # which means the denial is silently absent from the audit trail.
        assert deny_entries == []


@pytest.mark.parametrize('flavor', FLAVORS)
@pytest.mark.parametrize('independent', MODES)
def test_vulnerable_bleds_on_clean_short_circuit_even_when_dependent(
    flavor, independent
):
    app, recorder = _build(flavor, independent, secure=False)
    _drive(app, recorder)
    soft_entries = [
        e for e in recorder.audit_entries if e['path'] == '/soft-deny'
    ]

    # No exception was raised, so the response leg runs under both modes,
    # and the stale attribute bled regardless of independent_middleware.
    assert len(soft_entries) == 1
    assert soft_entries[0]['attributed_identity'] == 'alice'
    # req_succeeded is True even though the resource never ran.
    assert soft_entries[0]['req_succeeded'] is True


@pytest.mark.parametrize('flavor', FLAVORS)
@pytest.mark.parametrize('independent', MODES)
def test_secure_never_attributes_another_requests_identity(flavor, independent):
    app, recorder = _build(flavor, independent, secure=True)
    _drive(app, recorder)

    for entry in recorder.audit_entries:
        if entry['path'] in ('/deny', '/soft-deny'):
            assert entry['identity_established'] is False
            assert entry['attributed_identity'] is None
        else:
            assert entry['identity_established'] is True
            assert entry['attributed_identity'] in ('alice', 'bob')

    # Successful and failed resource requests keep their own identity.
    boom = next(e for e in recorder.audit_entries if e['path'] == '/boom')
    fail = next(e for e in recorder.audit_entries if e['path'] == '/fail')
    assert boom['attributed_identity'] == 'bob'
    assert fail['attributed_identity'] == 'alice'


@pytest.mark.parametrize('flavor', FLAVORS)
@pytest.mark.parametrize('independent', MODES)
def test_req_succeeded_semantics(flavor, independent):
    app, recorder = _build(flavor, independent, secure=True)
    _drive(app, recorder)
    by_path = {e['path']: e for e in recorder.audit_entries}

    # Exceptions (including ones converted to 409 by a handler) read False...
    assert by_path['/boom']['req_succeeded'] is False
    assert by_path['/fail']['req_succeeded'] is False
    # ...while the clean resp.complete short-circuit reads True even though
    # the resource was never reached and no identity was established.
    assert by_path['/soft-deny']['req_succeeded'] is True
    assert by_path['/soft-deny']['identity_established'] is False
    assert by_path['/ok']['req_succeeded'] is True
