import logging

import pytest

import falcon


def _options(trusted_proxies):
    options = falcon.RequestOptions()
    options.trusted_proxies = trusted_proxies
    return options


def _create_req(asgi, util, trusted_proxies, **kwargs):
    kwargs.setdefault('host', 'internal.example')
    kwargs.setdefault('path', '/languages')
    return util.create_req(asgi, options=_options(trusted_proxies), **kwargs)


# A three-element chain as received by the app after three proxies, where
# the leftmost element describes the original client and the rightmost
# element was written by the proxy closest to the app.
CHAIN_HEADER = (
    'for=203.0.113.1;proto=https;host=client.example, '
    'for=198.51.100.2;proto=http;host=proxy1.example, '
    'for=192.0.2.3;proto=http'
)

# (trusted_proxies, client_addr, forwarded_scheme, forwarded_host)
CHAIN_EXPECTATIONS = [
    (0, '203.0.113.1', 'https', 'client.example'),
    (1, '192.0.2.3', 'http', 'internal.example'),
    (2, '198.51.100.2', 'http', 'proxy1.example'),
    (3, '203.0.113.1', 'https', 'client.example'),
    (5, '203.0.113.1', 'https', 'client.example'),
]


@pytest.mark.parametrize(
    'trusted_proxies, client_addr, scheme, host', CHAIN_EXPECTATIONS
)
def test_chain_trust_boundary(asgi, util, trusted_proxies, client_addr, scheme, host):
    req = _create_req(
        asgi,
        util,
        trusted_proxies,
        headers={'Forwarded': CHAIN_HEADER},
        remote_addr='10.0.0.2',
    )

    assert req.client_addr == client_addr
    assert req.forwarded_scheme == scheme
    assert req.forwarded_host == host
    assert req.forwarded_uri == scheme + '://' + host + '/languages'

    # The raw parse remains available for inspection, leftmost first.
    assert req.forwarded[0].src == '203.0.113.1'
    assert req.remote_addr == '10.0.0.2'


@pytest.mark.parametrize(
    'trusted_proxies, client_addr',
    [
        (1, '2001:db8::2'),
        (2, '192.0.2.43'),
        (3, '2001:db8:cafe::17'),
    ],
)
def test_ipv6_quoted_chain(asgi, util, trusted_proxies, client_addr):
    req = _create_req(
        asgi,
        util,
        trusted_proxies,
        headers={
            'Forwarded': (
                'for="[2001:db8:cafe::17]:555";proto=https, '
                'for=192.0.2.43, '
                'for="[2001:db8::2]"'
            )
        },
        remote_addr='10.0.0.9',
    )

    assert req.client_addr == client_addr


@pytest.mark.parametrize(
    'trusted_proxies, client_addr, host',
    [
        (1, '198.51.100.77', 'proxy1.example'),
        (2, '_obf"uscated', 'exa"mple.org:8443'),
    ],
)
def test_quoted_and_escaped_values(asgi, util, trusted_proxies, client_addr, host):
    req = _create_req(
        asgi,
        util,
        trusted_proxies,
        headers={
            'Forwarded': (
                'for="_obf\\"uscated";host="exa\\"mple.org:8443";proto=https, '
                'for=198.51.100.77;host=proxy1.example'
            )
        },
        remote_addr='10.0.0.4',
    )

    assert req.client_addr == client_addr
    assert req.forwarded_host == host

    assert req.client_addr == client_addr
    assert req.forwarded_host == host


