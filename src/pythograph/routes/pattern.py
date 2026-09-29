"""라우트 경로 골격(리터럴·파라미터 토큰열)과 정규 템플릿 변환.

프레임워크 추출기는 경로를 토큰열(`Skeleton`)로 만든다. Django `re_path`·DRF 라우터의 정규식은
표준 라이브러리 정규식 파서(`re._parser`, 3.10은 `sre_parse`)의 구문 트리를 읽어 토큰열로 바꾼다.
정규식을 실행하지 않고 구조만 본다. 이 모듈은 토큰열을 isthmus 정규 템플릿으로 바꾸는 공통 규칙도 담는다.

변환 규칙(isthmus `docs/GRAPH-EXCHANGE.md` "정규 경로 템플릿"):
- 세그먼트 전체 파라미터는 `{}`, 일부면 리터럴 골격(`files/{}.json`), 한 세그먼트에 둘 이상이면 dynamic.
- `/`를 넘을 수 있는 파라미터는 마지막 세그먼트 전체일 때만 `{**}`이고, 그 밖은 dynamic.
- 빈 값을 받는 파라미터는 빈 값으로 채운 변형 템플릿을 함께 낸다(계약의 "빈 값 변형").
- 선택 리터럴·대안·유한 문자 집합은 템플릿 여러 개로 펼치고 16개를 넘으면 dynamic.
- 끝 슬래시만 다른 두 변형은 하나로 합쳐 `trailingSlash: "optional"`로 낸다.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from itertools import product
from typing import Any

from pythograph.exchange.template import MAX_TEMPLATE_LENGTH, encode_decoded_literal, template_problem
from pythograph.routes.model import ParamConstraint, RouteShape

if sys.version_info >= (3, 11):  # pragma: no cover - 실행 중인 파이썬 버전에 따라 한쪽만 탄다.
    import re._parser as _sre_parse
else:  # pragma: no cover
    import sre_parse as _sre_parse

#: 선택 세그먼트·대안을 펼치는 최대 개수다(계약 상한).
MAX_EXPANSIONS = 16

#: 정규식 IGNORECASE 플래그 값이다.
_IGNORECASE = 2

#: 파라미터 종류 판별에 쓰는 ASCII 문자 집합이다.
_ASCII = [chr(code) for code in range(128)]
_DIGITS = frozenset("0123456789")
_SLUG = frozenset("-_0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_NOT_SLASH = frozenset(_ASCII) - {"/"}

#: 알려진 UUID 정규식 표기(Django `UUIDConverter`, Werkzeug `UUIDConverter`)다.
UUID_PATTERNS = frozenset(
    {
        "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        "[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}",
    }
)


@dataclass(frozen=True)
class Literal:
    """디코드된 공간의 리터럴 문자열(`/` 포함 가능)."""

    text: str


@dataclass(frozen=True)
class Param:
    """경로 파라미터 자리.

    Attributes:
        kind: `int`·`uuid`·`slug`·`regex`·`path` 또는 None(제약 없음, `[^/]+`).
        pattern: `regex` 종류의 정보용 정규식 원문.
        may_be_empty: 빈 문자열과 맞을 수 있는지.
        crosses_slash: `/`와 맞을 수 있는지(catch-all 후보).
    """

    kind: str | None
    pattern: str | None
    may_be_empty: bool
    crosses_slash: bool


#: 토큰 하나다.
Token = Literal | Param

#: 토큰열이다.
Skeleton = tuple[Token, ...]


class Unconvertible(Exception):
    """정규 템플릿으로 확정할 수 없다. 메시지는 limitation용 이유 종류다."""


class ExpansionCapped(Unconvertible):
    """펼친 템플릿이 상한(16개)을 넘었다."""


@dataclass(frozen=True)
class RegexPieces:
    """정규식 하나를 토큰열로 바꾼 결과.

    Attributes:
        alternatives: 가능한 토큰열 목록(펼친 결과).
    """

    alternatives: tuple[Skeleton, ...]


def unconstrained_param() -> Param:
    """`[^/]+`(Django `str`, Werkzeug 기본) 파라미터를 만든다.

    Returns:
        제약 없는 한 세그먼트 파라미터.
    """
    return Param(kind=None, pattern=None, may_be_empty=False, crosses_slash=False)


def regex_param(pattern: str) -> Param:
    """정규식 원문 하나를 파라미터로 분석한다(변환기 정규식 등).

    Args:
        pattern: 파라미터 자리의 정규식(앵커 없음).

    Returns:
        분석한 파라미터.

    Raises:
        Unconvertible: 정규식을 파싱할 수 없거나 분석할 수 없는 구문을 쓸 때.
    """
    items = _parse(pattern)
    options = _sequence(items, capture_group_texts(pattern))
    if len(options) == 1 and len(options[0]) == 1 and isinstance(options[0][0], Param):
        return _annotate(options[0][0], pattern)
    return _param_from_items(items, pattern)


def convert_regex(pattern: str, *, endpoint: bool, anchored_by_fullmatch: bool) -> RegexPieces:
    """Django `RegexPattern` 정규식을 토큰열로 바꾼다.

    Django는 endpoint 정규식이 텍스트상 `$`로 끝나면 `fullmatch`, 아니면 `search`로 맞춘다(
    `django/urls/resolvers.py` `RegexPattern.match`). 그래서 시작이 `^`로 고정되지 않은 `search`는 앞에
    무엇이든 올 수 있어 확정할 수 없고, 끝이 열린 endpoint도 확정할 수 없다.

    Args:
        pattern: 정규식 원문.
        endpoint: 뷰에 닿는 패턴인지(아니면 include 접두사).
        anchored_by_fullmatch: 이 패턴을 `fullmatch`로 맞추는지.

    Returns:
        펼친 토큰열 목록.

    Raises:
        Unconvertible: 확정할 수 없을 때.
    """
    items = _parse(pattern)
    names = [_op(op) for op, _ in items]
    starts = bool(items) and names[0] == "AT" and _at_name(items[0][1]) in ("AT_BEGINNING", "AT_BEGINNING_STRING")
    ends = bool(items) and names[-1] == "AT" and _at_name(items[-1][1]) in ("AT_END", "AT_END_STRING")
    if not (starts or anchored_by_fullmatch):
        raise Unconvertible("an unanchored regular expression can match after any prefix")
    if endpoint and not (ends or anchored_by_fullmatch):
        raise Unconvertible("an endpoint regular expression without an end anchor matches longer paths")
    if not endpoint and ends:
        raise Unconvertible("an include regular expression ends with an end anchor")
    body = items[1 if starts else 0 : len(items) - 1 if ends else len(items)]
    return RegexPieces(tuple(_sequence(body, capture_group_texts(pattern))))


def _parse(pattern: str) -> list[tuple[Any, Any]]:
    """정규식을 구문 트리 항목 목록으로 파싱한다.

    Args:
        pattern: 정규식 원문.

    Returns:
        (연산, 인자) 목록.

    Raises:
        Unconvertible: 파싱할 수 없거나 대소문자 무시 플래그를 쓸 때.
    """
    try:
        parsed: Any = _sre_parse.parse(pattern, 0)
    except Exception as error:  # 정규식 파서는 re.error 외에도 다양한 예외를 낸다.
        raise Unconvertible("the regular expression does not parse") from error
    if parsed.state.flags & _IGNORECASE:
        raise Unconvertible("a case-insensitive regular expression is not modeled")
    items: list[tuple[Any, Any]] = [(op, av) for op, av in parsed]
    return items


def _op(op: Any) -> str:
    """구문 트리 연산의 이름을 돌려준다(파이썬 버전 사이에 안정적인 비교용).

    Args:
        op: 연산 상수.

    Returns:
        연산 이름.
    """
    return str(getattr(op, "name", op))


def _at_name(av: Any) -> str:
    """`AT` 연산의 인자(앵커 종류) 이름을 돌려준다.

    Args:
        av: 앵커 상수.

    Returns:
        앵커 이름.
    """
    return str(getattr(av, "name", av))


def _sequence(items: list[tuple[Any, Any]], groups: dict[int, str]) -> list[Skeleton]:
    """구문 트리 항목열을 토큰열 대안 목록으로 바꾼다.

    Args:
        items: (연산, 인자) 목록.
        groups: 캡처 묶음 번호 → 정규식 원문.

    Returns:
        토큰열 대안 목록.

    Raises:
        Unconvertible: 확정할 수 없는 구문.
    """
    alternatives: list[Skeleton] = [()]
    for item in items:
        options = _item_options(item, groups)
        alternatives = _cross(alternatives, options)
    return alternatives


def _cross(left: list[Skeleton], right: list[Skeleton]) -> list[Skeleton]:
    """두 대안 목록의 모든 이음을 만든다. 상한을 넘으면 실패한다.

    Args:
        left: 앞 대안 목록.
        right: 뒤 대안 목록.

    Returns:
        이은 대안 목록.

    Raises:
        ExpansionCapped: 16개를 넘을 때.
    """
    if len(left) * len(right) > MAX_EXPANSIONS:
        raise ExpansionCapped("optional parts expand to more than 16 templates")
    return [first + second for first, second in product(left, right)]


def _item_options(item: tuple[Any, Any], groups: dict[int, str]) -> list[Skeleton]:
    """구문 트리 항목 하나의 토큰열 대안을 만든다.

    Args:
        item: (연산, 인자).
        groups: 캡처 묶음 번호 → 정규식 원문.

    Returns:
        토큰열 대안 목록.

    Raises:
        Unconvertible: 확정할 수 없는 구문.
    """
    op, av = item
    name = _op(op)
    if name == "LITERAL":
        return [(Literal(chr(av)),)]
    if name == "IN":
        literals = _finite_literals(av)
        if literals is not None and len(literals) <= MAX_EXPANSIONS:
            return [(Literal(character),) for character in literals]
        return [(_param_from_items([item], None),)]
    if name in ("NOT_LITERAL", "ANY"):
        return [(_param_from_items([item], None),)]
    if name in ("MAX_REPEAT", "MIN_REPEAT"):
        return _repeat_options(item)
    if name == "SUBPATTERN":
        return _group_options(av, groups)
    if name == "BRANCH":
        options: list[Skeleton] = []
        for branch in av[1]:
            options.extend(_sequence(list(branch), groups))
        if len(options) > MAX_EXPANSIONS:
            raise ExpansionCapped("alternatives expand to more than 16 templates")
        return options
    raise Unconvertible(f"the regular expression uses {name.lower()}, which is not modeled")


def _finite_literals(set_items: list[tuple[Any, Any]]) -> list[str] | None:
    """문자 집합이 리터럴 문자만 나열하면 그 문자들을 돌려준다.

    Args:
        set_items: `IN` 인자.

    Returns:
        문자 목록, 부정·범위·범주가 있으면 None.
    """
    characters: list[str] = []
    for op, av in set_items:
        if _op(op) != "LITERAL":
            return None
        characters.append(chr(av))
    return characters


def _repeat_options(item: tuple[Any, Any]) -> list[Skeleton]:
    """반복 항목의 대안을 만든다. 리터럴의 `?`와 고정 횟수 반복은 펼치고 나머지는 파라미터다.

    Args:
        item: 반복 항목.

    Returns:
        토큰열 대안 목록.
    """
    minimum, maximum, sub = item[1]
    inner = list(sub)
    literal_text = _pure_literal(inner)
    if literal_text is not None and minimum == 0 and maximum == 1:
        return [(), (Literal(literal_text),)]
    if literal_text is not None and minimum == maximum and minimum * len(literal_text) <= MAX_TEMPLATE_LENGTH:
        return [(Literal(literal_text * minimum),)]
    return [(_param_from_items([item], None),)]


def _pure_literal(items: list[tuple[Any, Any]]) -> str | None:
    """항목열이 리터럴 문자만이면 그 문자열을 돌려준다.

    Args:
        items: 항목열.

    Returns:
        문자열 또는 None.
    """
    if not items or any(_op(op) != "LITERAL" for op, _ in items):
        return None
    return "".join(chr(av) for _, av in items)


def _group_options(av: Any, groups: dict[int, str]) -> list[Skeleton]:
    """괄호 묶음의 대안을 만든다.

    묶음은 매칭 집합을 바꾸지 않으므로 안쪽을 그대로 펼친다. 다만 캡처 묶음 안쪽이 파라미터 여러 개로
    나뉘면(`(?P<x>[a-z]+-[0-9]+)`) 한 세그먼트에 파라미터가 둘이 되어 dynamic이 되므로, `/`를 넘지 않는
    한 묶음 전체를 파라미터 하나로 본다. 묶음 전체가 파라미터 하나면 원문을 정보 필드로 붙인다.

    Args:
        av: `SUBPATTERN` 인자(묶음 번호, 추가 플래그, 제거 플래그, 하위 패턴).
        groups: 캡처 묶음 번호 → 정규식 원문.

    Returns:
        토큰열 대안 목록.

    Raises:
        Unconvertible: 묶음에 대소문자 무시 플래그가 있을 때.
    """
    group, add_flags, _, sub = av
    if add_flags & _IGNORECASE:
        raise Unconvertible("a case-insensitive regular expression is not modeled")
    inner = list(sub)
    text = groups.get(group) if group is not None else None
    try:
        options = _sequence(inner, groups)
    except ExpansionCapped:
        options = []
    if len(options) == 1 and len(options[0]) == 1 and isinstance(options[0][0], Param):
        return [(_annotate(options[0][0], text),)]
    if options and all(sum(isinstance(token, Param) for token in option) <= 1 for option in options):
        return options
    collapsed = _param_from_items(inner, text)
    if collapsed.crosses_slash and options:
        return options
    return [(collapsed,)]


def _annotate(param: Param, text: str | None) -> Param:
    """묶음 원문으로 파라미터 종류를 보정한다(알려진 UUID 정규식, `regex` 정보 필드).

    Args:
        param: 구조로 판별한 파라미터.
        text: 묶음 정규식 원문.

    Returns:
        보정한 파라미터.
    """
    if text is None or param.crosses_slash:
        return param
    if text in UUID_PATTERNS:
        return replace(param, kind="uuid", pattern=None)
    return replace(param, pattern=text) if param.kind == "regex" else param


def capture_group_texts(pattern: str) -> dict[int, str]:
    """정규식의 캡처 묶음 번호 → 안쪽 원문을 구한다(정보 필드와 UUID 판별용).

    이스케이프와 문자 집합(`[...]`)을 건너뛰며 괄호를 맞춘다. `(?P<name>`와 맨 `(`만 캡처다.

    Args:
        pattern: 정규식 원문.

    Returns:
        묶음 번호 → 원문.
    """
    texts: dict[int, str] = {}
    stack: list[tuple[int | None, int]] = []
    number = 0
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "\\":
            index += 2
            continue
        if character == "[":
            index = _skip_class(pattern, index)
            continue
        if character == "(":
            group, start = _open_group(pattern, index, number)
            if group is not None:
                number = group
            stack.append((group, start))
            index = start
            continue
        if character == ")" and stack:
            group, start = stack.pop()
            if group is not None:
                texts[group] = pattern[start:index]
        index += 1
    return texts


def _skip_class(pattern: str, index: int) -> int:
    """문자 집합 `[...]`의 끝 다음 위치를 돌려준다.

    Args:
        pattern: 정규식 원문.
        index: `[` 위치.

    Returns:
        `]` 다음 위치(닫히지 않으면 끝).
    """
    position = index + 1
    if position < len(pattern) and pattern[position] == "^":
        position += 1
    if position < len(pattern) and pattern[position] == "]":
        position += 1
    while position < len(pattern) and pattern[position] != "]":
        position += 2 if pattern[position] == "\\" else 1
    return position + 1


def _open_group(pattern: str, index: int, number: int) -> tuple[int | None, int]:
    """여는 괄호의 캡처 번호와 안쪽 시작 위치를 구한다.

    Args:
        pattern: 정규식 원문.
        index: `(` 위치.
        number: 지금까지의 캡처 묶음 수.

    Returns:
        (캡처 번호 또는 None, 안쪽 시작 위치).
    """
    if not pattern.startswith("(?", index):
        return number + 1, index + 1
    if pattern.startswith("(?P<", index):
        close = pattern.find(">", index)
        return number + 1, (close + 1 if close >= 0 else len(pattern))
    return None, index + 1


def _param_from_items(items: list[tuple[Any, Any]], pattern: str | None) -> Param:
    """항목열이 맞추는 값의 성질로 파라미터를 만든다.

    Args:
        items: 파라미터 자리의 항목열.
        pattern: 정규식 원문(있으면 `regex` 종류의 정보 필드와 UUID 판별에 쓴다).

    Returns:
        파라미터.

    Raises:
        Unconvertible: 분석할 수 없는 구문(역참조·전후방 탐색 등).
    """
    crosses = _may_match(items, "/")
    empty = _min_width(items) == 0
    if crosses:
        return Param(kind="path", pattern=None, may_be_empty=empty, crosses_slash=True)
    kind = _classify(items, pattern)
    return Param(kind=kind, pattern=pattern if kind == "regex" else None, may_be_empty=empty, crosses_slash=False)


def _classify(items: list[tuple[Any, Any]], pattern: str | None) -> str | None:
    """`/`를 넘지 않는 파라미터의 계약 종류를 정한다.

    `X+` 모양이고 X가 받는 ASCII 문자 집합이 숫자면 `int`, `[-A-Za-z0-9_]`면 `slug`, `/`를 뺀 전부면
    제약 없음(None)이다. 알려진 UUID 정규식이면 `uuid`, 그 밖은 `regex`다. ASCII 밖 문자는 요청에서
    퍼센트 인코딩되어 소비자가 평가하지 않으므로 판별에서 뺀다.

    Args:
        items: 항목열.
        pattern: 정규식 원문.

    Returns:
        종류 문자열 또는 None.
    """
    if pattern is not None and pattern in UUID_PATTERNS:
        return "uuid"
    if len(items) == 1 and _op(items[0][0]) == "MAX_REPEAT":
        minimum, maximum, sub = items[0][1]
        inner = list(sub)
        if minimum == 1 and _is_unbounded(maximum) and len(inner) == 1:
            accepted = frozenset(character for character in _ASCII if _single_accepts(inner[0], character))
            if accepted == _DIGITS:
                return "int"
            if accepted == _SLUG:
                return "slug"
            if accepted == _NOT_SLASH:
                return None
    return "regex"


def _is_unbounded(maximum: Any) -> bool:
    """반복 상한이 무한(`MAXREPEAT`)인지 확인한다.

    Args:
        maximum: 반복 상한.

    Returns:
        무한이면 True.
    """
    return bool(maximum >= 65535)


def _single_accepts(item: tuple[Any, Any], character: str) -> bool:
    """한 글자짜리 항목이 문자를 받는지 판정한다.

    Args:
        item: `LITERAL`·`NOT_LITERAL`·`ANY`·`IN` 항목.
        character: 검사할 문자.

    Returns:
        받으면 True(다른 연산은 False).
    """
    op, av = item
    name = _op(op)
    if name == "LITERAL":
        return bool(chr(av) == character)
    if name == "NOT_LITERAL":
        return bool(chr(av) != character)
    if name == "ANY":
        return character != "\n"
    if name == "IN":
        return _set_accepts(av, character)
    return False


def _set_accepts(set_items: list[tuple[Any, Any]], character: str) -> bool:
    """문자 집합(`IN`)이 문자를 받는지 판정한다.

    Args:
        set_items: 집합 항목.
        character: 검사할 문자.

    Returns:
        받으면 True.
    """
    negated = False
    matched = False
    for op, av in set_items:
        name = _op(op)
        if name == "NEGATE":
            negated = True
        elif name == "LITERAL":
            matched = matched or chr(av) == character
        elif name == "RANGE":
            matched = matched or av[0] <= ord(character) <= av[1]
        elif name == "CATEGORY":
            matched = matched or _category_accepts(_at_name(av), character)
    return matched != negated


def _category_accepts(category: str, character: str) -> bool:
    """정규식 범주(`\\d`·`\\w`·`\\s`와 부정)가 문자를 받는지 판정한다(유니코드 규칙).

    Args:
        category: 범주 이름.
        character: 문자.

    Returns:
        받으면 True.
    """
    positive = {
        "CATEGORY_DIGIT": character.isdecimal(),
        "CATEGORY_WORD": character.isalnum() or character == "_",
        "CATEGORY_SPACE": character.isspace(),
    }
    if category in positive:
        return positive[category]
    base = category.replace("CATEGORY_NOT_", "CATEGORY_")
    return not positive.get(base, True)


def _may_match(items: list[tuple[Any, Any]], character: str) -> bool:
    """항목열이 어느 위치에서든 문자를 맞출 수 있는지 판정한다(보수적).

    Args:
        items: 항목열.
        character: 검사할 문자.

    Returns:
        맞출 수 있으면 True.

    Raises:
        Unconvertible: 분석할 수 없는 구문.
    """
    for op, av in items:
        name = _op(op)
        if name in ("LITERAL", "NOT_LITERAL", "ANY", "IN"):
            if _single_accepts((op, av), character):
                return True
        elif name in ("MAX_REPEAT", "MIN_REPEAT"):
            if av[1] > 0 and _may_match(list(av[2]), character):
                return True
        elif name == "SUBPATTERN":
            if _may_match(list(av[3]), character):
                return True
        elif name == "BRANCH":
            if any(_may_match(list(branch), character) for branch in av[1]):
                return True
        else:
            raise Unconvertible(f"the regular expression uses {name.lower()}, which is not modeled")
    return False


def _min_width(items: list[tuple[Any, Any]]) -> int:
    """항목열이 맞추는 문자열의 최소 길이를 구한다.

    Args:
        items: 항목열.

    Returns:
        최소 길이.
    """
    total = 0
    for op, av in items:
        name = _op(op)
        if name in ("LITERAL", "NOT_LITERAL", "ANY", "IN"):
            total += 1
        elif name in ("MAX_REPEAT", "MIN_REPEAT"):
            total += int(av[0]) * _min_width(list(av[2]))
        elif name == "SUBPATTERN":
            total += _min_width(list(av[3]))
        elif name == "BRANCH":
            total += min(_min_width(list(branch)) for branch in av[1])
    return total


def skeleton_shapes(alternatives: list[Skeleton], *, raw: str | None, trailing_policy: str | None) -> list[RouteShape]:
    """펼친 토큰열 목록을 정규 템플릿 판정으로 바꾼다.

    빈 값을 받는 파라미터는 빈 값 변형을 더하고, 끝 슬래시만 다른 두 결과는 `optional`로 합친다.

    Args:
        alternatives: 토큰열 목록(루트 `/`부터 시작하는 전체 경로).
        raw: dynamic일 때 `channel`에 실을 원문 표현.
        trailing_policy: 합칠 수 없는 템플릿의 끝 슬래시 판정(`strict`·`optional`·None).

    Returns:
        판정 목록. 하나라도 확정할 수 없으면 dynamic 판정 하나.
    """
    try:
        expanded = _with_empty_variants(alternatives)
        shapes = [_skeleton_shape(skeleton, trailing_policy) for skeleton in expanded]
    except Unconvertible as error:
        return [dynamic_shape(raw, str(error))]
    return _merge_trailing_slash(shapes)


def dynamic_shape(raw: str | None, reason: str) -> RouteShape:
    """dynamic 판정을 만든다. 원문은 길이 상한과 제어 문자를 검사해 싣는다.

    Args:
        raw: 원문 표현.
        reason: 이유 종류(limitation 문구용).

    Returns:
        dynamic 판정.
    """
    safe = raw if raw is not None and len(raw) <= MAX_TEMPLATE_LENGTH and _is_safe_text(raw) else None
    return RouteShape(channel=safe, dynamic=True, coverage_reason=reason)


def _is_safe_text(text: str) -> bool:
    """문자열에 계약이 금지하는 제어 문자가 없는지 확인한다.

    Args:
        text: 검사할 문자열.

    Returns:
        안전하면 True.
    """
    return bool(text) and not any(
        ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F or character in "  " for character in text
    )


def _with_empty_variants(alternatives: list[Skeleton]) -> list[Skeleton]:
    """빈 값을 받는 한 세그먼트 파라미터마다 빈 값으로 채운 변형을 더한다.

    Args:
        alternatives: 토큰열 목록.

    Returns:
        변형을 더한 목록.

    Raises:
        ExpansionCapped: 16개를 넘을 때.
    """
    result: list[Skeleton] = []
    for skeleton in alternatives:
        variants: list[Skeleton] = [()]
        for token in skeleton:
            options: list[Skeleton] = [(token,)]
            if isinstance(token, Param) and token.may_be_empty:
                options.append(())
            variants = _cross(variants, options)
        result.extend(variants)
    unique = list(dict.fromkeys(result))
    if len(unique) > MAX_EXPANSIONS:
        raise ExpansionCapped("empty-value variants expand to more than 16 templates")
    return unique


def _skeleton_shape(skeleton: Skeleton, trailing_policy: str | None) -> RouteShape:
    """토큰열 하나를 정규 템플릿 판정으로 바꾼다.

    Args:
        skeleton: `/`로 시작하는 전체 경로 토큰열.
        trailing_policy: 끝 슬래시 판정.

    Returns:
        정적 판정.

    Raises:
        Unconvertible: 확정할 수 없을 때.
    """
    segments = _split_segments(_merge_adjacent(skeleton))
    rendered: list[str] = []
    constraints: list[ParamConstraint] = []
    for index, segment in enumerate(segments):
        text, constraint = _render_segment(segment, index, is_last=index == len(segments) - 1)
        rendered.append(text)
        if constraint is not None:
            constraints.append(constraint)
    template = "/" + "/".join(rendered)
    problem = template_problem(template)
    if problem is not None:
        raise Unconvertible(f"the path does not form a canonical template ({problem})")
    ends_with_catch_all = rendered[-1] == "{**}"
    return RouteShape(
        channel=template,
        dynamic=False,
        trailing_slash=None if ends_with_catch_all else trailing_policy,
        constraints=tuple(constraints),
    )


def _merge_adjacent(skeleton: Skeleton) -> Skeleton:
    """이웃한 리터럴을 잇고, 사이에 리터럴이 없는 파라미터는 하나로 합친다.

    Args:
        skeleton: 토큰열.

    Returns:
        정리한 토큰열.
    """
    merged: list[Token] = []
    for token in skeleton:
        previous = merged[-1] if merged else None
        if isinstance(token, Literal) and isinstance(previous, Literal):
            merged[-1] = Literal(previous.text + token.text)
        elif isinstance(token, Param) and isinstance(previous, Param):
            merged[-1] = Param(
                kind="path" if previous.crosses_slash or token.crosses_slash else "regex",
                pattern=None,
                may_be_empty=previous.may_be_empty and token.may_be_empty,
                crosses_slash=previous.crosses_slash or token.crosses_slash,
            )
        elif not (isinstance(token, Literal) and token.text == ""):
            merged.append(token)
    return tuple(merged)


def _split_segments(skeleton: Skeleton) -> list[list[Token]]:
    """토큰열을 `/` 기준 세그먼트로 나눈다. 첫 `/`는 루트다.

    Args:
        skeleton: 정리한 토큰열.

    Returns:
        세그먼트마다의 토큰 목록.

    Raises:
        Unconvertible: 경로가 `/`로 시작하지 않을 때.
    """
    if not skeleton or not isinstance(skeleton[0], Literal) or not skeleton[0].text.startswith("/"):
        raise Unconvertible("the path does not start with /")
    segments: list[list[Token]] = [[]]
    first = Literal(skeleton[0].text[1:])
    for token in (first, *skeleton[1:]):
        if isinstance(token, Param):
            segments[-1].append(token)
            continue
        pieces = token.text.split("/")
        for position, piece in enumerate(pieces):
            if position > 0:
                segments.append([])
            if piece:
                segments[-1].append(Literal(piece))
    return segments


def _render_segment(segment: list[Token], index: int, is_last: bool) -> tuple[str, ParamConstraint | None]:
    """세그먼트 하나를 정규 문자열과 제약으로 바꾼다.

    Args:
        segment: 세그먼트 토큰 목록.
        index: 템플릿 세그먼트 인덱스(0부터).
        is_last: 마지막 세그먼트인지.

    Returns:
        (정규 세그먼트 문자열, 제약 또는 None).

    Raises:
        Unconvertible: 파라미터가 둘 이상이거나 catch-all이 세그먼트 전체·마지막이 아닐 때.
    """
    params = [token for token in segment if isinstance(token, Param)]
    if len(params) > 1:
        raise Unconvertible("a path segment holds more than one parameter")
    if not params:
        return "".join(encode_decoded_literal(token.text) for token in segment if isinstance(token, Literal)), None
    param = params[0]
    if param.crosses_slash:
        if len(segment) != 1 or not is_last:
            raise Unconvertible("a parameter that matches / is not the whole last segment")
        return "{**}", ParamConstraint(index, "path")
    text = "".join("{}" if isinstance(token, Param) else encode_decoded_literal(token.text) for token in segment)
    return text, None if param.kind is None else ParamConstraint(index, param.kind, param.pattern)


def _merge_trailing_slash(shapes: list[RouteShape]) -> list[RouteShape]:
    """끝 슬래시만 다른 두 정적 판정을 `trailingSlash: "optional"` 하나로 합친다.

    합친 판정의 channel은 끝 슬래시가 없는 쪽이다. 루트 `/`와 `{**}`로 끝나는 템플릿은 합치지 않는다.

    Args:
        shapes: 정적 판정 목록.

    Returns:
        합친 목록(순서 유지, 중복 제거).
    """
    by_channel = {shape.channel: shape for shape in shapes}
    merged: list[RouteShape] = []
    consumed: set[str | None] = set()
    for shape in shapes:
        if shape.channel in consumed:
            continue
        bare = _bare_partner(shape.channel or "", by_channel)
        if bare is not None:
            merged.append(replace(by_channel[bare], trailing_slash="optional"))
            consumed.update({bare, bare + "/"})
            continue
        merged.append(shape)
        consumed.add(shape.channel)
    return merged


def _bare_partner(channel: str, by_channel: dict[str | None, RouteShape]) -> str | None:
    """끝 슬래시만 다른 짝이 있으면 끝 슬래시가 없는 쪽 channel을 돌려준다.

    Args:
        channel: 검사할 channel.
        by_channel: channel → 판정.

    Returns:
        끝 슬래시 없는 channel 또는 None.
    """
    bare = channel[:-1] if channel.endswith("/") else channel
    if bare in ("", "/") or bare.endswith("{**}"):
        return None
    return bare if bare in by_channel and bare + "/" in by_channel else None
