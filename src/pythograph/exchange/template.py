"""isthmus 정규 경로 템플릿 문법 검사와 리터럴 정규화.

정본은 isthmus `docs/GRAPH-EXCHANGE.md`의 "정규 경로 템플릿" 절이다. isthmus는 문법만 검사하고
어긴 문서를 다시 정규화하지 않고 거부하므로, 생산자인 pythograph가 여기서 같은 규칙으로 만들고
확인한다. 거부 사유 코드는 소비자(isthmus `src/exchange/route-template.ts`)와 같은 순서로 판정한다.
"""

from __future__ import annotations

import re

#: 정규 템플릿의 최대 길이다. 소비자가 더 긴 템플릿을 거부한다.
MAX_TEMPLATE_LENGTH = 2048

#: RFC 3986 unreserved 문자다. 인코딩하면 안 된다.
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

#: 세그먼트 리터럴로 그대로 쓸 수 있는 문자(RFC 3986 pchar에서 pct-encoded를 뺀 것)다.
_LITERAL = _UNRESERVED | frozenset("!$&'()*+,;=:@")

#: 퍼센트 인코딩 뒤 두 글자가 16진수인지 보는 정규식이다.
_HEX_PAIR = re.compile(r"[0-9A-Fa-f]{2}")


def template_problem(template: str) -> str | None:
    """템플릿이 정규 문법을 어기면 소비자와 같은 거부 사유 코드를 돌려준다.

    Args:
        template: 검사할 템플릿 문자열.

    Returns:
        정규 템플릿이면 None, 아니면 `not-rooted`·`too-long`·`invalid-character`·
        `malformed-percent`·`lowercase-percent-hex`·`encoded-unreserved`·`stray-brace`·
        `multiple-parameters`·`catch-all-partial`·`catch-all-not-last` 중 하나.
    """
    if len(template) > MAX_TEMPLATE_LENGTH:
        return "too-long"
    if not template.startswith("/"):
        return "not-rooted"
    segments = template[1:].split("/")
    for index, segment in enumerate(segments):
        problem = _segment_problem(segment)
        if problem is not None:
            return problem
        if segment == "{**}" and index != len(segments) - 1:
            return "catch-all-not-last"
    return None


def is_canonical_template(template: str) -> bool:
    """템플릿이 정규 문법을 지키는지 돌려준다.

    Args:
        template: 검사할 템플릿.

    Returns:
        정규 템플릿이면 True.
    """
    return template_problem(template) is None


def _segment_problem(segment: str) -> str | None:
    """세그먼트 하나의 거부 사유를 찾는다.

    Args:
        segment: `/` 사이의 세그먼트 문자열.

    Returns:
        문제가 없으면 None, 있으면 거부 사유 코드.
    """
    if segment == "{**}":
        return None
    has_parameter = False
    index = 0
    while index < len(segment):
        character = segment[index]
        if character == "{":
            if segment.startswith("{**}", index):
                return "catch-all-partial"
            if segment[index + 1 : index + 2] != "}":
                return "stray-brace"
            if has_parameter:
                return "multiple-parameters"
            has_parameter = True
            index += 2
            continue
        if character == "}":
            return "stray-brace"
        if character == "%":
            problem = _percent_problem(segment, index)
            if problem is not None:
                return problem
            index += 3
            continue
        if character not in _LITERAL:
            return "invalid-character"
        index += 1
    return None


def _percent_problem(segment: str, index: int) -> str | None:
    """`%` 위치의 퍼센트 인코딩이 정규형인지 검사한다.

    Args:
        segment: 세그먼트 문자열.
        index: `%`의 위치.

    Returns:
        정규형이면 None, 아니면 거부 사유 코드.
    """
    pair = segment[index + 1 : index + 3]
    if len(pair) != 2 or _HEX_PAIR.fullmatch(pair) is None:
        return "malformed-percent"
    if pair != pair.upper():
        return "lowercase-percent-hex"
    if chr(int(pair, 16)) in _UNRESERVED:
        return "encoded-unreserved"
    return None


def normalize_uri_path(path: str) -> str:
    """이미 URI 공간(퍼센트 인코딩된)인 경로를 정규 리터럴 형태로 바꾼다.

    대문자 hex로 맞추고, unreserved 문자의 인코딩은 풀고, pchar가 아닌 문자는 UTF-8로 인코딩한다.
    `%2F`처럼 인코딩된 구분자는 한 세그먼트 안에 그대로 둔다. 공유 벡터 `template.normalize`가
    검증하는 규칙이다.

    Args:
        path: `/`로 시작하는 URI 경로.

    Returns:
        정규화한 경로.
    """
    output: list[str] = []
    index = 0
    while index < len(path):
        character = path[index]
        pair = path[index + 1 : index + 3]
        if character == "%" and _HEX_PAIR.fullmatch(pair) is not None:
            decoded = chr(int(pair, 16))
            output.append(decoded if decoded in _UNRESERVED else f"%{pair.upper()}")
            index += 3
            continue
        output.append(character if character == "/" or character in _LITERAL else _encode_character(character))
        index += 1
    return "".join(output)


def encode_decoded_literal(text: str) -> str:
    """프레임워크가 디코드된 경로에 맞추는 리터럴을 정규 리터럴로 인코딩한다.

    Django와 Werkzeug는 퍼센트 디코드한 요청 경로(`PATH_INFO`)에 라우트 리터럴을 맞춘다. 그래서
    라우트 문자열의 `%`는 인코딩 표기가 아니라 글자 그대로이며 `%25`가 된다. `/`는 구분자로 둔다.

    Args:
        text: 디코드된 공간의 리터럴(세그먼트 구분자 `/` 포함 가능).

    Returns:
        정규 리터럴 문자열.
    """
    return "".join(
        character if character == "/" or character in _LITERAL else _encode_character(character) for character in text
    )


def _encode_character(character: str) -> str:
    """문자 하나를 UTF-8 퍼센트 인코딩(대문자 hex)으로 바꾼다.

    Args:
        character: 인코딩할 문자. UTF-8로 쓸 수 없는 짝 없는 서로게이트는 U+FFFD로 바꾼다.

    Returns:
        `%XX` 나열.
    """
    safe = "\ufffd" if 0xD800 <= ord(character) <= 0xDFFF else character
    encoded = safe.encode("utf-8")
    return "".join(f"%{byte:02X}" for byte in encoded)
