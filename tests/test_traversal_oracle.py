"""다중 출발 단일 패스 `traverse`를 root별 너비 우선 알고리즘(테스트 오라클)과 무작위 그래프에서 비교한다.

오라클은 계약의 의미를 그대로 계산한다: root마다 최단 거리, 자기 자신이 아닌 root만 싣는 `roots`, 가장 가까운
root(같으면 작은 인덱스) 기준 depth, 그 root에서 depth-1에 있는 선행 정점 중 가장 작은 id(UTF-16 순)의 via.
근거 등급은 등급별 그래프에서 root마다 거리를 구해, 정점에 깊이 상한 안에서 닿는 모든 root(자기 제외, 64개
상한과 무관) 각각의 가장 강한 등급 중 가장 약한 것이다. `rootsTruncated`가 거짓이면 잘림 이유도 같아야 하고,
참이면 `depth` 이유는 단일 패스 쪽이 부분집합일 수 있다(tsograph 오라클과 같은 조건).

`bound` 간선이 많은 그래프의 `bound` 모드, 모드 사이 등급 포개짐(direct ⊆ bound ⊆ candidates), 실제로 만든 그래프
(bound·candidate 간선 포함)에서도 같은 비교를 한다. 전파 중단 판정은 정점 기록 전체를 세던 단일 패스
(`linear_propagate`)와도 단계 기록 단위로 비교한다(정렬되지 않은 root가 수백 개 모이는 허브, 65개 경계).
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from pythograph.graph import traversal as traversal_module
from pythograph.graph.build import build_graph
from pythograph.graph.model import EVIDENCE_ORDER, CallGraph, GraphEdge, GraphNode, NodeLocation, weakest_evidence
from pythograph.graph.traversal import (
    DOMINATING_ROOTS,
    MAX_ROOTS_PER_NODE,
    Adjacency,
    Levels,
    ReachedSymbol,
    TraversalRequest,
    TraversalResult,
    build_adjacency,
    traverse,
    utf16_key,
)
from pythograph.source.project import Project


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


def bound_heavy_case(generator: random.Random) -> tuple[CallGraph, TraversalRequest]:
    """`bound` 간선이 많은 무작위 그래프와 `bound` 모드 요청이다(등급 비중 direct 3 : bound 5 : candidate 2).

    Args:
        generator: 난수 생성기.

    Returns:
        (그래프, 요청).
    """
    graph, request = random_case(generator, generator.randint(4, 20), generator.choice([0.08, 0.2]), 5)
    edges = [replace(edge, evidence=generator.choices(EVIDENCE_ORDER, weights=(3, 5, 2))[0]) for edge in graph.edges]
    return CallGraph(graph.nodes, edges, graph.limitations, {}), replace(request, dispatch="bound", max_reached=100_000)


@pytest.mark.parametrize("seed", range(60))
def test_bound_mode_matches_oracle(seed: int) -> None:
    """`bound` 간선이 많은 그래프의 `bound` 모드도 root별 오라클과 같다(root별 하한 등급 포함)."""
    graph, request = bound_heavy_case(random.Random(3000 + seed))
    assert_equivalent(traverse(graph, request), oracle(graph, request))


@pytest.mark.parametrize("seed", range(40))
def test_tiers_nest_across_modes(seed: int) -> None:
    """등급이 포개진다: direct 도달 ⊆ bound 도달 ⊆ candidates 도달, 모드가 허용하지 않는 등급은 싣지 않는다.

    root 하나면 등급은 그 root에서 정점에 닿는 가장 강한 등급 그래프이므로, direct로 닿는 정점은 어느 모드에서도
    `direct`이고 bound 모드에서 `direct`인 정점은 candidates 모드에서도 `direct`다(root가 여럿이면 다른 root가 약한
    등급으로만 닿아 하한이 내려갈 수 있다).
    """
    graph, request = bound_heavy_case(random.Random(4000 + seed))
    request = replace(request, max_depth=128, root_ids=request.root_ids[:1])
    results = {mode: traverse(graph, replace(request, dispatch=mode)) for mode in ("direct", "bound", "candidates")}
    reached = {mode: {entry.id: entry.evidence for entry in result.reached} for mode, result in results.items()}
    assert set(reached["direct"]) <= set(reached["bound"]) <= set(reached["candidates"])
    assert set(reached["direct"].values()) <= {"direct"}
    assert set(reached["bound"].values()) <= {"direct", "bound"}
    for node in reached["direct"]:
        assert reached["bound"][node] == reached["candidates"][node] == "direct"
    for node, evidence in reached["bound"].items():
        assert evidence != "direct" or reached["candidates"][node] == "direct"
        assert evidence != "bound" or reached["candidates"][node] == "bound"


@pytest.mark.parametrize("seed", range(20))
def test_bound_evidence_approximation_never_overstates(seed: int) -> None:
    """`bound` 모드의 메모리 상한 근사도 등급을 부풀리지 않는다."""
    graph, request = bound_heavy_case(random.Random(5000 + seed))
    exact = traverse(graph, request)
    approximate = traverse(graph, replace(request, evidence_memory_bytes=1))
    assert [entry.id for entry in approximate.reached] == [entry.id for entry in exact.reached]
    for strict, loose in zip(exact.reached, approximate.reached, strict=True):
        assert EVIDENCE_ORDER.index(loose.evidence) >= EVIDENCE_ORDER.index(strict.evidence)


def test_built_bound_graph_matches_oracle(make_project: Callable[[dict[str, str]], Path]) -> None:
    """실제로 만든 그래프(bound·candidate 간선 포함)에서도 모든 모드·방향이 오라클과 같다."""
    root = make_project(
        {
            "repos.py": """
                class Repo:
                    def save(self):
                        return audit()


                class SqlRepo(Repo):
                    def save(self):
                        return audit()


                class MemRepo(Repo):
                    def save(self):
                        return 1


                def audit():
                    return 2
            """,
            "app.py": """
                from repos import MemRepo, Repo, SqlRepo


                class Service:
                    def __init__(self, repo):
                        self.repo = repo

                    def run(self):
                        return self.repo.save()


                def typed(repo: Repo):
                    return repo.save()


                def main(flag):
                    repo = SqlRepo()
                    if flag:
                        repo = MemRepo()
                    typed(SqlRepo())
                    return Service(repo).run()
            """,
        }
    )
    graph = build_graph(Project.open(root), False)
    assert {edge.evidence for edge in graph.edges} == {"direct", "bound", "candidate"}
    roots = ("app.py#main", "app.py#typed", "repos.py#audit")
    for mode in ("direct", "bound", "candidates"):
        for direction in ("dependencies", "dependents"):
            request = TraversalRequest(roots, direction, 128, 100_000, mode)
            assert_equivalent(traverse(graph, request), oracle(graph, request))


def linear_propagate(adjacency: Adjacency, root_ids: tuple[str, ...], max_depth: int) -> tuple[Levels, bool, bool]:
    """정점 기록 전체를 세어 전파 중단을 판정하던 단일 패스 전파다(지배 판정 최적화의 오라클).

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
                held = levels.setdefault(neighbor, {})
                owner = owners.get(neighbor)
                for root in roots:
                    if root in held:
                        continue
                    smaller = sum(1 for index in held if index != owner and index < root)
                    if smaller >= DOMINATING_ROOTS:
                        pruned = True
                        continue
                    held[root] = level + 1
                    following.setdefault(neighbor, []).append(root)
        frontier = following
    return levels, traversal_module._has_depth_cut(adjacency, levels, frontier), pruned


