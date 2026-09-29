"""다중 출발 단일 패스 `traverse`를 root별 너비 우선 알고리즘(테스트 오라클)과 무작위 그래프에서 비교한다.

오라클은 계약의 의미를 그대로 계산한다: root마다 최단 거리, 자기 자신이 아닌 root만 싣는 `roots`, 가장 가까운
root(같으면 작은 인덱스) 기준 depth, 그 root에서 depth-1에 있는 선행 정점 중 가장 작은 id(UTF-16 순)의 via.
근거 등급은 등급별 그래프에서 root마다 거리를 구해, 정점에 깊이 상한 안에서 닿는 모든 root(자기 제외, 64개
상한과 무관) 각각의 가장 강한 등급 중 가장 약한 것이다. `rootsTruncated`가 거짓이면 잘림 이유도 같아야 하고,
참이면 `depth` 이유는 단일 패스 쪽이 부분집합일 수 있다(tsograph 오라클과 같은 조건).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace

import pytest

from pythograph.graph.model import EVIDENCE_ORDER, CallGraph, GraphEdge, GraphNode, NodeLocation, weakest_evidence
from pythograph.graph.traversal import (
    MAX_ROOTS_PER_NODE,
    ReachedSymbol,
    TraversalRequest,
    TraversalResult,
    traverse,
    utf16_key,
)


@dataclass
class OracleGraph:
    """오라클의 방향·등급별 그래프."""

    neighbors: dict[str, list[str]]
    predecessors: dict[str, list[str]]
    kinds: dict[tuple[str, str], tuple[str, ...]]


def oracle_graph(edges: list[GraphEdge], forward: bool, tier: str) -> OracleGraph:
    """등급 이하 간선으로 방향 그래프를 만든다(같은 쌍의 종류는 합친다).

    Args:
        edges: 간선.
        forward: 정방향이면 True.
        tier: 허용하는 가장 약한 등급.

    Returns:
        오라클 그래프.
    """
    graph = OracleGraph({}, {}, {})
    for edge in edges:
        if EVIDENCE_ORDER.index(edge.evidence) > EVIDENCE_ORDER.index(tier):
            continue
        source, target = (edge.source, edge.target) if forward else (edge.target, edge.source)
        if (source, target) not in graph.kinds:
            graph.neighbors.setdefault(source, []).append(target)
            graph.predecessors.setdefault(target, []).append(source)
        graph.kinds[(source, target)] = tuple(sorted(set(graph.kinds.get((source, target), ())) | set(edge.kinds)))
    return graph


def distances_from(graph: OracleGraph, root: str, max_depth: int) -> tuple[dict[str, int], bool]:
    """root 하나의 깊이 제한 너비 우선 거리다.

    Args:
        graph: 오라클 그래프.
        root: root id.
        max_depth: 최대 깊이.

    Returns:
        (거리, 깊이 잘림 여부).
    """
    depths = {root: 0}
    queue = [root]
    cut = False
    for node in queue:
        depth = depths[node]
        following = graph.neighbors.get(node, [])
        if depth >= max_depth:
            cut = cut or any(neighbor not in depths for neighbor in following)
            continue
        for neighbor in following:
            if neighbor not in depths:
                depths[neighbor] = depth + 1
                queue.append(neighbor)
    return depths, cut


def oracle(graph: CallGraph, request: TraversalRequest) -> TraversalResult:
    """root별 알고리즘이다(오라클).

    Args:
        graph: 그래프.
        request: 요청.

    Returns:
        순회 결과.
    """
    forward = request.direction == "dependencies"
    tiers = EVIDENCE_ORDER[: EVIDENCE_ORDER.index(weakest_evidence(request.dispatch)) + 1]
    full = oracle_graph(graph.edges, forward, tiers[-1])
    runs = [distances_from(full, root, request.max_depth) for root in request.root_ids]
    tier_depths = [
        [
            distances_from(oracle_graph(graph.edges, forward, tier), root, request.max_depth)[0]
            for root in request.root_ids
        ]
        for tier in tiers
    ]
    rows = []
    for node in {key for depths, _ in runs for key in depths}:
        roots = [index for index, (depths, _) in enumerate(runs) if request.root_ids[index] != node and node in depths]
        if not roots:
            continue
        depth = min(runs[index][0][node] for index in roots)
        nearest = next(index for index in roots if runs[index][0][node] == depth)
        via = sorted(
            (item for item in full.predecessors.get(node, []) if runs[nearest][0].get(item) == depth - 1), key=utf16_key
        )[0]
        weakest = max(
            next(level for level, by_root in enumerate(tier_depths) if node in by_root[index]) for index in roots
        )
        rows.append((depth, node, via, tuple(roots), full.kinds.get((via, node), ()), tiers[weakest]))
    rows.sort(key=lambda row: (row[0], utf16_key(row[1])))
    kept = rows[: request.max_reached]
    reasons = (["depth"] if any(cut for _, cut in runs) else []) + (["max-reached"] if len(rows) > len(kept) else [])
    reached = tuple(
        ReachedSymbol(node, via, depth, roots[:MAX_ROOTS_PER_NODE], kinds, evidence)
        for depth, node, via, roots, kinds, evidence in kept
    )
    truncated = any(len(roots) > MAX_ROOTS_PER_NODE for _, _, _, roots, _, _ in kept)
    return TraversalResult(reached, tuple(reasons), truncated, False)


def random_case(
    generator: random.Random, size: int, density: float, root_count: int
) -> tuple[CallGraph, TraversalRequest]:
    """무작위 그래프와 요청을 만든다.

    Args:
        generator: 난수 생성기.
        size: 정점 수.
        density: 간선 확률.
        root_count: root 수.

    Returns:
        (그래프, 요청).
    """
    ids = [f"n{index:03d}" if generator.random() < 0.9 else f"\U0001f600{index}" for index in range(size)]
    ids.extend(["ｚzz", "\U00010000aa"])
    nodes = [GraphNode(node, "function", NodeLocation("a.py", 1, 1)) for node in ids]
    edges = []
    for source in ids:
        for target in ids:
            if generator.random() < density:
                evidence = generator.choices(EVIDENCE_ORDER, weights=(6, 2, 2))[0]
                edges.append(GraphEdge(source, target, (generator.choice(["call", "reference", "new"]),), evidence))
    request = TraversalRequest(
        root_ids=tuple(generator.sample(ids, min(root_count, len(ids)))),
        direction=generator.choice(["dependencies", "dependents"]),
        max_depth=generator.choice([1, 2, 3, 128]),
        max_reached=generator.choice([3, 100_000]),
        dispatch=generator.choice(["direct", "bound", "candidates"]),
    )
    graph = CallGraph(nodes, edges, {mode: [] for mode in ("direct", "bound", "candidates")}, {})
    return graph, request


def assert_equivalent(actual: TraversalResult, expected: TraversalResult) -> None:
    """단일 패스 결과가 오라클과 같은지 확인한다.

    Args:
        actual: 단일 패스 결과.
        expected: 오라클 결과.
    """
    assert actual.reached == expected.reached
    assert actual.roots_truncated == expected.roots_truncated or actual.roots_truncated
    if not actual.roots_truncated:
        assert actual.truncation_reasons == expected.truncation_reasons
    else:
        assert set(actual.truncation_reasons) <= set(expected.truncation_reasons) | {"depth"}


@pytest.mark.parametrize("seed", range(120))
def test_single_pass_matches_per_root_oracle(seed: int) -> None:
    """무작위 그래프에서 단일 패스와 root별 오라클이 같다(도달·via·depth·roots·등급·잘림)."""
    generator = random.Random(seed)
    graph, request = random_case(generator, generator.randint(2, 18), generator.choice([0.05, 0.15, 0.3]), 4)
    assert_equivalent(traverse(graph, request), oracle(graph, request))


def test_many_roots_pruning_matches_oracle() -> None:
    """root가 65개를 넘어 전파를 멈춰도 출력(작은 인덱스 64개·depth·via)이 오라클과 같다."""
    truncated = 0
    for seed in range(8):
        generator = random.Random(1000 + seed)
        graph, request = random_case(generator, 90, 0.05, 80)
        request = replace(request, max_reached=100_000, max_depth=generator.choice([2, 128]))
        actual, expected = traverse(graph, request), oracle(graph, request)
        assert actual.reached == expected.reached
        assert actual.roots_truncated == expected.roots_truncated
        truncated += actual.roots_truncated
    assert truncated >= 3


def test_traversal_is_deterministic() -> None:
    """같은 입력이면 같은 결과다(간선 순서를 섞어도 같다)."""
    generator = random.Random(7)
    graph, request = random_case(generator, 30, 0.1, 5)
    shuffled = list(graph.edges)
    generator.shuffle(shuffled)
    other = CallGraph(graph.nodes, sorted(shuffled, key=lambda edge: (edge.source, edge.target)), graph.limitations, {})
    ordered = CallGraph(
        graph.nodes, sorted(graph.edges, key=lambda edge: (edge.source, edge.target)), graph.limitations, {}
    )
    assert traverse(ordered, request) == traverse(other, request) == traverse(ordered, request)


@pytest.mark.parametrize("seed", range(20))
def test_evidence_approximation_never_overstates(seed: int) -> None:
    """메모리 상한 근사는 등급을 약하게 적을 수는 있어도 부풀리지 않는다."""
    generator = random.Random(2000 + seed)
    graph, request = random_case(generator, 16, 0.2, 4)
    request = replace(request, max_reached=100_000, dispatch="candidates")
    exact = traverse(graph, request)
    approximate = traverse(graph, replace(request, evidence_memory_bytes=1))
    assert [entry.id for entry in approximate.reached] == [entry.id for entry in exact.reached]
    for strict, loose in zip(exact.reached, approximate.reached, strict=True):
        assert EVIDENCE_ORDER.index(loose.evidence) >= EVIDENCE_ORDER.index(strict.evidence)
    assert not exact.evidence_approximated
