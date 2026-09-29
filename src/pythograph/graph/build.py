"""프로젝트를 읽어 호출 그래프를 만든다.

순서: 선언 색인 → 정의마다 자기 범위의 간선·호출 지점 → 장식자·상속 간선 → 뷰 클래스 핸들러와 디스패치 경로
→ 요청된 상속 멤버 정점(작업 목록으로 고정점까지) → 간선 정리·미해석 수·한계 문구. 같은 입력이면 같은 그래프다
(모든 목록을 id 순으로 정렬한다).
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass, field

from pythograph.graph.calls import CallOutcome, CallResolver, InheritedRequest, NameFilter, Target
from pythograph.graph.collect import collect_scope, self_attribute_uses
from pythograph.graph.framework import (
    DISPATCH_ENTRY,
    framework_closure,
    handler_names,
    is_callback_attribute,
)
from pythograph.graph.index import Definition, DefinitionIndex
from pythograph.graph.limitations import graph_limitations
from pythograph.graph.model import DISPATCH_MODES, EVIDENCE_ORDER, CallGraph, GraphEdge, GraphNode, NodeLocation
from pythograph.graph.scope import Resolver
from pythograph.graph.values import ClassValue, FunctionValue
from pythograph.source.project import Project
from pythograph.source.symbols import SymbolTable


@dataclass
class NodeRecord:
    """만드는 중인 정점.

    Attributes:
        node: 정점.
        calls: 자기 호출 지점 해석 결과.
    """

    node: GraphNode
    calls: list[CallOutcome] = field(default_factory=list)


@dataclass
class GraphBuilder:
    """그래프를 조립하는 상태."""

    project: Project
    index: DefinitionIndex
    resolver: Resolver
    calls: CallResolver
    records: dict[str, NodeRecord] = field(default_factory=dict)
    edges: list[tuple[str, Target, str]] = field(default_factory=list)
    handlers: set[str] = field(default_factory=set)
    materialized: set[str] = field(default_factory=set)
    extra: dict[str, Counter[str]] = field(default_factory=dict)

    def add_extra(self, node_id: str, reason: str, count: int) -> None:
        """호출 지점 밖의 미해석 수를 더한다(모든 모드에 더한다). 상속 멤버 정점은 나중에 만들어져도 된다.

        Args:
            node_id: 정점 id.
            reason: 이유.
            count: 수(0이면 더하지 않는다).
        """
        if count:
            self.extra.setdefault(node_id, Counter())[reason] += count

    def add_edges(self, source: str, targets: list[Target], evidence: str = "direct") -> None:
        """간선을 더한다.

        Args:
            source: 출발 정점 id.
            targets: 대상 목록.
            evidence: 근거 등급.
        """
        self.edges.extend((source, target, evidence) for target in targets)


def build_graph(project: Project, include_tests: bool) -> CallGraph:
    """호출 그래프를 만든다.

    Args:
        project: 분석 대상 프로젝트.
        include_tests: 테스트 소스를 정점으로 포함할지.

    Returns:
        호출 그래프.
    """
    symbols = SymbolTable(project)
    index = DefinitionIndex(project, symbols, include_tests)
    resolver = Resolver(index, symbols)
    names, dynamic_writes = name_filter(index)
    builder = GraphBuilder(project, index, resolver, CallResolver(resolver, names))
    for definition in sorted(index.definitions.values(), key=lambda item: item.id):
        _add_definition(builder, definition)
    for definition in index.classes():
        _add_view_handlers(builder, definition)
    _materialize_requests(builder)
    return _finish(builder, dynamic_writes)


def _add_definition(builder: GraphBuilder, definition: Definition) -> None:
    """정의 하나의 정점·간선·호출 지점을 더한다.

    Args:
        builder: 조립 상태.
        definition: 정의.
    """
    record = NodeRecord(GraphNode(definition.id, definition.kind, definition_location(builder.index, definition)))
    builder.records[definition.id] = record
    facts = collect_scope(builder.calls, definition)
    record.calls.extend(facts.calls)
    builder.edges.extend((definition.id, target, evidence) for target, evidence in facts.edges)
    builder.add_edges(definition.id, _decorator_targets(builder, definition))
    if definition.kind == "class":
        bases = builder.resolver.linearizer.bases(definition)
        targets = [Target(entry.key, "inherit") for entry in bases if entry.kind == "project"]
        builder.add_edges(definition.id, targets)


def _decorator_targets(builder: GraphBuilder, definition: Definition) -> list[Target]:
    """정의 → 프로젝트 장식자(장식자 팩토리 호출이면 그 팩토리) 간선 대상이다.

    Args:
        builder: 조립 상태.
        definition: 정의.

    Returns:
        대상 목록.
    """
    node = definition.node
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or definition.parent is None:
        return []
    targets: list[Target] = []
    for decorator in node.decorator_list:
        expr = decorator.func if isinstance(decorator, ast.Call) else decorator
        value = builder.resolver.value(definition.parent, expr)
        if isinstance(value, (FunctionValue, ClassValue)):
            targets.append(Target(value.definition.id, "decorator"))
    return targets


def _add_view_handlers(builder: GraphBuilder, definition: Definition) -> None:
    """뷰 클래스의 핸들러 정점마다 디스패치 경로 훅 간선을 더한다.

    Args:
        builder: 조립 상태.
        definition: 클래스 정의.
    """
    names = handler_names(builder.resolver, definition)
    if not names:
        return
    closure = framework_closure(builder.resolver, definition, [DISPATCH_ENTRY])
    targets = [
        Target(_hook_target(builder, definition, hook.definition), "dispatch")
        for hook in closure.hooks
        if hook.definition
    ]
    targets.extend(Target(_owner_id(member), "attribute") for member in closure.attributes)
    callbacks = sum(1 for member in closure.attributes if is_callback_attribute(builder.resolver, member))
    for name in names:
        handler = _handler_node(builder, definition, name)
        if handler is None:
            continue
        builder.handlers.add(handler)
        builder.add_edges(handler, targets)
        builder.add_extra(handler, "framework-callback", callbacks)


def _handler_node(builder: GraphBuilder, definition: Definition, name: str) -> str | None:
    """핸들러 이름의 정점 id다(정의했으면 정의, 물려받았으면 상속 멤버 정점).

    Args:
        builder: 조립 상태.
        definition: 뷰 클래스.
        name: 핸들러 이름.

    Returns:
        정점 id, 멤버가 없으면 None.
    """
    member = builder.resolver.linearizer.lookup(definition, name)
    if member.kind == "method" and member.definition is not None:
        return builder.calls.exact_member(definition, member.definition)
    if member.kind == "framework-method":
        return builder.calls.framework_member(definition, name)
    return None


def _hook_target(builder: GraphBuilder, receiver: Definition, member: Definition) -> str:
    """정확한 수신자의 훅 정점 id다.

    Args:
        builder: 조립 상태.
        receiver: 수신자 클래스.
        member: 훅 메서드 정의.

    Returns:
        정점 id.
    """
    return builder.calls.exact_member(receiver, member)


def _owner_id(member: object) -> str:
    """클래스 속성 멤버를 정의한 클래스 id다.

    Args:
        member: 멤버 조회 결과.

    Returns:
        클래스 id.
    """
    owner = getattr(member, "owner", None)
    return owner.key if owner is not None else ""


def _materialize_requests(builder: GraphBuilder) -> None:
    """요청된 상속 멤버 정점을 고정점까지 만든다.

    Args:
        builder: 조립 상태.
    """
    while True:
        pending = [
            request for key, request in sorted(builder.calls.requests.items()) if key not in builder.materialized
        ]
        if not pending:
            break
        for request in pending:
            builder.materialized.add(request.id)
            if request.id not in builder.records:
                _add_inherited(builder, request)


def _add_inherited(builder: GraphBuilder, request: InheritedRequest) -> None:
    """상속 멤버 정점 하나와 그 간선을 더한다.

    Args:
        builder: 조립 상태.
        request: 요청.
    """
    location = definition_location(builder.index, request.receiver)
    builder.records[request.id] = NodeRecord(GraphNode(request.id, "inherited-method", location))
    member = builder.resolver.linearizer.lookup(request.receiver, request.name)
    if member.kind == "method" and member.definition is not None:
        builder.add_edges(request.id, [Target(member.definition.id, "inherit")])
        builder.add_edges(request.id, _exact_self_targets(builder, request.receiver, member.definition))
    elif member.kind == "framework-method":
        _add_framework_member(builder, request)
    else:
        builder.add_extra(request.id, "framework-implementation", 1)


def _exact_self_targets(builder: GraphBuilder, receiver: Definition, method: Definition) -> list[Target]:
    """물려받은 메서드 본문의 `self.X` 사용을 정확한 수신자 클래스로 다시 푼 대상이다.

    Args:
        builder: 조립 상태.
        receiver: 정확한 수신자 클래스.
        method: 물려받은 메서드 정의.

    Returns:
        대상 목록.
    """
    targets: list[Target] = []
    for name, is_call in self_attribute_uses(method):
        member = builder.resolver.linearizer.lookup(receiver, name)
        if member.kind == "method" and member.definition is not None:
            if is_call or _is_property(builder, member.definition, name):
                targets.append(Target(builder.calls.exact_member(receiver, member.definition), "call"))
        elif member.kind == "framework-method" and is_call:
            targets.append(Target(builder.calls.framework_member(receiver, name), "call"))
        elif member.kind == "attribute" and member.owner is not None:
            targets.append(Target(member.owner.key, "attribute"))
    return targets


def _is_property(builder: GraphBuilder, method: Definition, name: str) -> bool:
    """메서드가 property·cached_property인지 본다.

    Args:
        builder: 조립 상태.
        method: 메서드 정의.
        name: 이름.

    Returns:
        그렇다면 True.
    """
    if method.parent is None:
        return False
    decorators = builder.index.class_members(method.parent).decorators.get(name, set())
    return bool(decorators & {"property", "cached_property"})


def _add_framework_member(builder: GraphBuilder, request: InheritedRequest) -> None:
    """프레임워크가 구현한 상속 멤버 정점의 훅·속성 간선과 미해석 콜백 수를 더한다.

    Args:
        builder: 조립 상태.
        request: 요청.
    """
    closure = framework_closure(builder.resolver, request.receiver, [request.name])
    targets = [
        Target(_hook_target(builder, request.receiver, hook.definition), "framework")
        for hook in closure.hooks
        if hook.definition is not None
    ]
    targets.extend(Target(_owner_id(member), "attribute") for member in closure.attributes)
    builder.add_edges(request.id, targets)
    callbacks = sum(1 for member in closure.attributes if is_callback_attribute(builder.resolver, member))
    builder.add_extra(request.id, "framework-callback", callbacks)


def definition_location(index: DefinitionIndex, definition: Definition) -> NodeLocation:
    """정의의 위치다(모듈은 경로만). 열은 1부터 시작하는 UTF-8 바이트 열이다.

    Args:
        index: 선언 색인.
        definition: 정의.

    Returns:
        위치.
    """
    node = definition.node
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return NodeLocation(definition.path)
    column = node.col_offset + 1
    module = index.module_of(definition.path)
    if module is not None and module.has_bom and node.lineno == 1:
        column += 3
    return NodeLocation(definition.path, node.lineno, column)


def name_filter(index: DefinitionIndex) -> tuple[NameFilter, int]:
    """프로젝트가 정의·대입하는 속성 이름 집합을 만든다.

    Args:
        index: 선언 색인.

    Returns:
        (필터, 계산된 이름의 setattr 호출 수).
    """
    names: set[str] = set()
    disabled = False
    dynamic_writes = 0
    for module in index.modules:
        for node in ast.walk(module.node):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            if isinstance(node, ast.ClassDef):
                disabled = disabled or any(_is_dynamic_lookup(item) for item in node.body)
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                names.add(node.attr)
            elif isinstance(node, ast.Call) and _is_setattr(node):
                literal = _setattr_name(node)
                if literal is None:
                    dynamic_writes += 1
                else:
                    names.add(literal)
    for definition in index.classes():
        names.update(index.class_members(definition).attributes)
    return NameFilter(names, disabled), dynamic_writes


def _is_dynamic_lookup(statement: ast.stmt) -> bool:
    """클래스 본문 문장이 `__getattr__`·`__getattribute__` 정의인지 본다(모듈 수준 PEP 562 `__getattr__`은 뺀다 —
    모듈 속성은 모듈 값으로 따로 풀고, 모르면 이미 미해석이다).

    Args:
        statement: 클래스 본문 문장.

    Returns:
        그렇다면 True.
    """
    names = ("__getattr__", "__getattribute__")
    return isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) and statement.name in names


def _is_setattr(node: ast.Call) -> bool:
    """`setattr(...)` 호출인지 본다.

    Args:
        node: 호출.

    Returns:
        그렇다면 True.
    """
    return isinstance(node.func, ast.Name) and node.func.id == "setattr" and len(node.args) >= 2


def _setattr_name(node: ast.Call) -> str | None:
    """`setattr`의 리터럴 이름을 돌려준다.

    Args:
        node: setattr 호출.

    Returns:
        이름, 리터럴이 아니면 None.
    """
    name = node.args[1]
    return name.value if isinstance(name, ast.Constant) and isinstance(name.value, str) else None


def _finish(builder: GraphBuilder, dynamic_writes: int) -> CallGraph:
    """정점·간선을 정리하고 미해석 수와 한계를 계산한다.

    Args:
        builder: 조립 상태.
        dynamic_writes: 계산된 이름의 setattr 호출 수.

    Returns:
        호출 그래프.
    """
    nodes = [_finish_node(builder.records[key], builder.extra.get(key, Counter())) for key in sorted(builder.records)]
    edges = merge_edges(builder.edges, set(builder.records))
    statistics = _statistics(builder, nodes, edges, dynamic_writes)
    limitations = graph_limitations(builder.project, statistics)
    return CallGraph(nodes, edges, limitations, statistics)


def _finish_node(record: NodeRecord, extra: Counter[str]) -> GraphNode:
    """정점의 모드별 미해석 수와 이유를 채운다.

    Args:
        record: 정점 기록.
        extra: 호출 지점 밖의 미해석 수.

    Returns:
        정점.
    """
    reasons: Counter[str] = Counter(extra)
    partial = 0
    for outcome in record.calls:
        if outcome.status == "unresolved":
            reasons[outcome.reason] += 1
        elif outcome.status == "partial":
            partial += 1
            reasons[outcome.reason] += 1
    base = sum(reasons.values())
    record.node.unresolved = {mode: base - (partial if mode == "candidates" else 0) for mode in DISPATCH_MODES}
    record.node.reasons = dict(sorted(reasons.items()))
    return record.node


def merge_edges(raw: list[tuple[str, Target, str]], nodes: set[str]) -> list[GraphEdge]:
    """간선을 (출발, 도착, 등급)마다 하나로 합치고, 더 강한 간선이 종류를 모두 싣는 약한 간선은 뺀다.

    약한 간선이 빠져도 어느 모드의 순회도 바뀌지 않는다(tsograph 스냅샷 규칙과 같다).

    Args:
        raw: (출발, 대상, 등급) 목록.
        nodes: 존재하는 정점 id.

    Returns:
        정렬한 간선 목록.
    """
    grouped: dict[tuple[str, str], dict[str, set[str]]] = {}
    for source, target, evidence in raw:
        if target.node_id in nodes and source in nodes:
            grouped.setdefault((source, target.node_id), {}).setdefault(evidence, set()).add(target.kind)
    result: list[GraphEdge] = []
    for (source_id, target_id), tiers in sorted(grouped.items()):
        covered: set[str] = set()
        for evidence in EVIDENCE_ORDER:
            kinds = tiers.get(evidence)
            if kinds is None or kinds <= covered:
                continue
            result.append(GraphEdge(source_id, target_id, tuple(sorted(kinds)), evidence))
            covered |= kinds
    return result


def _statistics(
    builder: GraphBuilder, nodes: list[GraphNode], edges: list[GraphEdge], dynamic_writes: int
) -> dict[str, object]:
    """스냅샷 집계를 만든다.

    Args:
        builder: 조립 상태.
        nodes: 정점.
        edges: 간선.
        dynamic_writes: 계산된 이름의 setattr 호출 수.

    Returns:
        집계 사전.
    """
    statuses: Counter[str] = Counter()
    unresolved: Counter[str] = Counter()
    partial: Counter[str] = Counter()
    extra: Counter[str] = Counter()
    for node_id, record in builder.records.items():
        extra.update(builder.extra.get(node_id, Counter()))
        for outcome in record.calls:
            statuses[outcome.status] += 1
            if outcome.status == "unresolved":
                unresolved[outcome.reason] += 1
            elif outcome.status == "partial":
                partial[outcome.reason] += 1
    return {
        "nodes": dict(sorted(Counter(node.kind for node in nodes).items())),
        "edges": dict(sorted(Counter(kind for edge in edges for kind in edge.kinds).items())),
        "edgesByEvidence": dict(sorted(Counter(edge.evidence for edge in edges).items())),
        "callSites": dict(sorted(statuses.items())),
        "unresolvedCalls": dict(sorted(unresolved.items())),
        "partialCalls": dict(sorted(partial.items())),
        "frameworkUnresolved": dict(sorted(extra.items())),
        "viewHandlers": len(builder.handlers),
        "dynamicAttributeWrites": dynamic_writes,
        "unparsedFiles": builder.index.unparsed,
        "mroApproximated": len(builder.resolver.linearizer.approximated),
    }
