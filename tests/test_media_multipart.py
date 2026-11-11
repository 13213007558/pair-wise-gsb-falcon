import io
import itertools
import os
import random

import pytest

import falcon
from falcon import media
from falcon import testing
from falcon.media.multipart import MultipartParseOptions
from falcon.util import BufferedReader


@pytest.fixture(scope='session')
def msgpack():
    return pytest.importorskip('msgpack')


EXAMPLE1 = (
    b'--5b11af82ab65407ba8cdccf37d2a9c4f\r\n'
    b'Content-Disposition: form-data; name="hello"\r\n\r\n'
    b'world\r\n'
    b'--5b11af82ab65407ba8cdccf37d2a9c4f\r\n'
    b'Content-Disposition: form-data; name="document"\r\n'
    b'Content-Type: application/json\r\n\r\n'
    b'{"debug": true, "message": "Hello, world!", "score": 7}\r\n'
    b'--5b11af82ab65407ba8cdccf37d2a9c4f\r\n'
    b'Content-Disposition: form-data; name="file1"; filename="test.txt"\r\n'
    b'Content-Type: text/plain\r\n\r\n'
    b'Hello, world!\n\r\n'
    b'--5b11af82ab65407ba8cdccf37d2a9c4f--\r\n'
)

EXAMPLE2 = (
    b'-----------------------------1574247108204320607285918568\r\n'
    b'Content-Disposition: form-data; name="description"\r\n\r\n'
    b'\r\n'
    b'-----------------------------1574247108204320607285918568\r\n'
    b'Content-Disposition: form-data; name="moderation"\r\n\r\n'
    b'approved\r\n'
    b'-----------------------------1574247108204320607285918568\r\n'
    b'Content-Disposition: form-data; name="title"\r\n\r\n'
    b'A simple text file example.\r\n'
    b'-----------------------------1574247108204320607285918568\r\n'
    b'Content-Disposition: form-data; name="uploadid"\r\n\r\n'
    b'00l33t0174873295\r\n'
    b'-----------------------------1574247108204320607285918568\r\n'
    b'Content-Disposition: form-data; name="file"; filename="test.txt"\r\n'
    b'Content-Type: text/plain\r\n\r\n'
    b'Hello, world!\n'
    b'\r\n'
    b'-----------------------------1574247108204320607285918568--\r\n'
)
EXAMPLE2_PART_COUNT = 5

EXAMPLE3 = (
    b'--BOUNDARY\r\n'
    b'Content-Disposition: form-data; name="file"; filename="bytes"\r\n'
    b'Content-Type: application/x-falcon\r\n\r\n'
    + (b'123456789abcdef\n' * 64 * 1024 * 2)
    + b'\r\n'
    b'--BOUNDARY\r\n'
    b'Content-Disposition: form-data; name="empty"\r\n'
    b'Content-Type: text/plain\r\n\r\n'
    b'\r\n'
    b'--BOUNDARY--\r\n'
)

LOREM_IPSUM = (
    b'Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod '
    b'tempor incididunt ut labore et dolore magna aliqua. Dolor sed viverra '
    b'ipsum nunc aliquet bibendum enim. In massa tempor nec feugiat. Nunc '
    b'aliquet bibendum enim facilisis gravida. Nisl nunc mi ipsum faucibus '
    b'vitae aliquet nec ullamcorper. Amet luctus venenatis lectus magna '
    b'fringilla. Volutpat maecenas volutpat blandit aliquam etiam erat velit '
    b'scelerisque in. Egestas egestas fringilla phasellus faucibus scelerisque '
    b'eleifend. Sagittis orci a scelerisque purus semper eget duis. Nulla '
    b'pharetra diam sit amet nisl suscipit. Sed adipiscing diam donec '
    b'adipiscing tristique risus nec feugiat in. Fusce ut placerat orci nulla. '
    b'Pharetra vel turpis nunc eget lorem dolor. Tristique senectus et netus '
    b'et malesuada.\n'
)

EXAMPLE4 = (
    b'--boundary\r\n'
    b'Content-Disposition: form-data; name="lorem1"; filename="bytes1"\r\n'
    b'Content-Type: text/plain\r\n\r\n' + LOREM_IPSUM + b'\r\n'
    b'--boundary\r\n'
    b'Content-Disposition: form-data; name="empty"\r\n'
    b'Content-Type: text/plain\r\n\r\n'
    b'\r\n'
    b'--boundary\r\n'
    b'Content-Disposition: form-data; name="lorem2"; filename="bytes1"\r\n'
    b'Content-Type: text/plain\r\n\r\n' + LOREM_IPSUM + b'\r\n'
    b'--boundary--\r\n'
)


EXAMPLES = {
    '5b11af82ab65407ba8cdccf37d2a9c4f': EXAMPLE1,
    '---------------------------1574247108204320607285918568': EXAMPLE2,
    'BOUNDARY': EXAMPLE3,
    'boundary': EXAMPLE4,
}


# APPSEC: This SHA256 hash is safe against preimage attacks.
HASH_BOUNDARY = 'fbeff51e0f5630958701f4941aec5595addcb3ee1b70468c8bd4be920304c184'


@pytest.mark.parametrize('boundary', list(EXAMPLES))
def test_parse(boundary):
    handler = media.MultipartFormHandler()
    example = EXAMPLES[boundary]
    form = handler.deserialize(
        io.BytesIO(example), 'multipart/form-data; boundary=' + boundary, len(example)
    )

    for part in form:
        output = io.BytesIO()
        part.stream.pipe(output)
        assert isinstance(output.getvalue(), bytes)


