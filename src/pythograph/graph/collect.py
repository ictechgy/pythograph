"""정의 하나의 자기 범위(중첩 정의 본문 제외, 람다·컴프리헨션 포함)에서 간선과 호출 지점을 모은다.

간선 종류:
- `call`: 호출(메서드·함수·`super()`·property 읽기와 setter 쓰기), `new`: 클래스 호출(프로젝트 `__init__`,
  없으면 클래스 정점).
- `callback`: 인자로 넘긴 함수·클래스·메서드 값, `reference`: 그 밖의 값 참조(대입·반환·컨테이너).
- `attribute`: 프로젝트 클래스 본문 속성 읽기(그 클래스 정점에 기댄다).
- `decorator`·`inherit`는 `build.py`가 정의 단위로 더한다.

클래스 이름을 속성 수신자로만 쓴 경우(`Order.objects.filter(...)`)는 참조 간선을 만들지 않는다 — 그 질의의
relation-use 사실이 이미 감싼 함수에 실리고, 모델 선언 전체(모든 컬럼·외래 키 대상)로 넓히지 않기 위해서다.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from pythograph.graph.calls import BoundSite, CallOutcome, CallResolver, Target
from pythograph.graph.index import Definition
from pythograph.graph.values import (
    AttributeValue,
    ClassValue,
    FrameworkMethodValue,
    FunctionValue,
    MethodValue,
    Value,
)


@dataclass
class ScopeFacts:
    """정의 하나에서 모은 결과.

    Attributes:
        calls: 호출 지점 해석 결과(소스 순서).
        edges: (대상, 근거 등급) 목록.
    """

    calls: list[CallOutcome] = field(default_factory=list)
    edges: list[tuple[Target, str]] = field(default_factory=list)


def own_statements(definition: Definition) -> list[ast.stmt]:
    """정의의 자기 범위 문장을 돌려준다.

    Args:
        definition: 정의.

    Returns:
        문장 목록.
    """
    body = getattr(definition.node, "body", [])
    return list(body) if isinstance(body, list) else []


def collect_scope(calls: CallResolver, definition: Definition) -> ScopeFacts:
    """정의 하나의 간선과 호출 지점을 모은다.

    Args:
        calls: 호출 해석기.
        definition: 정의(모듈·클래스·함수).

    Returns:
        모은 결과.
    """
    visitor = _ScopeVisitor(calls, definition)
    for statement in own_statements(definition):
        visitor.visit(statement)
    return visitor.facts


class _ScopeVisitor(ast.NodeVisitor):
    """자기 범위의 식을 역할(값·피호출·인자·수신자)에 따라 방문한다."""

    def __init__(self, calls: CallResolver, scope: Definition) -> None:
        """방문자를 만든다.

        Args:
            calls: 호출 해석기.
            scope: 범위 정의.
        """
        self.calls = calls
        self.resolver = calls.resolver
        self.scope = scope
        self.facts = ScopeFacts()
        self.shadowed: list[set[str]] = []

    def _is_shadowed(self, expr: ast.expr) -> bool:
        """식의 뿌리 이름이 람다 매개변수·컴프리헨션 변수인지 본다.

        Args:
            expr: 식.

        Returns:
            가려졌으면 True.
        """
        root = expr
        while isinstance(root, (ast.Attribute, ast.Subscript)):
            root = root.value
        return isinstance(root, ast.Name) and any(root.id in names for names in self.shadowed)

    def _value(self, expr: ast.expr) -> Value | None:
        """가려지지 않은 식의 값을 푼다.

        Args:
            expr: 식.

        Returns:
            값, 가려졌으면 None.
        """
        return None if self._is_shadowed(expr) else self.resolver.value(self.scope, expr)

    def _add(self, targets: list[Target], evidence: str = "direct") -> None:
        """간선을 더한다.

        Args:
            targets: 대상 목록.
            evidence: 근거 등급.
        """
        self.facts.edges.extend((target, evidence) for target in targets)

    def _add_outcome(self, outcome: CallOutcome) -> None:
        """호출 해석 결과의 간선을 더한다(호출 지점으로 세지 않는다).

        Args:
            outcome: 해석 결과.
        """
        self._add(outcome.targets)
        self._add(outcome.candidates, "candidate")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """중첩 함수: 장식자와 기본값만 이 범위에서 평가된다."""
        self._definition_header(node, node.args)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """중첩 비동기 함수."""
        self._definition_header(node, node.args)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """중첩 클래스: 장식자만 이 범위에서 방문한다(기반은 상속 간선이 맡는다)."""
        for decorator in node.decorator_list:
            self.visit(decorator)

    def _definition_header(self, node: ast.FunctionDef | ast.AsyncFunctionDef, arguments: ast.arguments) -> None:
        """정의의 장식자·기본값을 방문한다.

        Args:
            node: 정의 노드.
            arguments: 인자 정의.
        """
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in [*arguments.defaults, *[item for item in arguments.kw_defaults if item is not None]]:
            self.visit(default)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """람다 본문은 이 범위의 일부다. 매개변수는 가린다."""
        names = {argument.arg for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]}
        names.update(argument.arg for argument in (node.args.vararg, node.args.kwarg) if argument is not None)
        for default in [*node.args.defaults, *[item for item in node.args.kw_defaults if item is not None]]:
            self.visit(default)
        self.shadowed.append(names)
        self.visit(node.body)
        self.shadowed.pop()

    def _comprehension(self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp) -> None:
        """컴프리헨션은 이 범위의 일부다. 반복 변수는 가린다.

        Args:
            node: 컴프리헨션 노드.
        """
        names = {
            child.id
            for generator in node.generators
            for child in ast.walk(generator.target)
            if isinstance(child, ast.Name)
        }
        self.shadowed.append(names)
        for generator in node.generators:
            self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        for element in _comprehension_elements(node):
            self.visit(element)
        self.shadowed.pop()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        """리스트 컴프리헨션."""
        self._comprehension(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        """집합 컴프리헨션."""
        self._comprehension(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        """제너레이터 식."""
        self._comprehension(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        """사전 컴프리헨션."""
        self._comprehension(node)

    def visit_Call(self, node: ast.Call) -> None:
        """호출 지점: 해석하고 피호출 식의 수신자·인자를 방문한다."""
        shadowed = self._is_shadowed(node.func)
        outcome = self.calls.resolve(self.scope, node, shadowed)
        if isinstance(node.func, ast.Attribute) and not shadowed and outcome.is_bound_candidate():
            outcome.site = BoundSite(self.scope, node, bool(self.shadowed))
        self.facts.calls.append(outcome)
        self._add_outcome(outcome)
        if isinstance(node.func, ast.Attribute):
            self._receiver(node.func.value)
            callee = self._value(node.func)
            if isinstance(callee, AttributeValue):
                self._member_edges(callee)
        elif not isinstance(node.func, ast.Name):
            self.visit(node.func)
        for argument in node.args:
            self._argument(argument.value if isinstance(argument, ast.Starred) else argument)
        for keyword in node.keywords:
            self._argument(keyword.value)

    def _argument(self, expr: ast.expr) -> None:
        """인자 식: 함수·클래스·메서드 값이면 콜백 간선이다.

        Args:
            expr: 인자 식.
        """
        if isinstance(expr, (ast.Name, ast.Attribute)):
            self._reference(expr, "callback")
        else:
            self.visit(expr)

    def visit_Name(self, node: ast.Name) -> None:
        """값 위치의 이름: 함수·클래스 참조 간선이다."""
        if isinstance(node.ctx, ast.Load):
            self._reference(node, "reference")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """값 위치의 속성 접근."""
        if isinstance(node.ctx, ast.Store):
            self._receiver(node.value)
            self._setter(node)
        elif isinstance(node.ctx, ast.Load):
            self._reference(node, "reference")
        else:
            self._receiver(node.value)

    def _reference(self, expr: ast.Name | ast.Attribute, kind: str) -> None:
        """값으로 쓴 이름·속성의 간선을 더한다(속성이면 수신자도 방문한다).

        Args:
            expr: 이름·속성 식.
            kind: `reference` 또는 `callback`.
        """
        if isinstance(expr, ast.Attribute):
            self._receiver(expr.value)
        value = self._value(expr)
        if value is None:
            return
        self._member_edges(value)
        self._add(self._value_targets(value, kind))
        if isinstance(value, MethodValue) and not value.exact and not value.via_super and not value.is_property:
            name = getattr(value.member.node, "name", "")
            self._add(self.calls.overrides(value.receiver, name, value.member, kind), "candidate")

    def _receiver(self, expr: ast.expr) -> None:
        """수신자 위치의 식: 참조 간선 없이 속성 읽기 간선만 더하고 안쪽을 방문한다.

        Args:
            expr: 수신자 식.
        """
        if isinstance(expr, ast.Attribute):
            self._receiver(expr.value)
            value = self._value(expr)
            if value is not None:
                self._member_edges(value)
        elif not isinstance(expr, ast.Name):
            self.visit(expr)

    def _member_edges(self, value: Value) -> None:
        """속성 읽기가 만드는 간선: 클래스 속성(그 클래스 정점)과 property 읽기(호출).

        Args:
            value: 속성 값.
        """
        if isinstance(value, AttributeValue):
            self._add([Target(value.owner.id, "attribute")])
        elif isinstance(value, MethodValue) and value.is_property:
            self._add_outcome(self.calls.method(value, "call"))
        elif isinstance(value, FrameworkMethodValue) and value.is_property:
            self._add([Target(self.calls.framework_member(value.receiver, value.name), "call")])

    def _value_targets(self, value: Value, kind: str) -> list[Target]:
        """값 참조의 direct 대상이다.

        Args:
            value: 값.
            kind: 간선 종류.

        Returns:
            대상 목록.
        """
        if isinstance(value, (FunctionValue, ClassValue)):
            return [Target(value.definition.id, kind)]
        if isinstance(value, MethodValue) and not value.is_property:
            node_id = self.calls.exact_member(value.receiver, value.member) if value.exact else value.member.id
            return [Target(node_id, kind)]
        if isinstance(value, FrameworkMethodValue) and not value.is_property:
            return [Target(self.calls.framework_member(value.receiver, value.name), kind)]
        return []

    def _setter(self, node: ast.Attribute) -> None:
        """property setter 쓰기(`obj.x = v`)를 호출 간선으로 잇는다.

        Args:
            node: 저장 위치의 속성 식.
        """
        value = self._value(node)
        if isinstance(value, MethodValue) and value.is_property:
            self._add_outcome(self.calls.method(value, "call"))


def _comprehension_elements(node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp) -> list[ast.expr]:
    """컴프리헨션의 결과 식을 돌려준다.

    Args:
        node: 컴프리헨션 노드.

    Returns:
        식 목록.
    """
    if isinstance(node, ast.DictComp):
        return [node.key, node.value]
    return [node.elt]


def self_attribute_uses(definition: Definition) -> list[tuple[str, bool]]:
    """메서드 본문이 첫 매개변수(`self`)로 쓰는 속성 이름과 호출 여부를 모은다(상속 멤버 정점의 정확한 재해석용).

    Args:
        definition: 메서드 정의.

    Returns:
        (이름, 호출인지) 목록(중복 없음, 소스 순서).
    """
    node = definition.node
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return []
    positional = node.args.posonlyargs + node.args.args
    if not positional:
        return []
    receiver = positional[0].arg
    called = {id(child.func) for child in _own_nodes(definition) if isinstance(child, ast.Call)}
    uses: dict[tuple[str, bool], None] = {}
    for child in _own_nodes(definition):
        if (
            isinstance(child, ast.Attribute)
            and isinstance(child.value, ast.Name)
            and child.value.id == receiver
            and isinstance(child.ctx, ast.Load)
        ):
            uses.setdefault((child.attr, id(child) in called), None)
    return list(uses)


def _own_nodes(definition: Definition) -> list[ast.AST]:
    """정의의 자기 범위 노드를 모두 돌려준다(중첩 정의 본문 제외).

    Args:
        definition: 정의.

    Returns:
        노드 목록.
    """
    result: list[ast.AST] = []
    pending: list[ast.AST] = list(own_statements(definition))
    while pending:
        current = pending.pop()
        result.append(current)
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            pending.extend(current.decorator_list)
            continue
        pending.extend(ast.iter_child_nodes(current))
    return result
