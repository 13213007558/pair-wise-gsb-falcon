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

import re
import string
from typing import Any

from falcon.util.uri import unquote_string

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


def _parse_forwarded_header(
    forwarded: str, strict: bool = False
) -> list[Forwarded] | None:
    """Parse the value of a Forwarded header.

    Makes an effort to parse Forwarded headers as specified by RFC 7239:

    - It checks that every value has valid syntax in general as specified
      in section 4: either a 'token' or a 'quoted-string'.
    - It un-escapes found escape sequences.
    - It does NOT validate 'by' and 'for' contents as specified in section
      6.
    - It does NOT validate 'host' contents (Host ABNF).
    - It does NOT validate 'proto' contents for valid URI scheme names.

    Arguments:
        forwarded (str): Value of a Forwarded header
        strict (bool): When ``True``, do not attempt to recover from
            malformed syntax; return ``None`` instead, so that the caller
            can treat the entire header as untrustworthy rather than
            acting on a partially-parsed value (default ``False``).

    Returns:
        list: Sequence of Forwarded instances, representing each forwarded-element
        in the header, in the same order as they appeared in the header.
        When `strict` is ``True`` and the header value is malformed,
        ``None`` is returned instead.
    """

    elements = []

    pos = 0
    end = len(forwarded)
    need_separator = False
    parsed_element = None

    while 0 <= pos < end:
        match = _FORWARDED_PAIR_RE.match(forwarded, pos)

        if match is not None and not need_separator:  # got a valid forwarded-pair
            pos += len(match.group(0))
            need_separator = True
            parsed_element = _parse_forwarded_pair(match, parsed_element)

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
            if strict:
                return None

            pos = forwarded.find(',', pos)

    # NOTE(kgriffs): Add the last forwarded-element, if any
    if parsed_element:
        elements.append(parsed_element)

    return elements


def _parse_forwarded_pair(
    match: re.Match[str], parsed_element: Forwarded | None
) -> Forwarded:
    """Parse a single forwarded-pair into the given forwarded-element.

    Arguments:
        match (re.Match): Match object for a valid forwarded-pair.
        parsed_element (Forwarded): The forwarded-element under
            construction, or ``None`` if this is the first pair
            encountered for the current forwarded-element.

    Returns:
        Forwarded: The updated forwarded-element.
    """
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

    return parsed_element


def _trusted_chain_suffix(items: list[Any], trusted_hops: int) -> list[Any]:
    """Select the trusted suffix of a forwarded chain.

    Elements of a forwarded chain (such as the elements of a ``Forwarded``
    header, or the addresses in an ``X-Forwarded-For`` header) are appended
    by each proxy in turn, so the rightmost elements were added by the
    proxies closest to the application. When `trusted_hops` proxies are
    trusted, only the rightmost `trusted_hops` elements can be relied upon;
    anything further to the left may have been forged by the client.

    Arguments:
        items (list): Elements of the chain, in the order they appeared
            in the header (leftmost first).
        trusted_hops (int): Number of trusted proxy hops, counting from
            the rightmost (closest to the app) element.

    Returns:
        list: The trusted suffix of `items`, i.e. its rightmost
        `trusted_hops` elements. If the chain contains fewer elements
        than `trusted_hops`, or `trusted_hops` is less than 1, an empty
        list is returned, since in that case no element can be trusted.
    """
    if trusted_hops < 1 or len(items) < trusted_hops:
        return []

    return items[-trusted_hops:]