@pytest.mark.parametrize(
    'buffer_size,chunk_size',
    list(
        itertools.product(
            (
                32,
                64,
                128,
                256,
            ),
            (
                7,
                8,
                9,
                10,
                32,
                64,
                128,
                256,
            ),
        )
    ),
)
def test_parsing_correctness(buffer_size, chunk_size):
    example = EXAMPLES['boundary']
    handler = media.MultipartFormHandler()
    stream = BufferedReader(io.BytesIO(example).read, len(example), buffer_size)
    form = handler.deserialize(
        stream, 'multipart/form-data; boundary=boundary', len(example)
    )

    for part in form:
        if part.name in ('lorem1', 'lorem2'):
            part_stream = part.stream
            result = []
            while True:
                chunk = part_stream.read(chunk_size)
                if not chunk:
                    break
                result.append(chunk)

            assert b''.join(result) == LOREM_IPSUM


def test_missing_boundary():
    handler = media.MultipartFormHandler()

    with pytest.raises(falcon.HTTPInvalidHeader):
        handler.deserialize(io.BytesIO(), 'multipart/form-data', 0)

    with pytest.raises(falcon.HTTPInvalidHeader):
        handler.deserialize(io.BytesIO(), 'multipart/form-data; boundary=', 0)

    overlong = '-' * 71
    content_type = 'multipart/form-data; boundary=' + overlong
    with pytest.raises(falcon.HTTPInvalidHeader):
        handler.deserialize(io.BytesIO(), content_type, 0)


def test_empty_input():
    handler = media.MultipartFormHandler()
    form = handler.deserialize(io.BytesIO(), 'multipart/form-data; boundary=404', 0)
    with pytest.raises(falcon.MediaMalformedError):
        for part in form:
            pass


def test_serialize():
    handler = media.MultipartFormHandler()
    with pytest.raises(NotImplementedError):
        handler.serialize({'key': 'value'}, 'multipart/form-data')


@pytest.mark.parametrize(
    'charset,data',
    [
        ('utf-8', b'Impossible byte: \xff'),
        ('utf-8', b'Overlong... \xfc\x83\xbf\xbf\xbf\xbf ... sequence'),
        ('ascii', b'\x80\x80\x80'),
        ('pecyn', b'AAHEHlRoZSBGYWxjb24gV2ViIEZyYW1ld29yaywgMjAxOQ=='),
    ],
)
def test_invalid_text_or_charset(charset, data):
    data = (
        b'--BOUNDARY\r\n'
        b'Content-Disposition: form-data; name="text"\r\n'
        b'Content-Type: text/plain; '
        + f'charset={charset}\r\n\r\n'.encode()
        + data
        + b'\r\n'
        b'--BOUNDARY\r\n'
        b'Content-Disposition: form-data; name="empty"\r\n'
        b'Content-Type: text/plain\r\n\r\n'
        b'\r\n'
        b'--BOUNDARY--\r\n'
    )

    handler = media.MultipartFormHandler()

    form = handler.deserialize(
        io.BytesIO(data), 'multipart/form-data; boundary=BOUNDARY', len(data)
    )
    with pytest.raises(falcon.MediaMalformedError):
        for part in form:
            part.text


def test_unknown_header():
    data = (
        b'--BOUNDARY\r\n'
        b'Content-Disposition: form-data; name="empty"\r\n'
        b'Content-Coolness: fair\r\n'
        b'Content-Type: text/plain\r\n\r\n'
        b'\r\n'
        b'--BOUNDARY--\r\n'
    )

    handler = media.MultipartFormHandler()
    form = handler.deserialize(
        io.BytesIO(data), 'multipart/form-data; boundary=BOUNDARY', len(data)
    )

    for part in form:
        assert part.data == b''


def test_from_buffered_stream():
    data = (
        b'--BOUNDARY\r\n'
        b'Content-Disposition: form-data; name="empty"\r\n'
        b'Content-Coolness: fair\r\n'
        b'Content-Type: text/plain\r\n\r\n'
        b'\r\n'
        b'--BOUNDARY--\r\n'
    )

    handler = media.MultipartFormHandler()
    stream = BufferedReader(io.BytesIO(data).read, len(data))
    form = handler.deserialize(
        stream, 'multipart/form-data; boundary=BOUNDARY', len(data)
    )

    for part in form:
        assert part.data == b''


def test_body_part_media():
    handler = media.MultipartFormHandler()

    content_type = 'multipart/form-data; boundary=' + '5b11af82ab65407ba8cdccf37d2a9c4f'
    form = handler.deserialize(io.BytesIO(EXAMPLE1), content_type, len(EXAMPLE1))

    expected = {'debug': True, 'message': 'Hello, world!', 'score': 7}

    for part in form:
        if part.content_type == 'application/json':
            assert part.media == part.media == expected


def test_body_part_properties():
    handler = media.MultipartFormHandler()

    content_type = 'multipart/form-data; boundary=' + '5b11af82ab65407ba8cdccf37d2a9c4f'
    form = handler.deserialize(io.BytesIO(EXAMPLE1), content_type, len(EXAMPLE1))

    for part in form:
        if part.content_type == 'application/json':
            # NOTE(vytas): This is not a typo, but a test that the name
            #   property can be safely referenced multiple times.
            assert part.name == part.name == 'document'
        elif part.name == 'file1':
            # NOTE(vytas): This is not a typo, but a test that the filename
            #   property can be safely referenced multiple times.
            assert part.filename == part.filename == 'test.txt'
            assert part.secure_filename == part.filename


