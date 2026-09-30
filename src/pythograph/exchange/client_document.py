"""클라이언트 추출 결과를 isthmus bridge-facts v1 `route-call` 문서로 조립한다.

route-decl 문서(`exchange.document`)와 같은 조립 규칙을 쓴다: 키 정렬, 완전히 같은 사실은 하나, 사실 수 100,000·
출력 16 Mi 문자 상한, 같은 공백은 한 문장으로 센다. 호출 측 문서라 `roles: ["client"]`이고 `dispatch`를 싣지 않는다.
사실이 0건이어도 target은 `http`다(계약의 http 예외: "스캔했으나 호출 없음"과 "스캔 안 함"을 구분한다).

dynamic 사실의 `channel`은 null이다. 원문 식은 userinfo·query·토큰을 담을 수 있어 싣지 않고, 증명한 리터럴 접두사만
마스킹한 `channelPrefix`로 싣는다(계약의 보안 절).
"""

from __future__ import annotations

import json

from pythograph.exchange.document import (
    MAX_FACTS,
    DocumentHeader,
    DocumentLimitError,
    build_limitations,
    format_timestamp,
)
from pythograph.routes.client.model import ClientExtraction, RouteCall


def build_client_document(header: DocumentHeader, extraction: ClientExtraction) -> dict[str, object]:
    """route-call 문서를 조립한다.

    Args:
        header: 문서 머리 필드.
        extraction: 추출 결과.

    Returns:
        bridge-facts v1 문서.

    Raises:
        DocumentLimitError: 사실 수가 상한을 넘을 때.
    """
    if len(extraction.calls) > MAX_FACTS:
        raise DocumentLimitError(
            f"the project produces more than {MAX_FACTS} route-call facts, which isthmus rejects; "
            "scan a smaller project root."
        )
    facts = _sorted_unique([_fact_json(call, header.service) for call in extraction.calls])
    document: dict[str, object] = {
        "format": "bridge-facts",
        "version": 1,
        "tool": {"name": "pythograph", "version": header.tool_version},
        "generatedAt": format_timestamp(header.generated_at),
        "platform": "python",
        "target": "http",
        "roles": ["client"],
        "sourceSets": {"tests": "included" if header.include_tests else "excluded"},
        "project": header.project,
        "facts": facts,
    }
    if header.service is not None:
        document["service"] = header.service
    limitations, scopes = build_limitations(extraction.gaps)
    document["limitations"] = limitations
    if scopes:
        document["limitationScopes"] = scopes
    return document


def _fact_json(call: RouteCall, document_service: str | None) -> dict[str, object]:
    """호출 하나를 route-call 사실 JSON으로 바꾼다.

    Args:
        call: 호출.
        document_service: 문서 service(사실 service가 없으면 이 값을 싣는다).

    Returns:
        사실 dict.
    """
    url = call.url
    fact: dict[str, object] = {
        "kind": "route-call",
        "channel": url.template,
        "dynamic": url.dynamic,
        "pathAnchor": url.anchor,
        "location": {"path": call.location.path, "line": call.location.line, "column": call.location.column},
        "symbol": {"qualifiedName": call.usr, "usr": call.usr},
    }
    optional: dict[str, object] = {
        "method": call.method,
        "methodDynamic": True if call.method is None else None,
        "authority": url.authority,
        "service": call.service or document_service,
        "queryTailStripped": True if url.query_tail_stripped and not url.dynamic else None,
        "channelPrefix": url.channel_prefix if url.dynamic else None,
        "maskedSegments": url.masked_segments if url.masked_segments and not url.dynamic else None,
        "testSource": True if call.test_source else None,
    }
    fact.update({key: value for key, value in optional.items() if value is not None})
    return fact


def _sorted_unique(facts: list[dict[str, object]]) -> list[dict[str, object]]:
    """사실을 결정적으로 정렬하고 완전히 같은 사실을 하나로 줄인다.

    Args:
        facts: 사실 목록.

    Returns:
        (channel, method, 앵커, dynamic, 파일, 줄, 열, 직렬화) 순으로 정렬한 목록.
    """
    unique = {json.dumps(fact, sort_keys=True, ensure_ascii=False): fact for fact in facts}

    def key(fact: dict[str, object]) -> tuple[object, ...]:
        location = fact["location"]
        assert isinstance(location, dict)
        return (
            str(fact["channel"] or ""),
            str(fact.get("method", "")),
            str(fact["pathAnchor"]),
            bool(fact["dynamic"]),
            str(location["path"]),
            int(location["line"]),
            int(location["column"]),
            json.dumps(fact, sort_keys=True, ensure_ascii=False),
        )

    return sorted(unique.values(), key=key)