@pytest.mark.parametrize(
    'trusted_proxies, client_addr, scheme, host',
    [
        (1, '192.0.2.20', 'http', 'internal.example'),
        (2, '198.51.100.10', 'https', 'trusted.example'),
    ],
)
def test_forwarded_takes_precedence_over_x_forwarded(
    asgi, util, trusted_proxies, client_addr, scheme, host
):
    req = _create_req(
        asgi,
        util,
        trusted_proxies,
        headers={
            'Forwarded': (
                'for=198.51.100.10;proto=https;host=trusted.example, '
                'for=192.0.2.20;proto=http'
            ),
            'X-Forwarded-For': '6.6.6.6, 198.51.100.10',
            'X-Forwarded-Proto': 'http',
            'X-Forwarded-Host': 'evil.example',
        },
        remote_addr='10.0.0.5',
    )

    assert req.client_addr == client_addr
    assert req.forwarded_scheme == scheme
    assert req.forwarded_host == host
    assert '6.6.6.6' not in req.client_addr
    assert 'evil.example' not in req.forwarded_host


@pytest.mark.parametrize(
    'trusted_proxies, client_addr',
    [
        (0, '203.0.113.5'),
        (1, '192.0.2.7'),
        (2, '198.51.100.6'),
        (3, '203.0.113.5'),
        (7, '203.0.113.5'),
    ],
)
def test_x_forwarded_for_chain(asgi, util, trusted_proxies, client_addr):
    req = _create_req(
        asgi,
        util,
        trusted_proxies,
        headers={'X-Forwarded-For': '203.0.113.5, 198.51.100.6, 192.0.2.7'},
        remote_addr='10.0.0.3',
    )

    assert req.client_addr == client_addr


@pytest.mark.parametrize(
    'trusted_proxies, scheme',
    [
        (1, 'http'),
        (2, 'https'),
    ],
)
def test_tls_termination_at_trusted_proxy(asgi, util, trusted_proxies, scheme):
    # TLS is terminated by the outermost trusted proxy; the app itself
    # only sees plain HTTP from the inner proxy.
    req = _create_req(
        asgi,
        util,
        trusted_proxies,
        scheme='http',
        headers={
            'Forwarded': (
                'for=203.0.113.9;proto=https;host=secure.example, '
                'for=192.0.2.2;proto=http'
            )
        },
        remote_addr='10.0.0.6',
    )

    assert req.scheme == 'http'
    assert req.forwarded_scheme == scheme
    if scheme == 'https':
        assert req.forwarded_uri == 'https://secure.example/languages'


def test_x_forwarded_proto_single_value(asgi, util):
    # X-Forwarded-Proto is treated as a single value; TLS termination
    # signaled this way keeps working when trusted proxies are set.
    req = _create_req(
        asgi,
        util,
        2,
        scheme='http',
        headers={'X-Forwarded-Proto': 'HTTPS'},
    )

    assert req.forwarded_scheme == 'https'

    assert req.forwarded_scheme == 'https'


@pytest.mark.parametrize(
    'headers',
    [
        {
            'Forwarded': 'for=6.6.6.6;proto=https;host=evil.example, '
            'for=203.0.113.66;proto=http;host=proxy-view.example'
        },
        {'X-Forwarded-For': '6.6.6.6, 203.0.113.66'},
    ],
    ids=['forwarded', 'x-forwarded-for'],
)
def test_malicious_injection_does_not_rewrite_client_info(asgi, util, headers):
    # The attacker, connecting through a single trusted proxy, prepends a
    # forged element; the trusted proxy appends the real client address.
    req = _create_req(asgi, util, 1, headers=headers, remote_addr='10.0.0.1')

    assert req.client_addr == '203.0.113.66'
    assert '6.6.6.6' not in req.client_addr

    if 'Forwarded' in headers:
        assert req.forwarded_scheme == 'http'
        assert req.forwarded_host == 'proxy-view.example'
        assert 'evil.example' not in req.forwarded_uri
        assert req.forwarded_uri == 'http://proxy-view.example/languages'

        # The forged element is still parsed (and remains available via
        # the raw API), but it is never used to derive client info.
        assert req.forwarded[0].host == 'evil.example'