def test_empty_or_missing_filename():
    data = (
        b'--a0d738bcdb30449eb0d13f4b72c2897e\r\n'
        b'Content-Disposition: form-data; name="file"; filename=\r\n\r\n'
        b'An empty filename.\r\n'
        b'--a0d738bcdb30449eb0d13f4b72c2897e\r\n'
        b'Content-Disposition: form-data; name="no file";\r\n\r\n'
        b'No filename.\r\n'
        b'--a0d738bcdb30449eb0d13f4b72c2897e--\r\n'
    )

    handler = media.MultipartFormHandler()
    content_type = 'multipart/form-data; boundary=' + 'a0d738bcdb30449eb0d13f4b72c2897e'
    stream = BufferedReader(io.BytesIO(data).read, len(data))
    form = handler.deserialize(stream, content_type, len(data))

    parts = 0
    for part in form:
        parts += 1
        if part.name == 'file':
            assert part.filename == ''
        else:
            assert part.filename is None
        with pytest.raises(falcon.MediaMalformedError):
            part.secure_filename
    assert parts == 2


class MultipartAnalyzer:
    def on_post(self, req, resp):
        values = []
        for part in req.media:
            values.append(
                {
                    'content_type': part.content_type,
                    'data': part.data.decode(),
                    'filename': part.filename,
                    'name': part.name,
                    'secure_filename': part.secure_filename if part.filename else None,
                    'text': part.text,
                }
            )

        resp.media = values

    def on_post_media(self, req, resp):
        deserialized = []
        for part in req.media:
            part_media = part.get_media()
            assert part_media == part.media
            deserialized.append(part_media)

        resp.media = deserialized

    def on_post_mirror(self, req, resp):
        parts = []
        for part in req.get_media():
            parts.append(
                {
                    'content': part.stream.read(),
                    'content_type': part.content_type,
                    'name': part.name,
                }
            )

        resp.content_type = falcon.MEDIA_MSGPACK
        resp.media = parts


class AsyncMultipartAnalyzer:
    async def on_post(self, req, resp):
        values = []
        form = await req.get_media()
        async for part in form:
            # NOTE(vytas): use the async property form to exercise that too.
            values.append(
                {
                    'content_type': part.content_type,
                    'data': (await part.data).decode(),
                    'filename': part.filename,
                    'name': part.name,
                    'secure_filename': part.secure_filename if part.filename else None,
                    'text': (await part.text),
                }
            )

        resp.media = values

    async def on_post_media(self, req, resp):
        deserialized = []
        form = await req.media
        async for part in form:
            part_media = await part.get_media()
            assert part_media == await part.media
            deserialized.append(part_media)

        resp.media = deserialized

    async def on_post_mirror(self, req, resp):
        parts = []
        async for part in await req.get_media():
            parts.append(
                {
                    'content': await part.stream.read(),
                    'content_type': part.content_type,
                    'name': part.name,
                }
            )

        resp.content_type = falcon.MEDIA_MSGPACK
        resp.media = parts


@pytest.fixture
def custom_client(asgi, util, msgpack):
    def _factory(options):
        multipart_handler = media.MultipartFormHandler()
        for key, value in options.items():
            setattr(multipart_handler.parse_options, key, value)
        req_handlers = {
            falcon.MEDIA_JSON: media.JSONHandler(),
            falcon.MEDIA_MULTIPART: multipart_handler,
        }
        resp_handlers = {
            falcon.MEDIA_JSON: media.JSONHandler(),
        }
        if msgpack:
            resp_handlers[falcon.MEDIA_MSGPACK] = media.MessagePackHandler()

        app = util.create_app(asgi)
        app.req_options.media_handlers = media.Handlers(req_handlers)
        app.resp_options.media_handlers = media.Handlers(resp_handlers)

        resource = AsyncMultipartAnalyzer() if asgi else MultipartAnalyzer()
        app.add_route('/submit', resource)
        app.add_route('/media', resource, suffix='media')
        app.add_route('/mirror', resource, suffix='mirror')

        return testing.TestClient(app)

    return _factory


@pytest.fixture
def client(custom_client):
    return custom_client({})


def test_upload_multipart(client):
    resp = client.simulate_post(
        '/submit',
        headers={
            'Content-Type': 'multipart/form-data; '
            'boundary=5b11af82ab65407ba8cdccf37d2a9c4f',
        },
        body=EXAMPLE1,
    )

    assert resp.status_code == 200
    assert resp.json == [
        {
            'content_type': 'text/plain',
            'data': 'world',
            'filename': None,
            'name': 'hello',
            'secure_filename': None,
            'text': 'world',
        },
        {
            'content_type': 'application/json',
            'data': '{"debug": true, "message": "Hello, world!", "score": 7}',
            'filename': None,
            'name': 'document',
            'secure_filename': None,
            'text': None,
        },
        {
            'content_type': 'text/plain',
            'data': 'Hello, world!\n',
            'filename': 'test.txt',
            'name': 'file1',
            'secure_filename': 'test.txt',
            'text': 'Hello, world!\n',
        },
    ]


