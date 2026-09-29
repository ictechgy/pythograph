"""그래프를 여러 root에서 한 번에 정방향(dependencies) 또는 역방향(dependents)으로 훑는다.

의미(isthmus `language-traversal` v1, tsograph `src/graph/traversal.ts`와 같은 알고리즘):
- `reached`는 자기 자신이 아닌 root에서 간선 1개 이상으로(깊이 상한 안에서) 닿은 정점 전부다. root이기도 한 정점도
  싣되 `roots`에는 그에 닿는 다른 root만 넣는다. 자기 자신에게서만 닿는 root는 싣지 않는다.
- `depth`는 그 root들 중 가장 가까운 것까지의 거리, `via`는 가장 가까운 root(같은 거리면 작은 인덱스)에서 depth-1
  거리에 있는 선행 정점 중 id가 가장 작은 것이다(UTF-16 코드 단위 순서, 깊이 1이면 그 root id).
- 구현은 모든 root를 한 번에 출발시키는 단계 동기 너비 우선 패스다. 정점이 자기 자신이 아닌 더 작은 인덱스 root를
  65개 이상 가졌으면 더 큰 인덱스 root는 그 정점에서 전파를 멈춘다(출력의 작은 인덱스 64개·depth·via를 바꿀 수
  없다). 옛 root별 알고리즘과의 동등성은 무작위 그래프 테스트(`tests/test_traversal_oracle.py`)로 확인한다.
- `evidence`는 root별 하한이다: 정점에 깊이 상한 안에서 닿는 모든 root 각각의 가장 강한 등급 중 가장 약한 것.
  약한 간선 출발점에 닿지 못하는 root는 모든 등급에서 같게 닿으므로 비교에서 뺀다. 비트 집합이 메모리 상한을 넘으면
  약하게 적을 수는 있어도 부풀리지 않는 근사로 바꾼다(`evidence_approximated`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pythograph.graph.model import EVIDENCE_ORDER, CallGraph, GraphEdge, evidence_allowed, weakest_evidence

#: 정점 하나가 싣는 root 인덱스의 상한이다(계약의 `rootsTruncated`).
MAX_ROOTS_PER_NODE = 64

#: 계약이 허용하는 최대 깊이다.
MAX_TRAVERSAL_DEPTH = 128

#: 계약이 허용하는 최대 도달 정점 수다.
MAX_REACHED = 100_000

#: 전파를 멈추게 하는 더 작은 root 수다(아래 정점이 자기 인덱스를 빼도 64개가 남도록 하나 더 둔다).
DOMINATING_ROOTS = MAX_ROOTS_PER_NODE + 1

#: 정확한 등급 비교(root당 비트 하나)가 쓸 수 있는 메모리 상한(바이트)이다.
EVIDENCE_MEMORY_BYTES = 64 * 1024 * 1024


def utf16_key(text: str) -> bytes:
    """UTF-16 코드 단위 순서 비교 키다(계약의 정렬 규칙, locale 무관).

    Args:
        text: 문자열.

    Returns:
        비교 키.
    """
    return text.encode("utf-16-be", "surrogatepass")


@dataclass(frozen=True)
class TraversalRequest:
    """순회 요청.

    Attributes:
        root_ids: root id(모두 정점이고 서로 다르다).
        direction: `dependencies` 또는 `dependents`.
        max_depth: 최대 깊이(1~128).
        max_reached: 최대 도달 정점 수.
        dispatch: 디스패치 모드.
        evidence_memory_bytes: 등급 비교 메모리 상한(테스트 주입용).
    """

    root_ids: tuple[str, ...]
    direction: str
    max_depth: int
    max_reached: int
    dispatch: str
    evidence_memory_bytes: int = EVIDENCE_MEMORY_BYTES


@dataclass(frozen=True)
class ReachedSymbol:
    """도달 정점 하나.

    Attributes:
        id: 정점 id.
        via: 최단 경로의 직전 정점.
        depth: 가장 가까운 root까지의 거리.
        roots: 닿는 root 인덱스(오름차순, 최대 64개).
        relationships: via와 이 정점 사이 간선 종류(정렬).
        evidence: root별 하한 근거 등급.
    """

    id: str
    via: str
    depth: int
    roots: tuple[int, ...]
    relationships: tuple[str, ...]
    evidence: str


@dataclass(frozen=True)
class TraversalResult:
    """순회 결과.

    Attributes:
        reached: (depth, id) 순 도달 정점.
        truncation_reasons: 정렬한 잘림 이유(`depth`·`max-reached`).
        roots_truncated: 정점당 root 64개 상한이 적용됐는지.
        evidence_approximated: 근거 등급을 보수적으로 근사했는지.
    """

    reached: tuple[ReachedSymbol, ...]
    truncation_reasons: tuple[str, ...]
    roots_truncated: bool
    evidence_approximated: bool


@dataclass(frozen=True)
class Adjacency:
    """방향에 맞춘 이웃 목록과 간선 종류 조회표.

    Attributes:
        neighbors: 정점 → 이웃(간선 순서).
        predecessors: 정점 → 선행 정점(UTF-16 순).
        kinds: (출발, 도착) → 종류(정렬).
    """

    neighbors: dict[str, list[str]]
    predecessors: dict[str, list[str]]
    kinds: dict[tuple[str, str], tuple[str, ...]]


#: 정점별 (root 인덱스 → 처음 닿은 단계) 기록이다.
Levels = dict[str, dict[int, int]]


def traverse(graph: CallGraph, request: TraversalRequest) -> TraversalResult:
    """순회한다.

    Args:
        graph: 호출 그래프.
        request: 순회 요청.

    Returns:
        순회 결과.
    """
    tier = weakest_evidence(request.dispatch)
    adjacency = build_adjacency(graph, request.direction, tier)
    levels, depth_cut, pruned = _propagate(adjacency, request.root_ids, request.max_depth)
    rows = _reached_rows(adjacency, levels, request.root_ids)
    kept = rows[: request.max_reached]
    reasons = (["depth"] if depth_cut else []) + (["max-reached"] if len(rows) > len(kept) else [])
    roots_truncated = pruned or any(len(row.roots) > MAX_ROOTS_PER_NODE for row in kept)
    evidence_of, approximated = _evidence_tiers(graph, request, adjacency, tier, levels)
    reached = tuple(
        ReachedSymbol(
            row.id, row.via, row.depth, row.roots[:MAX_ROOTS_PER_NODE], row.relationships, evidence_of(row.id)
        )
        for row in kept
    )
    return TraversalResult(reached, tuple(reasons), roots_truncated, approximated)


def build_adjacency(graph: CallGraph, direction: str, tier: str) -> Adjacency:
    """방향과 근거 범위에 맞춘 인접 목록을 만든다. 같은 쌍의 등급별 간선은 하나로 합치고 종류를 합친다.

    Args:
        graph: 호출 그래프.
        direction: 방향.
        tier: 따라갈 가장 약한 근거.

    Returns:
        인접 목록.
    """
    neighbors: dict[str, list[str]] = {}
    predecessors: dict[str, list[str]] = {}
    kinds: dict[tuple[str, str], tuple[str, ...]] = {}
    limit = EVIDENCE_ORDER.index(tier)
    for edge in graph.edges:
        if EVIDENCE_ORDER.index(edge.evidence) > limit:
            continue
        source, target = (edge.source, edge.target) if direction == "dependencies" else (edge.target, edge.source)
        known = kinds.get((source, target))
        if known is None:
            neighbors.setdefault(source, []).append(target)
            predecessors.setdefault(target, []).append(source)
            kinds[(source, target)] = edge.kinds
        else:
            kinds[(source, target)] = tuple(sorted(set(known) | set(edge.kinds)))
    for items in predecessors.values():
        items.sort(key=utf16_key)
    return Adjacency(neighbors, predecessors, kinds)


def _propagate(adjacency: Adjacency, root_ids: tuple[str, ...], max_depth: int) -> tuple[Levels, bool, bool]:
    """모든 root에서 단계 동기로 전파한다.

    Args:
        adjacency: 인접 목록.
        root_ids: root id.
        max_depth: 최대 깊이.

    Returns:
        (단계 기록, 깊이 잘림 여부, 전파 중단 여부).
    """
    levels: Levels = {}
    owners = {root: index for index, root in enumerate(root_ids)}
    frontier: dict[str, list[int]] = {}
    for index, root in enumerate(root_ids):
        levels[root] = {index: 0}
        frontier[root] = [index]
    pruned = False
    for level in range(max_depth):
        if not frontier:
            break
        following: dict[str, list[int]] = {}
        for node, roots in frontier.items():
            for neighbor in adjacency.neighbors.get(node, []):
                pruned = _spread(levels, owners.get(neighbor), neighbor, roots, level + 1, following) or pruned
        frontier = following
    return levels, _has_depth_cut(adjacency, levels, frontier), pruned


def _spread(
    levels: Levels, owner: int | None, neighbor: str, roots: list[int], level: int, following: dict[str, list[int]]
) -> bool:
    """한 간선으로 root 인덱스들을 이웃에 전파한다.

    Args:
        levels: 단계 기록(갱신).
        owner: 이웃이 root면 그 인덱스.
        neighbor: 이웃 정점.
        roots: 넘길 root 인덱스.
        level: 이웃이 닿는 단계.
        following: 다음 단계 경계(갱신).

    Returns:
        전파를 멈춘 쌍이 있었으면 True.
    """
    held = levels.setdefault(neighbor, {})
    pruned = False
    for root in roots:
        if root in held:
            continue
        if _dominated(held, owner, root):
            pruned = True
            continue
        held[root] = level
        following.setdefault(neighbor, []).append(root)
    return pruned


def _dominated(held: dict[int, int], owner: int | None, root: int) -> bool:
    """정점이 자기 자신이 아닌 root 중 이 root보다 작은 인덱스를 이미 65개 이상 가졌는지 본다.

    Args:
        held: 정점의 root 기록.
        owner: 정점이 root면 그 인덱스(세지 않는다).
        root: 새 root 인덱스.

    Returns:
        65개 이상이면 True.
    """
    if len(held) - (0 if owner is None else 1) < DOMINATING_ROOTS:
        return False
    smaller = 0
    for index in held:
        if index != owner and index < root:
            smaller += 1
            if smaller >= DOMINATING_ROOTS:
                return True
    return False


def _has_depth_cut(adjacency: Adjacency, levels: Levels, frontier: dict[str, list[int]]) -> bool:
    """마지막 단계 경계에서 아직 그 root를 갖지 않은 이웃이 있으면 깊이 상한에 잘린 것이다.

    Args:
        adjacency: 인접 목록.
        levels: 단계 기록.
        frontier: 최대 깊이 단계의 경계.

    Returns:
        잘렸으면 True.
    """
    for node, roots in frontier.items():
        for neighbor in adjacency.neighbors.get(node, []):
            held = levels.get(neighbor, {})
            if any(root not in held for root in roots):
                return True
    return False


@dataclass(frozen=True)
class _Row:
    """출력 전 도달 행."""

    id: str
    via: str
    depth: int
    roots: tuple[int, ...]
    relationships: tuple[str, ...]


def _reached_rows(adjacency: Adjacency, levels: Levels, root_ids: tuple[str, ...]) -> list[_Row]:
    """단계 기록에서 도달 행을 만든다. (depth, UTF-16 id) 순이다.

    Args:
        adjacency: 인접 목록.
        levels: 단계 기록.
        root_ids: root id.

    Returns:
        도달 행.
    """
    owners = {root: index for index, root in enumerate(root_ids)}
    rows: list[_Row] = []
    for node, held in levels.items():
        owner = owners.get(node)
        roots = tuple(sorted(index for index in held if index != owner))
        if not roots:
            continue
        depth = min(held[index] for index in roots)
        nearest = next(index for index in roots if held[index] == depth)
        via = next(
            candidate
            for candidate in adjacency.predecessors.get(node, [])
            if levels.get(candidate, {}).get(nearest) == depth - 1
        )
        rows.append(_Row(node, via, depth, roots, adjacency.kinds.get((via, node), ())))
    rows.sort(key=lambda row: (row.depth, utf16_key(row.id)))
    return rows


def _evidence_tiers(
    graph: CallGraph, request: TraversalRequest, full: Adjacency, tier: str, levels: Levels
) -> tuple[Callable[[str], str], bool]:
    """정점의 root별 하한 근거 등급을 구하는 함수를 만든다.

    Args:
        graph: 호출 그래프.
        request: 순회 요청.
        full: 모드가 허용하는 전체 인접 목록.
        tier: 모드가 허용하는 가장 약한 근거.
        levels: 전체 그래프 단계 기록.

    Returns:
        (정점 id → 등급 함수, 근사했는지).
    """
    tiers = EVIDENCE_ORDER[: EVIDENCE_ORDER.index(tier) + 1]
    weak = [
        edge for edge in graph.edges if edge.evidence != "direct" and evidence_allowed(edge.evidence, request.dispatch)
    ]
    sources = {edge.source if request.direction == "dependencies" else edge.target for edge in weak}
    compared = _weak_touching_roots(full, request.root_ids, sources, request.max_depth)
    if len(tiers) == 1 or not compared:
        return (lambda _node: "direct"), False
    estimate = len(graph.nodes) * ((len(compared) + 7) // 8 + 32) * (len(tiers) + 1)
    if estimate > request.evidence_memory_bytes:
        return _approximate_evidence(full, weak, request.direction, levels), True
    full_bits = _reach_bits(full, compared, request.max_depth)
    lower = [
        _reach_bits(build_adjacency(graph, request.direction, strong), compared, request.max_depth)
        for strong in tiers[:-1]
    ]

    def evidence_of(node: str) -> str:
        """정점의 등급: 전체 그래프와 같은 root 비트를 내는 가장 강한 등급이다."""
        reference = full_bits.get(node, 0)
        for position, bits in enumerate(lower):
            if bits.get(node, 0) == reference:
                return tiers[position]
        return tiers[-1]

    return evidence_of, False


def _weak_touching_roots(
    full: Adjacency, root_ids: tuple[str, ...], sources: set[str], max_depth: int
) -> tuple[str, ...]:
    """깊이 상한 안에서 약한 간선의 출발점에 닿을 수 있는 root다.

    Args:
        full: 전체 인접 목록.
        root_ids: root id.
        sources: 약한 간선의 출발점(순회 방향 기준).
        max_depth: 최대 깊이.

    Returns:
        root id(입력 순서).
    """
    seen = set(sources)
    frontier = list(sources)
    for _ in range(max_depth - 1):
        if not frontier:
            break
        following: list[str] = []
        for node in frontier:
            for previous in full.predecessors.get(node, []):
                if previous not in seen:
                    seen.add(previous)
                    following.append(previous)
        frontier = following
    return tuple(root for root in root_ids if root in seen)


def _reach_bits(adjacency: Adjacency, roots: tuple[str, ...], max_depth: int) -> dict[str, int]:
    """root마다 비트 하나를 두고 단계 동기로 전파해 깊이 상한 안에서 각 정점에 닿는 root 비트 집합을 구한다.

    Args:
        adjacency: 인접 목록.
        roots: 비교 대상 root id(순번이 비트 위치).
        max_depth: 최대 깊이.

    Returns:
        정점 → 비트 집합(정수).
    """
    seen: dict[str, int] = {}
    frontier: dict[str, int] = {}
    for position, root in enumerate(roots):
        seen[root] = seen.get(root, 0) | (1 << position)
        frontier[root] = frontier.get(root, 0) | (1 << position)
    for _ in range(max_depth):
        if not frontier:
            break
        following: dict[str, int] = {}
        for node, bits in frontier.items():
            for neighbor in adjacency.neighbors.get(node, []):
                fresh = bits & ~seen.get(neighbor, 0)
                if fresh:
                    seen[neighbor] = seen.get(neighbor, 0) | fresh
                    following[neighbor] = following.get(neighbor, 0) | fresh
        frontier = following
    return seen


def _approximate_evidence(
    full: Adjacency, weak: list[GraphEdge], direction: str, levels: Levels
) -> Callable[[str], str]:
    """메모리 상한을 넘을 때의 보수적 근거 등급이다.

    root에서 닿는 출발점을 가진 약한 간선의 도착점에서 깊이 제한 없이 닿는 정점을 등급별로 표시하고, 정점이 속한
    가장 약한 등급을 싣는다. 어느 표시에도 들지 않는 정점은 모든 root에서 약한 간선 없이 닿으므로 정확히 direct다.

    Args:
        full: 전체 인접 목록.
        weak: 약한 간선.
        direction: 방향.
        levels: 전체 그래프 단계 기록.

    Returns:
        정점 id → 등급 함수.
    """
    marks: dict[str, str] = {}
    for evidence in ("bound", "candidate"):
        heads: set[str] = set()
        for edge in weak:
            tail, head = (edge.source, edge.target) if direction == "dependencies" else (edge.target, edge.source)
            if edge.evidence == evidence and tail in levels:
                heads.add(head)
        _mark_reachable(full, heads, evidence, marks)
    return lambda node: marks.get(node, "direct")


def _mark_reachable(full: Adjacency, heads: set[str], evidence: str, marks: dict[str, str]) -> None:
    """시작 정점들에서 닿는 정점에 등급을 표시한다(뒤 등급이 덮는다).

    Args:
        full: 전체 인접 목록.
        heads: 시작 정점.
        evidence: 표시할 등급.
        marks: 표시(갱신).
    """
    pending = sorted(heads)
    seen = set(pending)
    while pending:
        node = pending.pop()
        marks[node] = evidence
        for neighbor in full.neighbors.get(node, []):
            if neighbor not in seen:
                seen.add(neighbor)
                pending.append(neighbor)