def test_malformed_and_empty_elements_fail_closed(asgi, util):
    req = _create_req(
        asgi,
        util,
        1,
        headers={
            'Forwarded': (
                ',,, for=198.51.100.7;;, garbage!@#value, for=, '
                'for=192.0.2.8;proto=https'
            )
        },
        remote_addr='10.0.0.7',
    )

    # Empty elements and garbage produce no forwarded-elements at all,
    # so they cannot shift the trust boundary.
    assert len(req.forwarded) == 2
    assert req.client_addr == '192.0.2.8'
    assert req.forwarded_scheme == 'https'


def test_oversized_forwarded_header_fails_closed(asgi, util, caplog):
    header_value = 'for=' + '9' * 9000 + ';proto=https;host=evil.example'

    with caplog.at_level(logging.WARNING, logger='falcon'):
        req = _create_req(
            asgi,
            util,
            1,
            headers={'Forwarded': header_value},
            remote_addr='10.0.0.8',
        )

        assert req.forwarded == []
        assert req.client_addr == '10.0.0.8'
        assert req.access_route == ['10.0.0.8']
        assert req.forwarded_scheme == 'http'
        assert req.forwarded_host == 'internal.example'

    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warning_records) == 1
    # The untrusted header value must not be leaked into the logs.
    assert header_value not in caplog.text
    assert 'evil.example' not in caplog.text

    assert 'evil.example' not in caplog.text


def test_duplicate_forwarded_headers_merged(asgi, util):
    # WSGI servers fold duplicate Forwarded fields into a single
    # comma-joined value; the resulting chain is resolved as usual.
    req = _create_req(
        asgi,
        util,
        1,
        headers={'Forwarded': 'for=203.0.113.1;proto=https,for=192.0.2.2;proto=http'},
        remote_addr='10.0.0.10',
    )

    assert len(req.forwarded) == 2
    assert req.client_addr == '192.0.2.2'
    assert req.forwarded_scheme == 'http'


def test_duplicate_forwarded_headers_asgi(util):
    # ASGI servers may pass duplicate fields through; Falcon folds them
    # exactly like a WSGI server would.
    req = util.create_req(
        True,
        options=_options(1),
        host='internal.example',
        path='/languages',
        headers=[
            ('Forwarded', 'for=203.0.113.1;proto=https'),
            ('Forwarded', 'for=192.0.2.2;proto=http'),
        ],
        remote_addr='10.0.0.10',
    )

    assert len(req.forwarded) == 2
    assert req.client_addr == '192.0.2.2'
    assert req.forwarded_scheme == 'http'


def test_no_forwarded_headers_with_trusted_proxies(asgi, util):
    req = _create_req(asgi, util, 2, remote_addr='10.0.0.11')

    assert req.forwarded is None
    assert req.client_addr == '10.0.0.11'
    assert req.forwarded_scheme == 'http'
    assert req.forwarded_host == 'internal.example'


def test_x_real_ip_with_trusted_proxies(asgi, util):
    req = _create_req(
        asgi,
        util,
        1,
        headers={'X-Real-IP': '203.0.113.21'},
        remote_addr='10.0.0.12',
    )

    assert req.client_addr == '203.0.113.21'


def test_untrusted_values_not_leaked_to_logs(asgi, util, caplog):
    req = _create_req(
        asgi,
        util,
        1,
        headers={
            'Forwarded': (
                'for=6.6.6.6;proto=https;host=evil.example, '
                'for=203.0.113.66;proto=http;host=proxy-view.example'
            )
        },
        remote_addr='10.0.0.1',
    )

    logger = logging.getLogger('myapp.request')
    with caplog.at_level(logging.INFO, logger='myapp.request'):
        logger.info(
            'client=%s scheme=%s host=%s',
            req.client_addr,
            req.forwarded_scheme,
            req.forwarded_host,
        )

    assert '203.0.113.66' in caplog.text
    assert 'proxy-view.example' in caplog.text
    assert '6.6.6.6' not in caplog.text
    assert 'evil.example' not in caplog.text