@pytest.mark.parametrize('epilogue', ['', '--', '\n', '\n\n', ' <-- no CRLF', '💥'])
def test_epilogue(client, epilogue):
    # NOTE(vytas): According to RFC 2046, actually including an epilogue might
    #   require a trailing CRLF first, but we do not mandate it either.
    form_body = EXAMPLE1[:-2] + epilogue.encode()

    resp = client.simulate_post(
        '/submit',
        headers={
            'Content-Type': 'multipart/form-data; '
            'boundary=5b11af82ab65407ba8cdccf37d2a9c4f',
        },
        body=form_body,
    )

    assert resp.status_code == 200
    assert [part['name'] for part in resp.json] == ['hello', 'document', 'file1']


@pytest.mark.parametrize('truncated_by', [1, 2, 3, 4])
def test_truncated_form(client, truncated_by):
    resp = client.simulate_post(
        '/submit',
        headers={
            'Content-Type': 'multipart/form-data; '
            'boundary=5b11af82ab65407ba8cdccf37d2a9c4f',
        },
        # NOTE(vytas): The trailing \r\n is not mandatory, hence +2.
        body=EXAMPLE1[: -(truncated_by + 2)],
    )

    assert resp.status_code == 400
    assert resp.json == {
        'description': 'unexpected form structure',
        'title': 'Malformed multipart/form-data request media',
    }


def test_unexpected_form_structure(client):
    resp1 = client.simulate_post(
        '/submit',
        headers={
            'Content-Type': 'multipart/form-data; '
            'boundary=5b11af82ab65407ba8cdccf37d2a9c4f',
        },
        body=EXAMPLE1[:-4] + b'__\r\n',
    )

    assert resp1.status_code == 400
    assert resp1.json == {
        'description': 'unexpected form structure',
        'title': 'Malformed multipart/form-data request media',
    }

    resp2 = client.simulate_post(
        '/submit',
        headers={
            'Content-Type': 'multipart/form-data; '
            'boundary=5b11af82ab65407ba8cdccf37d2a9c4f',
        },
        body=EXAMPLE1[:-4] + b'**\r\n',
    )

    assert resp2.status_code == 400
    assert resp2.json == {
        'description': 'unexpected form structure',
        'title': 'Malformed multipart/form-data request media',
    }


def test_data_too_large(client):
    resp = client.simulate_post(
        '/submit',
        headers={
            'Content-Type': 'multipart/form-data; boundary=BOUNDARY',
        },
        body=EXAMPLE3,
    )

    assert resp.status_code == 400
    assert resp.json == {
        'description': 'body part is too large',
        'title': 'Malformed multipart/form-data request media',
    }


@pytest.mark.parametrize('max_body_part_count', list(range(7)) + [100, 1000])
def test_too_many_body_parts(custom_client, max_body_part_count):
    client = custom_client({'max_body_part_count': max_body_part_count})
    boundary = '---------------------------1574247108204320607285918568'
    resp = client.simulate_post(
        '/submit',
        headers={
            'Content-Type': 'multipart/form-data; boundary=' + boundary,
        },
        body=EXAMPLE2,
    )

    if 0 < max_body_part_count < EXAMPLE2_PART_COUNT:
        assert resp.status_code == 400
        assert resp.json == {
            'description': 'maximum number of form body parts exceeded',
            'title': 'Malformed multipart/form-data request media',
        }
    else:
        assert resp.status_code == 200
        assert len(resp.json) == EXAMPLE2_PART_COUNT


@pytest.mark.parametrize('close_delimiter', ['--', '--\r\n'])
def test_random_form(client, close_delimiter, msgpack):
    part_data = [os.urandom(random.randint(0, 2**18)) for _ in range(64)]
    form_data = (
        b''.join(
            f'--{HASH_BOUNDARY}\r\n'.encode()
            + f'Content-Disposition: form-data; name="p{i}"\r\n'.encode()
            + b'Content-Type: application/x-falcon-urandom\r\n\r\n'
            + part_data[i]
            + b'\r\n'
            for i in range(64)
        )
        + f'--{HASH_BOUNDARY}{close_delimiter}'.encode()
    )

    handler = media.MultipartFormHandler()
    content_type = 'multipart/form-data; boundary=' + HASH_BOUNDARY
    form = handler.deserialize(io.BytesIO(form_data), content_type, len(form_data))

    resp = client.simulate_post(
        '/mirror', headers={'Content-Type': content_type}, body=form_data
    )
    assert resp.status_code == 200
    form = msgpack.unpackb(resp.content, raw=False)

    for index, part in enumerate(form):
        assert part['content'] == part_data[index]
        assert part['content_type'] == 'application/x-falcon-urandom'


def test_invalid_random_form(client):
    length = random.randint(2**20, 2**21)
    resp = client.simulate_post(
        '/submit',
        headers={
            'Content-Type': 'multipart/form-data; boundary=' + HASH_BOUNDARY,
        },
        body=os.urandom(length),
    )

    assert resp.status_code == 400


