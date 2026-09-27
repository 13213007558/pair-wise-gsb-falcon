# Based on code from the aiohttp project, Copyright 2013-2017 by Nikolay Kim and
# Andrew Svetlov, with modifications for the Falcon project by Kurt Griffiths.
#
# See also:
#
#   https://github.com/aio-libs/aiohttp/blob/master/aiohttp/web_request.py
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations

import logging
import re
import string

from falcon.util.uri import unquote_string

_logger = logging.getLogger('falcon')

# Maximum accepted size, in characters, of a Forwarded header field value.
#   Values larger than this are rejected wholesale (see below) in order to
#   bound the parsing cost of attacker-controlled input, and to avoid
#   deriving client connection info from a value that a size-conscious
#   upstream proxy may have truncated or refused to validate.
_MAX_FORWARDED_HEADER_SIZE = 8192

# '-' at the end to prevent interpretation as range in a char class
_TCHAR = string.digits + string.ascii_letters + r"!#$%&'*+.^_`|~-"

_TOKEN = r'[{tchar}]+'.format(tchar=_TCHAR)

# qdtext includes 0x5C to escape 0x5D ('\]')
# qdtext excludes obs-text (because obsoleted, and encoding not specified)
_QDTEXT = r'[{0}]'.format(
    r''.join(chr(c) for c in (0x09, 0x20, 0x21) + tuple(range(0x23, 0x7F)))
)

_QUOTED_PAIR = r'\\[\t !-~]'

_QUOTED_STRING = r'"(?:{quoted_pair}|{qdtext})*"'.format(
    qdtext=_QDTEXT, quoted_pair=_QUOTED_PAIR
)

_FORWARDED_PAIR = r'({token})=({token}|{quoted_string})'.format(
    token=_TOKEN, quoted_string=_QUOTED_STRING
)

# same pattern as _QUOTED_PAIR but contains a capture group
_QUOTED_PAIR_REPLACE_RE = re.compile(r'\\([\t !-~])')

_FORWARDED_PAIR_RE = re.compile(_FORWARDED_PAIR)


class Forwarded:
    """Represents a parsed Forwarded header.

    (See also: RFC 7239, Section 4)
    """

    # NOTE(kgriffs): Use "src" since "for" is a keyword, and
    # "scheme" instead of "proto" to be consistent with the
    # falcon.Request interface.
    __slots__ = ('src', 'dest', 'host', 'scheme')

    src: str | None
    """The value of the "for" parameter, or ``None`` if the parameter is absent.

    Identifies the node making the request to the proxy.
    """
    dest: str | None
    """The value of the "by" parameter, or ``None`` if the parameter is absent.

    Identifies the client-facing interface of the proxy.
    """
    host: str | None
    """The value of the "host" parameter, or ``None`` if the parameter is absent.

    Provides the host request header field as received by the proxy.
    """
    scheme: str | None
    """The value of the "proto" parameter, or ``None`` if the parameter is absent.

    Indicates the protocol that was used to make the request to the proxy.
    """

    def __init__(self) -> None:
        self.src = None
        self.dest = None
        self.host = None
        self.scheme = None


def _parse_forwarded_header(forwarded: str) -> list[Forwarded]:
    """Parse the value of a Forwarded header.

    Makes an effort to parse Forwarded headers as specified by RFC 7239:

    - It checks that every value has valid syntax in general as specified
      in section 4: either a 'token' or a 'quoted-string'.
    - It un-escapes found escape sequences.
    - It does NOT validate 'by' and 'for' contents as specified in section
      6.
    - It does NOT validate 'host' contents (Host ABNF).
    - It does NOT validate 'proto' contents for valid URI scheme names.

    If the header value exceeds a reasonable size, it is rejected
    wholesale (fail-closed) and an empty list is returned, rather than
    attempting to salvage potentially untrustworthy elements.

    Arguments:
        forwarded (str): Value of a Forwarded header

    Returns:
        list: Sequence of Forwarded instances, representing each forwarded-element
        in the header, in the same order as they appeared in the header.
    """

    if len(forwarded) > _MAX_FORWARDED_HEADER_SIZE:
        # Fail closed: the raw value is untrusted input, so it is
        #   deliberately not included in the log message.
        _logger.warning(
            'Ignoring an oversized Forwarded header value '
            '(%d characters exceeds the limit of %d).',
            len(forwarded),
            _MAX_FORWARDED_HEADER_SIZE,
        )
        return []

    return _parse_forwarded_elements(forwarded)


