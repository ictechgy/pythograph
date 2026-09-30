"""구문 트리의 URL 식을 조각(리터럴·값·query 꼬리)으로 바꾼다.

분석 대상 코드를 실행하지 않고 다음만 따라간다(나머지는 값 조각이다):

- 문자열 리터럴, f-string(형식 지정자 없는 보간), `+` 연결.
- 한 범위에서 한 번만 묶인 지역 이름(바깥 함수 포함), 모듈 수준 상수(`source.evaluate`), 다른 모듈의 상수.
- 클래스 속성과 인스턴스 필드(`self.BASE_URL`, `cls.BASE_URL`, `ApiClient.BASE_URL`, `self.base = "…"`): 그 이름을
  쓰는 모든 대입(수신자 클래스의 프로젝트 MRO와, 하위 클래스일 수 있는 수신자면 프로젝트 하위 클래스의 클래스 본문·
  메서드 안 `self.X = …`)이 같은 리터럴이면 그 값이다. 수신자 밖에서 그 이름을 쓰는 속성 대입(`api.base = …`,
  리터럴 `setattr`)이 그 클래스들이나 타입 모르는 객체에 있으면 값을 모른다. 계산된 이름의 `setattr`은 따라가지 않는다.

끝 지역 변수가 query 꼬리임을 증명하면(`compose.suffix`: 초기식의 비어 있지 않은 값이 모두 `?`로 시작하고 나머지
가지는 빈 문자열) query 꼬리 조각이다.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from pythograph.graph.index import Definition
from pythograph.graph.scope import Binding, Resolver
from pythograph.graph.values import ClassValue, InstanceValue, ModuleValue
from pythograph.routes.client.compose import Literal, Part, QueryTail, Value
from pythograph.routes.client.constants import ModuleConstants
from pythograph.routes.client.formats import Field, Piece, format_pieces, percent_pieces
from pythograph.source.symbols import ValueSymbol

#: 조각을 따라가는 최대 깊이다(순환·과도한 재귀 방지).
MAX_PART_DEPTH = 16

#: 값을 알 수 없는 대입(누적 대입·풀기·반복 변수)의 자리 표식이다. 문자열이 아니라 값 조각으로 읽힌다.
UNKNOWN_EXPR = ast.Constant(value=Ellipsis)


@dataclass(frozen=True)
class LocalBinding:
    """지역 이름 하나의 유일한 묶음.

    Attributes:
        kind: `assign`(값 식), `with`(문맥 관리자 식), `param`(매개변수), `other`(모르는 묶음).
        scope: 묶음이 있는 범위.
        value: 대입 값·문맥 관리자 식·매개변수 주석(없으면 None).
        name: 이름.
    """

    kind: str
    scope: Definition
    value: ast.expr | None
    name: str


@dataclass
class AttributeWrites:
    """프로젝트 전체의 바깥 속성 대입 색인(수신자가 `self`·`cls`가 아닌 대입과 리터럴 `setattr`).

    Attributes:
        untyped: 수신자 타입을 모르는 대입의 속성 이름. 어느 클래스의 속성이든 바꿀 수 있어 값을 모른다.
        typed: (프로젝트 클래스 id, 속성 이름) — 그 클래스(또는 하위 클래스) 객체에 쓴 대입.
        module: (모듈 경로, 이름) — 모듈 속성 대입(`constants.API_ROOT = …`). 그 이름은 상수가 아니다.
    """

    untyped: set[str] = field(default_factory=set)
    typed: set[tuple[str, str]] = field(default_factory=set)
    module: set[tuple[str, str]] = field(default_factory=set)

    def touches(self, resolver: Resolver, owner: Definition, name: str) -> bool:
        """클래스 속성을 바깥에서 쓸 수 있는지 본다.

        Args:
            resolver: 값 해석기.
            owner: 수신자의 정적 클래스.
            name: 속성 이름.

        Returns:
            바깥 대입이 있으면 True.
        """
        if name in self.untyped:
            return True
        return any((related.id, name) in self.typed for related in related_classes(resolver, owner, exact=False))


def collect_attribute_writes(resolver: Resolver) -> AttributeWrites:
    """프로젝트의 바깥 속성 대입을 모은다. 계산된 이름의 `setattr`은 모델링하지 않는다(문서의 한계).

    Args:
        resolver: 값 해석기.

    Returns:
        색인.
    """
    writes = AttributeWrites()
    for definition in sorted(resolver.index.definitions.values(), key=lambda item: item.id):
        for node in own_nodes(definition):
            target = _written_attribute(node)
            if target is not None:
                _record_write(resolver, definition, target[0], target[1], writes)
    return writes


def _written_attribute(node: ast.AST) -> tuple[ast.expr, str] | None:
    """노드가 바깥 속성 대입이면 (수신자 식, 속성 이름)을 돌려준다.

    Args:
        node: 구문 노드.

    Returns:
        (수신자, 이름) 또는 None.
    """
    if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return node.value, node.attr
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "setattr":
        name = node.args[1] if len(node.args) >= 2 else None
        if isinstance(name, ast.Constant) and isinstance(name.value, str):
            return node.args[0], name.value
    return None


def _record_write(
    resolver: Resolver, scope: Definition, receiver: ast.expr, name: str, writes: AttributeWrites
) -> None:
    """바깥 대입 하나를 기록한다. `self`·`cls` 대입은 클래스 대입 수집이 따로 본다.

    Args:
        resolver: 값 해석기.
        scope: 대입을 담은 범위.
        receiver: 수신자 식.
        name: 속성 이름.
        writes: 채울 색인.
    """
    if isinstance(receiver, ast.Name) and receiver.id in ("self", "cls"):
        return
    value = resolver.value(scope, receiver)
    if isinstance(value, (ClassValue, InstanceValue)):
        writes.typed.add((value.definition.id, name))
    elif isinstance(value, ModuleValue):
        writes.module.add((value.path, name))
    else:
        writes.untyped.add(name)


class PartBuilder:
    """URL 식을 조각으로 바꾸는 도우미."""

    def __init__(self, resolver: Resolver, writes: AttributeWrites) -> None:
        """도우미를 만든다.

        Args:
            resolver: 정적 값 해석기.
            writes: 속성 대입 색인.
        """
        self.resolver = resolver
        self.writes = writes
        self.constants = ModuleConstants(resolver.symbols, writes.module)
        self._with_items: dict[str, dict[str, list[ast.expr]]] = {}
        self._attribute_cache: dict[tuple[str, str, bool], str | None] = {}

    def parts(self, scope: Definition, expr: ast.expr, depth: int = 0) -> tuple[Part, ...]:
        """식을 조각으로 바꾼다.

        Args:
            scope: 식을 담은 범위.
            expr: 식.
            depth: 재귀 깊이.

        Returns:
            조각 튜플.
        """
        if depth > MAX_PART_DEPTH:
            return (Value(),)
        if isinstance(expr, ast.Constant):
            return (Literal(expr.value),) if isinstance(expr.value, str) else (Value(),)
        if isinstance(expr, ast.JoinedStr):
            return self._joined(scope, expr, depth)
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            return self.parts(scope, expr.left, depth + 1) + self.parts(scope, expr.right, depth + 1)
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Mod):
            return self._percent(scope, expr, depth)
        if isinstance(expr, ast.Call):
            return self._format(scope, expr, depth)
        if isinstance(expr, ast.Name):
            return self._name(scope, expr.id, depth)
        if isinstance(expr, ast.Attribute):
            text = self.attribute_string(scope, expr, depth)
            return (Literal(text),) if text is not None else (Value(),)
        return (Value(),)

    def string(self, scope: Definition, expr: ast.expr) -> str | None:
        """식이 리터럴 문자열로 확정되면 그 값을 돌려준다.

        Args:
            scope: 범위.
            expr: 식.

        Returns:
            문자열 또는 None.
        """
        parts = self.parts(scope, expr)
        if all(isinstance(part, Literal) for part in parts):
            return "".join(part.text for part in parts if isinstance(part, Literal))
        return None

    def _joined(self, scope: Definition, expr: ast.JoinedStr, depth: int) -> tuple[Part, ...]:
        """f-string을 조각으로 바꾼다. 형식 지정자·`!r`·`!a` 변환이 붙은 보간은 값이다.

        Args:
            scope: 범위.
            expr: f-string.
            depth: 재귀 깊이.

        Returns:
            조각 튜플.
        """
        result: list[Part] = []
        for value in expr.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                result.append(Literal(value.value))
            elif isinstance(value, ast.FormattedValue) and value.format_spec is None and value.conversion in (-1, 115):
                result.extend(self.parts(scope, value.value, depth + 1))
            else:
                result.append(Value())
        return tuple(result)

    def _name(self, scope: Definition, name: str, depth: int) -> tuple[Part, ...]:
        """이름을 조각으로 바꾼다.

        Args:
            scope: 범위.
            name: 이름.
            depth: 재귀 깊이.

        Returns:
            조각 튜플.
        """
        binding = self.local_binding(scope, name)
        if binding is None:
            return self.module_constant(scope.path, name, depth)
        if binding.kind == "param":
            return (Value(parameter=name),)
        if binding.kind != "assign" or binding.value is None:
            return (Value(),)
        if proves_query_tail(binding.value):
            return (QueryTail(),)
        return self.parts(binding.scope, binding.value, depth + 1)

    def local_binding(self, scope: Definition, name: str) -> LocalBinding | None:
        """이름의 지역 묶음을 어휘 범위대로 찾는다(클래스 범위는 건너뛴다).

        Args:
            scope: 이름을 쓰는 범위.
            name: 이름.

        Returns:
            함수 범위의 유일한 묶음(두 번 이상이면 `other`), 모듈 전역이면 None.
        """
        current: Definition | None = scope
        first = True
        while current is not None and current.kind != "module":
            if first or current.kind != "class":
                bindings = self.resolver.bindings(current)
                if name in bindings.globals:
                    return None
                if name in bindings.names and name not in bindings.nonlocals:
                    return self._single(current, name, bindings.names[name])
            first = False
            current = current.parent
        return None

    def _single(self, scope: Definition, name: str, bindings: list[Binding]) -> LocalBinding:
        """묶음 목록을 유일한 묶음으로 바꾼다.

        Args:
            scope: 범위.
            name: 이름.
            bindings: 묶음 목록.

        Returns:
            유일한 묶음(아니면 `other`).
        """
        if len(bindings) != 1:
            return LocalBinding("other", scope, None, name)
        binding = bindings[0]
        if binding.kind in ("assign", "param"):
            return LocalBinding(binding.kind, scope, binding.value, name)
        if binding.reason == "context-manager":
            items = self.with_items(scope).get(name, [])
            if len(items) == 1:
                return LocalBinding("with", scope, items[0], name)
        return LocalBinding("other", scope, None, name)

    def with_items(self, scope: Definition) -> dict[str, list[ast.expr]]:
        """범위의 `with … as 이름` 문맥 관리자 식을 모은다(캐시).

        Args:
            scope: 함수 범위.

        Returns:
            이름 → 문맥 관리자 식 목록.
        """
        cached = self._with_items.get(scope.id)
        if cached is None:
            cached = {}
            for node in own_nodes(scope):
                if isinstance(node, ast.withitem) and isinstance(node.optional_vars, ast.Name):
                    cached.setdefault(node.optional_vars.id, []).append(node.context_expr)
            self._with_items[scope.id] = cached
        return cached

    def attribute_string(self, scope: Definition, expr: ast.Attribute, depth: int = 0) -> str | None:
        """속성 접근의 문자열 값을 찾는다(모듈 상수, 클래스 속성·인스턴스 필드).

        Args:
            scope: 범위.
            expr: 속성 접근 식.
            depth: 재귀 깊이.

        Returns:
            문자열 또는 None.
        """
        receiver = self.resolver.value(scope, expr.value)
        if isinstance(receiver, (ClassValue, InstanceValue)):
            return self.class_attribute(receiver.definition, expr.attr, receiver.exact, depth)
        symbol = self.resolver.symbols.resolve_expr(scope.path, expr)
        if isinstance(symbol, ValueSymbol) and self.constants.proven(symbol.path, symbol.name):
            return _joined_literal(self._symbol_parts(symbol, depth))
        return None

    def module_constant(self, path: str, name: str, depth: int) -> tuple[Part, ...]:
        """모듈 전역 이름을 증명한 상수면 그 값 조각으로 바꾼다.

        쓰는 모듈에서 그 이름이 한 번만 묶이고(import 포함), 정의한 모듈에서도 한 번만 묶인 상수여야 한다.

        Args:
            path: 이름을 쓰는 모듈 경로.
            name: 이름.
            depth: 재귀 깊이.

        Returns:
            조각 튜플(상수가 아니면 값 조각).
        """
        symbol = self.resolver.symbols.resolve_name(path, name)
        if not isinstance(symbol, ValueSymbol) or not self.constants.proven(path, name):
            return (Value(),)
        if not self.constants.proven(symbol.path, symbol.name):
            return (Value(),)
        return self._symbol_parts(symbol, depth)

    def _symbol_parts(self, symbol: ValueSymbol, depth: int) -> tuple[Part, ...]:
        """모듈 변수의 값 식을 그 모듈 범위에서 조각으로 바꾼다.

        Args:
            symbol: 모듈 변수.
            depth: 재귀 깊이.

        Returns:
            조각 튜플.
        """
        module = self.resolver.index.definitions.get(f"{symbol.path}#<module>")
        return self.parts(module, symbol.node, depth + 1) if module is not None else (Value(),)

    def _percent(self, scope: Definition, expr: ast.BinOp, depth: int) -> tuple[Part, ...]:
        """`"…%s…" % 값` 서식을 조각으로 바꾼다.

        Args:
            scope: 범위.
            expr: `%` 식.
            depth: 재귀 깊이.

        Returns:
            조각 튜플(확정하지 못하면 값 조각 하나).
        """
        template = expr.left.value if isinstance(expr.left, ast.Constant) else None
        if not isinstance(template, str):
            return (Value(),)
        right = expr.right
        keyed = isinstance(right, ast.Dict)
        pieces = percent_pieces(template, keyed)
        if pieces is None:
            return (Value(),)
        if isinstance(right, ast.Dict):
            if any(key is None for key in right.keys):
                return (Value(),)
            named = {
                key.value: value
                for key, value in zip(right.keys, right.values, strict=True)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            return self._fill(scope, pieces, [], named, depth)
        values = list(right.elts) if isinstance(right, ast.Tuple) else [right]
        return self._fill(scope, pieces, values, {}, depth)

    def _format(self, scope: Definition, call: ast.Call, depth: int) -> tuple[Part, ...]:
        """`"…{}…".format(값)`을 조각으로 바꾼다. 그 밖의 호출은 값 조각이다.

        Args:
            scope: 범위.
            call: 호출식.
            depth: 재귀 깊이.

        Returns:
            조각 튜플.
        """
        func = call.func
        template = None
        if isinstance(func, ast.Attribute) and func.attr == "format" and isinstance(func.value, ast.Constant):
            template = func.value.value
        if not isinstance(template, str) or any(isinstance(item, ast.Starred) for item in call.args):
            return (Value(),)
        if any(keyword.arg is None for keyword in call.keywords):
            return (Value(),)
        pieces = format_pieces(template)
        if pieces is None:
            return (Value(),)
        named = {str(keyword.arg): keyword.value for keyword in call.keywords}
        return self._fill(scope, pieces, list(call.args), named, depth)

    def _fill(
        self,
        scope: Definition,
        pieces: list[Piece],
        positional: list[ast.expr],
        named: dict[str, ast.expr],
        depth: int,
    ) -> tuple[Part, ...]:
        """서식 조각의 값 자리를 인자 조각으로 채운다.

        값을 그대로 넣는 자리의 인자는 따라간 조각(상수면 리터럴), 형식 지정자가 있는 자리는 값 조각이다. 인자를
        찾지 못하면(서식 오류) 식 전체를 값 조각으로 본다.

        Args:
            scope: 범위.
            pieces: 서식 조각.
            positional: 위치 인자.
            named: 키워드 인자.
            depth: 재귀 깊이.

        Returns:
            조각 튜플.
        """
        result: list[Part] = []
        for piece in pieces:
            if isinstance(piece, str):
                result.append(Literal(piece))
                continue
            argument = _field_argument(piece, positional, named)
            if argument is None:
                return (Value(),)
            result.extend(self.parts(scope, argument, depth + 1) if piece.plain else self._opaque(scope, argument))
        return tuple(result)

    def _opaque(self, scope: Definition, argument: ast.expr) -> tuple[Part, ...]:
        """모양이 바뀔 수 있는 값 자리를 값 조각으로 만든다(매개변수면 이름을 남긴다).

        Args:
            scope: 범위.
            argument: 인자 식.

        Returns:
            값 조각 하나.
        """
        if isinstance(argument, ast.Name):
            binding = self.local_binding(scope, argument.id)
            if binding is not None and binding.kind == "param":
                return (Value(parameter=argument.id),)
        return (Value(),)

    def class_attribute(self, owner: Definition, name: str, exact: bool, depth: int = 0) -> str | None:
        """클래스 속성·인스턴스 필드가 모든 대입에서 같은 리터럴이면 그 값을 돌려준다(캐시).

        Args:
            owner: 수신자의 정적 클래스.
            name: 속성 이름.
            exact: 수신자 클래스가 정확한지(아니면 하위 클래스의 대입도 본다).
            depth: 재귀 깊이.

        Returns:
            문자열 또는 None.
        """
        key = (owner.id, name, exact)
        if key not in self._attribute_cache:
            self._attribute_cache[key] = None
            self._attribute_cache[key] = self._class_attribute(owner, name, exact, depth)
        return self._attribute_cache[key]

    def _class_attribute(self, owner: Definition, name: str, exact: bool, depth: int) -> str | None:
        """`class_attribute`의 계산이다.

        Args:
            owner: 수신자의 정적 클래스.
            name: 속성 이름.
            exact: 수신자 클래스가 정확한지.
            depth: 재귀 깊이.

        Returns:
            문자열 또는 None.
        """
        if self.writes.touches(self.resolver, owner, name):
            return None
        values: set[str] = set()
        assignments = attribute_assignments(self.resolver, owner, name, exact)
        if not assignments:
            return None
        for scope, value in assignments:
            parts = self.parts(scope, value, depth + 1)
            if not all(isinstance(part, Literal) for part in parts):
                return None
            values.add("".join(part.text for part in parts if isinstance(part, Literal)))
        return values.pop() if len(values) == 1 else None


def _joined_literal(parts: tuple[Part, ...]) -> str | None:
    """조각이 모두 리터럴이면 이어 붙인 문자열을 돌려준다.

    Args:
        parts: 조각.

    Returns:
        문자열 또는 None.
    """
    if all(isinstance(part, Literal) for part in parts):
        return "".join(part.text for part in parts if isinstance(part, Literal))
    return None


def _field_argument(field: Field, positional: list[ast.expr], named: dict[str, ast.expr]) -> ast.expr | None:
    """서식 값 자리에 들어갈 인자를 찾는다.

    Args:
        field: 값 자리.
        positional: 위치 인자.
        named: 키워드 인자.

    Returns:
        인자 식 또는 None.
    """
    if isinstance(field.key, int):
        return positional[field.key] if field.key < len(positional) else None
    return named.get(field.key)


def related_classes(resolver: Resolver, owner: Definition, exact: bool) -> list[Definition]:
    """속성 값을 정할 수 있는 프로젝트 클래스: 수신자 MRO의 프로젝트 클래스와, 하위 클래스일 수 있으면 하위 클래스다.

    Args:
        resolver: 값 해석기.
        owner: 수신자의 정적 클래스.
        exact: 수신자 클래스가 정확한지.

    Returns:
        클래스 정의 목록(중복 없음).
    """
    found: dict[str, Definition] = {owner.id: owner}
    for entry in resolver.linearizer.mro(owner):
        definition = resolver.index.definitions.get(entry.key) if entry.kind == "project" else None
        if definition is not None:
            found.setdefault(definition.id, definition)
    if not exact:
        for subclass in resolver.linearizer.subclasses(owner):
            found.setdefault(subclass.id, subclass)
    return list(found.values())


def attribute_assignments(
    resolver: Resolver, owner: Definition, name: str, exact: bool
) -> list[tuple[Definition, ast.expr]]:
    """속성 이름의 모든 대입(클래스 본문과 메서드 안 `self.X = …`)을 모은다.

    Args:
        resolver: 값 해석기.
        owner: 수신자의 정적 클래스.
        name: 속성 이름.
        exact: 수신자 클래스가 정확한지.

    Returns:
        (값을 풀 범위, 값 식) 목록.
    """
    result: list[tuple[Definition, ast.expr]] = []
    for definition in related_classes(resolver, owner, exact):
        members = resolver.index.class_members(definition)
        if name in members.attributes:
            result.append((definition, members.attributes[name]))
        for method in members.methods.values():
            result.extend((method, value) for value in self_assignments(method, name))
    return result


def self_assignments(method: Definition, name: str) -> list[ast.expr]:
    """메서드 본문의 `self.<name> = 값` 대입 값을 모은다(첫 매개변수 기준, 주석 대입 포함).

    값을 알 수 없는 대입(누적 대입·풀기·반복 변수·`with … as self.x`)은 `UNKNOWN_EXPR`로 둔다. 빼면 남은 대입만으로
    값을 확정하는 거짓 증명이 되기 때문이다.

    Args:
        method: 메서드 정의.
        name: 속성 이름.

    Returns:
        값 식 목록.
    """
    node = method.node
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return []
    positional = node.args.posonlyargs + node.args.args
    if not positional:
        return []
    receiver = positional[0].arg
    values: list[ast.expr] = []
    for child in own_nodes(method):
        values.extend(_assigned_values(child, receiver, name))
    return values


def _assigned_values(node: ast.AST, receiver: str, name: str) -> list[ast.expr]:
    """노드가 `receiver.name`에 대입하면 값 식을 돌려준다.

    Args:
        node: 구문 노드.
        receiver: 첫 매개변수 이름.
        name: 속성 이름.

    Returns:
        값 식 목록(값을 모르는 대입은 `UNKNOWN_EXPR`).
    """

    def targets_attribute(target: ast.expr) -> bool:
        return (
            isinstance(target, ast.Attribute)
            and target.attr == name
            and isinstance(target.value, ast.Name)
            and target.value.id == receiver
        )

    if isinstance(node, ast.Assign):
        values: list[ast.expr] = []
        for target in node.targets:
            if targets_attribute(target):
                values.append(node.value)
            elif isinstance(target, (ast.Tuple, ast.List)) and any(targets_attribute(item) for item in target.elts):
                values.append(UNKNOWN_EXPR)
        return values
    if isinstance(node, ast.AnnAssign) and targets_attribute(node.target):
        return [node.value] if node.value is not None else []
    if isinstance(node, (ast.AugAssign, ast.For, ast.AsyncFor)) and targets_attribute(node.target):
        return [UNKNOWN_EXPR]
    if isinstance(node, ast.withitem) and node.optional_vars is not None and targets_attribute(node.optional_vars):
        return [UNKNOWN_EXPR]
    return []


def own_nodes(definition: Definition) -> list[ast.AST]:
    """정의의 자기 범위 노드를 모두 돌려준다(중첩 정의 본문 제외, 장식자·기본값 포함).

    Args:
        definition: 정의.

    Returns:
        노드 목록(소스 순서).
    """
    result: list[ast.AST] = []
    body = getattr(definition.node, "body", [])
    pending: list[ast.AST] = list(reversed(body)) if isinstance(body, list) else []
    while pending:
        current = pending.pop()
        result.append(current)
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            inner: list[ast.AST] = list(current.decorator_list)
            if not isinstance(current, ast.ClassDef):
                inner.extend(current.args.defaults)
                inner.extend(item for item in current.args.kw_defaults if item is not None)
            else:
                inner.extend(current.bases)
                inner.extend(keyword.value for keyword in current.keywords)
            pending.extend(reversed(inner))
            continue
        pending.extend(reversed(list(ast.iter_child_nodes(current))))
    return result


def proves_query_tail(value: ast.expr) -> bool:
    """지역 변수 초기식이 query 꼬리임을 증명하는지 판정한다(`compose.suffix`).

    비어 있지 않은 모든 값이 `?` 리터럴로 시작하고(`"?" + urlencode(q)`, `f"?page={n}"`) 나머지 가지가 빈 문자열
    (조건식 `a if c else ""`)이어야 한다.

    Args:
        value: 초기식.

    Returns:
        증명하면 True.
    """
    branches = _branches(value)
    non_empty = [branch for branch in branches if not _is_empty_string(branch)]
    return bool(non_empty) and all(_starts_with_question(branch) for branch in non_empty)


def _branches(value: ast.expr) -> list[ast.expr]:
    """조건식 가지를 펼친다.

    Args:
        value: 식.

    Returns:
        가지 목록.
    """
    if isinstance(value, ast.IfExp):
        return _branches(value.body) + _branches(value.orelse)
    return [value]


def _is_empty_string(value: ast.expr) -> bool:
    """빈 문자열 리터럴인지 본다.

    Args:
        value: 식.

    Returns:
        빈 문자열이면 True.
    """
    return isinstance(value, ast.Constant) and value.value == ""


def _starts_with_question(value: ast.expr) -> bool:
    """식이 `?` 리터럴로 시작하는지 본다.

    Args:
        value: 식.

    Returns:
        `?`로 시작하면 True.
    """
    if isinstance(value, ast.Constant):
        return isinstance(value.value, str) and value.value.startswith("?")
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
        return _starts_with_question(value.left)
    if isinstance(value, ast.JoinedStr) and value.values:
        return _starts_with_question(value.values[0]) if isinstance(value.values[0], ast.Constant) else False
    return False
