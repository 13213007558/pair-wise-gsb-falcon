import pytest

import falcon


def test_no_forwarded_headers(asgi, util):
    req = util.create_req(
        asgi, host='example.com', path='/languages', root_path='backoffice'
    )

    assert req.forwarded is None
    assert req.forwarded_uri == req.uri
    assert req.forwarded_uri == 'http://example.com/backoffice/languages'
    assert req.forwarded_prefix == 'http://example.com/backoffice'


def test_no_forwarded_headers_with_port(asgi, util):
    req = util.create_req(
        asgi, host='example.com', port=8000, path='/languages', root_path='backoffice'
    )

    assert req.forwarded is None
    assert req.forwarded_uri == req.uri
    assert req.forwarded_uri == 'http://example.com:8000/backoffice/languages'
    assert req.forwarded_prefix == 'http://example.com:8000/backoffice'


def test_x_forwarded_host(asgi, util):
    req = util.create_req(
        asgi,
        host='suchproxy.suchtesting.com',
        path='/languages',
        headers={'X-Forwarded-Host': 'something.org'},
    )

    assert req.forwarded is None
    assert req.forwarded_host == 'something.org'
    assert req.forwarded_uri != req.uri
    assert req.forwarded_uri == 'http://something.org/languages'
    assert req.forwarded_prefix == 'http://something.org'
    assert req.forwarded_prefix == 'http://something.org'  # Check cached value


def test_x_forwarded_host_with_port(asgi, util):
    req = util.create_req(
        asgi,
        host='suchproxy.suchtesting.com',
        path='/languages',
        headers={'X-Forwarded-Host': 'something.org:8000'},
    )

    assert req.forwarded is None
    assert req.forwarded_host == 'something.org:8000'
    assert req.forwarded_uri != req.uri
    assert req.forwarded_uri == 'http://something.org:8000/languages'
    assert req.forwarded_prefix == 'http://something.org:8000'
    assert req.forwarded_prefix == 'http://something.org:8000'  # Check cached value


def test_x_forwarded_proto(asgi, util):
    req = util.create_req(
        asgi,
        host='example.org',
        path='/languages',
        headers={'X-Forwarded-Proto': 'HTTPS'},
    )

    assert req.forwarded is None
    assert req.forwarded_scheme == 'https'
    assert req.forwarded_uri != req.uri
    assert req.forwarded_uri == 'https://example.org/languages'
    assert req.forwarded_prefix == 'https://example.org'


def test_forwarded_host(asgi, util):
    req = util.create_req(
        asgi,
        host='suchproxy02.suchtesting.com',
        path='/languages',
        headers={'Forwarded': 'host=something.org , host=suchproxy01.suchtesting.com'},
    )

    assert req.forwarded is not None
    for f in req.forwarded:
        assert f.src is None
        assert f.dest is None
        assert f.scheme is None

    assert req.forwarded[0].host == 'something.org'
    assert req.forwarded[1].host == 'suchproxy01.suchtesting.com'

    assert req.forwarded_host == 'something.org'
    assert req.forwarded_scheme == 'http'
    assert req.forwarded_uri != req.uri
    assert req.forwarded_uri == 'http://something.org/languages'
    assert req.forwarded_prefix == 'http://something.org'


def test_forwarded_invalid(asgi, util):
    req = util.create_req(
        asgi,
        host='suchproxy02.suchtesting.com',
        path='/languages',
        headers={'Forwarded': 'invalid'},
    )

    assert req.forwarded == []

    assert req.forwarded_host == 'suchproxy02.suchtesting.com'
    assert req.forwarded_scheme == 'http'
    assert req.forwarded_uri == req.uri
    assert req.forwarded_prefix == 'http://suchproxy02.suchtesting.com'