def _parse_forwarded_elements(forwarded: str) -> list[Forwarded]:
    """Parse forwarded-elements from a size-checked Forwarded header value."""
    elements = []

    pos = 0
    end = len(forwarded)
    need_separator = False
    parsed_element = None

    while 0 <= pos < end:
        match = _FORWARDED_PAIR_RE.match(forwarded, pos)

        if match is not None:  # got a valid forwarded-pair
            if need_separator:
                # bad syntax here, skip to next comma
                pos = forwarded.find(',', pos)

            else:
                pos += len(match.group(0))
                need_separator = True

                name, value = match.groups()

                # NOTE(kgriffs): According to RFC 7239, parameter
                # names are case-insensitive.
                name = name.lower()

                if value[0] == '"':
                    value = unquote_string(value)

                # NOTE(kgriffs): If this is the first pair we've encountered
                # for this forwarded-element, initialize a new object.
                if not parsed_element:
                    parsed_element = Forwarded()

                if name == 'by':
                    parsed_element.dest = value
                elif name == 'for':
                    parsed_element.src = value
                elif name == 'host':
                    parsed_element.host = value
                elif name == 'proto':
                    # NOTE(kgriffs): RFC 7239 only requires that
                    # the "proto" value conform to the Host ABNF
                    # described in RFC 7230. The Host ABNF, in turn,
                    # does not require that the scheme be in any
                    # particular case, so we normalize it here to be
                    # consistent with the WSGI spec that *does*
                    # require the value of 'wsgi.url_scheme' to be
                    # either 'http' or 'https' (case-sensitive).
                    parsed_element.scheme = value.lower()

        elif forwarded[pos] == ',':  # next forwarded-element
            need_separator = False
            pos += 1

            # NOTE(kgriffs): It's possible that we arrive here without a
            # parsed element if the header is malformed.
            if parsed_element:
                elements.append(parsed_element)
                parsed_element = None

        elif forwarded[pos] == ';':  # next forwarded-pair
            need_separator = False
            pos += 1

        elif forwarded[pos] in ' \t':
            # Allow whitespace even between forwarded-pairs, though
            # RFC 7239 doesn't. This simplifies code and is in line
            # with Postel's law.
            pos += 1

        else:
            # bad syntax here, skip to next comma
            pos = forwarded.find(',', pos)

    # NOTE(kgriffs): Add the last forwarded-element, if any
    if parsed_element:
        elements.append(parsed_element)

    return elements


def _trusted_hop_index(chain_length: int, trusted_proxies: int) -> int:
    """Compute the index of the client info hop within a proxy chain.

    A chain is ordered from the original client (leftmost) towards the
    server, and `chain_length` is assumed to include the server's own hop
    as the final element. The `trusted_proxies` rightmost hops are
    considered to be under the operator's control, so the returned index
    points to the hop recorded by the outermost trusted proxy, i.e. the
    first hop to the left of the trusted proxies. Its value describes
    the client as seen by the trusted chain; any elements further to
    the left may have been forged by the client or by an untrusted
    proxy, and must not be used to derive client connection info.

    If the chain contains fewer hops than the number of trusted proxies,
    all the recorded hops were written by trusted proxies, so the
    leftmost one (index ``0``) is returned.

    Arguments:
        chain_length (int): Number of hops in the chain, including the
            server's own hop as the final element.
        trusted_proxies (int): Number of trusted rightmost proxy hops.
            Must be greater than zero.

    Returns:
        int: Index of the hop holding the client connection info.
    """
    return max(chain_length - trusted_proxies - 1, 0)
