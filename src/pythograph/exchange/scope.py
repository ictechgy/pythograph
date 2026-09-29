"""http limitation 스코프 검증과 적용 판정(참조 구현).

정본은 isthmus `docs/GRAPH-EXCHANGE.md`의 "http limitation 스코프" 절이다.

- `scope_problem`: 생산자가 내는 스코프 항목을 소비자가 거부하지 않도록 같은 규칙으로 미리 검사한다.
  잘못된 스코프를 내면 isthmus가 문서 전체를 입력 오류로 거부하므로, 문서 조립 단계에서 이 검사를
  통과하지 못한 스코프는 버리고 한계를 스코프 없이(문서 전체 효과로) 남긴다.
- `scope_applies`: 한계가 호출·선언 하나에 적용되는지의 소비자 판정이다. 생산자는 이 판정을 쓰지 않지만
  공유 벡터(`scope.applies`)를 생산자 쪽에서도 실행해 스코프 설계가 기대는 규칙(세그먼트 경계, 대소문자·
  끝 슬래시 접기, method 규칙)이 바뀌면 테스트가 알리게 한다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import lru_cache

from pythograph.exchange.template import is_canonical_template

#: 스코프 항목에 올 수 있는 키다.
_SCOPE_KEYS = frozenset({"limitationIndex", "templates", "templatePrefixes", "templateSuffixes", "methods"})

#: 경로 필드 이름이다. 하나 이상 있어야 한다.
_PATH_FIELDS = ("templates", "templatePrefixes", "templateSuffixes")

#: 스코프 `methods`에 쓸 수 있는 HTTP 동사다(`ANY` 제외).
HTTP_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE")

#: 경로 비교에서 0개 이상의 세그먼트를 뜻하는 내부 표식이다.
_ANY_SEGMENTS = "\x00*"


def scope_problem(scope: Mapping[str, object]) -> str | None:
    """스코프 항목 하나가 계약을 어기면 이유를 돌려준다.

    Args:
        scope: `limitationIndex`를 포함하거나 뺀 스코프 항목.

    Returns:
        올바르면 None, 아니면 사람이 읽을 이유(오류 문구용, 입력 원문은 싣지 않는다).
    """
    unknown = sorted(set(scope) - _SCOPE_KEYS)
    if unknown:
        return "unknown scope key"
    if not any(field in scope for field in _PATH_FIELDS):
        return "a scope needs templates, templatePrefixes, or templateSuffixes"
    for field in _PATH_FIELDS:
        if field in scope:
            problem = _path_field_problem(field, scope[field])
            if problem is not None:
                return problem
    if "methods" in scope:
        return _methods_problem(scope["methods"])
    return None


def _path_field_problem(field: str, value: object) -> str | None:
    """경로 필드 하나(배열)를 검사한다.

    Args:
        field: 필드 이름.
        value: 필드 값.

    Returns:
        올바르면 None, 아니면 이유.
    """
    if not isinstance(value, list) or not value:
        return f"{field} must be a non-empty array"
    for element in value:
        if not isinstance(element, str) or not is_canonical_template(element):
            return f"{field} holds a non-canonical template"
        if field != "templates" and "{**}" in element:
            return f"{field} cannot hold {{**}}"
        if field == "templatePrefixes" and element != "/" and element.endswith("/"):
            return "a non-root template prefix cannot end with /"
        if field == "templateSuffixes" and element == "/":
            return "a template suffix cannot be /"
    return None


def _methods_problem(value: object) -> str | None:
    """`methods` 필드를 검사한다.

    Args:
        value: 필드 값.

    Returns:
        올바르면 None, 아니면 이유.
    """
    if not isinstance(value, list) or not value:
        return "methods must be a non-empty array"
    if any(not isinstance(method, str) or method not in HTTP_METHODS for method in value):
        return "methods holds a value that is not an HTTP method"
    if len(set(value)) != len(value):
        return "methods holds duplicates"
    return None


def scope_applies(scope: Mapping[str, object], probe: Mapping[str, object]) -> bool:
    """스코프가 있는 한계가 호출·선언 하나에 적용되는지(겹칠 수 있는지) 판정한다.

    항상 넓게 근사한다: 리터럴은 ASCII 대소문자를 접고, 끝 슬래시 하나는 무시하며, `{}`와 부분
    세그먼트 파라미터는 빈 값을 포함한 어떤 값도 될 수 있다고 본다.

    Args:
        scope: 검증을 통과한 스코프 항목.
        probe: `template`, `pathAnchor`(`root`|`base`), `side`(`call`|`declaration`),
            선택 `method`(없으면 동적 동사 호출)를 담은 사실 요약.

    Returns:
        적용되면 True.
    """
    if not _method_applies(scope.get("methods"), probe.get("method"), probe.get("side")):
        return False
    probe_tokens = _probe_tokens(str(probe["template"]), probe.get("pathAnchor") == "base")
    return any(_overlaps(probe_tokens, element) for element in _scope_elements(scope))


def _method_applies(methods: object, method: object, side: object) -> bool:
    """스코프 `methods`가 사실의 동사를 덮는지 판정한다.

    Args:
        methods: 스코프의 `methods`(없으면 모든 동사).
        method: 사실의 동사(없으면 동적 동사 호출).
        side: `call` 또는 `declaration`.

    Returns:
        덮으면 True.
    """
    if not isinstance(methods, list):
        return True
    allowed = set(methods)
    if side == "declaration":
        return method == "ANY" or method in allowed or (method == "GET" and "HEAD" in allowed) or "OPTIONS" in allowed
    if method is None or method == "OPTIONS":
        return True
    if method == "HEAD":
        return "HEAD" in allowed or "GET" in allowed
    return method in allowed


def _scope_elements(scope: Mapping[str, object]) -> list[tuple[str, ...]]:
    """스코프의 경로 원소를 비교용 토큰열로 바꾼다.

    Args:
        scope: 스코프 항목.

    Returns:
        원소마다 하나의 토큰열.
    """
    elements: list[tuple[str, ...]] = []
    for template in _string_list(scope.get("templates")):
        elements.append(_template_tokens(template))
    for prefix in _string_list(scope.get("templatePrefixes")):
        elements.append((*_template_tokens(prefix), _ANY_SEGMENTS))
    for suffix in _string_list(scope.get("templateSuffixes")):
        elements.append((_ANY_SEGMENTS, *_template_tokens(suffix)))
    return elements


def _string_list(value: object) -> list[str]:
    """값이 문자열 배열이면 그대로, 아니면 빈 목록을 돌려준다.

    Args:
        value: 임의 값.

    Returns:
        문자열 목록.
    """
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _probe_tokens(template: str, is_base: bool) -> tuple[str, ...]:
    """사실 템플릿을 비교용 토큰열로 바꾼다. base 앵커는 앞에 0개 이상 세그먼트를 둔다.

    Args:
        template: 정규 템플릿.
        is_base: base 앵커 여부.

    Returns:
        토큰열.
    """
    tokens = _template_tokens(template)
    return (_ANY_SEGMENTS, *tokens) if is_base else tokens


def _template_tokens(template: str) -> tuple[str, ...]:
    """템플릿을 세그먼트 토큰열로 바꾼다. 끝 슬래시 하나는 접고 `{**}`는 0개 이상 세그먼트로 본다.

    Args:
        template: 정규 템플릿.

    Returns:
        세그먼트 토큰열(루트 `/`는 빈 토큰열).
    """
    segments = template[1:].split("/")
    if segments and segments[-1] == "":
        segments = segments[:-1]
    return tuple(_ANY_SEGMENTS if segment == "{**}" else segment for segment in segments)


def _overlaps(left: Sequence[str], right: Sequence[str]) -> bool:
    """두 토큰열이 같은 요청 경로를 받을 수 있는지(교집합이 비지 않을 수 있는지) 판정한다.

    Args:
        left: 토큰열.
        right: 토큰열.

    Returns:
        겹칠 수 있으면 True.
    """
    return _overlap_from(tuple(left), tuple(right), 0, 0)


@lru_cache(maxsize=4096)
def _overlap_from(left: tuple[str, ...], right: tuple[str, ...], i: int, j: int) -> bool:
    """와일드카드(0개 이상 세그먼트)를 포함한 두 토큰열의 교집합 여부를 위치 (i, j)부터 판정한다.

    Args:
        left: 왼쪽 토큰열.
        right: 오른쪽 토큰열.
        i: 왼쪽 위치.
        j: 오른쪽 위치.

    Returns:
        남은 부분이 겹칠 수 있으면 True.
    """
    if i == len(left) and j == len(right):
        return True
    if i < len(left) and left[i] == _ANY_SEGMENTS:
        return _overlap_from(left, right, i + 1, j) or (j < len(right) and _overlap_from(left, right, i, j + 1))
    if j < len(right) and right[j] == _ANY_SEGMENTS:
        return _overlap_from(left, right, i, j + 1) or (i < len(left) and _overlap_from(left, right, i + 1, j))
    if i == len(left) or j == len(right):
        return False
    return _segments_compatible(left[i], right[j]) and _overlap_from(left, right, i + 1, j + 1)


def _segments_compatible(left: str, right: str) -> bool:
    """두 세그먼트가 같은 값일 수 있는지 판정한다.

    Args:
        left: 세그먼트(리터럴, `{}`, 부분 세그먼트).
        right: 세그먼트.

    Returns:
        같은 값일 수 있으면 True.
    """
    left_has_param, right_has_param = "{}" in left, "{}" in right
    if left_has_param and right_has_param:
        return True
    if left_has_param:
        return _literal_fits_partial(right, left)
    if right_has_param:
        return _literal_fits_partial(left, right)
    return left.lower() == right.lower()


def _literal_fits_partial(literal: str, partial: str) -> bool:
    """리터럴 세그먼트가 `p{}s` 모양에 맞을 수 있는지(가운데는 빈 값 허용) 판정한다.

    Args:
        literal: 리터럴 세그먼트.
        partial: `{}`를 하나 담은 세그먼트.

    Returns:
        맞을 수 있으면 True.
    """
    prefix, suffix = (part.lower() for part in partial.split("{}", 1))
    folded = literal.lower()
    return len(folded) >= len(prefix) + len(suffix) and folded.startswith(prefix) and folded.endswith(suffix)