def test_forwarded_multiple_params(asgi, util):
    req = util.create_req(
        asgi,
        host='suchproxy02.suchtesting.com',
        path='/languages',
        headers={
            'Forwarded': (
                'host=something.org;proto=hTTps;ignore=me;for=108.166.30.185, '
                'by=203.0.113.43;host=suchproxy01.suchtesting.com;proto=httP'
            )
        },
    )

    assert req.forwarded is not None

    assert req.forwarded[0].host == 'something.org'
    assert req.forwarded[0].scheme == 'https'
    assert req.forwarded[0].src == '108.166.30.185'
    assert req.forwarded[0].dest is None

    assert req.forwarded[1].host == 'suchproxy01.suchtesting.com'
    assert req.forwarded[1].scheme == 'http'
    assert req.forwarded[1].src is None
    assert req.forwarded[1].dest == '203.0.113.43'

    assert req.forwarded_scheme == 'https'
    assert req.forwarded_host == 'something.org'
    assert req.forwarded_uri != req.uri
    assert req.forwarded_uri == 'https://something.org/languages'
    assert req.forwarded_prefix == 'https://something.org'


def test_forwarded_missing_first_hop_host(asgi, util):
    req = util.create_req(
        asgi,
        host='suchproxy02.suchtesting.com',
        path='/languages',
        root_path='doge',
        headers={'Forwarded': 'for=108.166.30.185,host=suchproxy01.suchtesting.com'},
    )

    assert req.forwarded[0].host is None
    assert req.forwarded[0].src == '108.166.30.185'

    assert req.forwarded[1].host == 'suchproxy01.suchtesting.com'
    assert req.forwarded[1].src is None

    assert req.forwarded_scheme == 'http'
    assert req.forwarded_host == 'suchproxy02.suchtesting.com'
    assert req.forwarded_uri == req.uri
    assert req.forwarded_uri == 'http://suchproxy02.suchtesting.com/doge/languages'
    assert req.forwarded_prefix == 'http://suchproxy02.suchtesting.com/doge'


def test_forwarded_quote_escaping(asgi, util):
    req = util.create_req(
        asgi,
        host='suchproxy02.suchtesting.com',
        path='/languages',
        root_path='doge',
        headers={'Forwarded': 'for="1\\.2\\.3\\.4";some="extra,\\"info\\""'},
    )

    assert req.forwarded[0].host is None
    assert req.forwarded[0].src == '1.2.3.4'


@pytest.mark.parametrize(
    'forwarded, expected_dest',
    [
        ('for=1.2.3.4;by="', None),
        ('for=1.2.3.4;by=4\\.3.2.1thing=blah', '4'),
        ('for=1.2.3.4;by="\\4.3.2.1"thing=blah', '4.3.2.1'),
        ('for=1.2.3.4;by="4.3.2.\\1"thing="blah"', '4.3.2.1'),
        ('for=1.2.3.4;by="4.3.\\2\\.1" thing="blah"', '4.3.2.1'),
    ],
)
def test_escape_malformed_requests(forwarded, expected_dest, asgi, util):
    req = util.create_req(
        asgi,
        host='suchproxy02.suchtesting.com',
        path='/languages',
        root_path='doge',
        headers={'Forwarded': forwarded},
    )

    assert len(req.forwarded) == 1
    assert req.forwarded[0].src == '1.2.3.4'
    assert req.forwarded[0].dest == expected_dest


# ------------------------------------------------------------------------
# Trusted proxy hops
# ------------------------------------------------------------------------

CHAIN_THREE_HOPS = (
    'for=203.0.2.43;proto=https;host=client.example.com, '
    'for=198.51.100.17;proto=http;host=proxy1.example.com, '
    'for=192.0.2.60;proto=http;host=proxy2.example.com'
)


def _create_trusted_req(
    asgi, util, trusted_hops=None, max_forwarded_size=None, **kwargs
):
    options = falcon.RequestOptions()
    options.trusted_hops = trusted_hops
    options.max_forwarded_size = max_forwarded_size
    return util.create_req(asgi, options=options, **kwargs)