def test_nested_multipart_mixed():
    class Forms:
        def on_post(self, req, resp):
            example = {}
            for part in req.media:
                if part.content_type.startswith('multipart/mixed'):
                    for nested in part.media:
                        example[nested.filename] = nested.text

            resp.media = example

    parser = media.MultipartFormHandler()
    parser.parse_options.media_handlers['multipart/mixed'] = (
        media.MultipartFormHandler()
    )

    app = falcon.App()
    app.req_options.media_handlers[falcon.MEDIA_MULTIPART] = parser
    app.add_route('/forms', Forms())
    client = testing.TestClient(app)

    form_data = (
        b'--AaB03x\r\n'
        b'Content-Disposition: form-data; name="field1"\r\n\r\n'
        b'Joe Blow\r\n'
        b'--AaB03x\r\n'
        b'Content-Disposition: form-data; name="docs"\r\n'
        b'Content-Type: multipart/mixed; boundary=BbC04y\r\n\r\n'
        b'--BbC04y\r\n'
        b'Content-Disposition: attachment; filename="file1.txt"\r\n\r\n'
        b'This is file1.\r\n\r\n'
        b'--BbC04y\r\n'
        b'Content-Disposition: attachment; filename="file2.txt"\r\n'
        b'Content-Transfer-Encoding: binary\r\n\r\n'
        b'Hello, World!\r\n\r\n'
        b'--BbC04y--\r\n\r\n'
        b'--AaB03x--\r\n'
    )
    resp = client.simulate_post(
        '/forms',
        headers={'Content-Type': 'multipart/form-data; boundary=AaB03x'},
        body=form_data,
    )
    assert resp.status_code == 200
    assert resp.json == {
        'file1.txt': 'This is file1.\r\n',
        'file2.txt': 'Hello, World!\r\n',
    }


def test_content_transfer_encoding_header(client):
    data = (
        b'--BOUNDARY\r\n'
        b'Content-Disposition: form-data; name="file"; filename="bytes"\r\n'
        b'Content-Transfer-Encoding: Base64'
        b'Content-Type: application/x-falcon\r\n\r\n'
        b'UGVyZWdyaW5lIEZhbGNvbiADLgA='
        b'\r\n'
        b'--BOUNDARY\r\n'
        b'Content-Disposition: form-data; name="empty"\r\n'
        b'Content-Type: text/plain\r\n\r\n'
        b'\r\n'
        b'--BOUNDARY--\r\n'
    )

    resp = client.simulate_post(
        '/submit',
        headers={'Content-Type': 'multipart/form-data; boundary=BOUNDARY'},
        body=data,
    )

    assert resp.status_code == 400
    assert resp.json == {
        'description': (
            'the deprecated Content-Transfer-Encoding header field is unsupported'
        ),
        'title': 'Malformed multipart/form-data request media',
    }


def test_unsupported_charset(client):
    data = (
        b'--BOUNDARY\r\n'
        b'Content-Disposition: form-data; name="text"\r\n'
        b'Content-Type: text/plain; charset=pecyn\r\n\r\n'
        b'AAHEHlRoZSBGYWxjb24gV2ViIEZyYW1ld29yaywgMjAxOQ=='
        b'\r\n'
        b'--BOUNDARY\r\n'
        b'Content-Disposition: form-data; name="empty"\r\n'
        b'Content-Type: text/plain\r\n\r\n'
        b'\r\n'
        b'--BOUNDARY--\r\n'
    )

    resp = client.simulate_post(
        '/submit',
        headers={'Content-Type': 'multipart/form-data; boundary=BOUNDARY'},
        body=data,
    )

    assert resp.status_code == 400
    assert resp.json == {
        'description': 'invalid text or charset: pecyn',
        'title': 'Malformed multipart/form-data request media',
    }


def test_filename_star(client):
    # NOTE(vytas): Generated by requests_toolbelt 0.9.1 on Py2
    #   (interestingly, one gets a "normal" filename on Py3.7).

    data = (
        b'--a0d738bcdb30449eb0d13f4b72c2897e\r\n'
        b'Content-Disposition: form-data; name="file"; '
        b"filename*=utf-8''%E2%AC%85%20Arrow.txt\r\n\r\n"
        b'A unicode arrow in the filename.\r\n'
        b'--a0d738bcdb30449eb0d13f4b72c2897e--\r\n'
    )
    content_type = 'multipart/form-data; boundary=' + 'a0d738bcdb30449eb0d13f4b72c2897e'

    resp = client.simulate_post(
        '/submit', headers={'Content-Type': content_type}, body=data
    )
    assert resp.status_code == 200
    assert resp.json == [
        {
            'content_type': 'text/plain',
            'data': 'A unicode arrow in the filename.',
            'filename': '⬅ Arrow.txt',
            'name': 'file',
            'secure_filename': '__Arrow.txt',
            'text': 'A unicode arrow in the filename.',
        }
    ]

    data = data.replace(b'*=utf-8', b'*=esoteric')
    resp = client.simulate_post(
        '/submit', headers={'Content-Type': content_type}, body=data
    )
    assert resp.status_code == 400
    assert resp.json == {
        'description': 'invalid text or charset: esoteric',
        'title': 'Malformed multipart/form-data request media',
    }


@pytest.mark.parametrize(
    'max_length,expected',
    [
        (None, '__Arrow.txt'),
        (0, '__Arrow.txt'),
        (4, '__Ar'),
        (5, '_.txt'),
        (7, '__A.txt'),
        (10, '__Arro.txt'),
        (64, '__Arrow.txt'),
    ],
)
def test_max_secure_filename_length(custom_client, max_length, expected):
    client = custom_client({'max_secure_filename_length': max_length})

    data = (
        b'--a0d738bcdb30449eb0d13f4b72c2897e\r\n'
        b'Content-Disposition: form-data; name="file"; '
        b'filename=\xe2\xac\x85 Arrow.txt\r\n\r\n'
        b'A unicode arrow in the filename.\r\n'
        b'--a0d738bcdb30449eb0d13f4b72c2897e--\r\n'
    )
    content_type = 'multipart/form-data; boundary=' + 'a0d738bcdb30449eb0d13f4b72c2897e'

    resp = client.simulate_post(
        '/submit', headers={'Content-Type': content_type}, body=data
    )
    assert resp.status_code == 200
    assert resp.json == [
        {
            'content_type': 'text/plain',
            'data': 'A unicode arrow in the filename.',
            'filename': '⬅ Arrow.txt',
            'name': 'file',
            'secure_filename': expected,
            'text': 'A unicode arrow in the filename.',
        }
    ]


