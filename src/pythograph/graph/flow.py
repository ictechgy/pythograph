"""`bound` 디스패치의 값 흐름: 수신자 자리로 들어오는 관찰된 값이 모두 프로젝트 클래스 인스턴스인지.

전체 프로그램·문맥 무관·경로 무관 분석이다(tsograph `value-flow.ts`와 같은 모양). 식의 흐름은 정확한 프로젝트
클래스 id 집합이거나 **열림**(모름, 이유 하나)이다. `None` 상수는 집합에 넣지 않는다(`None`의 메서드 호출은 프로젝트
코드를 부르지 않는다).

따라가는 흐름:
- 생성자 호출 `K(...)`(정확한 K), `cls(...)`·`type(self)(...)`(K와 프로젝트 하위 클래스).
- 지역 이름의 모든 대입(두 번 이상 묶여도 합친다), 조건식·`or`/`and`·바다코끼리.
- 매개변수: 기본값 + 모든 호출 지점의 같은 자리 실인자. 함수와 `__init__`만 — 그 밖의 메서드 매개변수는 호출자를
  열거할 수 없어 연다(tsograph와 같다). 호출 지점이 없거나 `exposure`가 연 자리는 열림이다.
- 모듈 전역: 모듈 수준 대입 전부, `global` 선언 함수의 대입, 그 모듈이나 타입 모르는 수신자에 대한 같은 이름 속성 쓰기.
- 인스턴스 속성 `obj.attr`: obj가 가리킬 수 있는 클래스들의 클래스 본문 값 + 그 클래스(또는 타입 모르는 수신자)에 대한
  같은 이름 속성 쓰기 전부(`self.repo = repo`가 `__init__` 매개변수로, 다시 생성 지점 실인자로 이어진다).
- 호출 결과: 프로젝트 함수·정확한 수신자 메서드의 모든 `return` 값(장식·제너레이터·코루틴은 열림).

질의마다 단계 예산이 있고, 순환(자기를 거쳐 돌아오는 자리)은 열림으로 끊는다(결과가 줄 뿐 틀리지 않는다).
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

from pythograph.graph.exposure import ALL_MODULES, AttributeWrite, ProgramFacts
from pythograph.graph.framework_table import FRAMEWORK_CLASSES
from pythograph.graph.index import Definition, DefinitionIndex
from pythograph.graph.scope import Binding, Resolver, class_bindings
from pythograph.graph.values import (
    ClassValue,
    ExternalResult,
    ExternalValue,
    FrameworkMethodValue,
    FunctionValue,
    InstanceValue,
    MethodValue,
    ModuleValue,
    UnknownValue,
    Value,
)
from pythograph.source.symbols import ImportBinding, ValueSymbol

#: 질의 하나가 쓸 수 있는 단계 수다.
MAX_FLOW_STEPS = 20_000

#: 식을 따라가는 최대 깊이다.
MAX_FLOW_DEPTH = 64

#: 동적 속성 훅이다(정의한 클래스의 속성 자리는 연다).
_ATTRIBUTE_HOOKS = frozenset({"__getattr__", "__getattribute__", "__setattr__", "__delattr__"})

#: 흐름을 바꾸지 않는 메서드 장식자다.
_TRANSPARENT_DECORATORS = frozenset({"staticmethod", "classmethod"})

#: 인스턴스가 아닌 정적 값이다(이 값이 수신자인 속성 쓰기는 어떤 클래스 인스턴스에도 닿지 않는다).
_NOT_INSTANCES = (ModuleValue, FunctionValue, MethodValue, ExternalValue, FrameworkMethodValue)


@dataclass(frozen=True)
class Flow:
    """식 하나의 흐름.

    Attributes:
        classes: 정확한 프로젝트 클래스 id 집합(인스턴스).
        open: 열림 이유(모르면), 닫혔으면 None.
    """

    classes: frozenset[str] = frozenset()
    open: str | None = None


#: 빈 흐름(`None`만 들어온다)이다.
EMPTY = Flow()


def opened(reason: str) -> Flow:
    """열린 흐름을 만든다.

    Args:
        reason: 이유.

    Returns:
        흐름.
    """
    return Flow(open=reason)


def union(flows: Iterable[Flow]) -> Flow:
    """흐름을 합친다. 하나라도 열리면 처음 열린 이유로 열린다.

    Args:
        flows: 흐름들.

    Returns:
        합친 흐름.
    """
    classes: set[str] = set()
    for flow in flows:
        if flow.open is not None:
            return flow
        classes.update(flow.classes)
    return Flow(frozenset(classes))


def _includes_subclasses(value: InstanceValue | ClassValue) -> bool:
    """값이 그 클래스의 하위 클래스 인스턴스(클래스 객체)도 가리킬 수 있는지 본다(정확하지 않은 인스턴스, 클래스 객체).

    Args:
        value: 인스턴스·클래스 값.

    Returns:
        그렇다면 True.
    """
    return not value.exact or isinstance(value, ClassValue)


class BudgetExceeded(Exception):
    """질의가 단계 예산을 넘었다."""


@dataclass
class _NameWrites:
    """속성 이름 하나의 쓰기를 수신자의 정적 값으로 나눈 색인이다(위치는 `ProgramFacts.writes[name]`의 순번).

    속성 자리·메서드 가림·모듈 전역 질의가 같은 이름 쓰기를 질의마다 모두 다시 판정하면 클래스·모듈 수에 제곱이다.
    그래서 이름마다 한 번만 나눠 두고, 질의는 걸리는 위치만 원래 순서로 합쳐 돈다(흐름 계산 순서가 모두 훑을 때와 같다).
    쓰기 하나는 많아야 두 목록에 든다. 하위 클래스까지 가리키는 정적 수신자도 하위 클래스마다 펼치지 않고 수신자
    클래스 하나에 담는다(펼치면 같은 수신자 쓰기 수 × 하위 클래스 수로 메모리가 커진다). 질의가 조상 쪽에서 찾는다.
    색인을 만들 때는 하위 클래스 표를 묻지 않는다. 표는 모든 클래스의 MRO를 계산하므로, 옛 판정처럼 속성 자리·메서드
    가림 질의가 하위 클래스까지 가리키는 첫 쓰기를 판정할 때 만든다(모듈 전역 질의는 만들지 않았다). 수신자 값도
    해석기가 이미 푼 것만 쓴다(수집 단계가 이름·속성 수신자를 푼다). 아직 풀지 않은 수신자(호출식 등)를 색인이 먼저
    풀면 MRO 메모가 옛 판정과 다른 순서로 채워지므로, 그런 쓰기는 질의가 옛 판정처럼 차례로 푼다.

    Attributes:
        writes: 쓰기(원래 순서).
        exact: 클래스 id → 정적 수신자가 그 클래스의 인스턴스만 가리키는 쓰기(정확한 인스턴스).
        widened: 클래스 id → 정적 수신자가 그 클래스와 하위 클래스의 인스턴스·클래스 객체일 수 있는 쓰기.
        first_widened: `widened`의 첫 쓰기 (순번, 수신자 클래스), 없으면 None.
        by_module: 모듈 경로 → 그 모듈이 수신자인 쓰기.
        unresolved: 수신자 흐름으로 판정할 쓰기(람다 뿌리, 아직 풀지 않은 수신자, 모듈·함수·외부 값이 아닌 모르는 값).
        module_candidates: 타입 모르는 값·외부 호출 결과라 모듈일 수 있는 수신자 쓰기.
        replayed: 모듈 전역 질의가 옛 판정처럼 그때 수신자 값을 풀어 고르는 쓰기(람다 뿌리, 아직 풀지 않은 수신자).
    """

    writes: tuple[AttributeWrite, ...]
    exact: dict[str, list[int]] = field(default_factory=dict)
    widened: dict[str, list[int]] = field(default_factory=dict)
    first_widened: tuple[int, Definition] | None = None
    by_module: dict[str, list[int]] = field(default_factory=dict)
    unresolved: list[int] = field(default_factory=list)
    module_candidates: list[int] = field(default_factory=list)
    replayed: list[int] = field(default_factory=list)


def _index_static(index: _NameWrites, position: int, value: InstanceValue | ClassValue) -> None:
    """정적 인스턴스·클래스 수신자 쓰기를 수신자 클래스의 버킷에 넣는다(하위 클래스 표는 묻지 않는다).

    하위 클래스까지 가리키는 값은 하위 클래스가 없어도 `widened`에 둔다. 질의가 질의 클래스와 그 조상의 버킷을 읽으므로
    판정은 같고, 하위 클래스가 있는지 물으면 하위 클래스 표를 옛 판정보다 먼저 만들게 된다.

    Args:
        index: 채울 색인.
        position: 쓰기 순번.
        value: 수신자 값.
    """
    owner = value.definition
    if not _includes_subclasses(value):
        index.exact.setdefault(owner.id, []).append(position)
        return
    index.widened.setdefault(owner.id, []).append(position)
    if index.first_widened is None:
        index.first_widened = (position, owner)


def _exact_writes(index: _NameWrites, classes: frozenset[str]) -> set[int]:
    """정확한 인스턴스 수신자 쓰기 가운데 그 클래스들의 인스턴스에 닿는 것(수신자 클래스가 질의 클래스)의 순번이다.

    Args:
        index: 이름의 쓰기 색인.
        classes: 클래스 id.

    Returns:
        쓰기 순번 집합.
    """
    return {position for class_id in classes for position in index.exact.get(class_id, ())}


class FlowAnalyzer:
    """전체 프로그램 값 흐름 질의기."""

    def __init__(self, index: DefinitionIndex, resolver: Resolver, facts: ProgramFacts) -> None:
        """질의기를 만든다.

        Args:
            index: 선언 색인.
            resolver: 값 해석기.
            facts: 전체 프로그램 사실.
        """
        self.index = index
        self.resolver = resolver
        self.facts = facts
        self.linearizer = resolver.linearizer
        self.budget_exceeded = 0
        self._memo: dict[tuple[object, ...], Flow] = {}
        self._active: set[tuple[object, ...]] = set()
        self._steps = 0
        self._module_scopes: dict[str, list[Definition]] | None = None
        self._pending_flows: list[Flow] | None = None
        self._restricted = False
        self._nonlocals: dict[str, frozenset[str]] = {}
        self._name_writes: dict[str, _NameWrites] = {}
        self._instance_classes: dict[tuple[str, bool], frozenset[str]] = {}

    # --- 질의 입구 ---

    def receiver_flow(self, scope: Definition, expr: ast.expr) -> Flow:
        """호출 지점 수신자 식의 흐름을 구한다(예산을 새로 잡는다).

        Args:
            scope: 식을 담은 범위.
            expr: 수신자 식.

        Returns:
            흐름.
        """
        self._steps = 0
        try:
            return self.expr_flow(scope, expr, 0)
        except BudgetExceeded:
            self.budget_exceeded += 1
            self._active.clear()
            return opened("flow-budget")

    def method_blocked(self, class_id: str, name: str) -> str | None:
        """정확한 클래스의 메서드가 인스턴스 속성으로 가려질 수 있는 이유다(예산을 새로 잡는다, 넘으면 막는다).

        Args:
            class_id: 클래스 id.
            name: 메서드 이름.

        Returns:
            이유, 가려지지 않으면 None.
        """
        self._steps = 0
        try:
            dirty = self.class_dirty(class_id)
        except BudgetExceeded:
            self.budget_exceeded += 1
            self._active.clear()
            return "flow-budget"
        if dirty is not None:
            return dirty
        return "shadowed-method" if self.name_written(name, frozenset({class_id})) else None

    def name_written(self, name: str, classes: frozenset[str]) -> bool:
        """그 클래스 인스턴스(또는 클래스 객체)에 같은 이름을 쓰는 곳이 있는지 본다(메서드 가림 판정, 예산 새로).

        Args:
            name: 속성 이름.
            classes: 클래스 id.

        Returns:
            있거나 예산을 넘으면 True.
        """
        self._steps = 0
        try:
            return any(True for _ in self._reaching_writes(name, classes, 0))
        except BudgetExceeded:
            self.budget_exceeded += 1
            self._active.clear()
            return True

    def _memoized(self, key: tuple[object, ...], compute: Callable[[], Flow]) -> Flow:
        """메모와 순환 끊기를 적용해 흐름을 계산한다.

        Args:
            key: 메모 키.
            compute: 인자 없는 계산 함수.

        Returns:
            흐름.
        """
        cached = self._memo.get(key)
        if cached is not None:
            return cached
        if key in self._active:
            return opened("flow-cycle")
        self._steps += 1
        if self._steps > MAX_FLOW_STEPS:
            raise BudgetExceeded
        self._active.add(key)
        try:
            result = compute()
        finally:
            self._active.discard(key)
        self._memo[key] = result
        return result

    def _scoped_flow(self, scope: Definition, expr: ast.expr, shadowed: frozenset[str], depth: int) -> Flow:
        """람다·컴프리헨션 안의 식이면 그 변수를 쓰는지 보고 흐름을 구한다(쓰면 바깥 범위로 풀 수 없어 열림).

        Args:
            scope: 범위.
            expr: 식.
            shadowed: 감싼 람다 매개변수·컴프리헨션 변수 이름.
            depth: 깊이.

        Returns:
            흐름.
        """
        if shadowed and any(isinstance(node, ast.Name) and node.id in shadowed for node in ast.walk(expr)):
            return opened("lambda-scope")
        return self.expr_flow(scope, expr, depth)

    def _write_flow(self, write: AttributeWrite, depth: int) -> Flow:
        """속성 쓰기 값의 흐름이다(값을 모르면 열림).

        Args:
            write: 쓰기.
            depth: 깊이.

        Returns:
            흐름.
        """
        if write.value is None:
            return opened("unknown-write")
        return self._scoped_flow(write.scope, write.value, write.shadowed, depth)

    # --- 식 ---

    def expr_flow(self, scope: Definition, expr: ast.expr, depth: int) -> Flow:
        """식의 흐름을 구한다.

        Args:
            scope: 식을 담은 범위.
            expr: 식.
            depth: 깊이.

        Returns:
            흐름.
        """
        if depth > MAX_FLOW_DEPTH:
            return opened("flow-depth")
        return self._memoized(("expr", scope.id, id(expr)), lambda: self._compute(scope, expr, depth + 1))

    def _compute(self, scope: Definition, expr: ast.expr, depth: int) -> Flow:
        """식 종류별로 흐름을 계산한다.

        Args:
            scope: 범위.
            expr: 식.
            depth: 깊이.

        Returns:
            흐름.
        """
        if isinstance(expr, ast.Constant):
            return EMPTY if expr.value is None else opened("not-instance")
        if isinstance(expr, ast.Name):
            return self.name_flow(scope, expr.id, depth)
        if isinstance(expr, ast.Attribute):
            return self._attribute_expr_flow(scope, expr, depth)
        if isinstance(expr, ast.Call):
            return self._call_flow(scope, expr, depth)
        if isinstance(expr, ast.IfExp):
            return union(self.expr_flow(scope, item, depth) for item in (expr.body, expr.orelse))
        if isinstance(expr, ast.BoolOp):
            return union(self.expr_flow(scope, item, depth) for item in expr.values)
        if isinstance(expr, ast.NamedExpr):
            return self.expr_flow(scope, expr.value, depth)
        return opened("dynamic-expression")

    # --- 이름 ---

    def name_flow(self, scope: Definition, name: str, depth: int) -> Flow:
        """이름의 흐름을 어휘 범위 규칙(LEGB, 클래스 범위 건너뜀)대로 구한다.

        Args:
            scope: 이름을 쓰는 범위.
            name: 이름.
            depth: 깊이.

        Returns:
            흐름.
        """
        current: Definition | None = scope
        first = True
        while current is not None:
            if current.kind == "module":
                return self.global_flow(current.path, name, depth)
            if first or current.kind != "class":
                bindings = self.resolver.bindings(current)
                if name in bindings.globals:
                    return self.global_flow(current.path, name, depth)
                if name in bindings.names and name not in bindings.nonlocals:
                    if self._has_nonlocal_writer(current, name):
                        return opened("nonlocal-write")
                    return self._bindings_flow(current, name, bindings.names[name], depth)
            first = False
            current = current.parent
        return opened("unresolved-name")

    def _has_nonlocal_writer(self, scope: Definition, name: str) -> bool:
        """안쪽 함수가 `nonlocal name`으로 이 범위의 이름을 다시 묶는지 본다.

        Args:
            scope: 이름을 묶는 범위.
            name: 이름.

        Returns:
            그렇다면 True.
        """
        names = self._nonlocals.get(scope.id)
        if names is None:
            body = getattr(scope.node, "body", [])
            statements = body if isinstance(body, list) else []
            names = frozenset(
                item
                for statement in statements
                for node in ast.walk(statement)
                if isinstance(node, ast.Nonlocal)
                for item in node.names
            )
            self._nonlocals[scope.id] = names
        return name in names

    def _bindings_flow(self, scope: Definition, name: str, bindings: list[Binding], depth: int) -> Flow:
        """한 범위의 묶음 전부의 흐름을 합친다.

        Args:
            scope: 범위.
            name: 이름.
            bindings: 묶음 목록.
            depth: 깊이.

        Returns:
            흐름.
        """
        return union(self._binding_flow(scope, name, binding, depth) for binding in bindings)

    def _binding_flow(self, scope: Definition, name: str, binding: Binding, depth: int) -> Flow:
        """묶음 하나의 흐름이다.

        Args:
            scope: 범위.
            name: 이름.
            binding: 묶음.
            depth: 깊이.

        Returns:
            흐름.
        """
        if binding.kind == "assign" and binding.value is not None:
            return self.expr_flow(scope, binding.value, depth)
        if binding.kind == "param":
            return self._parameter_flow(scope, name, binding, depth)
        if binding.kind == "import" and binding.binding is not None:
            return self._import_flow(binding.binding, depth)
        if binding.kind == "def":
            return opened("not-instance")
        return opened(binding.reason or "opaque-local")

    def _import_flow(self, binding: ImportBinding, depth: int) -> Flow:
        """`from m import name`으로 묶은 이름은 m의 모듈 전역 흐름이다(조건부 대입도 모두 본다).

        Args:
            binding: import 묶음.
            depth: 깊이.

        Returns:
            흐름(모듈 import·외부 이름은 인스턴스가 아니다).
        """
        path = self.resolver.symbols.project.resolve_module(binding.module)
        if binding.attribute is None or path is None:
            return opened("not-instance")
        module = self._module(path)
        if module is not None and binding.attribute in self.resolver.bindings(module).names:
            return self.global_flow(path, binding.attribute, depth)
        symbol = self.resolver.symbols.resolve_binding(binding)
        if isinstance(symbol, ValueSymbol):
            return self.global_flow(symbol.path, symbol.name, depth)
        return opened("not-instance")

    def _module(self, path: str) -> Definition | None:
        """모듈 정의를 돌려준다.

        Args:
            path: 모듈 경로.

        Returns:
            정의(프로그램 밖이면 None).
        """
        return self.index.definitions.get(f"{path}#<module>")

    # --- 매개변수 ---

    def _parameter_flow(self, function: Definition, name: str, binding: Binding, depth: int) -> Flow:
        """매개변수 자리의 흐름: 기본값 + 모든 호출 지점의 실인자. 열린 자리는 열림이다.

        Args:
            function: 함수 정의.
            name: 매개변수 이름.
            binding: 매개변수 묶음.
            depth: 깊이.

        Returns:
            흐름.
        """
        reason = self._parameter_open(function, binding)
        if reason is not None:
            return opened(reason)
        return self._memoized(("param", function.id, name), lambda: self._parameter_sites(function, name, depth))

    def _parameter_open(self, function: Definition, binding: Binding) -> str | None:
        """매개변수 자리를 여는 이유다.

        Args:
            function: 함수 정의.
            binding: 매개변수 묶음.

        Returns:
            이유, 닫혔으면 None.
        """
        if binding.position == -1:
            return "variadic-parameter"
        if function.kind == "method":
            if binding.position == 0 and not self._is_static(function):
                return "self-receiver"
            if getattr(function.node, "name", "") != "__init__":
                return "method-parameter"
            return self.init_open(function)
        if function.kind != "function":
            return "not-a-function"
        return self.function_open(function)

    def _is_static(self, method: Definition) -> bool:
        """정적 메서드인지 본다.

        Args:
            method: 메서드 정의.

        Returns:
            그렇다면 True.
        """
        assert method.parent is not None
        decorators = self.index.class_members(method.parent).decorators.get(getattr(method.node, "name", ""), set())
        return "staticmethod" in decorators

    def _parameter_sites(self, function: Definition, name: str, depth: int) -> Flow:
        """기본값과 호출 지점 실인자의 흐름을 합친다.

        Args:
            function: 함수 정의.
            name: 매개변수 이름.
            depth: 깊이.

        Returns:
            흐름.
        """
        sites = self.facts.sites.get(function.id, [])
        if not sites:
            return opened("no-call-site")
        node = function.node
        assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        shape = _parameter_shape(node.args, name)
        flows: list[Flow] = []
        if shape.default is not None and function.parent is not None:
            flows.append(self.expr_flow(function.parent, shape.default, depth))
        for site in sites:
            argument = _argument_for(site.call, site.offset, shape)
            if argument is _SPREAD:
                return opened("argument-spread")
            if isinstance(argument, ast.expr):
                flows.append(self._scoped_flow(site.scope, argument, site.shadowed, depth))
        return union(flows)

    def function_open(self, function: Definition) -> str | None:
        """함수(메서드가 아닌)의 매개변수 자리를 여는 이유다.

        Args:
            function: 함수 정의.

        Returns:
            이유, 닫혔으면 None.
        """
        facts = self.facts
        name = getattr(function.node, "name", "")
        if facts.all_open is not None:
            return facts.all_open
        if function.id in facts.open_functions:
            return facts.open_functions[function.id]
        if getattr(function.node, "decorator_list", []):
            return "decorated"
        if name in facts.mentions:
            return "string-reference"
        outside = facts.outside(function)
        if outside is not None:
            return outside
        if function.parent is not None and function.parent.kind == "module" and self._module_open(function.path):
            return "module-namespace"
        return None

    def init_open(self, init: Definition) -> str | None:
        """`__init__` 매개변수 자리를 여는 이유다. 그 정의로 생성되는 모든 클래스가 닫혀야 한다.

        Args:
            init: `__init__` 정의.

        Returns:
            이유, 닫혔으면 None.
        """
        facts = self.facts
        if facts.all_open is not None:
            return facts.all_open
        if init.id in facts.open_functions:
            return facts.open_functions[init.id]
        if facts.dynamic_construction:
            return "dynamic-construction"
        if facts.dynamic_subclassing:
            return "dynamic-subclass"
        if self._decorators(init) - _TRANSPARENT_DECORATORS:
            return "decorated"
        owner = init.parent
        assert owner is not None
        for definition in [owner, *self.linearizer.subclasses(owner)]:
            member = self.linearizer.lookup(definition, "__init__")
            if member.definition is init:
                reason = self.class_open(definition)
                if reason is not None:
                    return reason
        return None

    def _decorators(self, function: Definition) -> set[str]:
        """함수 장식자의 마지막 이름 집합이다.

        Args:
            function: 함수 정의.

        Returns:
            이름 집합(해석하지 못하는 장식자는 `<dynamic>`).
        """
        names: set[str] = set()
        for decorator in getattr(function.node, "decorator_list", []):
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
            else:
                names.add("<dynamic>")
        return names

    def class_open(self, definition: Definition) -> str | None:
        """클래스 생성자를(누가 어떤 인자로 생성하는지를) 여는 이유다.

        Args:
            definition: 클래스 정의.

        Returns:
            이유, 닫혔으면 None.
        """
        facts = self.facts
        node = definition.node
        assert isinstance(node, ast.ClassDef)
        if definition.id in facts.open_classes:
            return facts.open_classes[definition.id]
        if node.decorator_list:
            return "decorated-class"
        if self._has_metaclass(definition):
            return "metaclass"
        if not self.pure_project(definition):
            return "framework-base"
        if facts.dynamic_subclassing:
            return "dynamic-subclass"
        if node.name in facts.mentions:
            return "string-reference"
        outside = facts.outside(definition)
        if outside is not None:
            return outside
        if definition.parent is not None and definition.parent.kind == "module" and self._module_open(definition.path):
            return "module-namespace"
        return None

    def pure_project(self, definition: Definition) -> bool:
        """MRO가 프로젝트 클래스로만 이뤄졌는지 본다(`object`·`Generic`·`Protocol`·`ABC`는 선형화에서 빠진다).

        프레임워크·외부 기반은 프레임워크가 인스턴스를 만들고 속성을 쓴다(뷰의 `self.request`, ORM이 만든 모델).

        Args:
            definition: 클래스 정의.

        Returns:
            그렇다면 True.
        """
        return all(entry.kind == "project" for entry in self.linearizer.mro(definition)[1:])

    def _module_open(self, path: str) -> bool:
        """모듈 멤버를 계산된 이름으로 가져갈 수 있는지 본다.

        Args:
            path: 모듈 경로.

        Returns:
            그렇다면 True.
        """
        facts = self.facts
        exposed = path in facts.exposed_modules or ALL_MODULES in facts.exposed_modules
        return path in facts.computed_modules or (exposed and facts.computed_unknown)

    # --- 모듈 전역 ---

    def global_flow(self, path: str, name: str, depth: int) -> Flow:
        """모듈 전역 이름의 흐름: 모듈 수준 묶음, `global` 대입, 그 모듈에 대한 속성 쓰기.

        Args:
            path: 모듈 경로.
            name: 이름.
            depth: 깊이.

        Returns:
            흐름.
        """
        return self._memoized(("global", path, name), lambda: self._global_flow(path, name, depth))

    def _global_flow(self, path: str, name: str, depth: int) -> Flow:
        """`global_flow`의 계산이다.

        Args:
            path: 모듈 경로.
            name: 이름.
            depth: 깊이.

        Returns:
            흐름.
        """
        module = self.index.definitions.get(f"{path}#<module>")
        if module is None:
            return opened("excluded-source")
        outside = self.facts.outside(module, name)
        if outside is not None:
            return opened(outside)
        if path in self.facts.computed_modules:
            return opened("computed-attribute-write")
        bindings = self.resolver.bindings(module).names.get(name, [])
        if not bindings:
            return self._imported_global(path, name, depth)
        flows = [self._binding_flow(module, name, binding, depth) for binding in bindings]
        for scope in self._scopes_of(path):
            scope_bindings = self.resolver.bindings(scope)
            if name in scope_bindings.globals:
                flows.extend(
                    self._binding_flow(scope, name, item, depth) for item in scope_bindings.names.get(name, [])
                )
        flows.extend(self._module_writes(path, name, depth))
        return union(flows)

    def _imported_global(self, path: str, name: str, depth: int) -> Flow:
        """모듈 수준 묶음이 없는 이름(별 import로 들어온 이름)이다.

        Args:
            path: 모듈 경로.
            name: 이름.
            depth: 깊이.

        Returns:
            흐름.
        """
        symbol = self.resolver.symbols.resolve_name(path, name)
        if isinstance(symbol, ValueSymbol) and (symbol.path, symbol.name) != (path, name):
            return self.global_flow(symbol.path, symbol.name, depth)
        return opened("not-instance")

    def _scopes_of(self, path: str) -> list[Definition]:
        """모듈의 함수·메서드 범위다(`global` 선언을 찾는다).

        Args:
            path: 모듈 경로.

        Returns:
            정의 목록.
        """
        if self._module_scopes is None:
            table: dict[str, list[Definition]] = {}
            for definition in sorted(self.index.definitions.values(), key=lambda item: item.id):
                if definition.kind in ("function", "method"):
                    table.setdefault(definition.path, []).append(definition)
            self._module_scopes = table
        return self._module_scopes.get(path, [])

    def _module_writes(self, path: str, name: str, depth: int) -> list[Flow]:
        """그 모듈(또는 타입 모르는 수신자 — 모듈일 수 있다)에 대한 같은 이름 속성 쓰기의 흐름이다(원래 순서).

        색인이 미리 풀지 않은 쓰기(람다 뿌리, 아직 풀지 않은 수신자)는 옛 판정처럼 그 순번에서 수신자 값을 풀어 고른다.

        Args:
            path: 모듈 경로.
            name: 이름.
            depth: 깊이.

        Returns:
            흐름 목록.
        """
        index = self._writes_named(name)
        replayed = set(index.replayed)
        positions = sorted({*index.by_module.get(path, ()), *index.module_candidates, *replayed})
        return [
            self._write_flow(index.writes[position], depth)
            for position in positions
            if position not in replayed or self._may_be_module(index.writes[position], path)
        ]

    def _may_be_module(self, write: AttributeWrite, path: str) -> bool:
        """쓰기 수신자가 그 모듈이거나 모듈일 수 있는지 옛 판정대로 본다(수신자 값을 지금 푼다).

        Args:
            write: 쓰기.
            path: 모듈 경로.

        Returns:
            그렇다면 True.
        """
        value = self.resolver.value(write.scope, write.receiver)
        targets_module = isinstance(value, ModuleValue) and value.path == path
        return write.lambda_receiver or targets_module or isinstance(value, (UnknownValue, ExternalResult))

    # --- 속성 ---

    def _attribute_expr_flow(self, scope: Definition, expr: ast.Attribute, depth: int) -> Flow:
        """`obj.attr` 읽기의 흐름이다.

        Args:
            scope: 범위.
            expr: 속성 식.
            depth: 깊이.

        Returns:
            흐름.
        """
        base = self.resolver.value(scope, expr.value)
        if isinstance(base, ModuleValue):
            return self.global_flow(base.path, expr.attr, depth)
        if isinstance(base, InstanceValue):
            return self.attribute_flow(self.instance_classes(base), expr.attr, depth)
        if isinstance(base, ClassValue):
            return opened("class-attribute")
        flow = self.expr_flow(scope, expr.value, depth)
        if flow.open is not None:
            return flow
        return self.attribute_flow(frozenset(flow.classes), expr.attr, depth) if flow.classes else EMPTY

    def instance_classes(self, value: InstanceValue | ClassValue) -> frozenset[str]:
        """값이 가리킬 수 있는 프로젝트 클래스 id다(정확하지 않으면 하위 클래스 포함).

        하위 클래스 표는 처음 만든 뒤 바뀌지 않으므로 (클래스, 하위 클래스 포함 여부)로 메모한다.

        Args:
            value: 인스턴스·클래스 값.

        Returns:
            클래스 id 집합.
        """
        widened = _includes_subclasses(value)
        key = (value.definition.id, widened)
        cached = self._instance_classes.get(key)
        if cached is None:
            classes = {value.definition.id}
            if widened:
                classes.update(item.id for item in self.linearizer.subclasses(value.definition))
            cached = self._instance_classes[key] = frozenset(classes)
        return cached

    def attribute_flow(self, classes: frozenset[str], name: str, depth: int) -> Flow:
        """클래스들의 인스턴스 속성 자리 흐름: 클래스 본문 값 + 관련 수신자에 대한 같은 이름 쓰기.

        Args:
            classes: 인스턴스가 속할 수 있는 클래스 id.
            name: 속성 이름.
            depth: 깊이.

        Returns:
            흐름.
        """
        key = ("attribute", tuple(sorted(classes)), name)
        return self._memoized(key, lambda: self._attribute_flow(classes, name, depth))

    def _attribute_flow(self, classes: frozenset[str], name: str, depth: int) -> Flow:
        """`attribute_flow`의 계산이다.

        Args:
            classes: 클래스 id.
            name: 속성 이름.
            depth: 깊이.

        Returns:
            흐름.
        """
        flows: list[Flow] = []
        for class_id in sorted(classes):
            definition = self.index.definitions[class_id]
            reason = self.attribute_open(definition) or self._class_body_opaque(definition, name)
            if reason is not None:
                return opened(reason)
            member = self.linearizer.lookup(definition, name)
            if member.kind == "attribute" and member.owner is not None and member.owner.definition is not None:
                assert member.expr is not None
                flows.append(self.expr_flow(member.owner.definition, member.expr, depth))
            elif member.kind != "missing":
                return opened("not-data-attribute")
        flows.extend(self._write_flow(write, depth) for write in self._reaching_writes(name, classes, depth))
        return self._without_descriptors(union(flows))

    def _writes_named(self, name: str) -> _NameWrites:
        """같은 이름 속성 쓰기의 색인이다(이름마다 처음 질의할 때 한 번 만든다).

        Args:
            name: 속성 이름.

        Returns:
            색인.
        """
        index = self._name_writes.get(name)
        if index is None:
            index = _NameWrites(tuple(self.facts.writes.get(name, ())))
            for position, write in enumerate(index.writes):
                self._index_write(index, position, write)
            self._name_writes[name] = index
        return index

    def _index_write(self, index: _NameWrites, position: int, write: AttributeWrite) -> None:
        """쓰기 하나를 수신자의 정적 값으로 나눠 색인에 넣는다(`write_reaches`·`_module_writes`와 같은 분기다).

        해석기가 이미 푼 수신자 값만 쓴다. 람다 뿌리와 아직 풀지 않은 수신자는 질의가 옛 판정처럼 그 순번에서 판정한다.

        Args:
            index: 채울 색인.
            position: 쓰기 순번.
            write: 쓰기.
        """
        value = None if write.lambda_receiver else self.resolver.known_value(write.scope, write.receiver)
        if value is None:
            index.unresolved.append(position)
            index.replayed.append(position)
        elif isinstance(value, (InstanceValue, ClassValue)):
            _index_static(index, position, value)
        elif isinstance(value, ModuleValue):
            index.by_module.setdefault(value.path, []).append(position)
        elif not isinstance(value, _NOT_INSTANCES):
            index.unresolved.append(position)
            if isinstance(value, (UnknownValue, ExternalResult)):
                index.module_candidates.append(position)

    def _reaching_writes(self, name: str, classes: frozenset[str], depth: int) -> Iterator[AttributeWrite]:
        """그 클래스들의 인스턴스(또는 클래스 객체)에 닿을 수 있는 같은 이름 쓰기를 원래 순서로 낸다.

        정적 수신자 쓰기는 색인에서 바로 고르고, 수신자 흐름으로 판정할 쓰기만 `write_reaches`로 본다. 하나 낼 때마다
        호출자가 소비하므로 흐름 메모·순환 끊기·단계 예산을 쓰는 순서가 모든 쓰기를 차례로 판정할 때와 같다(처음 열린
        이유도 같다). 하위 클래스 표가 아직 없으면 하위 클래스까지 가리키는 첫 쓰기 앞의 쓰기를 먼저 판정하고, 그
        순번에 와서 표를 만든다(옛 판정은 그 쓰기의 `instance_classes`에서 만들었고, 메서드 가림 질의가 그 앞에서 멈추면
        만들지 않았다).

        Args:
            name: 속성 이름.
            classes: 클래스 id.
            depth: 깊이.

        Yields:
            닿을 수 있는 쓰기.
        """
        index = self._writes_named(name)
        reached = _exact_writes(index, classes)
        split = self._table_split(index)
        early = sorted(position for position in reached.union(index.unresolved) if position < split)
        yield from self._judged_writes(index, early, reached, classes, depth)
        reached |= self._widened_writes(index, classes)
        late = sorted(position for position in reached.union(index.unresolved) if position >= split)
        yield from self._judged_writes(index, late, reached, classes, depth)

    def _judged_writes(
        self, index: _NameWrites, positions: list[int], reached: set[int], classes: frozenset[str], depth: int
    ) -> Iterator[AttributeWrite]:
        """순번을 차례로 보며 정적으로 닿는 쓰기는 바로, 나머지는 `write_reaches`로 판정해 닿는 쓰기를 낸다.

        Args:
            index: 이름의 쓰기 색인.
            positions: 볼 순번(오름차순).
            reached: 정적으로 닿는 순번.
            classes: 클래스 id.
            depth: 깊이.

        Yields:
            닿을 수 있는 쓰기.
        """
        for position in positions:
            write = index.writes[position]
            if position in reached or self.write_reaches(
                write.scope, write.receiver, classes, depth, write.lambda_receiver
            ):
                yield write

    def _table_split(self, index: _NameWrites) -> int:
        """하위 클래스 표를 만들 쓰기 순번이다(표가 이미 있거나 하위 클래스까지 가리키는 쓰기가 없으면 0).

        Args:
            index: 이름의 쓰기 색인.

        Returns:
            순번.
        """
        if index.first_widened is None or self.linearizer.has_subclass_table():
            return 0
        return index.first_widened[0]

    def _widened_writes(self, index: _NameWrites, classes: frozenset[str]) -> set[int]:
        """하위 클래스까지 가리키는 수신자 쓰기 가운데 그 클래스들의 인스턴스(또는 클래스 객체)에 닿는 것의 순번이다.

        수신자 값의 `instance_classes`가 질의 클래스 c를 담는 쓰기다: 수신자 클래스가 c이거나 c의 프로젝트 상위
        클래스다(`Linearizer.superclass_ids`는 하위 클래스 표의 역관계라 두 판정이 정확히 같다). 질의 클래스와 그 조상을
        먼저 모아 버킷마다 한 번만 읽는다. 하위 클래스 표가 없으면 옛 판정이 첫 쓰기의 `instance_classes`에서 했듯이
        지금 만든다.

        Args:
            index: 이름의 쓰기 색인.
            classes: 클래스 id.

        Returns:
            쓰기 순번 집합.
        """
        if index.first_widened is None:
            return set()
        # 반환값은 쓰지 않는다: 하위 클래스 표를 옛 판정과 같은 순번에서 만들려고 부른다.
        self.linearizer.subclasses(index.first_widened[1])
        owners = set(classes)
        for class_id in classes:
            owners.update(self.linearizer.superclass_ids(class_id))
        return {position for owner in owners for position in index.widened.get(owner, ())}

    def _class_body_opaque(self, definition: Definition, name: str) -> str | None:
        """MRO의 프로젝트 클래스 본문이 이름을 멤버 표가 모르는 방식으로 묶는지 본다(두 번 대입, 반복 변수, import 등).

        Args:
            definition: 클래스 정의.
            name: 속성 이름.

        Returns:
            그렇다면 `rebound-attribute`, 아니면 None.
        """
        for entry in self.linearizer.mro(definition):
            if entry.definition is None:
                continue
            count = class_bindings(entry.definition, name)
            known = name in self.index.class_members(entry.definition).attributes
            if count > 1 or (count == 1 and not known):
                return "rebound-attribute"
        return None

    def _without_descriptors(self, flow: Flow) -> Flow:
        """속성 자리 흐름에 서술자(`__get__`을 정의한 프로젝트 클래스)가 있으면 연다.

        클래스 속성에 놓인 서술자는 읽을 때 `__get__`의 반환 값이 된다. 클래스 본문 값인지 클래스 객체에 쓴 값인지
        인스턴스 속성인지 구별하지 않고 연다(보수적이다).

        Args:
            flow: 속성 자리 흐름.

        Returns:
            흐름.
        """
        for class_id in flow.classes:
            for entry in self.linearizer.mro(self.index.definitions[class_id]):
                if entry.definition is not None and "__get__" in self.index.class_members(entry.definition).methods:
                    return opened("descriptor")
        return flow

    def attribute_open(self, definition: Definition) -> str | None:
        """클래스 인스턴스의 속성·메서드 자리를 여는 이유다.

        Args:
            definition: 클래스 정의.

        Returns:
            이유, 닫혔으면 None.
        """
        node = definition.node
        assert isinstance(node, ast.ClassDef)
        if not self.pure_project(definition):
            return "framework-base"
        if node.decorator_list:
            return "decorated-class"
        if self._has_metaclass(definition):
            return "metaclass"
        if self.facts.dynamic_subclassing:
            return "dynamic-subclass"
        dirty = self.class_dirty(definition.id)
        if dirty is not None:
            return dirty
        outside = self.facts.outside(definition)
        if outside is not None:
            return outside
        for entry in self.linearizer.mro(definition):
            if entry.definition is not None and _ATTRIBUTE_HOOKS & set(
                self.index.class_members(entry.definition).methods
            ):
                return "attribute-hook"
        return None

    def class_dirty(self, class_id: str) -> str | None:
        """계산된 이름의 쓰기(`setattr(x, name, v)`, `x.__dict__`, `vars(x)`, `x.__class__ = …`)가 그 클래스
        인스턴스에 닿을 수 있으면 이유를 돌려준다.

        정적 값으로 대상을 안 쓰기는 `exposure`가 이미 표시했다. 나머지는 `_pending_targets`의 수신자 흐름이 닫혔을
        때만 좁히고, 흐름이 열린 쓰기는 대상을 모르는 틈(`bound-assumptions:`)이라 막지 않는다.

        Args:
            class_id: 클래스 id.

        Returns:
            이유, 닿지 않으면 None.
        """
        if class_id in self.facts.dirty_classes:
            return self.facts.dirty_classes[class_id]
        if self._restricted:
            return None
        for flow in self._pending_targets():
            if flow.open is None and class_id in flow.classes:
                return "computed-attribute-write"
        return None

    def _pending_targets(self) -> list[Flow]:
        """정적 값으로 대상을 모르는 계산된 쓰기의 수신자 흐름이다(한 번 계산한다).

        계산된 쓰기가 연 클래스는 다시 흐름을 바꾸므로(순환 의존), 이 흐름은 그 쓰기들을 무시한 제한 분석으로 따로
        구한다(메모·활성 집합·예산을 분리한다). 제한 분석은 전체 분석보다 닫히거나 같으므로 닫힌 결과로 더 많은
        클래스를 막을 뿐이고, 제한 분석이 열렸다면 전체 분석도 열려 있다.

        Returns:
            쓰기마다 수신자 흐름.
        """
        if self._pending_flows is not None:
            return self._pending_flows
        saved = (self._memo, self._active, self._steps)
        self._memo, self._restricted = {}, True
        results: list[Flow] = []
        try:
            for write in self.facts.pending_writes:
                self._active, self._steps = set(), 0
                results.append(self._pending_flow(write))
        finally:
            self._memo, self._active, self._steps = saved
            self._restricted = False
        self._pending_flows = results
        return results

    def _pending_flow(self, write: AttributeWrite) -> Flow:
        """계산된 쓰기 하나의 수신자 흐름이다(예산을 넘으면 열림).

        Args:
            write: 계산된 쓰기.

        Returns:
            흐름.
        """
        if write.lambda_receiver:
            return opened("lambda-scope")
        try:
            return self._scoped_flow(write.scope, write.receiver, write.shadowed, 0)
        except BudgetExceeded:
            self.budget_exceeded += 1
            return opened("flow-budget")

    def unknown_target_writes(self) -> int:
        """수신자 흐름이 열려 대상을 모르는 계산된 이름의 쓰기 수다(모델링하지 않은 틈).

        Returns:
            수.
        """
        return sum(flow.open is not None for flow in self._pending_targets())

    def write_reaches(
        self, scope: Definition, receiver: ast.expr, classes: frozenset[str], depth: int, opaque: bool = False
    ) -> bool:
        """속성 쓰기의 수신자가 그 클래스들의 인스턴스(또는 클래스 객체)일 수 있는지 본다.

        정적 값이 프로젝트 인스턴스·클래스면 그 클래스 집합으로, 타입을 모르면 수신자 흐름이 닫혔을 때만 좁히고 아니면
        닿을 수 있다고 본다. 모듈·외부 값·함수는 인스턴스가 아니다.

        Args:
            scope: 쓰기를 담은 범위.
            receiver: 수신자 식.
            classes: 클래스 id.
            depth: 깊이.
            opaque: 수신자 뿌리가 람다 매개변수·컴프리헨션 변수라 풀 수 없는지.

        Returns:
            닿을 수 있으면 True.
        """
        if opaque:
            return True
        value: Value = self.resolver.value(scope, receiver)
        if isinstance(value, (InstanceValue, ClassValue)):
            return not self.instance_classes(value).isdisjoint(classes)
        if isinstance(value, _NOT_INSTANCES):
            return False
        flow = self.expr_flow(scope, receiver, depth)
        return flow.open is not None or not flow.classes.isdisjoint(classes)

    # --- 호출 결과 ---

    def _call_flow(self, scope: Definition, call: ast.Call, depth: int) -> Flow:
        """호출 결과의 흐름: 생성자, 프로젝트 함수·정확한 수신자 메서드의 반환 값.

        Args:
            scope: 범위.
            call: 호출 식.
            depth: 깊이.

        Returns:
            흐름.
        """
        callee = self.resolver.value(scope, call.func)
        if isinstance(callee, ClassValue):
            return self.construct_flow(callee)
        if isinstance(callee, FunctionValue):
            return self.return_flow(callee.definition, depth)
        if isinstance(callee, MethodValue) and callee.exact and not callee.is_property:
            return self.return_flow(callee.member, depth)
        return opened("call-result")

    def construct_flow(self, value: ClassValue) -> Flow:
        """클래스 호출 결과: 정확한 K, 하위 클래스일 수 있으면(`cls`) K와 프로젝트 하위 클래스.

        장식한 클래스(다른 객체로 바뀔 수 있다), 메타클래스, 프로젝트 `__new__`, 표에 없는 외부·모르는 기반은 결과를
        모른다. 라이브러리의 공개 클래스를 `cls`로 만들면 외부 하위 클래스일 수 있다.

        Args:
            value: 클래스 값.

        Returns:
            흐름.
        """
        definitions = [value.definition]
        if not value.exact:
            outside = self.facts.outside(value.definition)
            if outside is not None:
                return opened(outside)
            if self.facts.dynamic_subclassing:
                return opened("dynamic-subclass")
            definitions.extend(self.linearizer.subclasses(value.definition))
        for definition in definitions:
            reason = self._constructor_opaque(definition, 0)
            if reason is not None:
                return opened(reason)
        return Flow(frozenset(item.id for item in definitions))

    def _constructor_opaque(self, definition: Definition, depth: int) -> str | None:
        """생성 결과가 그 클래스 인스턴스라고 말할 수 없는 이유다.

        MRO 어디든 장식한 클래스·메타클래스(물려받은 것 포함 — `__call__`이 무엇이든 돌려줄 수 있다)·프로젝트나
        프레임워크 `__new__`(메서드·클래스 본문 대입), 표에 없는 외부·모르는 기반이 있거나, MRO 클래스에 `__new__`를
        쓰는 곳(`Repo.__new__ = …`)이 있으면 모른다.

        Args:
            definition: 클래스 정의.
            depth: 깊이.

        Returns:
            이유, 없으면 None.
        """
        node = definition.node
        assert isinstance(node, ast.ClassDef)
        if node.decorator_list:
            return "decorated-class"
        if self._has_metaclass(definition):
            return "metaclass"
        classes: set[str] = set()
        for entry in self.linearizer.mro(definition):
            if entry.kind in ("external", "unknown"):
                return "opaque-constructor"
            if entry.kind == "framework" and "__new__" in FRAMEWORK_CLASSES[entry.key]["methods"]:
                return "custom-new"
            if entry.definition is not None:
                members = self.index.class_members(entry.definition)
                if "__new__" in members.methods or "__new__" in members.attributes:
                    return "custom-new"
                classes.add(entry.definition.id)
        for write in self.facts.writes.get("__new__", []):
            if self.write_reaches(write.scope, write.receiver, frozenset(classes), depth, write.lambda_receiver):
                return "custom-new"
        return None

    def _has_metaclass(self, definition: Definition) -> bool:
        """MRO의 프로젝트 클래스 중 하나라도 클래스 키워드(메타클래스 등)를 쓰는지 본다(메타클래스는 물려받는다).

        Args:
            definition: 클래스 정의.

        Returns:
            그렇다면 True.
        """
        return any(
            isinstance(entry.definition.node, ast.ClassDef) and bool(entry.definition.node.keywords)
            for entry in self.linearizer.mro(definition)
            if entry.definition is not None
        )

    def return_flow(self, function: Definition, depth: int) -> Flow:
        """함수의 모든 `return` 값의 흐름이다.

        Args:
            function: 함수·메서드 정의.
            depth: 깊이.

        Returns:
            흐름.
        """
        return self._memoized(("return", function.id), lambda: self._return_flow(function, depth))

    def _return_flow(self, function: Definition, depth: int) -> Flow:
        """`return_flow`의 계산이다.

        Args:
            function: 함수 정의.
            depth: 깊이.

        Returns:
            흐름.
        """
        node = function.node
        if not isinstance(node, ast.FunctionDef):
            return opened("coroutine" if isinstance(node, ast.AsyncFunctionDef) else "not-a-function")
        if self._decorators(function) - _TRANSPARENT_DECORATORS:
            return opened("decorated")
        values: list[ast.expr] = []
        for child in _own_nodes(node):
            if isinstance(child, (ast.Yield, ast.YieldFrom)):
                return opened("generator")
            if isinstance(child, ast.Return) and child.value is not None:
                values.append(child.value)
        return union(self.expr_flow(function, value, depth) for value in values)


@dataclass(frozen=True)
class _ParameterShape:
    """호출 지점 실인자를 매개변수에 맞추는 정보.

    Attributes:
        name: 매개변수 이름.
        position: 위치 순번(키워드 전용이면 None).
        keyword: 키워드로 넘길 수 있는지(위치 전용이 아니다).
        default: 기본값 식.
    """

    name: str
    position: int | None
    keyword: bool
    default: ast.expr | None


#: 실인자가 펼치기(`*args`·`**kwargs`)라 자리를 모른다는 표식이다.
_SPREAD = object()


def _parameter_shape(arguments: ast.arguments, name: str) -> _ParameterShape:
    """매개변수 하나의 위치·키워드 여부·기본값을 구한다.

    Args:
        arguments: 인자 정의.
        name: 매개변수 이름.

    Returns:
        정보.
    """
    positional = [*arguments.posonlyargs, *arguments.args]
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(arguments.defaults)) + list(arguments.defaults)
    for position, argument in enumerate(positional):
        if argument.arg == name:
            return _ParameterShape(name, position, position >= len(arguments.posonlyargs), defaults[position])
    for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True):
        if argument.arg == name:
            return _ParameterShape(name, None, True, default)
    return _ParameterShape(name, None, False, None)


def _argument_for(call: ast.Call, offset: int, shape: _ParameterShape) -> ast.expr | object | None:
    """호출 지점에서 매개변수를 채우는 실인자 식이다.

    Args:
        call: 호출.
        offset: 첫 실인자가 채우는 매개변수 순번.
        shape: 매개변수 정보.

    Returns:
        실인자 식, 펼치기면 `_SPREAD`, 넘기지 않았으면 None(기본값).
    """
    if any(isinstance(item, ast.Starred) for item in call.args) or any(item.arg is None for item in call.keywords):
        return _SPREAD
    if shape.position is not None:
        index = shape.position - offset
        if 0 <= index < len(call.args):
            return call.args[index]
    if shape.keyword:
        for keyword in call.keywords:
            if keyword.arg == shape.name:
                return keyword.value
    return None


def _own_nodes(node: ast.FunctionDef) -> list[ast.AST]:
    """함수 자기 본문의 노드다(중첩 정의·람다 제외).

    Args:
        node: 함수 정의.

    Returns:
        노드 목록.
    """
    result: list[ast.AST] = []
    pending: list[ast.AST] = list(node.body)
    while pending:
        current = pending.pop()
        result.append(current)
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        pending.extend(ast.iter_child_nodes(current))
    return result