def test_untrusted_by_default(asgi, util):
    # By default (no trusted hops configured), the historical behavior
    #   is preserved: the leftmost element is used.
    req = util.create_req(
        asgi,
        host='app.example.com',
        path='/languages',
        headers={'Forwarded': CHAIN_THREE_HOPS},
    )

    assert req.forwarded_scheme == 'https'
    assert req.forwarded_host == 'client.example.com'


@pytest.mark.parametrize(
    'trusted_hops, expected_scheme, expected_host',
    [
        # The rightmost element was added by the closest trusted proxy.
        (1, 'http', 'proxy2.example.com'),
        (2, 'http', 'proxy1.example.com'),
        # The leftmost element describes the original client; it is only
        #   trusted when all three hops are.
        (3, 'https', 'client.example.com'),
        # The chain is shorter than the number of trusted hops, so the
        #   header cannot be trusted at all.
        (4, 'http', 'app.example.com'),
        # Trusting zero hops disables the use of the header altogether.
        (0, 'http', 'app.example.com'),
    ],
)
def test_trusted_hops_scheme_and_host(
    asgi, util, trusted_hops, expected_scheme, expected_host
):
    req = _create_trusted_req(
        asgi,
        util,
        trusted_hops,
        host='app.example.com',
        path='/languages',
        headers={'Forwarded': CHAIN_THREE_HOPS},
    )

    assert req.forwarded_scheme == expected_scheme
    assert req.forwarded_host == expected_host
    assert req.forwarded_uri == expected_scheme + '://' + expected_host + '/languages'
    assert req.forwarded_prefix == expected_scheme + '://' + expected_host


@pytest.mark.parametrize(
    'trusted_hops, expected_scheme',
    [
        # The scheme between the two proxies was plain HTTP.
        (1, 'http'),
        # TLS was terminated at the first proxy; the client used HTTPS.
        (2, 'https'),
    ],
)
def test_trusted_hops_tls_termination(asgi, util, trusted_hops, expected_scheme):
    req = _create_trusted_req(
        asgi,
        util,
        trusted_hops,
        host='app.example.com',
        path='/languages',
        headers={
            'Forwarded': ('for=203.0.2.43;proto=https, for=198.51.100.17;proto=http')
        },
    )

    assert req.forwarded_scheme == expected_scheme


@pytest.mark.parametrize(
    'trusted_hops, expected_scheme, expected_host, expected_route',
    [
        (1, 'http', 'proxy.example.com', ['192.0.2.43', '127.0.0.1']),
        (
            2,
            'https',
            'client.example.com:8443',
            ['2001:db8:cafe::17', '192.0.2.43', '127.0.0.1'],
        ),
    ],
)
def test_trusted_hops_ipv6_quoted_values(
    asgi, util, trusted_hops, expected_scheme, expected_host, expected_route
):
    req = _create_trusted_req(
        asgi,
        util,
        trusted_hops,
        host='app.example.com',
        path='/languages',
        headers={
            'Forwarded': (
                'for="[2001:db8:cafe::17]:555";'
                'host="client.example.com:8443";proto=https, '
                'for=192.0.2.43;host=proxy.example.com;proto=http'
            )
        },
    )

    assert req.forwarded_scheme == expected_scheme
    assert req.forwarded_host == expected_host
    assert req.access_route == expected_route


def test_trusted_hops_untrusted_elements_ignored(asgi, util):
    req = _create_trusted_req(
        asgi,
        util,
        1,
        host='proxy2.example.com',
        path='/languages',
        remote_addr='192.0.2.60',
        headers={
            'Forwarded': (
                'for=6.6.6.6;proto=https;host=evil.example.com, '
                'for=203.0.113.43;proto=http;host=proxy1.example.com'
            )
        },
    )

    assert req.forwarded_scheme == 'http'
    assert req.forwarded_host == 'proxy1.example.com'
    assert req.access_route == ['203.0.113.43', '192.0.2.60']

    # Client-controlled values must not leak into the resolved client
    #   info, including anything that might end up in a log statement.
    assert '6.6.6.6' not in req.access_route
    assert 'evil' not in req.forwarded_host
    assert 'evil' not in req.forwarded_uri
    assert 'evil' not in repr(req)