@pytest.mark.parametrize('max_headers_size', [64, 140, 141, 142, 256, 1024])
def test_headers_edge_cases(custom_client, max_headers_size):
    client = custom_client({'max_body_part_headers_size': max_headers_size})

    data = (
        b'--a0d738bcdb30449eb0d13f4b72c2897e\r\n'
        b'X-Falcon: Peregrine\r\n'
        b'Content-Type: application/vnd.oasis.opendocument.text\r\n'
        b'Junk\r\n'
        b'Content-Disposition: form-data; name="file"; filename=hd.txt\r\n\r\n'
        b'No, it is not an ODT document...\r\n'
        b'--a0d738bcdb30449eb0d13f4b72c2897e--\r\n'
    )
    content_type = 'multipart/form-data; boundary=' + 'a0d738bcdb30449eb0d13f4b72c2897e'

    resp = client.simulate_post(
        '/submit', headers={'Content-Type': content_type}, body=data
    )

    if max_headers_size < 142:
        assert resp.status_code == 400
        assert resp.json == {
            'description': 'incomplete body part headers',
            'title': 'Malformed multipart/form-data request media',
        }
    else:
        assert len(resp.json) == 1


def test_deserialize_part_media(client):
    data = (
        b'--BOUNDARY\r\n'
        b'Content-Disposition: form-data; name="factorials"\r\n'
        b'Content-Type: application/json\r\n\r\n'
        b'{"count": 6, "numbers": [1, 2, 6, 24, 120, 720]}\r\n'
        b'--BOUNDARY\r\n'
        b'Content-Disposition: form-data; name="person"\r\n'
        b'Content-Type: application/x-www-form-urlencoded\r\n\r\n'
        b'name=Jane&surname=Doe&fruit=%F0%9F%8D%8F\r\n'
        b'--BOUNDARY--\r\n'
    )

    resp = client.simulate_post(
        '/media',
        headers={'Content-Type': 'multipart/form-data; boundary=BOUNDARY'},
        body=data,
    )

    assert resp.status_code == 200
    assert resp.json == [
        {'count': 6, 'numbers': [1, 2, 6, 24, 120, 720]},
        {'fruit': '🍏', 'name': 'Jane', 'surname': 'Doe'},
    ]


def test_deserialize_custom_media(custom_client):
    class FirstByteHandler(media.BaseHandler):
        exhaust_stream = True

        def deserialize(self, stream, content_type, content_length):
            first_byte = stream.read(1)
            if first_byte:
                return f'0x{first_byte[0]:02x}'
            return ''

        async def deserialize_async(self, stream, content_type, content_length):
            first_byte = await stream.read(1)
            if first_byte:
                return f'0x{first_byte[0]:02x}'
            return ''

    handlers = media.Handlers({'application/x-falcon-first-byte': FirstByteHandler()})
    client = custom_client({'media_handlers': handlers})

    data = (
        b'--BOUNDARY\r\n'
        b'Content-Disposition: form-data; name="first"\r\n'
        b'Content-Type: application/x-falcon-first-byte\r\n\r\n'
        b'\r\n'
        b'--BOUNDARY\r\n'
        b'Content-Disposition: form-data; name="second"\r\n'
        b'Content-Type: application/x-falcon-first-byte\r\n\r\n'
        b'Hi!\r\n'
        b'--BOUNDARY--\r\n'
    )

    resp = client.simulate_post(
        '/media',
        headers={'Content-Type': 'multipart/form-data; boundary=BOUNDARY'},
        body=data,
    )

    assert resp.status_code == 200
    assert resp.json == ['', '0x48']


def test_multipart_parse_options_default_handlers_unique():
    parse_options_one = MultipartParseOptions()
    parse_options_two = MultipartParseOptions()

    parse_options_one.media_handlers.pop(falcon.MEDIA_JSON)

    assert parse_options_one.media_handlers is not parse_options_two.media_handlers
    assert len(parse_options_one.media_handlers) == 1
    assert len(parse_options_two.media_handlers) >= 2


def test_parse_options_total_size_default_unlimited():
    parse_options = MultipartParseOptions()
    assert parse_options.max_body_part_total_size == 0


def test_body_size_budget_limit_branches():
    from falcon.media.multipart import _BodySizeBudget

    unlimited = _BodySizeBudget(0)
    unlimited.charge(10**9)
    unlimited.charge(0)
    assert unlimited._consumed == 10**9

    budget = _BodySizeBudget(10)
    budget.charge(0)
    assert budget._consumed == 0
    budget.charge(10)
    assert budget._consumed == 10
    with pytest.raises(falcon.MultipartParseError) as exc_info:
        budget.charge(1)
    assert exc_info.value.description == (
        'total body part content size limit exceeded'
    )