def hub_case(generator: random.Random, root_count: int, direction: str) -> tuple[CallGraph, TraversalRequest]:
    """root 여럿이 서로 다른 거리에서 허브 세 개로 모이고 허브 뒤로 사슬이 이어지는 그래프와 요청이다.

    root 인덱스와 허브까지 거리를 따로 섞어 허브에 root가 정렬되지 않은 순서로 도착한다(큰 인덱스가 먼저 오기도
    하고 작은 인덱스가 뒤에 와서 기록을 바꾸기도 한다). 허브 하나는 root이기도 하다(자기 인덱스는 세지 않는다).
    사슬·허브 간선은 `direct`라 모든 모드에서 따라가고, 덧붙인 무작위 간선만 등급을 섞는다.

    Args:
        generator: 난수 생성기.
        root_count: 허브로 모이는 root 수.
        direction: 방향(간선을 그 방향으로 따라가면 root에서 허브로 간다).

    Returns:
        (그래프, 요청).
    """
    edges: list[GraphEdge] = []

    def connect(source: str, target: str, evidence: str = "direct") -> None:
        """순회 방향으로 source에서 target으로 가는 간선을 더한다."""
        pair = (source, target) if direction == "dependencies" else (target, source)
        edges.append(GraphEdge(pair[0], pair[1], ("call",), evidence))

    hubs = [f"hub{index}" for index in range(3)]
    ids = list(hubs)
    roots = []
    for index in range(root_count):
        previous = f"r{index:03d}"
        roots.append(previous)
        ids.append(previous)
        for step in range(generator.randint(0, 3)):
            current = f"c{index:03d}_{step}"
            ids.append(current)
            connect(previous, current)
            previous = current
        connect(previous, generator.choice(hubs))
    for hub in hubs:
        previous = hub
        for step in range(4):
            current = f"{hub}_t{step}"
            ids.append(current)
            connect(previous, current)
            previous = current
    connect("hub0", "hub1")
    connect("hub1", "hub2")
    connect("hub2", "hub0")
    for _ in range(root_count // 4):
        connect(generator.choice(ids), generator.choice(ids), generator.choices(EVIDENCE_ORDER, weights=(6, 2, 2))[0])
    generator.shuffle(roots)
    request = TraversalRequest(
        root_ids=(*roots[: root_count // 2], "hub1", *roots[root_count // 2 :]),
        direction=direction,
        max_depth=generator.choice([5, 8, 128]),
        max_reached=100_000,
        dispatch=generator.choice(["direct", "candidates"]),
    )
    nodes = [GraphNode(node, "function", NodeLocation("a.py", 1, 1)) for node in ids]
    return CallGraph(nodes, edges, {mode: [] for mode in ("direct", "bound", "candidates")}, {}), request


def crowded(held: dict[int, int], owner: int | None) -> bool:
    """정점에 자기 자신이 아닌 root가 65개보다 많이 모였는지 본다.

    65개가 찬 뒤에 기록에 들어온 root는 그때까지 모인 가장 작은 65개 중 가장 큰 것보다 작다(아니면 전파를 멈췄다).
    그래서 이런 정점에서는 가장 작은 65개가 바뀌는 경로를 탔다.

    Args:
        held: 정점의 root 기록.
        owner: 정점이 root면 그 인덱스.

    Returns:
        그렇다면 True.
    """
    return sum(1 for index in held if index != owner) > DOMINATING_ROOTS


@pytest.mark.parametrize("seed", range(6))
def test_hub_with_many_unsorted_roots_matches_linear_pruning(seed: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """정렬되지 않은 root 200개 이상이 모이는 허브에서도 전파 중단 판정이 정점 기록 전체를 셀 때와 같다.

    단계 기록·깊이 잘림·전파 중단 여부와 순회 결과 전체(reached·roots·rootsTruncated·via·depth·등급)가 오라클
    전파로 계산한 것과 같고, root별 오라클과도 같다. 허브에서 실제로 중단과 가장 작은 65개 교체가 일어나야 의미가 있다.
    """
    direction = ("dependencies", "dependents")[seed % 2]
    graph, request = hub_case(random.Random(6000 + seed), 200, direction)
    adjacency = build_adjacency(graph, direction, weakest_evidence(request.dispatch))
    expected = linear_propagate(adjacency, request.root_ids, request.max_depth)
    assert traversal_module._propagate(adjacency, request.root_ids, request.max_depth) == expected
    levels, _, pruned = expected
    owner = request.root_ids.index("hub1")
    assert pruned
    assert any(crowded(levels[hub], owner if hub == "hub1" else None) for hub in ("hub0", "hub1", "hub2"))
    actual = traverse(graph, request)
    monkeypatch.setattr(traversal_module, "_propagate", linear_propagate)
    assert actual == traverse(graph, request)
    assert_equivalent(actual, oracle(graph, request))


class CountedRecord(dict[int, int]):
    """순회로 읽은 항목 수를 세는 정점 root 기록이다(전파 중단 판정·갱신이 기록을 몇 번 다시 읽는지 본다).

    `in`·`len`·대입은 세지 않고, `for`·`sorted`·`keys()`·`values()`·`items()`처럼 항목을 하나씩 읽을 때만 센다.
    """

    def __init__(self, items: dict[int, int]) -> None:
        """기록을 옮겨 담는다.

        Args:
            items: 원래 기록.
        """
        super().__init__(items)
        self.read = 0

    def __iter__(self) -> Iterator[int]:
        """키를 낼 때마다 센다."""
        for key in super().__iter__():
            self.read += 1
            yield key

    def keys(self) -> Iterator[int]:  # type: ignore[override]
        """키를 낼 때마다 센다."""
        return iter(self)

    def values(self) -> Iterator[int]:  # type: ignore[override]
        """값을 낼 때마다 센다."""
        return (self[key] for key in self)

    def items(self) -> Iterator[tuple[int, int]]:  # type: ignore[override]
        """항목을 낼 때마다 센다."""
        return ((key, self[key]) for key in self)


def count_record_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """전파가 이웃 정점 기록을 읽기 전에 세는 기록으로 바꿔 둔다(원래 `_spread`는 그대로 부른다).

    Args:
        monkeypatch: pytest monkeypatch.
    """
    original = traversal_module._spread

    def spread(levels: Levels, smallest: Any, neighbor: str, *rest: Any) -> bool:
        """이웃 기록을 세는 기록으로 바꾸고 원래 전파를 부른다."""
        record = levels.get(neighbor)
        if not isinstance(record, CountedRecord):
            levels[neighbor] = CountedRecord(record or {})
        return bool(original(levels, smallest, neighbor, *rest))

    monkeypatch.setattr(traversal_module, "_spread", spread)


def test_pruning_reads_each_vertex_record_at_most_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """전파 중단 판정과 갱신은 허브 기록 크기와 무관하게 정점마다 기록을 많아야 두 번(가장 작은 65개를 모을 때) 읽는다.

    정렬되지 않은 root 400개가 모이는 허브는 기록이 130개를 넘는다. 판정마다 기록 전체를 세거나(수정 전), 힙을 고치는
    대신 root가 더해질 때마다 기록을 다시 정렬하면 전파 비용이 (전파 수 × 기록 크기)가 된다. 판정 결과는 같아 결과
    비교로는 드러나지 않고, 비용을 다른 함수로 옮기는 회귀도 있으므로 판정 인자가 아니라 기록에서 읽은 항목 수를 센다.
    root 정점은 자기 자신을 빼면 65개가 안 되는 때가 있어 한 번 더 읽는다(65 + 66개).
    """
    graph, request = hub_case(random.Random(7000), 400, "dependencies")
    adjacency = build_adjacency(graph, request.direction, weakest_evidence(request.dispatch))
    expected = linear_propagate(adjacency, request.root_ids, request.max_depth)
    levels, _, pruned = expected
    assert pruned
    assert max(len(levels[hub]) for hub in ("hub0", "hub1", "hub2")) > 2 * DOMINATING_ROOTS
    count_record_reads(monkeypatch)
    actual = traversal_module._propagate(adjacency, request.root_ids, request.max_depth)
    assert actual == expected
    reads = [record.read for record in actual[0].values() if isinstance(record, CountedRecord)]
    assert DOMINATING_ROOTS <= max(reads) <= 2 * (DOMINATING_ROOTS + 1)


def staircase_case() -> tuple[CallGraph, TraversalRequest]:
    """root 인덱스마다 허브에 닿는 단계를 정해 둔 사례다(전파 중단 경계).

    `hub`는 인덱스 10인 root이기도 하다. 허브에는 단계 1에 인덱스 0~63(10 제외)과 102, 단계 2에 100, 단계 3에 64,
    단계 4에 101, 단계 5에 65, 단계 6에 66~99가 닿는다. 허브는 `down`과 root `s005`로 이어진다.

    Returns:
        (그래프, 요청).
    """
    arrivals = {index: 1 for index in range(64) if index != 10}
    arrivals.update({102: 1, 100: 2, 64: 3, 101: 4, 65: 5})
    arrivals.update(dict.fromkeys(range(66, 100), 6))
    root_ids = tuple("hub" if index == 10 else f"s{index:03d}" for index in range(103))
    edges = [GraphEdge("hub", "down", ("call",), "direct"), GraphEdge("hub", "s005", ("call",), "direct")]
    for index, level in arrivals.items():
        previous = root_ids[index]
        for step in range(level - 1):
            current = f"{previous}_c{step}"
            edges.append(GraphEdge(previous, current, ("call",), "direct"))
            previous = current
        edges.append(GraphEdge(previous, "hub", ("call",), "direct"))
    ids = sorted({edge.source for edge in edges} | {edge.target for edge in edges})
    nodes = [GraphNode(node, "function", NodeLocation("a.py", 1, 1)) for node in ids]
    graph = CallGraph(nodes, edges, {mode: [] for mode in ("direct", "bound", "candidates")}, {})
    return graph, TraversalRequest(root_ids, "dependencies", 128, 100_000, "direct")


def test_staircase_hub_keeps_the_smallest_roots_at_the_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """자기 자신이 아닌 더 작은 root가 정확히 65개면 멈추고 64개면 잇는다(뒤에 온 작은 인덱스가 기록을 바꾼다).

    허브(자기 인덱스 10은 세지 않는다): 101은 더 작은 65개(0~64 중 10 제외 64개 + 100) 뒤라 멈추고, 65는 더 작은 64개
    뒤라 잇고, 66~99는 멈춘다. `down`은 10도 세므로 65에서 멈추고, root `s005`는 자기 인덱스 5를 빼고 65를 잇는다.
    """
    graph, request = staircase_case()
    adjacency = build_adjacency(graph, request.direction, "direct")
    levels, depth_cut, pruned = traversal_module._propagate(adjacency, request.root_ids, request.max_depth)
    assert (levels, depth_cut, pruned) == linear_propagate(adjacency, request.root_ids, request.max_depth)
    assert pruned
    assert set(levels["hub"]) == set(range(66)) | {100, 102}
    assert set(levels["down"]) == set(range(65)) | {100, 102}
    assert set(levels["s005"]) == set(range(66)) | {100, 102}
    actual = traverse(graph, request)
    monkeypatch.setattr(traversal_module, "_propagate", linear_propagate)
    assert actual == traverse(graph, request)
    assert_equivalent(actual, oracle(graph, request))


def threshold_case() -> tuple[CallGraph, TraversalRequest]:
    """root가 아닌 정점 `meet`의 기록이 단계 1에 정확히 65개(인덱스 0~64)가 되고, 단계 2에 65·66이 닿는 사례다.

    65·66은 root `s065`·`s066`에서 `relay`를 거쳐 한 간선으로 함께 온다. `meet`는 `after`로 이어진다.

    Returns:
        (그래프, 요청).
    """
    root_ids = tuple(f"s{index:03d}" for index in range(67))
    edges = [GraphEdge(root, "meet", ("call",), "direct") for root in root_ids[:65]]
    edges += [GraphEdge(root, "relay", ("call",), "direct") for root in root_ids[65:]]
    edges += [GraphEdge("relay", "meet", ("call",), "direct"), GraphEdge("meet", "after", ("call",), "direct")]
    ids = sorted({edge.source for edge in edges} | {edge.target for edge in edges})
    nodes = [GraphNode(node, "function", NodeLocation("a.py", 1, 1)) for node in ids]
    graph = CallGraph(nodes, edges, {mode: [] for mode in ("direct", "bound", "candidates")}, {})
    return graph, TraversalRequest(root_ids, "dependencies", 128, 100_000, "direct")


def test_plain_vertex_prunes_right_after_sixty_five_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    """root가 아닌 정점은 기록이 65개가 된 바로 다음 root부터 멈춘다(가장 작은 65개를 처음 모으는 경계).

    `meet`에는 더 작은 root 65개(0~64)가 이미 있으므로 다음에 오는 65·66은 멈추고, `after`에도 0~64만 간다. 가장 작은
    65개를 한 박자 늦게 모으거나 기록이 정확히 65개일 때 판정을 건너뛰면 65가 새어 들어간다.
    """
    graph, request = threshold_case()
    adjacency = build_adjacency(graph, request.direction, "direct")
    levels, depth_cut, pruned = traversal_module._propagate(adjacency, request.root_ids, request.max_depth)
    assert (levels, depth_cut, pruned) == linear_propagate(adjacency, request.root_ids, request.max_depth)
    assert pruned
    assert levels["meet"] == dict.fromkeys(range(65), 1)
    assert levels["after"] == dict.fromkeys(range(65), 2)
    actual = traverse(graph, request)
    monkeypatch.setattr(traversal_module, "_propagate", linear_propagate)
    assert actual == traverse(graph, request)
    assert_equivalent(actual, oracle(graph, request))