@pytest.mark.parametrize(
    'forwarded',
    [
        'for=1.2.3.4;by="',
        'for=1.2.3.4;by="\\4.3.2.1"thing=blah',
        'for="unterminated',
        'for=1.2.3.4 trailing-garbage',
        'for="1.2.3.4"\r\nX-Injected: evil',
    ],
)
def test_trusted_hops_malformed_header_ignored(asgi, util, forwarded):
    req = _create_trusted_req(
        asgi,
        util,
        1,
        host='app.example.com',
        path='/languages',
        headers={'Forwarded': forwarded},
    )

    # A malformed header is ignored in its entirety rather than being
    #   acted upon partially.
    assert req.forwarded is None
    assert req.forwarded_scheme == 'http'
    assert req.forwarded_host == 'app.example.com'
    assert req.forwarded_uri == req.uri
    assert req.access_route == ['127.0.0.1']


@pytest.mark.parametrize(
    'forwarded, trusted_hops, expected_route',
    [
        (
            'for=203.0.113.43,,for=198.51.100.17',
            1,
            ['198.51.100.17', '127.0.0.1'],
        ),
        (
            'for=203.0.113.43,,for=198.51.100.17',
            2,
            ['203.0.113.43', '198.51.100.17', '127.0.0.1'],
        ),
        # Empty elements never count as hops.
        (', ,', 1, ['127.0.0.1']),
        # An empty value is malformed, so the entire header is ignored.
        ('for=,for=203.0.113.43', 1, ['127.0.0.1']),
    ],
)
def test_trusted_hops_empty_elements_not_counted(
    asgi, util, forwarded, trusted_hops, expected_route
):
    req = _create_trusted_req(
        asgi,
        util,
        trusted_hops,
        host='app.example.com',
        path='/languages',
        headers={'Forwarded': forwarded},
    )

    assert req.access_route == expected_route


def test_trusted_hops_element_without_for(asgi, util):
    req = _create_trusted_req(
        asgi,
        util,
        2,
        host='app.example.com',
        path='/languages',
        headers={
            'Forwarded': (
                'for=6.6.6.6, host=proxy1.example.com, for=203.0.113.43;proto=https'
            )
        },
    )

    # The trusted element without a "for" parameter contributes no
    #   address to the route, and no scheme to fall back on.
    assert req.access_route == ['203.0.113.43', '127.0.0.1']
    assert req.forwarded_scheme == 'http'
    assert req.forwarded_host == 'proxy1.example.com'


@pytest.mark.parametrize('trusted_hops', [None, 1])
def test_max_forwarded_size_exceeded(asgi, util, trusted_hops):
    req = _create_trusted_req(
        asgi,
        util,
        trusted_hops,
        max_forwarded_size=16,
        host='app.example.com',
        path='/languages',
        headers={'Forwarded': 'for=203.0.113.43;proto=https;host=client.example.com'},
    )

    # An oversized header is ignored rather than parsed.
    assert req.forwarded is None
    assert req.forwarded_scheme == 'http'
    assert req.forwarded_host == 'app.example.com'
    assert req.access_route == ['127.0.0.1']


def test_max_forwarded_size_not_exceeded(asgi, util):
    header = 'for=203.0.113.43;proto=https'
    req = _create_trusted_req(
        asgi,
        util,
        1,
        max_forwarded_size=len(header),
        host='app.example.com',
        path='/languages',
        headers={'Forwarded': header},
    )

    assert req.forwarded is not None
    assert req.forwarded_scheme == 'https'