TOTAL_SIZE_BOUNDARY = 'TOTAL-SIZE-BOUNDARY'
TOTAL_SIZE_DELIMITER = b'\r\n--' + TOTAL_SIZE_BOUNDARY.encode()
TOTAL_SIZE_PARTS = [
    ('first', b'a' * 100),
    ('second', b'b' * 50),
    ('third', b'c' * 10),
]
TOTAL_SIZE_CONTENT = 160
TOTAL_SIZE_JSON_PART = b'{"answer": 42}'


def _build_total_size_form(parts=TOTAL_SIZE_PARTS, json_names=()):
    json_names = set(json_names)
    body = b''
    for name, content in parts:
        content_type = (
            b'Content-Type: application/json\r\n' if name in json_names else b''
        )
        body += (
            f'--{TOTAL_SIZE_BOUNDARY}\r\n'.encode()
            + f'Content-Disposition: form-data; name="{name}"\r\n'.encode()
            + content_type
            + b'\r\n'
            + content
            + b'\r\n'
        )
    body += f'--{TOTAL_SIZE_BOUNDARY}--\r\n'.encode()
    return body


TOTAL_SIZE_CONTENT_TYPE = (
    'multipart/form-data; boundary=' + TOTAL_SIZE_BOUNDARY
)


async def _fragmented(data, chunk_size):
    for index in range(0, len(data), chunk_size):
        yield data[index : index + chunk_size]


def _make_total_size_handler(limit):
    handler = media.MultipartFormHandler()
    handler.parse_options.max_body_part_total_size = limit
    return handler


def _make_total_size_form(asgi, body, chunk_size, limit=0):
    handler = _make_total_size_handler(limit)
    if asgi:
        from falcon.asgi.reader import BufferedReader as AsyncBufferedReader

        stream = AsyncBufferedReader(_fragmented(body, chunk_size))
        return falcon.async_to_sync(
            handler.deserialize_async,
            stream,
            TOTAL_SIZE_CONTENT_TYPE,
            len(body),
        )

    stream = BufferedReader(
        io.BytesIO(body).read, len(body), chunk_size
    )
    return handler.deserialize(stream, TOTAL_SIZE_CONTENT_TYPE, len(body))


async def _consume_part(asgi, part, strategy):
    if strategy == 'stream-chunks':
        if asgi:
            chunks = [chunk async for chunk in part.stream]
        else:
            chunks = []
            while True:
                chunk = part.stream.read(7)
                if not chunk:
                    break
                chunks.append(chunk)
        return b''.join(chunks)

    if strategy == 'data':
        return await part.data if asgi else part.data

    if strategy == 'text':
        return await part.text if asgi else part.text

    if strategy == 'media':
        return await part.get_media() if asgi else part.get_media()

    raise AssertionError('unknown strategy: {}'.format(strategy))


async def _iterate_total_size_form(asgi, form, read_strategies, skip=False):
    parts = form.__aiter__() if asgi else iter(form)
    collected = {}
    index = 0

    while True:
        try:
            part = await parts.__anext__() if asgi else next(parts)
        except (StopAsyncIteration, StopIteration):
            break

        if skip and index == 1:
            # NOTE: Move on to the next part without reading this one at all;
            #   its content must still be charged against the total budget.
            index += 1
            continue

        strategy = read_strategies[index % len(read_strategies)]
        collected[part.name] = await _consume_part(asgi, part, strategy)
        index += 1

    return collected


@pytest.mark.parametrize(
    'chunk_size',
    [
        len(TOTAL_SIZE_DELIMITER),
        len(TOTAL_SIZE_DELIMITER) + 1,
        37,
        64,
    ],
)
@pytest.mark.parametrize(
    'strategy',
    ['stream-chunks', 'data', 'text'],
)
def test_total_body_part_size_enforced(asgi, chunk_size, strategy):
    body = _build_total_size_form()
    form = _make_total_size_form(asgi, body, chunk_size, TOTAL_SIZE_CONTENT - 1)

    with pytest.raises(falcon.MultipartParseError) as exc_info:
        falcon.async_to_sync(
            _iterate_total_size_form, asgi, form, [strategy]
        )

    assert exc_info.value.description == (
        'total body part content size limit exceeded'
    )


@pytest.mark.parametrize(
    'chunk_size',
    [
        len(TOTAL_SIZE_DELIMITER),
        len(TOTAL_SIZE_DELIMITER) + 1,
        37,
        64,
    ],
)
def test_total_body_part_size_accepts_exact_limit(asgi, chunk_size):
    body = _build_total_size_form()
    form = _make_total_size_form(asgi, body, chunk_size, TOTAL_SIZE_CONTENT)

    collected = falcon.async_to_sync(
        _iterate_total_size_form, asgi, form, ['data']
    )

    assert collected == {
        'first': b'a' * 100,
        'second': b'b' * 50,
        'third': b'c' * 10,
    }


@pytest.mark.parametrize(
    'chunk_size',
    [len(TOTAL_SIZE_DELIMITER), len(TOTAL_SIZE_DELIMITER) + 5, 64],
)
def test_total_body_part_size_shared_across_interfaces(asgi, chunk_size):
    parts = [
        ('first', b'a' * 100),
        ('second', TOTAL_SIZE_JSON_PART),
        ('third', b'c' * 10),
    ]
    content_size = sum(len(content) for _, content in parts)
    body = _build_total_size_form(parts, json_names=('second',))
    form = _make_total_size_form(asgi, body, chunk_size, content_size)

    collected = falcon.async_to_sync(
        _iterate_total_size_form,
        asgi,
        form,
        ['stream-chunks', 'media', 'text'],
    )

    assert collected == {
        'first': b'a' * 100,
        'second': {'answer': 42},
        'third': 'cccccccccc',
    }


