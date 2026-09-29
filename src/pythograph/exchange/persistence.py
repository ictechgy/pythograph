"""persistence 추출 결과를 isthmus bridge-facts v1 `relation-use` 문서로 조립한다.

계약(GRAPH-EXCHANGE `target: "persistence"`): 비sql 생산자는 `relation-use`만 내고, `channel`은 관계 이름(한정·
비한정 그대로, 조각의 `.`은 `%2E`), `method`는 선택적 컬럼 이름이다. 사실이 없으면 `target`은 null이다(null
문서는 persistence 도메인의 호출 측으로 세지 않는다). 조립 규칙은 routes 문서와 같다: 키 정렬, 같은 사실은 하나,
사실 수 100,000·출력 16 Mi 문자 상한.
"""

from __future__ import annotations

import json
import re
from collections import Counter

from pythograph.exchange.document import MAX_FACTS, DocumentHeader, DocumentLimitError, format_timestamp
from pythograph.persistence.model import PersistenceExtraction, RelationUse

#: 계약이 금지하는 문자(제어 문자, U+2028/2029, 짝 없는 서로게이트)다. dynamic 원문에서 공백으로 바꾼다.
_FORBIDDEN = re.compile("[\u0000-\u001f\u007f-\u009f  \ud800-\udfff]")


def build_persistence_document(header: DocumentHeader, extraction: PersistenceExtraction) -> dict[str, object]:
    """relation-use 문서를 조립한다.

    Args:
        header: 문서 머리 필드(`service`·`include_tests`는 persistence 문서에 싣지 않는다).
        extraction: 추출 결과.

    Returns:
        bridge-facts v1 문서.

    Raises:
        DocumentLimitError: 사실 수가 상한을 넘을 때.
    """
    converted = [_fact_json(use) for use in extraction.uses]
    facts = _sorted_unique(converted)
    if len(facts) > MAX_FACTS:
        raise DocumentLimitError(
            f"the project produces more than {MAX_FACTS} relation-use facts, which isthmus rejects; "
            "scan a smaller project root."
        )
    missing = sum(1 for fact in facts if "symbol" not in fact)
    gaps = [(gap.prefix, gap.message) for gap in extraction.gaps]
    invalid = sum(
        1 for use, fact in zip(extraction.uses, converted, strict=False) if not use.dynamic and fact["dynamic"]
    )
    if invalid:
        gaps.append(("invalid-relation-names:", f"{invalid} names the exchange format cannot carry became dynamic"))
    if missing:
        gaps.append(("missing-relation-usrs:", f"{missing} facts are outside any function or class"))
    return {
        "format": "bridge-facts",
        "version": 1,
        "tool": {"name": "pythograph", "version": header.tool_version},
        "generatedAt": format_timestamp(header.generated_at),
        "platform": "python",
        "target": "persistence" if facts else None,
        "project": header.project,
        "facts": facts,
        "limitations": _limitations(gaps),
    }


def _fact_json(use: RelationUse) -> dict[str, object]:
    """사실 하나를 JSON으로 바꾼다. 계약을 어기는 정적 이름은 dynamic 사실로 내린다.

    Args:
        use: 사실.

    Returns:
        사실 dict.
    """
    static_ok = (
        not use.dynamic
        and all(use.channel.split("."))
        and not _FORBIDDEN.search(use.channel)
        and (use.column is None or (use.column and not _FORBIDDEN.search(use.column)))
    )
    fact: dict[str, object] = {
        "kind": "relation-use",
        "channel": use.channel if static_ok else _dynamic_text(use.channel),
        "dynamic": not static_ok,
        "location": {"path": use.location.path, "line": use.location.line, "column": use.location.column},
    }
    if static_ok and use.column is not None:
        fact["method"] = use.column
    if use.symbol is not None:
        fact["symbol"] = {"qualifiedName": use.symbol, "usr": use.symbol}
    return fact


def _dynamic_text(text: str) -> str:
    """dynamic 원문을 계약이 허용하는 비어 있지 않은 문자열로 만든다.

    Args:
        text: 원문.

    Returns:
        금지 문자를 공백으로 바꾼 문자열(비면 `<dynamic>`).
    """
    cleaned = _FORBIDDEN.sub(" ", text).strip()
    return cleaned or "<dynamic>"


def _sorted_unique(facts: list[dict[str, object]]) -> list[dict[str, object]]:
    """사실을 (channel, method, dynamic, 파일, 줄, 열, usr) 순으로 정렬하고 같은 사실을 하나로 줄인다.

    Args:
        facts: 사실 목록.

    Returns:
        정렬·중복 제거한 목록.
    """
    unique = {json.dumps(fact, sort_keys=True, ensure_ascii=False): fact for fact in facts}

    def key(item: tuple[str, dict[str, object]]) -> tuple[object, ...]:
        """정렬 키를 만든다."""
        fact = item[1]
        location = fact["location"]
        assert isinstance(location, dict)
        return (
            str(fact["channel"]),
            str(fact.get("method", "")),
            bool(fact["dynamic"]),
            str(location["path"]),
            int(location["line"]),
            int(location["column"]),
            item[0],
        )

    return [fact for _, fact in sorted(unique.items(), key=key)]


def _limitations(gaps: list[tuple[str, str]]) -> list[str]:
    """같은 공백을 모아 개수를 채운 limitation 문장 목록을 만든다.

    Args:
        gaps: (접두사, 문장 틀) 목록.

    Returns:
        정렬한 문장 목록.
    """
    counts = Counter(gaps)
    return sorted(f"{prefix} {message.replace('{count}', str(count))}" for (prefix, message), count in counts.items())