@pytest.mark.parametrize(
    'trusted_hops, expected_scheme',
    [
        (1, 'http'),
        (2, 'https'),
        # The chain is shorter than the number of trusted hops, so fall
        #   back to the connection-level scheme.
        (3, 'http'),
    ],
)
def test_trusted_hops_x_forwarded_proto(asgi, util, trusted_hops, expected_scheme):
    req = _create_trusted_req(
        asgi,
        util,
        trusted_hops,
        host='app.example.com',
        path='/languages',
        headers={'X-Forwarded-Proto': 'https, http'},
    )

    assert req.forwarded_scheme == expected_scheme


@pytest.mark.parametrize(
    'trusted_hops, expected_host',
    [
        (1, 'proxy1.example.com'),
        (2, 'client.example.com'),
        # The chain is shorter than the number of trusted hops, so fall
        #   back to the Host header.
        (3, 'app.example.com'),
    ],
)
def test_trusted_hops_x_forwarded_host(asgi, util, trusted_hops, expected_host):
    req = _create_trusted_req(
        asgi,
        util,
        trusted_hops,
        host='app.example.com',
        path='/languages',
        headers={'X-Forwarded-Host': 'client.example.com, proxy1.example.com'},
    )

    assert req.forwarded_host == expected_host


def test_trusted_hops_forwarded_takes_precedence(asgi, util):
    req = _create_trusted_req(
        asgi,
        util,
        1,
        host='app.example.com',
        path='/languages',
        headers={
            'Forwarded': 'for=203.0.113.43;proto=https;host=client.example.com',
            'X-Forwarded-Proto': 'http',
            'X-Forwarded-Host': 'ignored.example.com',
            'X-Forwarded-For': '6.6.6.6',
        },
    )

    assert req.forwarded_scheme == 'https'
    assert req.forwarded_host == 'client.example.com'
    assert req.access_route == ['203.0.113.43', '127.0.0.1']


def test_trusted_hops_duplicate_forwarded_headers(asgi, util):
    # Multiple Forwarded headers are combined into a single chain,
    #   either by the WSGI server or by Falcon itself (ASGI); trusted
    #   hops are then counted from the right of the combined chain.
    if asgi:
        headers = [
            ('Forwarded', 'for=6.6.6.6;proto=https;host=evil.example.com'),
            ('Forwarded', 'for=203.0.113.43;proto=http;host=proxy.example.com'),
        ]
    else:
        headers = {
            'Forwarded': (
                'for=6.6.6.6;proto=https;host=evil.example.com, '
                'for=203.0.113.43;proto=http;host=proxy.example.com'
            )
        }

    req = _create_trusted_req(
        asgi, util, 1, host='app.example.com', path='/languages', headers=headers
    )

    assert req.forwarded_scheme == 'http'
    assert req.forwarded_host == 'proxy.example.com'
    assert req.access_route == ['203.0.113.43', '127.0.0.1']
    assert '6.6.6.6' not in req.access_route


def test_trusted_hops_malicious_values_not_leaked(asgi, util):
    req = _create_trusted_req(
        asgi,
        util,
        1,
        host='app.example.com',
        path='/languages',
        headers={
            'Forwarded': (
                'for="6.6.6.6\\r\\nX-Injected: evil";proto=gopher;'
                'host=evil.example.com, '
                'for=203.0.113.43;proto=https;host=client.example.com'
            )
        },
    )

    assert req.forwarded_scheme == 'https'
    assert req.forwarded_host == 'client.example.com'
    assert req.access_route == ['203.0.113.43', '127.0.0.1']

    # Client-controlled values must not leak into anything that might
    #   end up in a log statement or an error message.
    assert '6.6.6.6' not in req.access_route
    assert 'evil' not in req.forwarded_uri
    assert 'evil' not in repr(req)
    assert 'gopher' not in req.forwarded_scheme
