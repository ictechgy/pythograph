"""어휘 범위를 따르는 정적 값 해석기.

파이썬 이름 규칙(LEGB)대로 푼다: 함수 지역 → 바깥 함수(클래스 범위는 건너뛴다) → 모듈 전역(import·정의·대입,
`from x import *`) → 내장 이름. 클래스 본문은 자기 본문 이름을 먼저 본다. 지역 이름은 흐름을 따지지 않는 근사다 —
한 범위에서 두 번 이상 묶인 이름은 모르는 값이다(추측하지 않는다). 매개변수는 첫 매개변수(`self`·`cls`)와
프로젝트 클래스 주석만 값을 안다.

속성은 모듈 속성·재수출(`__init__`), 프로젝트 클래스의 MRO(메서드·중첩 클래스·클래스 속성·프레임워크 멤버),
외부 이름의 점 경로로 푼다. 호출 결과는 생성자(정확한 인스턴스)와 프로젝트 클래스 반환 주석(하위 클래스 가능)만
안다.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass

from pythograph.graph.framework_table import FRAMEWORK_CLASSES
from pythograph.graph.index import Definition, DefinitionIndex
from pythograph.graph.mro import Linearizer, Member
from pythograph.graph.values import (
    AttributeValue,
    ClassValue,
    ExternalResult,
    ExternalValue,
    FrameworkMethodValue,
    FunctionValue,
    InstanceValue,
    MethodValue,
    ModuleValue,
    SuperValue,
    UnknownValue,
    Value,
)
from pythograph.source.symbols import (
    ExternalSymbol,
    ImportBinding,
    MemberSymbol,
    ModuleSymbol,
    ProjectSymbol,
    Symbol,
    SymbolTable,
    ValueSymbol,
    absolute_module,
)

#: 값을 따라가는 최대 깊이다(순환·과도한 재귀 방지).
MAX_VALUE_DEPTH = 32

#: 내장 이름(CPython 3.13 `dir(builtins)`의 공개 이름). 실행 중인 인터프리터와 무관하게 같은 결과를 내려고 고정한다.
BUILTIN_NAMES = frozenset(
    """ArithmeticError AssertionError AttributeError BaseException BaseExceptionGroup BlockingIOError BrokenPipeError
    BufferError BytesWarning ChildProcessError ConnectionAbortedError ConnectionError ConnectionRefusedError
    ConnectionResetError DeprecationWarning EOFError Ellipsis EncodingWarning EnvironmentError Exception
    ExceptionGroup False FileExistsError FileNotFoundError FloatingPointError FutureWarning GeneratorExit IOError
    ImportError ImportWarning IndentationError IndexError InterruptedError IsADirectoryError KeyError
    KeyboardInterrupt LookupError MemoryError ModuleNotFoundError NameError None NotADirectoryError NotImplemented
    NotImplementedError OSError OverflowError PendingDeprecationWarning PermissionError ProcessLookupError
    PythonFinalizationError RecursionError ReferenceError ResourceWarning RuntimeError RuntimeWarning
    StopAsyncIteration StopIteration SyntaxError SyntaxWarning SystemError SystemExit TabError TimeoutError True
    TypeError UnboundLocalError UnicodeDecodeError UnicodeEncodeError UnicodeError UnicodeTranslateError
    UnicodeWarning UserWarning ValueError Warning ZeroDivisionError abs aiter all anext any ascii bin bool breakpoint
    bytearray bytes callable chr classmethod compile complex copyright credits delattr dict dir divmod enumerate eval
    exec exit filter float format frozenset getattr globals hasattr hash help hex id input int isinstance issubclass
    iter len license list locals map max memoryview min next object oct open ord pow print property quit range repr
    reversed round set setattr slice sorted staticmethod str sum super tuple type vars zip __import__ __name__
    __file__ __doc__ __package__ __spec__ __loader__ __build_class__ __debug__""".split()  # noqa: SIM905 — 긴 고정 목록
)

#: 매개변수 이름 수집 대상 인자 필드다.
_PARAMETER_FIELDS = ("posonlyargs", "args", "kwonlyargs")


@dataclass(frozen=True)
class Binding:
    """지역 이름 묶음 하나.

    Attributes:
        kind: `param`·`assign`·`def`·`import`·`opaque`.
        value: 대입 값 식(`assign`), 주석 식(`param`).
        position: 매개변수 순번(`param`, 가변 인자는 -1).
        definition: 중첩 정의(`def`).
        binding: import 묶음(`import`).
        reason: 모르는 값의 이유(`opaque`).
    """

    kind: str
    value: ast.expr | None = None
    position: int = 0
    definition: Definition | None = None
    binding: ImportBinding | None = None
    reason: str = ""


@dataclass
class ScopeBindings:
    """한 함수·클래스 범위의 이름 묶음.

    Attributes:
        names: 이름 → 묶음 목록.
        globals: `global` 선언 이름.
        nonlocals: `nonlocal` 선언 이름.
    """

    names: dict[str, list[Binding]]
    globals: set[str]
    nonlocals: set[str]


class Resolver:
    """프로젝트 전체의 정적 값 해석기."""

    def __init__(self, index: DefinitionIndex, symbols: SymbolTable) -> None:
        """해석기를 만든다.

        Args:
            index: 선언 색인.
            symbols: 모듈 색인·이름 해석기.
        """
        self.index = index
        self.symbols = symbols
        self.linearizer = Linearizer(index, self._base_value)
        self._bindings: dict[str, ScopeBindings] = {}
        self._memo: dict[tuple[str, int], Value] = {}
        self._active: set[tuple[str, str]] = set()
        self._annotations: dict[int, tuple[ast.Constant, ast.expr | None]] = {}

    def _base_value(self, definition: Definition, expr: ast.expr) -> Value:
        """클래스 기반 식을 클래스를 감싼 범위에서 푼다.

        Args:
            definition: 클래스 정의.
            expr: 기반 식.

        Returns:
            값.
        """
        assert definition.parent is not None
        return self.value(definition.parent, expr)

    def value(self, scope: Definition, expr: ast.expr, depth: int = 0) -> Value:
        """식의 값을 푼다(메모).

        Args:
            scope: 식을 담은 범위(모듈·클래스·함수 정의).
            expr: 식.
            depth: 재귀 깊이.

        Returns:
            값.
        """
        key = (scope.id, id(expr))
        cached = self._memo.get(key)
        if cached is not None:
            return cached
        if depth > MAX_VALUE_DEPTH:
            return UnknownValue("depth-limit")
        result = self._compute(scope, expr, depth)
        self._memo[key] = result
        return result

    def _compute(self, scope: Definition, expr: ast.expr, depth: int) -> Value:
        """식 종류별로 값을 계산한다.

        Args:
            scope: 범위.
            expr: 식.
            depth: 재귀 깊이.

        Returns:
            값.
        """
        if isinstance(expr, ast.Name):
            return self.name_value(scope, expr.id, depth + 1)
        if isinstance(expr, ast.Attribute):
            return self.member_value(scope, self.value(scope, expr.value, depth + 1), expr.attr, depth + 1)
        if isinstance(expr, ast.Call):
            return self.call_value(scope, expr, depth + 1)
        if isinstance(expr, ast.NamedExpr):
            return self.value(scope, expr.value, depth + 1)
        if isinstance(expr, ast.Await):
            return UnknownValue("call-result")
        return UnknownValue("dynamic-expression")

    def name_value(self, scope: Definition, name: str, depth: int) -> Value:
        """이름을 어휘 범위 규칙대로 푼다.

        Args:
            scope: 이름을 쓰는 범위.
            name: 이름.
            depth: 재귀 깊이.

        Returns:
            값.
        """
        current: Definition | None = scope
        first = True
        while current is not None:
            if current.kind == "module":
                return self.module_name_value(current.path, name, depth)
            if first or current.kind != "class":
                bindings = self.bindings(current)
                if name in bindings.globals:
                    return self.module_name_value(current.path, name, depth)
                if name in bindings.names and name not in bindings.nonlocals:
                    return self._binding_value(current, name, bindings.names[name], depth)
            first = False
            current = current.parent
        return UnknownValue("unresolved-name")

    def _binding_value(self, scope: Definition, name: str, bindings: list[Binding], depth: int) -> Value:
        """지역 묶음의 값을 푼다. 두 번 이상 묶였으면 모른다.

        Args:
            scope: 묶음이 있는 범위.
            name: 이름.
            bindings: 묶음 목록.
            depth: 재귀 깊이.

        Returns:
            값.
        """
        if len(bindings) != 1:
            return UnknownValue("rebound-local")
        key = (scope.id, name)
        if key in self._active:
            return UnknownValue("recursive-binding")
        self._active.add(key)
        try:
            return self._single_binding(scope, bindings[0], depth)
        finally:
            self._active.discard(key)

    def _single_binding(self, scope: Definition, binding: Binding, depth: int) -> Value:
        """묶음 하나의 값을 푼다.

        Args:
            scope: 범위.
            binding: 묶음.
            depth: 재귀 깊이.

        Returns:
            값.
        """
        if binding.kind == "def" and binding.definition is not None:
            return _definition_value(binding.definition)
        if binding.kind == "assign" and binding.value is not None:
            return self.value(scope, binding.value, depth + 1)
        if binding.kind == "import" and binding.binding is not None:
            return self.symbol_value(self.symbols.resolve_binding(binding.binding), depth)
        if binding.kind == "param":
            return self._parameter_value(scope, binding, depth)
        return UnknownValue(binding.reason or "opaque-local")

    def _parameter_value(self, scope: Definition, binding: Binding, depth: int) -> Value:
        """매개변수의 값을 푼다: 메서드 첫 매개변수, 프로젝트 클래스 주석, 그 밖은 모름.

        Args:
            scope: 함수 정의.
            binding: 매개변수 묶음.
            depth: 재귀 깊이.

        Returns:
            값.
        """
        if binding.position == 0 and scope.kind == "method" and scope.parent is not None:
            decorators = self.index.class_members(scope.parent).decorators.get(_node_name(scope.node), set())
            if "staticmethod" not in decorators:
                is_class = "classmethod" in decorators or _node_name(scope.node) == "__new__"
                return ClassValue(scope.parent, exact=False) if is_class else InstanceValue(scope.parent, exact=False)
        if binding.value is not None and scope.parent is not None:
            return self.annotation_value(scope.parent, binding.value, depth)
        return UnknownValue("parameter")

    def annotation_value(self, scope: Definition, annotation: ast.expr, depth: int) -> Value:
        """타입 주석을 인스턴스 값으로 푼다(`Optional[X]`·`X | None`·문자열 주석 포함).

        Args:
            scope: 주석을 푸는 범위.
            annotation: 주석 식.
            depth: 재귀 깊이.

        Returns:
            프로젝트 클래스면 하위 클래스 가능 인스턴스, 외부 타입이면 외부 결과, 아니면 모름.
        """
        target = _annotation_target(annotation, self._parse_annotation)
        if target is None:
            return UnknownValue("parameter")
        resolved = self.value(scope, target, depth + 1)
        if isinstance(resolved, ClassValue):
            return InstanceValue(resolved.definition, exact=False)
        if isinstance(resolved, ExternalValue):
            return ExternalResult()
        return UnknownValue("parameter")

    def _parse_annotation(self, constant: ast.Constant) -> ast.expr | None:
        """문자열 주석을 파싱한다(캐시). 상수 노드와 파싱 결과를 함께 보관해 노드 id가 재사용되지 않게 한다.

        Args:
            constant: 문자열 상수 노드.

        Returns:
            파싱한 식, 문법 오류면 None.
        """
        cached = self._annotations.get(id(constant))
        if cached is None:
            try:
                parsed: ast.expr | None = ast.parse(str(constant.value), mode="eval").body
            except (SyntaxError, ValueError, RecursionError, MemoryError):
                parsed = None
            cached = (constant, parsed)
            self._annotations[id(constant)] = cached
        return cached[1]

    def module_name_value(self, path: str, name: str, depth: int) -> Value:
        """모듈 전역 이름을 푼다: 정의·import·대입 → 프로젝트 `*` import → 내장 이름.

        Args:
            path: 모듈 경로.
            name: 이름.
            depth: 재귀 깊이.

        Returns:
            값.
        """
        symbol = self.symbols.resolve_name(path, name)
        if symbol is not None:
            return self.symbol_value(symbol, depth)
        # 프로젝트 `*` import는 이름 해석기가 따라간다. 외부·`__all__` 미확정 모듈의 `*`를 만나면 그 이름(내장 이름
        # 포함)을 그 모듈이 가릴 수 있어 모른다.
        if self.symbols.star_blocked(path, name):
            return UnknownValue("star-import")
        return ExternalValue(f"builtins.{name}") if name in BUILTIN_NAMES else UnknownValue("unresolved-name")

    def symbol_value(self, symbol: Symbol | None, depth: int) -> Value:
        """모듈 색인의 해석 결과를 값으로 바꾼다.

        Args:
            symbol: 해석 결과.
            depth: 재귀 깊이.

        Returns:
            값.
        """
        if isinstance(symbol, ProjectSymbol):
            definition = self.index.by_node.get(symbol.node)
            return _definition_value(definition) if definition else UnknownValue("excluded-source")
        if isinstance(symbol, ModuleSymbol):
            return ModuleValue(symbol.path, symbol.dotted)
        if isinstance(symbol, ExternalSymbol):
            return self._external_value(symbol.dotted)
        if isinstance(symbol, ValueSymbol):
            module = self.index.definitions.get(f"{symbol.path}#<module>")
            return self.value(module, symbol.node, depth + 1) if module else UnknownValue("excluded-source")
        if isinstance(symbol, MemberSymbol):
            owner = self.index.by_node.get(symbol.owner.node)
            if owner is None:
                return UnknownValue("excluded-source")
            return self._class_member(ClassValue(owner), symbol.name)
        return UnknownValue("unresolved-name")

    def _external_value(self, dotted: str) -> Value:
        """외부 점 경로를 값으로 바꾼다. 최상위 패키지가 프로젝트에 있으면 풀지 못한 import다.

        Args:
            dotted: 점 경로.

        Returns:
            외부 값 또는 모름.
        """
        top = dotted.split(".")[0]
        if self.symbols.project.resolve_module(top) is not None:
            return UnknownValue("unresolved-import")
        return ExternalValue(dotted)

    def member_value(self, scope: Definition, base: Value, name: str, depth: int) -> Value:
        """값의 속성을 푼다.

        Args:
            scope: 식을 담은 범위.
            base: 속성을 가진 값.
            name: 속성 이름.
            depth: 재귀 깊이.

        Returns:
            값.
        """
        if isinstance(base, ModuleValue):
            return self.symbol_value(self.symbols.member(ModuleSymbol(base.path, base.dotted), name), depth)
        if isinstance(base, ExternalValue):
            return ExternalValue(f"{base.dotted}.{name}")
        if isinstance(base, (ClassValue, InstanceValue)):
            return self._class_member(base, name)
        if isinstance(base, SuperValue):
            return self._super_member(base, name)
        if isinstance(base, AttributeValue):
            return self.member_value(scope, self.attribute_target(base, depth), name, depth + 1)
        if isinstance(base, MethodValue) and base.is_property:
            return self.member_value(scope, self._return_value(base.member, depth), name, depth + 1)
        if isinstance(base, FrameworkMethodValue) and base.is_property:
            return UnknownValue("untyped-receiver")
        if isinstance(base, (FunctionValue, MethodValue, FrameworkMethodValue)):
            return UnknownValue("function-attribute")
        return UnknownValue("untyped-receiver")

    def attribute_target(self, attribute: AttributeValue, depth: int) -> Value:
        """클래스 속성 값 식을 그 클래스 본문 범위에서 푼다.

        Args:
            attribute: 클래스 속성.
            depth: 재귀 깊이.

        Returns:
            값.
        """
        return self.value(attribute.owner, attribute.expr, depth + 1)

    def _class_member(self, base: ClassValue | InstanceValue, name: str) -> Value:
        """프로젝트 클래스·인스턴스의 멤버를 MRO로 푼다.

        Args:
            base: 클래스 또는 인스턴스 값.
            name: 멤버 이름.

        Returns:
            값.
        """
        member = self.linearizer.lookup(base.definition, name)
        return self._member_to_value(base, member)

    def _super_member(self, base: SuperValue, name: str) -> Value:
        """`super().name`을 푼다.

        Args:
            base: super 값.
            name: 멤버 이름.

        Returns:
            값.
        """
        member = self.linearizer.lookup(base.receiver, name, after=base.owner.id)
        return self._member_to_value(InstanceValue(base.receiver, exact=False), member, super_owner=base.owner.id)

    def _member_to_value(
        self, base: ClassValue | InstanceValue, member: Member, super_owner: str | None = None
    ) -> Value:
        """MRO 조회 결과를 값으로 바꾼다.

        Args:
            base: 수신자.
            member: 조회 결과.
            super_owner: `super()` 조회면 호출한 클래스 id.

        Returns:
            값.
        """
        if member.kind == "method" and member.definition is not None and member.definition.parent is not None:
            decorators = self.index.class_members(member.definition.parent).decorators.get(member.name, set())
            is_property = bool(decorators & {"property", "cached_property"})
            return MethodValue(base.definition, member.definition, base.exact, super_owner is not None, is_property)
        if member.kind == "class" and member.definition is not None:
            return ClassValue(member.definition)
        if member.kind == "attribute" and member.owner is not None and member.owner.definition is not None:
            assert member.expr is not None
            return AttributeValue(member.owner.definition, member.name, member.expr)
        if member.kind == "framework-method" and member.owner is not None:
            entry = FRAMEWORK_CLASSES[member.owner.key]["methods"][member.name]
            is_property = bool(entry.get("property"))
            return FrameworkMethodValue(base.definition, member.name, base.exact, is_property, super_owner)
        if member.kind in ("framework-attribute", "external"):
            return ExternalValue(f"{member.owner.key if member.owner else 'external'}.{member.name}")
        if member.kind == "unknown":
            return UnknownValue("unknown-base")
        return UnknownValue("instance-attribute" if isinstance(base, InstanceValue) else "dynamic-attribute")

    def call_value(self, scope: Definition, call: ast.Call, depth: int) -> Value:
        """호출 결과 값을 푼다: 생성자, 반환 주석, `super()`·`type()`·리터럴 `getattr()`.

        Args:
            scope: 범위.
            call: 호출 식.
            depth: 재귀 깊이.

        Returns:
            값.
        """
        callee = self.value(scope, call.func, depth + 1)
        if isinstance(callee, ClassValue):
            return InstanceValue(callee.definition, callee.exact)
        if isinstance(callee, AttributeValue):
            target = self.attribute_target(callee, depth)
            return (
                InstanceValue(target.definition, exact=False)
                if isinstance(target, ClassValue)
                else UnknownValue("call-result")
            )
        if isinstance(callee, (FunctionValue, MethodValue)):
            definition = callee.definition if isinstance(callee, FunctionValue) else callee.member
            return self._return_value(definition, depth)
        if isinstance(callee, ExternalValue):
            return self._builtin_call(scope, callee.dotted, call, depth)
        return UnknownValue("call-result")

    def _return_value(self, definition: Definition, depth: int) -> Value:
        """함수 반환 주석을 인스턴스 값으로 푼다.

        Args:
            definition: 함수 정의.
            depth: 재귀 깊이.

        Returns:
            값.
        """
        node = definition.node
        returns = node.returns if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
        if returns is not None and definition.parent is not None:
            resolved = self.annotation_value(definition.parent, returns, depth)
            if isinstance(resolved, InstanceValue):
                return resolved
        return UnknownValue("call-result")

    def _builtin_call(self, scope: Definition, dotted: str, call: ast.Call, depth: int) -> Value:
        """값을 아는 내장 호출(`super()`, `type(x)`, `getattr(x, "리터럴")`)을 푼다.

        Args:
            scope: 범위.
            dotted: 외부 점 경로.
            call: 호출 식.
            depth: 재귀 깊이.

        Returns:
            값, 그 밖의 외부 호출은 외부 결과.
        """
        if dotted == "builtins.super":
            return self._super_value(scope)
        if dotted == "builtins.type" and len(call.args) == 1 and not call.keywords:
            inner = self.value(scope, call.args[0], depth + 1)
            return ClassValue(inner.definition, inner.exact) if isinstance(inner, InstanceValue) else ExternalResult()
        if dotted == "builtins.getattr" and len(call.args) == 2:
            name = call.args[1]
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                base = self.value(scope, call.args[0], depth + 1)
                return self.member_value(scope, base, name.value, depth + 1)
            return UnknownValue("getattr")
        return ExternalResult()

    def _super_value(self, scope: Definition) -> Value:
        """인자 없는 `super()`를 감싼 메서드의 클래스로 푼다.

        Args:
            scope: 호출을 담은 범위.

        Returns:
            super 값 또는 모름.
        """
        current: Definition | None = scope
        while current is not None and current.kind != "method":
            current = current.parent
        if current is None or current.parent is None:
            return UnknownValue("dynamic-super")
        return SuperValue(current.parent, current.parent)

    def bindings(self, scope: Definition) -> ScopeBindings:
        """함수·클래스 범위의 이름 묶음을 모은다(캐시).

        Args:
            scope: 함수 또는 클래스 정의.

        Returns:
            묶음.
        """
        cached = self._bindings.get(scope.id)
        if cached is None:
            cached = collect_bindings(self.index, scope)
            self._bindings[scope.id] = cached
        return cached


def _definition_value(definition: Definition) -> Value:
    """정의를 값으로 바꾼다.

    Args:
        definition: 정의.

    Returns:
        클래스 값 또는 함수 값.
    """
    return ClassValue(definition) if definition.kind == "class" else FunctionValue(definition)


def _node_name(node: ast.AST) -> str:
    """정의 노드의 이름을 돌려준다.

    Args:
        node: 정의 노드.

    Returns:
        이름(없으면 빈 문자열).
    """
    return getattr(node, "name", "")


#: 문자열 주석을 파싱하는 함수 타입이다(같은 상수 노드면 같은 파싱 결과를 돌려준다).
AnnotationParser = Callable[[ast.Constant], "ast.expr | None"]


def _annotation_target(annotation: ast.expr, parse: AnnotationParser) -> ast.expr | None:
    """주석에서 클래스 이름 식을 꺼낸다.

    Args:
        annotation: 주석 식.
        parse: 문자열 주석 파서(파싱한 노드를 살려 두어 메모 키가 재사용되지 않게 한다).

    Returns:
        이름·속성 식, 모르면 None.
    """
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        parsed = parse(annotation)
        return None if parsed is None else _annotation_target(parsed, parse)
    if isinstance(annotation, (ast.Name, ast.Attribute)):
        return annotation
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _optional_side(annotation.left, annotation.right, parse)
    if isinstance(annotation, ast.Subscript) and _subscript_name(annotation.value) in ("Optional", "Annotated"):
        inner = annotation.slice
        return _annotation_target(inner.elts[0] if isinstance(inner, ast.Tuple) and inner.elts else inner, parse)
    return None


def _optional_side(left: ast.expr, right: ast.expr, parse: AnnotationParser) -> ast.expr | None:
    """`X | None`에서 X를 꺼낸다.

    Args:
        left: 왼쪽.
        right: 오른쪽.
        parse: 문자열 주석 파서.

    Returns:
        X의 이름 식 또는 None.
    """
    if isinstance(right, ast.Constant) and right.value is None:
        return _annotation_target(left, parse)
    if isinstance(left, ast.Constant) and left.value is None:
        return _annotation_target(right, parse)
    return None


def _subscript_name(node: ast.expr) -> str:
    """첨자 식의 마지막 이름(`typing.Optional` → `Optional`)을 돌려준다.

    Args:
        node: 식.

    Returns:
        이름.
    """
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def collect_bindings(index: DefinitionIndex, scope: Definition) -> ScopeBindings:
    """범위 하나의 이름 묶음을 모은다. 중첩 정의 본문·람다·컴프리헨션 안은 들어가지 않는다.

    Args:
        index: 선언 색인.
        scope: 함수 또는 클래스 정의.

    Returns:
        묶음.
    """
    result = ScopeBindings({}, set(), set())
    node = scope.node
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        _collect_parameters(node.args, result)
    body = getattr(node, "body", [])
    collector = _BindingCollector(index, scope, result)
    for statement in body:
        collector.visit(statement)
    return result


def _collect_parameters(arguments: ast.arguments, result: ScopeBindings) -> None:
    """매개변수를 묶음으로 더한다.

    Args:
        arguments: 인자 정의.
        result: 채울 묶음.
    """
    position = 0
    for field_name in _PARAMETER_FIELDS:
        for argument in getattr(arguments, field_name):
            binding = Binding("param", argument.annotation, position if field_name != "kwonlyargs" else -2)
            result.names.setdefault(argument.arg, []).append(binding)
            position += 1
    for argument in (arguments.vararg, arguments.kwarg):
        if argument is not None:
            result.names.setdefault(argument.arg, []).append(Binding("param", None, -1))


class _BindingCollector(ast.NodeVisitor):
    """범위 본문에서 이름 묶음을 모으는 방문자."""

    def __init__(self, index: DefinitionIndex, scope: Definition, result: ScopeBindings) -> None:
        """방문자를 만든다.

        Args:
            index: 선언 색인.
            scope: 범위 정의.
            result: 채울 묶음.
        """
        self.index = index
        self.scope = scope
        self.result = result

    def _add(self, name: str, binding: Binding) -> None:
        """묶음을 더한다.

        Args:
            name: 이름.
            binding: 묶음.
        """
        self.result.names.setdefault(name, []).append(binding)

    def _definition(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        """중첩 정의 이름을 묶는다(본문은 들어가지 않는다). 장식한 정의도 모듈 수준과 같이 정의로 푼다
        (장식자 경유는 정의 → 장식자 간선이 맡는다).

        Args:
            node: 정의 노드.
        """
        definition = self.index.by_node.get(node)
        if definition is None:
            self._add(node.name, Binding("opaque", reason="excluded-source"))
        else:
            self._add(node.name, Binding("def", definition=definition))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """함수 정의."""
        self._definition(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """비동기 함수 정의."""
        self._definition(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """클래스 정의."""
        self._definition(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """람다 본문은 별도 범위다."""

    def visit_ListComp(self, node: ast.ListComp) -> None:
        """컴프리헨션은 별도 범위다(바다코끼리 대입만 새지만 모르는 값으로 둔다)."""
        self._walrus(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        """컴프리헨션."""
        self._walrus(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        """컴프리헨션."""
        self._walrus(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        """제너레이터 식."""
        self._walrus(node)

    def _walrus(self, node: ast.AST) -> None:
        """컴프리헨션 안 바다코끼리 대입 이름을 모르는 값으로 묶는다.

        Args:
            node: 컴프리헨션 노드.
        """
        for child in ast.walk(node):
            if isinstance(child, ast.NamedExpr) and isinstance(child.target, ast.Name):
                self._add(child.target.id, Binding("opaque", reason="comprehension-binding"))

    def visit_Global(self, node: ast.Global) -> None:
        """global 선언."""
        self.result.globals.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        """nonlocal 선언."""
        self.result.nonlocals.update(node.names)

    def visit_Assign(self, node: ast.Assign) -> None:
        """대입문."""
        for target in node.targets:
            self._target(target, node.value)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """주석 대입문(값이 없으면 묶지 않는다)."""
        if node.value is not None:
            self._target(node.target, node.value)
            self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        """누적 대입은 값을 모른다."""
        self._target(node.target, None)
        self.visit(node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        """바다코끼리 대입."""
        self._target(node.target, node.value)
        self.visit(node.value)

    def visit_For(self, node: ast.For) -> None:
        """반복 변수는 값을 모른다."""
        self._target(node.target, None, "loop-variable")
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        """비동기 반복 변수."""
        self._target(node.target, None, "loop-variable")
        self.generic_visit(node)

    def visit_withitem(self, node: ast.withitem) -> None:
        """`with … as x`는 `__enter__` 결과라 값을 모른다."""
        if node.optional_vars is not None:
            self._target(node.optional_vars, None, "context-manager")
        self.visit(node.context_expr)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        """예외 이름."""
        if node.name:
            self._add(node.name, Binding("opaque", reason="exception"))
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        """함수 안 import."""
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            module = alias.name if alias.asname else alias.name.split(".")[0]
            self._add(name, Binding("import", binding=ImportBinding(module, None)))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """함수 안 from import."""
        base = absolute_module(self.scope.module.module, node.module, node.level)
        for alias in node.names:
            if alias.name == "*":
                continue
            if base is None:
                self._add(alias.asname or alias.name, Binding("opaque", reason="unresolved-import"))
            else:
                self._add(alias.asname or alias.name, Binding("import", binding=ImportBinding(base, alias.name)))

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        """match 캡처."""
        if node.name:
            self._add(node.name, Binding("opaque", reason="match-capture"))
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        """match 별 캡처."""
        if node.name:
            self._add(node.name, Binding("opaque", reason="match-capture"))

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        """match 사전 나머지 캡처."""
        if node.rest:
            self._add(node.rest, Binding("opaque", reason="match-capture"))
        self.generic_visit(node)

    def _target(self, target: ast.expr, value: ast.expr | None, reason: str = "unpacked") -> None:
        """대입 대상을 묶는다. 이름 하나에 값이 있으면 값 묶음, 풀기·누적은 모르는 값이다.

        Args:
            target: 대입 대상.
            value: 대입 값.
            reason: 모르는 값의 이유.
        """
        if isinstance(target, ast.Name):
            binding = Binding("assign", value) if value is not None else Binding("opaque", reason=reason)
            self._add(target.id, binding)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._target(element, None, "unpacked")
        elif isinstance(target, ast.Starred):
            self._target(target.value, None, "unpacked")
