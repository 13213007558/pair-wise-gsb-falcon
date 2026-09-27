import pytest

from falcon.request import Request
from falcon.request import RequestOptions
import falcon.testing as testing


def test_remote_addr_default(asgi, util):
    req = util.create_req(asgi)
    assert req.remote_addr == '127.0.0.1'


def test_remote_addr_non_default(asgi, util):
    client_ip = '10.132.0.5'
    req = util.create_req(asgi, remote_addr=client_ip)
    assert req.remote_addr == client_ip


def test_remote_addr_only(asgi, util):
    req = util.create_req(
        asgi,
        host='example.com',
        path='/access_route',
        headers={
            'Forwarded': (
                'for=192.0.2.43, for="[2001:db8:cafe::17]:555",'
                'for="unknown", by=_hidden,for="\\"\\\\",'
                'for="198\\.51\\.100\\.17\\:1236";'
                'proto=https;host=example.com'
            )
        },
    )

    assert req.remote_addr == '127.0.0.1'


def test_rfc_forwarded(asgi, util):
    req = util.create_req(
        asgi,
        host='example.com',
        path='/access_route',
        headers={
            'Forwarded': (
                'for=192.0.2.43,for=,'
                'for="[2001:db8:cafe::17]:555",'
                'for=x,'
                'for="unknown", by=_hidden,for="\\"\\\\",'
                'for="_don\\"t_\\try_this\\\\at_home_\\42",'
                'for="198\\.51\\.100\\.17\\:1236";'
                'proto=https;host=example.com'
            )
        },
    )

    compares = [
        '192.0.2.43',
        '2001:db8:cafe::17',
        'x',
        'unknown',
        '"\\',
        '_don"t_try_this\\at_home_42',
        '198.51.100.17',
        '127.0.0.1',
    ]

    assert req.access_route == compares

    # test cached
    assert req.access_route == compares


def test_malformed_rfc_forwarded(asgi, util):
    req = util.create_req(
        asgi, host='example.com', path='/access_route', headers={'Forwarded': 'for'}
    )

    assert req.access_route == ['127.0.0.1']

    # test cached
    assert req.access_route == ['127.0.0.1']


@pytest.mark.parametrize('include_localhost', [True, False])
def test_x_forwarded_for(asgi, util, include_localhost):
    forwarded_for = '192.0.2.43, 2001:db8:cafe::17,unknown, _hidden, 203.0.113.60'

    if include_localhost:
        forwarded_for += ', 127.0.0.1'

    req = util.create_req(
        asgi,
        host='example.com',
        path='/access_route',
        headers={'X-Forwarded-For': forwarded_for},
    )

    assert req.access_route == [
        '192.0.2.43',
        '2001:db8:cafe::17',
        'unknown',
        '_hidden',
        '203.0.113.60',
        '127.0.0.1',
    ]


def test_x_real_ip(asgi, util):
    req = util.create_req(
        asgi,
        host='example.com',
        path='/access_route',
        headers={'X-Real-IP': '2001:db8:cafe::17'},
    )

    assert req.access_route == ['2001:db8:cafe::17', '127.0.0.1']


@pytest.mark.parametrize('remote_addr', ['10.0.0.1', '98.245.211.177'])
def test_remote_addr(asgi, util, remote_addr):
    req = util.create_req(
        asgi,
        host='example.com',
        path='/access_route',
        remote_addr=remote_addr,
    )

    assert req.access_route == [remote_addr]


def test_remote_addr_missing():
    env = testing.create_environ(host='example.com', path='/access_route')

    # NOTE(kgriffs): It should not be present, but include this check so
    #   that in the future if things change, we still cover this case.
    if 'REMOTE_ADDR' in env:
        del env['REMOTE_ADDR']

    req = Request(env)
    assert req.access_route == ['127.0.0.1']


def _create_trusted_req(asgi, util, trusted_hops, **kwargs):
    options = RequestOptions()
    options.trusted_hops = trusted_hops
    return util.create_req(asgi, options=options, **kwargs)


@pytest.mark.parametrize(
    'trusted_hops, expected_route',
    [
        (1, ['198.51.100.17', '127.0.0.1']),
        (2, ['unknown', '198.51.100.17', '127.0.0.1']),
        (3, ['2001:db8:cafe::17', 'unknown', '198.51.100.17', '127.0.0.1']),
        # The chain is shorter than the number of trusted hops, so no
        #   element can be trusted.
        (5, ['127.0.0.1']),
        (0, ['127.0.0.1']),
    ],
)
def test_forwarded_trusted_hops(asgi, util, trusted_hops, expected_route):
    req = _create_trusted_req(
        asgi,
        util,
        trusted_hops,
        host='example.com',
        path='/access_route',
        headers={
            'Forwarded': (
                'for=192.0.2.43,'
                'for="[2001:db8:cafe::17]:555",'
                'for="unknown",'
                'for="198\\.51\\.100\\.17\\:1236";proto=https'
            )
        },
    )

    assert req.access_route == expected_route


@pytest.mark.parametrize(
    'trusted_hops, expected_route',
    [
        (1, ['203.0.113.60', '127.0.0.1']),
        (2, ['198.51.100.17', '203.0.113.60', '127.0.0.1']),
        (3, ['192.0.2.43', '198.51.100.17', '203.0.113.60', '127.0.0.1']),
        # The chain is shorter than the number of trusted hops, so no
        #   element can be trusted.
        (4, ['127.0.0.1']),
        (0, ['127.0.0.1']),
    ],
)
def test_x_forwarded_for_trusted_hops(asgi, util, trusted_hops, expected_route):
    req = _create_trusted_req(
        asgi,
        util,
        trusted_hops,
        host='example.com',
        path='/access_route',
        headers={'X-Forwarded-For': '192.0.2.43, 198.51.100.17, 203.0.113.60'},
    )

    assert req.access_route == expected_route


def test_x_forwarded_for_trusted_hops_empty_elements(asgi, util):
    req = _create_trusted_req(
        asgi,
        util,
        2,
        host='example.com',
        path='/access_route',
        headers={'X-Forwarded-For': '6.6.6.6,, 198.51.100.17, ,203.0.113.60'},
    )

    # Empty elements are dropped and never counted as trusted hops, and
    #   the untrusted remainder of the chain is excluded.
    assert req.access_route == ['198.51.100.17', '203.0.113.60', '127.0.0.1']
