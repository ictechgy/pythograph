"""`%` 서식과 `str.format` 템플릿을 리터럴과 값 자리로 나눈다(계약 `compose.interpolation`의 Python 보간).

분석 대상 코드를 실행하지 않는다. `str.format` 템플릿은 표준 라이브러리 `string.Formatter.parse`로 문법만 읽는다
(`{{`·`}}`는 리터럴이다). `%` 서식은 `%%`를 리터럴 `%`로, 나머지 변환(`%s`·`%d`·`%(name)s` 등, 플래그·폭·정밀도
포함)을 값 자리로 읽는다. 문법을 확정하지 못하면 None을 돌려주고 호출자는 식 전체를 값으로 본다.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass

#: `%` 서식 변환 하나(`%%` 포함)를 찾는 정규식이다.
_PERCENT = re.compile(
    r"%(?:(?P<escape>%)|(?:\((?P<key>[^)]*)\))?(?P<flags>[-#0 +]*)(?P<width>\*|\d+)?"
    r"(?:\.(?P<precision>\*|\d+))?[hlL]?(?P<conversion>[diouxXeEfFgGcrsa]))"
)


@dataclass(frozen=True)
class Field:
    """서식 템플릿의 값 자리 하나.

    Attributes:
        key: 인자 자리(정수 위치 또는 이름).
        plain: 값을 그대로 문자열로 넣는 자리인지(`%s`, `{}`·`{0}`·`{name}`에 형식 지정자·`!r` 없음). 아니면 값이
            문자열 상수여도 모양이 바뀔 수 있어 값 자리로만 본다.
    """

    key: int | str
    plain: bool


#: 템플릿 조각: 리터럴 문자열 또는 값 자리다.
Piece = str | Field


def percent_pieces(template: str, keyed: bool) -> list[Piece] | None:
    """`%` 서식 템플릿을 조각으로 나눈다.

    Args:
        template: 서식 문자열.
        keyed: 오른쪽이 사전 리터럴인지(`%(name)s`만 받는다).

    Returns:
        조각 목록, 확정하지 못하면(`*` 폭, 이름·위치 섞임, 짝 없는 `%`) None.
    """
    pieces: list[Piece] = []
    position = 0
    index = 0
    for match in _PERCENT.finditer(template):
        pieces.append(template[position : match.start()])
        position = match.end()
        if match["escape"]:
            pieces.append("%")
            continue
        if "*" in (match["width"] or "", match["precision"] or "") or (match["key"] is not None) != keyed:
            return None
        plain = match["conversion"] == "s" and not (match["flags"] or match["width"] or match["precision"])
        pieces.append(Field(match["key"] if keyed else index, plain))
        index += 1
    pieces.append(template[position:])
    return None if _stray(template) else pieces


def _stray(template: str) -> bool:
    """변환으로 읽히지 않은 `%`가 남았는지 본다(`TypeError`를 낼 서식).

    Args:
        template: 서식 문자열.

    Returns:
        남았으면 True.
    """
    return "%" in _PERCENT.sub("", template)


def format_pieces(template: str) -> list[Piece] | None:
    """`str.format` 템플릿을 조각으로 나눈다.

    Args:
        template: 템플릿 문자열.

    Returns:
        조각 목록, 문법 오류·속성·첨자 접근(`{0.x}`, `{a[0]}`)이 있으면 None.
    """
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError:
        return None
    pieces: list[Piece] = []
    automatic = 0
    for literal, name, spec, conversion in parsed:
        pieces.append(literal)
        if name is None:
            continue
        if "." in name or "[" in name:
            return None
        key: int | str
        if name == "":
            key = automatic
            automatic += 1
        else:
            key = int(name) if name.isdigit() else name
        pieces.append(Field(key, not spec and conversion in (None, "s")))
    return pieces
