"""registration-order `order` 필드 검증(참조 구현).

정본은 isthmus `docs/GRAPH-EXCHANGE.md`의 "디스패치 모델" 절과 공유 벡터 `http-dispatch`의 `dispatch.validate`
사례다(소비자·생산자 모두 적용). 소비자가 거부할 문서를 내지 않도록 같은 규칙으로 검사한다.

- `order`는 `dispatch: "registration-order"` 문서의 사실에만 온다.
- `order`는 `{group, index}`만 담는다. group은 제어 문자·앞뒤 공백 없는 비어 있지 않은 문자열(UTF-16 256자 이하),
  index는 0 이상의 안전 정수(불리언 제외)다.
- 한 group은 한 서비스다(사실 `service` 또는 문서 `service`가 같다).
- 한 (group, index)는 한 등록이다(같은 `location` 경로·줄·열).
- catch-all 접두사 decl(`catchAllPrefix`)은 같은 method·usr·서비스·`order`의 원본 `{**}` decl이 있어야 한다(순서를
  원본에서 그대로 물려받는다).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

#: group 최대 길이(UTF-16 코드 단위)다.
MAX_GROUP_LENGTH = 256

#: 안전 정수 상한(JavaScript `Number.MAX_SAFE_INTEGER`)이다.
MAX_SAFE_INTEGER = 2**53 - 1

#: 소비자가 거부하는 제어 문자다.
_CONTROL = re.compile("[\u0000-\u001f\u007f-\u009f  ]")

#: JavaScript `String.prototype.trim`이 지우는 공백이다.
_JS_WHITESPACE = " \t\n\v\f\r                 　﻿"


def order_problem(document: Mapping[str, object]) -> str | None:
    """문서의 `order` 필드 문제를 찾는다.

    Args:
        document: bridge-facts 문서.

    Returns:
        문제 설명, 없으면 None.
    """
    facts = document.get("facts")
    if not isinstance(facts, Sequence):
        return None
    for position, fact in enumerate(facts):
        if isinstance(fact, Mapping) and "order" in fact:
            problem = _fact_problem(fact["order"], document.get("dispatch"))
            if problem is not None:
                return f"fact {position}: {problem}"
    return _group_problem(document, facts) or _catch_all_problem(document, facts)


def _fact_problem(order: object, dispatch: object) -> str | None:
    """사실 하나의 `order` 모양을 검사한다.

    Args:
        order: `order` 값.
        dispatch: 문서 dispatch.

    Returns:
        문제 설명 또는 None.
    """
    if dispatch != "registration-order":
        return "order requires dispatch registration-order"
    if not isinstance(order, Mapping) or set(order) != {"group", "index"}:
        return "order must be exactly {group, index}"
    group, index = order["group"], order["index"]
    if not _is_safe_group(group):
        return "order group must be a trimmed non-empty string of at most 256 characters without control characters"
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= MAX_SAFE_INTEGER:
        return "order index must be a non-negative safe integer"
    return None


def _is_safe_group(group: object) -> bool:
    """group 문자열 규칙을 검사한다.

    Args:
        group: group 값.

    Returns:
        규칙을 지키면 True.
    """
    if not isinstance(group, str) or not group.strip(_JS_WHITESPACE):
        return False
    if _CONTROL.search(group) or group.strip(_JS_WHITESPACE) != group:
        return False
    try:
        encoded = group.encode("utf-16-le")
    except UnicodeEncodeError:
        return False
    return len(encoded) // 2 <= MAX_GROUP_LENGTH


def _group_problem(document: Mapping[str, object], facts: Sequence[object]) -> str | None:
    """group·index 공유 규칙을 검사한다.

    Args:
        document: 문서.
        facts: 사실 목록.

    Returns:
        문제 설명 또는 None.
    """
    services: dict[object, object] = {}
    registrations: dict[tuple[object, object], tuple[object, ...]] = {}
    for position, fact in enumerate(facts):
        if not isinstance(fact, Mapping) or not isinstance(fact.get("order"), Mapping):
            continue
        order = fact["order"]
        group, index = order["group"], order["index"]
        service = fact.get("service", document.get("service"))
        if services.setdefault(group, service) != service:
            return f"fact {position}: order group is shared by facts of different services"
        location = fact.get("location")
        place = tuple(location.get(key) for key in ("path", "line", "column")) if isinstance(location, Mapping) else ()
        if registrations.setdefault((group, index), place) != place:
            return f"fact {position}: order index is shared by registrations at different locations"
    return None


def _catch_all_problem(document: Mapping[str, object], facts: Sequence[object]) -> str | None:
    """catch-all 접두사 decl마다 원본 `{**}` decl이 있는지 검사한다.

    Args:
        document: 문서.
        facts: 사실 목록.

    Returns:
        문제 설명 또는 None.
    """
    originals = {
        _origin_key(document, fact, str(fact.get("channel")))
        for fact in facts
        if isinstance(fact, Mapping)
        and fact.get("kind") == "route-decl"
        and not fact.get("dynamic")
        and "catchAllPrefix" not in fact
    }
    for position, fact in enumerate(facts):
        if isinstance(fact, Mapping) and fact.get("catchAllPrefix") is True:
            channel = str(fact.get("channel"))
            original = "/{**}" if channel == "/" else f"{channel}/{{**}}"
            if _origin_key(document, fact, original) not in originals:
                return f"fact {position}: a catch-all prefix declaration has no matching {{**}} declaration"
    return None


def _origin_key(document: Mapping[str, object], fact: Mapping[str, object], channel: str) -> str:
    """원본 decl 비교 키다.

    Args:
        document: 문서.
        fact: 사실.
        channel: 비교할 channel.

    Returns:
        키 문자열.
    """
    symbol = fact.get("symbol")
    usr = symbol.get("usr") if isinstance(symbol, Mapping) else None
    order = fact.get("order")
    order_key = [order.get("group"), order.get("index")] if isinstance(order, Mapping) else None
    service = fact.get("service", document.get("service"))
    return repr([fact.get("method"), usr, channel, service, order_key])
