"""그래프 스냅샷(`pythograph-graph` v1)과 isthmus `language-traversal` v1 문서를 조립한다.

- 스냅샷은 pythograph 자체 형식이다(isthmus 입력이 아니다): 정점(id·종류·위치·모드별 미해석 수·이유), 간선(출발·
  도착·종류·등급), 집계, `direct` 모드 한계, `graphRevision`. isthmus 입력 상한(16 Mi 문자)을 따를 이유가 없어
  스냅샷만 256 Mi 문자까지 쓴다(`encode_snapshot`, 메모리 예산은 `docs/GRAPH.md`). 형식·바이트는 같다.
- `graphRevision`은 정점 id·종류·모드별 미해석 수와 간선(등급 포함)의 SHA-256이다(위치 제외). 그래서 같은 그래프의
  `graph`·`reach`·`impact`는 모드와 무관하게 같은 값을 싣는다(isthmus는 같은 플랫폼 분석끼리 비교한다).
- 순회 문서는 `dispatch`를 싣는다. 계약상 이것은 모든 도달 정점의 근거 등급을 분류하고(`evidence`를 항상 싣는다)
  잇지 못한 호출이 1개 이상인 모든 root·도달 정점에 `unresolvedCalls`를 싣는다는 선언이다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from pythograph.exchange.document import DocumentLimitError, format_timestamp
from pythograph.graph.model import CallGraph, GraphNode, NodeLocation
from pythograph.graph.traversal import ReachedSymbol, TraversalResult

#: 계약이 허용하는 `unresolvedCalls` 최댓값이다.
MAX_UNRESOLVED_CALLS = 1_000_000

#: 그래프 스냅샷 출력 상한(문자)이다. 스냅샷은 isthmus 입력이 아니므로 isthmus 상한(16 Mi) 대신 메모리 예산으로 정한다:
#: 직렬화 봉우리는 ASCII 출력 문자당 약 2바이트(JSON 문자열 + 인코더 조각 목록, 합성 88 Mi 문자 그래프에서 측정)라
#: 상한에서 약 0.5 GiB를 더 쓴다(비 ASCII id는 문자당 더 쓴다).
MAX_SNAPSHOT_LENGTH = 256 * 1024 * 1024


def encode_snapshot(document: dict[str, object]) -> str:
    """그래프 스냅샷을 다른 문서와 같은 형식(키 정렬·두 칸 들여쓰기·끝 줄바꿈)으로 직렬화한다.

    Args:
        document: 스냅샷 문서.

    Returns:
        JSON 문자열(16 Mi 문자 이하면 `encode_document`와 바이트가 같다).

    Raises:
        DocumentLimitError: 스냅샷 상한을 넘을 때(부분 문서를 내지 않는다).
    """
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if len(text) > MAX_SNAPSHOT_LENGTH:
        raise DocumentLimitError(f"the graph snapshot would exceed {MAX_SNAPSHOT_LENGTH} characters")
    return text


@dataclass(frozen=True)
class GraphHeader:
    """문서 머리 필드.

    Attributes:
        tool_version: pythograph 버전.
        generated_at: 생성 시각(UTC).
        project: 프로젝트 POSIX realpath.
        revision: 소스 revision(없으면 None).
    """

    tool_version: str
    generated_at: datetime
    project: str
    revision: str | None


def graph_revision(graph: CallGraph) -> str:
    """그래프 내용 해시다(위치 제외).

    Args:
        graph: 호출 그래프.

    Returns:
        `sha256:<hex>`.
    """
    payload = {
        "nodes": [
            [node.id, node.kind, [node.unresolved.get(mode, 0) for mode in sorted(node.unresolved)]]
            for node in graph.nodes
        ],
        "edges": [[edge.source, edge.target, list(edge.kinds), edge.evidence] for edge in graph.edges],
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def location_json(location: NodeLocation) -> dict[str, object]:
    """위치를 JSON으로 바꾼다(줄·열은 있을 때만).

    Args:
        location: 위치.

    Returns:
        사전.
    """
    result: dict[str, object] = {"path": location.path}
    if location.line is not None:
        result["line"] = location.line
        if location.column is not None:
            result["column"] = location.column
    return result


def build_graph_document(header: GraphHeader, graph: CallGraph) -> dict[str, object]:
    """그래프 스냅샷 문서를 조립한다.

    Args:
        header: 머리 필드.
        graph: 호출 그래프.

    Returns:
        문서.
    """
    document: dict[str, object] = {
        "format": "pythograph-graph",
        "version": 1,
        "tool": {"name": "pythograph", "version": header.tool_version},
        "generatedAt": format_timestamp(header.generated_at),
        "project": header.project,
        "graphRevision": graph_revision(graph),
        "nodes": [_node_json(node) for node in graph.nodes],
        "edges": [
            {"from": edge.source, "to": edge.target, "kinds": list(edge.kinds), "evidence": edge.evidence}
            for edge in graph.edges
        ],
        "statistics": graph.statistics,
        "limitations": graph.limitations["direct"],
    }
    if header.revision is not None:
        document["revision"] = header.revision
    return document


def _node_json(node: GraphNode) -> dict[str, object]:
    """스냅샷 정점 표기다.

    Args:
        node: 정점.

    Returns:
        사전.
    """
    result: dict[str, object] = {"id": node.id, "kind": node.kind, "location": location_json(node.location)}
    if any(node.unresolved.values()):
        result["unresolvedCalls"] = dict(sorted(node.unresolved.items()))
        result["unresolvedReasons"] = node.reasons
    return result


@dataclass(frozen=True)
class TraversalInput:
    """순회 문서 조립 입력.

    Attributes:
        header: 머리 필드.
        graph: 호출 그래프.
        direction: `dependencies` 또는 `dependents`.
        dispatch: 디스패치 모드.
        requested: 요청 순서의 전체 root id.
        missing: 정점이 아닌 root id.
        result: 요청 순서의 root 인덱스를 쓰는 순회 결과.
    """

    header: GraphHeader
    graph: CallGraph
    direction: str
    dispatch: str
    requested: tuple[str, ...]
    missing: frozenset[str]
    result: TraversalResult


def build_traversal_document(data: TraversalInput) -> dict[str, object]:
    """language-traversal v1 문서를 조립한다.

    Args:
        data: 조립 입력.

    Returns:
        문서.
    """
    nodes = data.graph.node_map()
    reasons = list(data.result.truncation_reasons) + (["root-not-found"] if data.missing else [])
    document: dict[str, object] = {
        "format": "language-traversal",
        "version": 1,
        "tool": {"name": "pythograph", "version": data.header.tool_version},
        "generatedAt": format_timestamp(data.header.generated_at),
        "platform": "python",
        "project": data.header.project,
        "graphRevision": graph_revision(data.graph),
        "dispatch": data.dispatch,
        "direction": data.direction,
        "roots": [_root_json(root, nodes, data) for root in data.requested],
        "reached": [_reached_json(entry, nodes[entry.id], data.dispatch) for entry in data.result.reached],
        "truncated": bool(reasons),
        "limitations": _traversal_limitations(data, nodes),
    }
    if data.header.revision is not None:
        document["revision"] = data.header.revision
    if data.result.roots_truncated:
        document["rootsTruncated"] = True
    if reasons:
        document["truncationReasons"] = sorted(reasons)
    return document


def _unresolved_field(node: GraphNode, dispatch: str) -> dict[str, int]:
    """정점의 모드별 미해석 수 필드다(0이면 싣지 않고 상한을 넘으면 상한).

    Args:
        node: 정점.
        dispatch: 디스패치 모드.

    Returns:
        `{"unresolvedCalls": n}` 또는 빈 사전.
    """
    count = node.unresolved.get(dispatch, 0)
    return {"unresolvedCalls": min(count, MAX_UNRESOLVED_CALLS)} if count else {}


def _root_json(root: str, nodes: dict[str, GraphNode], data: TraversalInput) -> dict[str, object]:
    """root 항목이다. 정점이 아닌 root는 원문 id만 싣는다.

    Args:
        root: root id.
        nodes: 정점 사전.
        data: 조립 입력.

    Returns:
        사전.
    """
    if root in data.missing:
        return {"id": root}
    return {"id": root, "symbol": {"usr": root, "qualifiedName": root}, **_unresolved_field(nodes[root], data.dispatch)}


def _reached_json(entry: ReachedSymbol, node: GraphNode, dispatch: str) -> dict[str, object]:
    """도달 정점 항목이다.

    Args:
        entry: 도달 정점.
        node: 정점.
        dispatch: 디스패치 모드.

    Returns:
        사전.
    """
    result: dict[str, object] = {
        "symbol": {
            "usr": node.id,
            "qualifiedName": node.id,
            "kind": node.kind,
            "location": location_json(node.location),
        },
        "via": entry.via,
        "depth": entry.depth,
        "roots": list(entry.roots),
        "evidence": entry.evidence,
        **_unresolved_field(node, dispatch),
    }
    if entry.relationships:
        result["relationships"] = list(entry.relationships)
    return result


def _traversal_limitations(data: TraversalInput, nodes: dict[str, GraphNode]) -> list[str]:
    """순회 문서 한계다: 모드의 그래프 한계, 정점이 아닌 root, 등급 근사, 미해석 수 상한.

    Args:
        data: 조립 입력.
        nodes: 정점 사전.

    Returns:
        문구 목록.
    """
    lines = list(data.graph.limitations[data.dispatch])
    if data.missing:
        lines.append(
            f"root-not-found: {len(data.missing)} requested root(s) are not graph nodes and are listed without symbol; "
            "pass symbol ids from 'pythograph graph --project <root>' (routes and schema symbol.usr values)"
        )
    if data.result.evidence_approximated:
        lines.append(
            "evidence-approximated: exact per-root evidence tiers would exceed the memory budget; evidence is the "
            "weakest tier of any non-direct edge upstream of the symbol, which may understate but never overstates it"
        )
    touched = [nodes[root] for root in data.requested if root not in data.missing]
    touched.extend(nodes[entry.id] for entry in data.result.reached)
    capped = sum(1 for node in touched if node.unresolved.get(data.dispatch, 0) > MAX_UNRESOLVED_CALLS)
    if capped:
        lines.append(
            f"unresolved-calls-capped: {capped} symbol(s) have more than {MAX_UNRESOLVED_CALLS} unresolved call "
            f"sites; unresolvedCalls reports {MAX_UNRESOLVED_CALLS} for them"
        )
    return lines
