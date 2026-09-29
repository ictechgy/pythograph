"""Werkzeug 규칙 문자열(`/items/<int:id>`)을 경로 골격으로 바꾼다.

Werkzeug 3.1.9 `werkzeug/routing/rules.py`(`_part_re`, `parse_converter_args`, `Rule.compile`)와
`werkzeug/routing/converters.py`(기본 변환기)를 옮긴 규칙이다:

- `merge_slashes`(기본 True)면 규칙의 연속 `/`를 하나로 합친다.
- 변환기: `default`·`string`(`[^/]{minlength,maxlength}`, 기본 minlength 1), `int`(`\\d+`, signed면 `-?`),
  `float`(`\\d+\\.\\d+`), `uuid`, `path`(`[^/].*?`, `/`를 넘음), `any(a, b)`(대안), 그리고
  `url_map.converters`에 등록한 사용자 변환기(클래스 `regex`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pythograph.routes.pattern import (
    MAX_EXPANSIONS,
    ExpansionCapped,
    Literal,
    Param,
    Skeleton,
    Unconvertible,
    regex_param,
    unconstrained_param,
)

#: 규칙 문자열 토큰 정규식이다(`werkzeug/routing/rules.py` `_part_re`).
_PART = re.compile(
    r"""
    (?:
        (?P<slash>/)
      |
        (?P<static>[^</]+)
      |
        (?:
          <
            (?:
              (?P<converter>[a-zA-Z_][a-zA-Z0-9_]*)
              (?:\((?P<arguments>.*?)\))?
              :
            )?
            (?P<variable>[a-zA-Z_][a-zA-Z0-9_]*)
          >
        )
    )
    """,
    re.VERBOSE,
)

#: 변환기 인자 정규식이다(`_converter_args_re`).
_ARGUMENT = re.compile(
    r"""
    \s*
    ((?P<name>\w+)\s*=\s*)?
    (?P<value>
        True|False|
        \d+.\d+|
        \d+.|
        \d+|
        [\w\d_.]+|
        [urUR]?(?P<stringval>"[^"]*?"|'[^']*')
    )\s*,
    """,
    re.VERBOSE | re.UNICODE,
)


@dataclass(frozen=True)
class ConverterArguments:
    """변환기 인자."""

    positional: tuple[object, ...]
    keywords: dict[str, object]


def rule_alternatives(rule: str, merge_slashes: bool, custom: dict[str, str | None]) -> list[Skeleton]:
    """Werkzeug 규칙 문자열을 토큰열 대안으로 바꾼다.

    Args:
        rule: 접두사를 합친 전체 규칙(`/`로 시작).
        merge_slashes: 연속 `/`를 합칠지.
        custom: 사용자 변환기 이름 → 정규식(모르면 None).

    Returns:
        토큰열 대안(`any` 변환기를 펼친 결과).

    Raises:
        Unconvertible: Werkzeug가 거부하거나 확정할 수 없는 규칙.
    """
    if not rule.startswith("/"):
        raise Unconvertible("a Flask rule does not start with /, which Werkzeug rejects")
    text = re.sub("/{2,}", "/", rule) if merge_slashes else rule
    alternatives: list[Skeleton] = [()]
    position = 0
    while position < len(text):
        match = _PART.match(text, position)
        if match is None:
            raise Unconvertible("a Flask rule is malformed, which Werkzeug rejects")
        alternatives = _extend(alternatives, _part_options(match, custom))
        position = match.end()
    return alternatives


def _extend(alternatives: list[Skeleton], options: list[Skeleton]) -> list[Skeleton]:
    """대안에 토큰 선택지를 잇는다.

    Args:
        alternatives: 지금까지의 대안.
        options: 이을 선택지.

    Returns:
        이은 대안.

    Raises:
        ExpansionCapped: 16개를 넘을 때.
    """
    if len(alternatives) * len(options) > MAX_EXPANSIONS:
        raise ExpansionCapped("any() converters expand to more than 16 templates")
    return [first + second for first in alternatives for second in options]


def _part_options(match: re.Match[str], custom: dict[str, str | None]) -> list[Skeleton]:
    """규칙 토큰 하나의 선택지를 만든다.

    Args:
        match: `_PART` 매치.
        custom: 사용자 변환기.

    Returns:
        선택지 목록.
    """
    if match.group("slash") is not None:
        return [(Literal("/"),)]
    if match.group("static") is not None:
        return [(Literal(match.group("static")),)]
    name = match.group("converter") or "default"
    arguments = parse_converter_arguments(match.group("arguments") or "")
    return converter_options(name, arguments, custom)


def parse_converter_arguments(text: str) -> ConverterArguments:
    """변환기 인자 문자열을 파이썬 값으로 바꾼다(`parse_converter_args`).

    Args:
        text: 괄호 안 문자열.

    Returns:
        위치·키워드 인자.

    Raises:
        Unconvertible: Werkzeug가 읽지 못하는 인자.
    """
    source = text + ","
    positional: list[object] = []
    keywords: dict[str, object] = {}
    position = 0
    for item in _ARGUMENT.finditer(source):
        if item.start() != position:
            raise Unconvertible("a Flask converter argument cannot be parsed, which Werkzeug rejects")
        value = _pythonize(item.group("stringval") or item.group("value"))
        if item.group("name"):
            keywords[item.group("name")] = value
        else:
            positional.append(value)
        position = item.end()
    if text.strip() and position != len(source):
        raise Unconvertible("a Flask converter argument cannot be parsed, which Werkzeug rejects")
    return ConverterArguments(tuple(positional), keywords)


def _pythonize(value: str) -> object:
    """인자 토큰을 값으로 바꾼다(Werkzeug `_pythonize`와 같다: 상수·정수·실수·따옴표 문자열·그 밖은 문자열).

    Args:
        value: 토큰.

    Returns:
        값.
    """
    constants: dict[str, object] = {"None": None, "True": True, "False": False}
    if value in constants:
        return constants[value]
    for convert in (int, float):
        try:
            return convert(value)
        except ValueError:
            continue
    if value[:1] == value[-1:] and value[:1] in ('"', "'"):
        value = value[1:-1]
    return str(value)


def converter_options(name: str, arguments: ConverterArguments, custom: dict[str, str | None]) -> list[Skeleton]:
    """변환기 하나를 토큰 선택지로 바꾼다.

    Args:
        name: 변환기 이름.
        arguments: 변환기 인자.
        custom: 사용자 변환기.

    Returns:
        선택지 목록.

    Raises:
        Unconvertible: 알 수 없는 변환기·인자.
    """
    if name in custom:
        regex = custom[name]
        if regex is None:
            raise Unconvertible("a custom Flask converter has a regex that is not a literal")
        return [(regex_param(regex),)]
    if name in ("default", "string"):
        return [(_string_param(arguments),)]
    if name == "int":
        return [(Param(kind="int", pattern=None, may_be_empty=False, crosses_slash=False),)]
    if name == "float":
        signed = bool(arguments.keywords.get("signed", False))
        pattern = r"-?\d+\.\d+" if signed else r"\d+\.\d+"
        return [(Param(kind="regex", pattern=pattern, may_be_empty=False, crosses_slash=False),)]
    if name == "uuid":
        return [(Param(kind="uuid", pattern=None, may_be_empty=False, crosses_slash=False),)]
    if name == "path":
        return [(Param(kind="path", pattern=None, may_be_empty=False, crosses_slash=True),)]
    if name == "any":
        items = [str(item) for item in arguments.positional]
        if not items or len(items) > MAX_EXPANSIONS:
            raise ExpansionCapped("an any() converter expands to more than 16 templates")
        return [(Literal(item),) for item in items]
    raise Unconvertible("a Flask rule uses a converter that is not registered statically")


def _string_param(arguments: ConverterArguments) -> Param:
    """`string`(`UnicodeConverter`) 인자로 파라미터를 만든다.

    Args:
        arguments: 변환기 인자(`minlength=1`, `maxlength=None`, `length=None`).

    Returns:
        파라미터.

    Raises:
        Unconvertible: 인자가 숫자가 아닐 때.
    """
    names = ("minlength", "maxlength", "length")
    values = dict(zip(names, arguments.positional, strict=False))
    values.update({key: value for key, value in arguments.keywords.items() if key in names})
    minimum, maximum, length = values.get("minlength", 1), values.get("maxlength"), values.get("length")
    for value in (minimum, maximum, length):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            raise Unconvertible("a string converter length is not an integer")
    if length is None and minimum == 1 and maximum is None:
        return unconstrained_param()
    quantifier = f"{{{length}}}" if length is not None else f"{{{minimum},{'' if maximum is None else maximum}}}"
    empty = (length == 0) if length is not None else minimum == 0
    return Param(kind="regex", pattern=f"[^/]{quantifier}", may_be_empty=bool(empty), crosses_slash=False)