@pytest.mark.parametrize(
    'chunk_size',
    [len(TOTAL_SIZE_DELIMITER), len(TOTAL_SIZE_DELIMITER) + 7, 64],
)
def test_total_body_part_size_enforced_via_media(asgi, chunk_size):
    json_parts = [
        ('one', b'{"v": 1, "padding": "aaaaaaaaaaaaaaaaaaaa"}'),
        ('two', b'{"v": 2, "padding": "bbbbbbbbbbbbbbbbbbbb"}'),
        ('three', b'{"v": 3, "padding": "cccccccccccccccccccc"}'),
    ]
    body = _build_total_size_form(
        json_parts, json_names=('one', 'two', 'three')
    )
    limit = sum(len(content) for _, content in json_parts) - 1
    form = _make_total_size_form(asgi, body, chunk_size, limit)

    with pytest.raises(falcon.MultipartParseError) as exc_info:
        falcon.async_to_sync(
            _iterate_total_size_form, asgi, form, ['media']
        )

    assert exc_info.value.description == (
        'total body part content size limit exceeded'
    )


@pytest.mark.parametrize(
    'chunk_size',
    [len(TOTAL_SIZE_DELIMITER), len(TOTAL_SIZE_DELIMITER) + 3, 64],
)
def test_total_body_part_size_skipping_large_part(asgi, chunk_size):
    skipped_size = 500
    body = _build_total_size_form(
        [
            ('small', b'q'),
            ('big', b'B' * skipped_size),
            ('after', b'z'),
        ]
    )
    form = _make_total_size_form(
        asgi, body, chunk_size, skipped_size
    )

    with pytest.raises(falcon.MultipartParseError) as exc_info:
        falcon.async_to_sync(
            _iterate_total_size_form,
            asgi,
            form,
            ['stream-chunks'],
            skip=True,
        )

    assert exc_info.value.description == (
        'total body part content size limit exceeded'
    )


def test_total_body_part_size_skip_under_limit_continues(asgi):
    body = _build_total_size_form(
        [
            ('small', b'q'),
            ('big', b'B' * 100),
            ('after', b'z'),
        ]
    )
    form = _make_total_size_form(asgi, body, 64, 102)

    collected = falcon.async_to_sync(
        _iterate_total_size_form,
        asgi,
        form,
        ['stream-chunks'],
        skip=True,
    )

    assert collected == {'small': b'q', 'after': b'z'}


def test_total_body_part_size_counts_headers_neither_twice(asgi):
    body = _build_total_size_form()
    form = _make_total_size_form(asgi, body, 64, TOTAL_SIZE_CONTENT)

    falcon.async_to_sync(_assert_headers_and_caching_not_double_counted, asgi, form)


async def _assert_headers_and_caching_not_double_counted(asgi, form):
    parts = form.__aiter__() if asgi else iter(form)
    while True:
        try:
            part = await parts.__anext__() if asgi else next(parts)
        except (StopAsyncIteration, StopIteration):
            break

        # NOTE: The part headers add well over a dozen bytes each; if they
        #   were counted, the exact-content limit would be exceeded. Re-reading
        #   cached data/text must not charge the budget a second time either.
        if asgi:
            assert await part.data
            assert await part.data
            assert await part.text
        else:
            assert part.data
            assert part.data
            assert part.text


class _TotalSizeResource:
    def on_post(self, req, resp):
        names = []
        for part in req.media:
            names.append(part.name)
            part.stream.exhaust()

        resp.media = {'names': names}

    async def on_post_async(self, req, resp):
        names = []
        async for part in await req.media:
            names.append(part.name)
            await part.stream.exhaust()

        resp.media = {'names': names}


@pytest.mark.parametrize('limit', [0, 10 * 1024 * 1024])
def test_total_body_part_size_end_to_end(asgi, util, limit):
    handler = _make_total_size_handler(limit)
    app = util.create_app(asgi)
    app.req_options.media_handlers = media.Handlers(
        {falcon.MEDIA_MULTIPART: handler}
    )
    resource = _TotalSizeResource()
    app.add_route('/submit', resource, suffix='async' if asgi else '')
    client = testing.TestClient(app)

    body = _build_total_size_form()
    resp = client.simulate_post(
        '/submit',
        headers={'Content-Type': TOTAL_SIZE_CONTENT_TYPE},
        body=body,
    )

    assert resp.status_code == 200
    assert resp.json == {'names': ['first', 'second', 'third']}


def test_total_body_part_size_end_to_end_rejected(asgi, util):
    handler = _make_total_size_handler(TOTAL_SIZE_CONTENT - 1)
    app = util.create_app(asgi)
    app.req_options.media_handlers = media.Handlers(
        {falcon.MEDIA_MULTIPART: handler}
    )
    resource = _TotalSizeResource()
    app.add_route('/submit', resource, suffix='async' if asgi else '')
    client = testing.TestClient(app)

    body = _build_total_size_form()
    resp = client.simulate_post(
        '/submit',
        headers={'Content-Type': TOTAL_SIZE_CONTENT_TYPE},
        body=body,
    )

    assert resp.status_code == 400
    assert resp.json == {
        'description': 'total body part content size limit exceeded',
        'title': 'Malformed multipart/form-data request media',
    }
