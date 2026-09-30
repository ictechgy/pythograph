"""라우트 추출 결과를 isthmus bridge-facts v1 `route-decl` 문서로 조립하고 직렬화한다.

파일 시스템을 읽지 않는 순수 조립이다. 결정 사항:

- 키는 정렬하고 두 칸 들여쓰기로 쓴다(diff 가능, 같은 입력이면 같은 바이트). 사실은
  (channel, method, 앵커, dynamic, 파일, 줄, 열, usr) 순으로 정렬하고 완전히 같은 사실은 하나로 줄인다.
- 스코프 없는 같은 공백은 한 문장으로 모아 개수를 센다. 스코프 있는 공백은 스코프마다 한 문장이며
  스코프는 계약 검사(`scope_problem`)를 통과할 때만 싣는다. 통과하지 못하면 스코프를 버리고 문서 전체
  효과로 남긴다(좁히는 쪽보다 넓히는 쪽이 안전하다).
- 사실 수 상한(100,000)과 출력 길이 상한(16 Mi 문자)은 isthmus 입력 상한과 같다. 넘으면 부분 문서
  대신 `DocumentLimitError`로 실패한다.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

from pythograph.exchange.scope import scope_problem
from pythograph.routes.model import Extraction, Gap, RouteDecl, ScopeRange

#: 문서 하나에 담는 최대 사실 수다(isthmus 입력 상한).
MAX_FACTS = 100_000

#: 출력 문서의 최대 길이(문자)다(isthmus 파일당 입력 상한).
MAX_OUTPUT_LENGTH = 16 * 1024 * 1024

#: 문서당 최대 스코프 수다(계약 상한).
MAX_SCOPES = 1000


class DocumentLimitError(Exception):
    """사실 수나 출력 길이가 isthmus 상한을 넘었다. 원인과 해결 방향을 메시지에 담는다."""


@dataclass(frozen=True)
class DocumentHeader:
    """문서 머리 필드.

    Attributes:
        tool_version: pythograph 버전.
        generated_at: 추출 시각(UTC).
        project: 프로젝트 POSIX realpath.
        service: 서비스 신원(없으면 None).
        include_tests: 테스트 소스를 포함했는지.
    """

    tool_version: str
    generated_at: datetime
    project: str
    service: str | None
    include_tests: bool


def format_timestamp(instant: datetime) -> str:
    """시각을 계약의 정규 형식(`YYYY-MM-DDTHH:mm:ss.sssZ`, UTC)으로 바꾼다.

    Args:
        instant: 시각. 시간대가 없으면 UTC로 본다.

    Returns:
        밀리초 세 자리의 UTC ISO 8601 문자열.
    """
    aware = instant if instant.tzinfo is not None else instant.replace(tzinfo=timezone.utc)
    utc = aware.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def build_document(header: DocumentHeader, extraction: Extraction) -> dict[str, object]:
    """route-decl 문서를 조립한다.

    Args:
        header: 문서 머리 필드.
        extraction: 추출 결과.

    Returns:
        bridge-facts v1 문서(JSON으로 쓸 수 있는 dict).

    Raises:
        DocumentLimitError: 사실 수가 상한을 넘을 때.
    """
    if len(extraction.decls) > MAX_FACTS:
        raise DocumentLimitError(
            f"the project produces more than {MAX_FACTS} route-decl facts, which isthmus rejects; "
            "scan a smaller project root."
        )
    facts = _sorted_unique_facts([_fact_json(decl, header.service) for decl in extraction.decls])
    document: dict[str, object] = {
        "format": "bridge-facts",
        "version": 1,
        "tool": {"name": "pythograph", "version": header.tool_version},
        "generatedAt": format_timestamp(header.generated_at),
        "platform": "python",
        "target": "http",
        "roles": ["server"],
        "sourceSets": {"tests": "included" if header.include_tests else "excluded"},
        "project": header.project,
        "facts": facts,
    }
    if extraction.dispatch is not None:
        document["dispatch"] = extraction.dispatch
    if header.service is not None:
        document["service"] = header.service
    limitations, scopes = _limitations(extraction.gaps)
    document["limitations"] = limitations
    if scopes:
        document["limitationScopes"] = scopes
    return document


def encode_document(document: dict[str, object]) -> str:
    """문서를 키 정렬·두 칸 들여쓰기 JSON으로 직렬화한다.

    Args:
        document: 조립한 문서.

    Returns:
        줄바꿈으로 끝나는 JSON 문자열.

    Raises:
        DocumentLimitError: 출력이 isthmus 입력 상한을 넘을 때.
    """
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if len(text) > MAX_OUTPUT_LENGTH:
        raise DocumentLimitError(
            f"the output document would exceed {MAX_OUTPUT_LENGTH} characters, which isthmus rejects; "
            "scan a smaller project root."
        )
    return text


def _fact_json(decl: RouteDecl, service: str | None) -> dict[str, object]:
    """선언 하나를 route-decl 사실 JSON으로 바꾼다.

    Args:
        decl: 선언.
        service: 서비스 신원(없으면 None).

    Returns:
        사실 dict.
    """
    shape = decl.shape
    fact: dict[str, object] = {
        "kind": "route-decl",
        "method": decl.method,
        "channel": shape.channel,
        "dynamic": shape.dynamic,
        "pathAnchor": decl.path_anchor,
        "location": {"path": decl.location.path, "line": decl.location.line, "column": decl.location.column},
        "symbol": _symbol_json(decl),
    }
    optional: dict[str, object] = {
        "service": service,
        "trailingSlash": None if shape.dynamic else shape.trailing_slash,
        "paramConstraints": None
        if shape.dynamic or not shape.constraints
        else [
            {
                key: value
                for key, value in (("segment", item.segment), ("kind", item.kind), ("pattern", item.pattern))
                if value is not None
            }
            for item in shape.constraints
        ],
        "catchAllPrefix": True if shape.catch_all_prefix and decl.usr is not None else None,
        "testSource": True if decl.test_source else None,
        "order": None if decl.order is None else {"group": decl.order.group, "index": decl.order.index},
    }
    fact.update({key: value for key, value in optional.items() if value is not None})
    return fact


def _symbol_json(decl: RouteDecl) -> dict[str, str]:
    """사실의 `symbol`을 만든다. usr가 없으면 qualifiedName만 싣는다.

    Args:
        decl: 선언.

    Returns:
        symbol dict.
    """
    symbol = {"qualifiedName": decl.qualified_name}
    if decl.usr is not None:
        symbol["usr"] = decl.usr
    return symbol


def _sorted_unique_facts(facts: list[dict[str, object]]) -> list[dict[str, object]]:
    """사실을 결정적으로 정렬하고 완전히 같은 사실을 하나로 줄인다.

    Args:
        facts: 사실 목록.

    Returns:
        정렬·중복 제거한 목록.
    """
    unique = {json.dumps(fact, sort_keys=True, ensure_ascii=False): fact for fact in facts}
    return sorted(unique.values(), key=_fact_sort_key)


def _fact_sort_key(fact: dict[str, object]) -> tuple[object, ...]:
    """사실 정렬 키다.

    Args:
        fact: 사실 dict.

    Returns:
        (channel, method, 앵커, dynamic, 파일, 줄, 열, 직렬화) 튜플.
    """
    location = fact["location"]
    assert isinstance(location, dict)
    return (
        str(fact["channel"] or ""),
        str(fact["method"]),
        str(fact["pathAnchor"]),
        bool(fact["dynamic"]),
        str(location["path"]),
        int(location["line"]),
        int(location["column"]),
        json.dumps(fact, sort_keys=True, ensure_ascii=False),
    )


def build_limitations(gaps: list[Gap]) -> tuple[list[str], list[dict[str, object]]]:
    """공백을 limitation 문장과 스코프 목록으로 바꾼다(route-decl·route-call 문서가 같은 규칙을 쓴다).

    Args:
        gaps: 추출기가 모은 공백.

    Returns:
        (정렬한 limitation 문장, 인덱스를 맞춘 스코프 목록).
    """
    return _limitations(gaps)


def _limitations(gaps: list[Gap]) -> tuple[list[str], list[dict[str, object]]]:
    """공백을 limitation 문장과 스코프 목록으로 바꾼다.

    Args:
        gaps: 추출기가 모은 공백.

    Returns:
        (정렬한 limitation 문장, 인덱스를 맞춘 스코프 목록).
    """
    valid_gaps = [_with_valid_scope(gap) for gap in gaps]
    unscoped = Counter((gap.prefix, gap.message) for gap in valid_gaps if gap.scope is None)
    scoped = {(gap.prefix, gap.message, gap.scope) for gap in valid_gaps if gap.scope is not None}
    if len(scoped) > MAX_SCOPES:
        # 상한을 넘으면 스코프를 모두 버리고 개수만 남긴다(넓히는 쪽이라 안전하다).
        for prefix, message, _ in scoped:
            unscoped[(prefix, message)] += 1
        scoped = set()
    entries: list[tuple[str, ScopeRange | None]] = [
        (_sentence(prefix, message, count), None) for (prefix, message), count in unscoped.items()
    ]
    entries.extend((_sentence(prefix, message, 1), scope) for prefix, message, scope in scoped)
    entries.sort(key=lambda entry: (entry[0], json.dumps(entry[1].to_json() if entry[1] else {}, sort_keys=True)))
    limitations = [text for text, _ in entries]
    scopes: list[dict[str, object]] = [
        {"limitationIndex": index, **scope.to_json()} for index, (_, scope) in enumerate(entries) if scope
    ]
    return limitations, scopes


def _with_valid_scope(gap: Gap) -> Gap:
    """공백의 스코프가 계약 검사를 통과하지 못하면 스코프를 뗀다.

    Args:
        gap: 공백.

    Returns:
        그대로이거나 스코프를 뗀 공백.
    """
    if gap.scope is None or scope_problem(gap.scope.to_json()) is None:
        return gap
    return Gap(gap.prefix, gap.message, None)


def _sentence(prefix: str, message: str, count: int) -> str:
    """접두사와 문장 틀로 limitation 문장을 만든다.

    Args:
        prefix: 콜론으로 끝나는 접두사.
        message: `{count}`를 담을 수 있는 문장 틀.
        count: 개수.

    Returns:
        `"<prefix> <message>"` 문장.
    """
    return f"{prefix} {message.replace('{count}', str(count))}"
