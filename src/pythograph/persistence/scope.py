"""함수 범위 도우미: 감싸는 선언의 심볼 id와 함수 안 지역 이름 묶음.

사실의 `symbol`은 사실을 담은 가장 안쪽 함수·클래스 선언이며 routes와 같은 id 규칙
(`<경로>#<어휘적 점 경로>`, `<locals>` 없음)을 쓴다. 람다·컴프리헨션은 투명하다. 모듈 수준 문장에는 symbol이
없다.

지역 이름 묶음은 흐름을 따지지 않는 근사다. 한 함수 안에서 이름이 서로 다른 값으로 두 번 이상 묶이면 모르는
값으로 본다(추측하지 않는다).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from pythograph.source.symbols import ModuleIndex, symbol_id

#: 함수 정의 노드 타입이다.
FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class Binding:
    """지역 이름 하나의 묶음.

    Attributes:
        kind: `assign`(대입 값), `with`(문맥 관리자 식), `for`(반복 대상 식), `param`(매개변수).
        value: 값 식(`param`이면 주석 식이나 None).
        is_self: 메서드의 첫 매개변수(`self`)인지.
    """

    kind: str
    value: ast.expr | None
    is_self: bool = False


class ModuleScopes:
    """모듈 하나의 감싸는 선언과 지역 이름 묶음을 계산한다."""

    def __init__(self, index: ModuleIndex) -> None:
        """도우미를 만든다.

        Args:
            index: 모듈 색인(부모 관계 포함).
        """
        self.index = index
        self._locals: dict[ast.AST, dict[str, list[Binding]]] = {}

    @property
    def path(self) -> str:
        """모듈 경로를 돌려준다.

        Returns:
            프로젝트 상대 경로.
        """
        return self.index.module.path

    def enclosing_definition(self, node: ast.AST) -> ast.AST | None:
        """노드를 담은 가장 안쪽 함수·클래스 정의를 돌려준다(노드 자신은 제외).

        Args:
            node: 구문 노드.

        Returns:
            정의 노드 또는 None(모듈 수준).
        """
        current = self.index.parents.get(node)
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return current
            current = self.index.parents.get(current)
        return None

    def enclosing_function(self, node: ast.AST) -> FunctionNode | None:
        """노드를 담은 가장 안쪽 함수를 돌려준다. 사이에 클래스가 있으면 None이다.

        Args:
            node: 구문 노드.

        Returns:
            함수 정의 또는 None.
        """
        definition = self.enclosing_definition(node)
        return definition if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)) else None

    def symbol_for(self, node: ast.AST) -> str | None:
        """사실을 담은 선언의 심볼 id를 돌려준다.

        Args:
            node: 사실 근거 노드.

        Returns:
            `<경로>#<점 경로>` 또는 None.
        """
        definition = self.enclosing_definition(node)
        if definition is None:
            return None
        return symbol_id(self.path, self.index.qualname(definition))

    def owner_class(self, function: FunctionNode) -> ast.ClassDef | None:
        """메서드를 정의한 클래스를 돌려준다.

        Args:
            function: 함수 정의.

        Returns:
            바로 바깥 클래스 또는 None.
        """
        parent = self.index.parents.get(function)
        return parent if isinstance(parent, ast.ClassDef) else None

    def bindings(self, function: FunctionNode, name: str) -> list[Binding]:
        """함수 안 지역 이름의 모든 묶음을 돌려준다.

        Args:
            function: 함수 정의.
            name: 이름.

        Returns:
            묶음 목록(묶이지 않으면 빈 목록).
        """
        if function not in self._locals:
            self._locals[function] = _collect_locals(function, self.owner_class(function) is not None)
        return self._locals[function].get(name, [])

    def local(self, function: FunctionNode, name: str) -> Binding | None:
        """함수 안 지역 이름의 유일한 묶음을 돌려준다.

        Args:
            function: 함수 정의.
            name: 이름.

        Returns:
            묶음, 없거나 여러 개면 None.
        """
        found = self.bindings(function, name)
        return found[0] if len(found) == 1 else None

    def binds_locally(self, function: FunctionNode, name: str) -> bool:
        """이름이 함수 안에서 묶이는지(유일하든 아니든) 돌려준다. 모듈 이름을 가리는지 판정용이다.

        Args:
            function: 함수 정의.
            name: 이름.

        Returns:
            묶이면 True.
        """
        return bool(self.bindings(function, name))


def _collect_locals(function: FunctionNode, is_method: bool) -> dict[str, list[Binding]]:
    """함수의 매개변수와 본문(중첩 함수·클래스 제외)의 이름 묶음을 모은다.

    Args:
        function: 함수 정의.
        is_method: 클래스 본문의 메서드인지(첫 매개변수가 `self`).

    Returns:
        이름 → 묶음 목록.
    """
    found: dict[str, list[Binding]] = {}
    arguments = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
    static = any(isinstance(item, ast.Name) and item.id == "staticmethod" for item in function.decorator_list)
    for position, argument in enumerate(arguments):
        is_self = is_method and not static and position == 0 and not _is_classmethod(function)
        _bind(found, argument.arg, Binding("param", argument.annotation, is_self))
    for node in _body_nodes(function):
        for name, binding in _node_bindings(node):
            _bind(found, name, binding)
    return found


def _is_classmethod(function: FunctionNode) -> bool:
    """`@classmethod` 장식자가 있는지 본다.

    Args:
        function: 함수 정의.

    Returns:
        있으면 True.
    """
    return any(isinstance(item, ast.Name) and item.id == "classmethod" for item in function.decorator_list)


def _bind(found: dict[str, list[Binding]], name: str, binding: Binding) -> None:
    """묶음을 더한다.

    Args:
        found: 이름 → 묶음 목록.
        name: 이름.
        binding: 새 묶음.
    """
    found.setdefault(name, []).append(binding)


def _body_nodes(function: FunctionNode) -> list[ast.AST]:
    """함수 본문의 노드를 모은다. 중첩 함수·클래스·람다 안은 제외한다.

    Args:
        function: 함수 정의.

    Returns:
        노드 목록.
    """
    nodes: list[ast.AST] = []
    pending: list[ast.AST] = list(function.body)
    while pending:
        node = pending.pop()
        nodes.append(node)
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                pending.append(child)
    return nodes


def _node_bindings(node: ast.AST) -> list[tuple[str, Binding]]:
    """노드 하나가 만드는 이름 묶음을 돌려준다.

    Args:
        node: 구문 노드.

    Returns:
        (이름, 묶음) 목록. 구조 분해·속성 대상은 모르는 값으로 묶는다.
    """
    if isinstance(node, ast.Assign):
        return [pair for target in node.targets for pair in _target_bindings(target, Binding("assign", node.value))]
    if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
        value = node.value if not isinstance(node, ast.AugAssign) else None
        return _target_bindings(node.target, Binding("assign", value))
    if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        return _target_bindings(node.target, Binding("for", node.iter))
    if isinstance(node, ast.withitem) and node.optional_vars is not None:
        return _target_bindings(node.optional_vars, Binding("with", node.context_expr))
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [((alias.asname or alias.name).split(".")[0], Binding("assign", None)) for alias in node.names]
    if isinstance(node, ast.ExceptHandler) and node.name:
        return [(node.name, Binding("assign", None))]
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return [(name, Binding("assign", None)) for name in node.names]
    return []


def _target_bindings(target: ast.expr, binding: Binding) -> list[tuple[str, Binding]]:
    """대입 대상의 이름 묶음을 만든다. 튜플 구조 분해 원소는 값을 모르는 묶음이다.

    Args:
        target: 대입 대상 식.
        binding: 단일 이름일 때의 묶음.

    Returns:
        (이름, 묶음) 목록.
    """
    if isinstance(target, ast.Name):
        return [(target.id, binding)]
    if isinstance(target, (ast.Tuple, ast.List)):
        unknown = Binding(binding.kind, None)
        return [pair for element in target.elts for pair in _target_bindings(element, unknown)]
    if isinstance(target, ast.Starred):
        return _target_bindings(target.value, Binding(binding.kind, None))
    return []
