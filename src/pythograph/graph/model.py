"""호출 그래프의 자료 모델: 정점·간선·근거 등급·디스패치 모드.

근거 등급은 간선 집합이 포개진다(`direct ⊂ bound ⊂ candidate`, isthmus `language-traversal` v1). pythograph는
지금 `bound` 간선을 만들지 않으므로 `bound` 모드는 `direct` 그래프를 따른다(`docs/GRAPH.md`). 디스패치 모드는
따라갈 가장 약한 등급을 정한다: `direct`→direct, `bound`→bound, `candidates`→candidate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 근거 등급의 강한 것부터의 순서다.
EVIDENCE_ORDER = ("direct", "bound", "candidate")

#: 디스패치 모드다.
DISPATCH_MODES = ("direct", "bound", "candidates")

#: 모드가 허용하는 가장 약한 등급이다.
_WEAKEST = {"direct": "direct", "bound": "bound", "candidates": "candidate"}


def weakest_evidence(mode: str) -> str:
    """모드가 따라가는 가장 약한 근거 등급을 돌려준다.

    Args:
        mode: 디스패치 모드.

    Returns:
        근거 등급.
    """
    return _WEAKEST[mode]


def evidence_allowed(evidence: str, mode: str) -> bool:
    """간선 등급을 모드가 따라가는지 돌려준다.

    Args:
        evidence: 간선 근거 등급.
        mode: 디스패치 모드.

    Returns:
        따라가면 True.
    """
    return EVIDENCE_ORDER.index(evidence) <= EVIDENCE_ORDER.index(weakest_evidence(mode))


@dataclass(frozen=True)
class NodeLocation:
    """정점 위치. 모듈 정점은 줄·열이 없다(1로 채우지 않는다).

    Attributes:
        path: 프로젝트 상대 POSIX 경로.
        line: 1부터 시작하는 줄(없으면 None).
        column: 1부터 시작하는 UTF-8 바이트 열(없으면 None).
    """

    path: str
    line: int | None = None
    column: int | None = None


@dataclass
class GraphNode:
    """그래프 정점.

    Attributes:
        id: 심볼 id(`<경로>#<점 경로>`, 모듈은 `<경로>#<module>`).
        kind: `module`·`function`·`method`·`class`·`inherited-method`.
        location: 위치.
        unresolved: 디스패치 모드 → 잇지 못한 자기 호출 지점 수.
        reasons: 이유 → 수(`direct` 기준, 스냅샷 진단용).
    """

    id: str
    kind: str
    location: NodeLocation
    unresolved: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphEdge:
    """방향 간선(`source`가 `target`에 기댄다).

    Attributes:
        source: 출발 정점 id.
        target: 도착 정점 id.
        kinds: 간선 종류(정렬).
        evidence: 근거 등급.
    """

    source: str
    target: str
    kinds: tuple[str, ...]
    evidence: str


@dataclass
class CallGraph:
    """완성한 호출 그래프.

    Attributes:
        nodes: id 순 정점.
        edges: (출발, 도착, 등급) 순 간선.
        limitations: 디스패치 모드 → 한계 문구.
        statistics: 스냅샷에 싣는 집계.
    """

    nodes: list[GraphNode]
    edges: list[GraphEdge]
    limitations: dict[str, list[str]]
    statistics: dict[str, object]

    def node_map(self) -> dict[str, GraphNode]:
        """id → 정점 사전을 만든다.

        Returns:
            사전.
        """
        return {node.id: node for node in self.nodes}
