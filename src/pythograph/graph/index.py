"""그래프 정점이 될 선언 색인: 모듈·함수·메서드·클래스·중첩 정의와 클래스 멤버.

id는 routes·schema와 같은 규칙(`<경로>#<어휘적 점 경로>`, `<locals>` 없음)이다. 같은 점 경로가 두 번 정의되면
(조건부 정의, property setter) 한 정점으로 합친다. 람다·컴프리헨션은 정점이 아니다(감싼 선언의 일부다).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from pythograph.source.project import Project, SourceModule, is_test_path
from pythograph.source.symbols import ModuleIndex, SymbolTable, symbol_id

#: 함수 정의 노드 타입이다.
FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef

#: 정의 노드 타입이다.
DefinitionNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef

#: 모듈 정점의 점 경로 자리 이름이다(tsograph와 같은 표기).
MODULE_NAME = "<module>"


@dataclass(eq=False)
class Definition:
    """정점 하나가 되는 선언(모듈 포함).

    Attributes:
        id: 심볼 id.
        kind: `module`·`function`·`method`·`class`.
        path: 프로젝트 상대 경로.
        module: 모듈 색인.
        node: 정의 노드(모듈이면 `ast.Module`).
        parent: 바깥 선언(모듈 수준 정의는 모듈 정의).
    """

    id: str
    kind: str
    path: str
    module: ModuleIndex
    node: ast.AST
    parent: Definition | None


@dataclass
class ClassMembers:
    """프로젝트 클래스 본문의 멤버.

    Attributes:
        methods: 이름 → 메서드 정의(같은 이름이면 마지막).
        classes: 이름 → 중첩 클래스 정의.
        attributes: 이름 → 클래스 수준 대입 값 식.
        decorators: 메서드 이름 → 장식자 이름 집합(`property`·`classmethod`·`staticmethod` 등).
    """

    methods: dict[str, Definition] = field(default_factory=dict)
    classes: dict[str, Definition] = field(default_factory=dict)
    attributes: dict[str, ast.expr] = field(default_factory=dict)
    decorators: dict[str, set[str]] = field(default_factory=dict)


#: 메서드 종류를 정하는 장식자 이름이다(마지막 이름 기준).
_METHOD_DECORATORS = frozenset({"property", "cached_property", "classmethod", "staticmethod", "setter", "getter"})


class DefinitionIndex:
    """프로젝트 전체의 선언 색인."""

    def __init__(self, project: Project, symbols: SymbolTable, include_tests: bool) -> None:
        """색인을 만든다.

        Args:
            project: 분석 대상 프로젝트.
            symbols: 모듈 색인·이름 해석기.
            include_tests: 테스트 소스를 포함할지.
        """
        self.project = project
        self.symbols = symbols
        self.definitions: dict[str, Definition] = {}
        self.by_node: dict[ast.AST, Definition] = {}
        self.members: dict[str, ClassMembers] = {}
        self.modules: list[Definition] = []
        self.unparsed = 0
        for path in project.python_files():
            if include_tests or not is_test_path(path):
                self._add_module(path)

    def _add_module(self, path: str) -> None:
        """모듈 하나의 선언을 모은다.

        Args:
            path: 프로젝트 상대 경로.
        """
        index = self.symbols.index(path)
        if index is None:
            self.unparsed += 1
            return
        module = Definition(symbol_id(path, MODULE_NAME), "module", path, index, index.module.tree, None)
        self.definitions[module.id] = module
        self.by_node[index.module.tree] = module
        self.modules.append(module)
        self._collect(module, index.module.tree.body)

    def _collect(self, parent: Definition, statements: list[ast.stmt]) -> None:
        """문장 목록에서 정의를 찾아 재귀로 모은다(조건문·반복문 안 포함).

        Args:
            parent: 바깥 선언.
            statements: 문장 목록.
        """
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                definition = self._define(parent, statement)
                self._collect(definition, statement.body)
            else:
                for block in _nested_blocks(statement):
                    self._collect(parent, block)

    def _define(self, parent: Definition, node: DefinitionNode) -> Definition:
        """정의 하나를 등록한다. 같은 id가 이미 있으면 그 정의에 노드를 더한다.

        Args:
            parent: 바깥 선언.
            node: 정의 노드.

        Returns:
            등록한 정의.
        """
        qualname = parent.module.qualname(node)
        identifier = symbol_id(parent.path, qualname)
        kind = _definition_kind(parent, node)
        definition = self.definitions.get(identifier)
        if definition is None:
            definition = Definition(identifier, kind, parent.path, parent.module, node, parent)
            self.definitions[identifier] = definition
        self.by_node[node] = definition
        if parent.kind == "class":
            self._add_member(parent, node, definition)
        return definition

    def _add_member(self, owner: Definition, node: DefinitionNode, definition: Definition) -> None:
        """클래스 멤버 표에 메서드·중첩 클래스를 더한다.

        Args:
            owner: 클래스 정의.
            node: 멤버 정의 노드.
            definition: 멤버 정의.
        """
        members = self.members.setdefault(owner.id, ClassMembers())
        if isinstance(node, ast.ClassDef):
            members.classes[node.name] = definition
            return
        members.methods[node.name] = definition
        members.decorators.setdefault(node.name, set()).update(_decorator_names(node))

    def class_members(self, definition: Definition) -> ClassMembers:
        """클래스 멤버 표를 돌려준다(클래스 수준 대입 포함).

        Args:
            definition: 클래스 정의.

        Returns:
            멤버 표.
        """
        members = self.members.setdefault(definition.id, ClassMembers())
        if not members.attributes and isinstance(definition.node, ast.ClassDef):
            _collect_attributes(definition.node.body, members.attributes)
        return members

    def classes(self) -> list[Definition]:
        """모든 클래스 정의를 id 순으로 돌려준다.

        Returns:
            클래스 정의 목록.
        """
        return sorted((item for item in self.definitions.values() if item.kind == "class"), key=lambda item: item.id)

    def module_of(self, path: str) -> SourceModule | None:
        """경로의 파싱한 모듈을 돌려준다.

        Args:
            path: 프로젝트 상대 경로.

        Returns:
            모듈 또는 None.
        """
        index = self.symbols.index(path)
        return index.module if index is not None else None


def _definition_kind(parent: Definition, node: DefinitionNode) -> str:
    """정의 종류를 정한다.

    Args:
        parent: 바깥 선언.
        node: 정의 노드.

    Returns:
        `class`·`method`·`function`.
    """
    if isinstance(node, ast.ClassDef):
        return "class"
    return "method" if parent.kind == "class" else "function"


def _nested_blocks(statement: ast.stmt) -> list[list[ast.stmt]]:
    """복합 문장의 하위 블록을 돌려준다(정의 본문은 제외).

    Args:
        statement: 문장.

    Returns:
        블록 목록.
    """
    blocks: list[list[ast.stmt]] = []
    for name in ("body", "orelse", "finalbody"):
        value = getattr(statement, name, None)
        if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
            blocks.append(value)
    for handler in getattr(statement, "handlers", []) or []:
        blocks.append(handler.body)
    for case in getattr(statement, "cases", []) or []:
        blocks.append(case.body)
    return blocks


def _decorator_names(node: FunctionNode) -> set[str]:
    """메서드 종류를 정하는 장식자 이름을 모은다(`@x.setter`는 `setter`).

    Args:
        node: 함수 정의.

    Returns:
        이름 집합.
    """
    names: set[str] = set()
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = target.attr if isinstance(target, ast.Attribute) else target.id if isinstance(target, ast.Name) else ""
        if name in _METHOD_DECORATORS:
            names.add(name)
    return names


def _collect_attributes(statements: list[ast.stmt], attributes: dict[str, ast.expr]) -> None:
    """클래스 본문(조건문 포함)의 단순 이름 대입 값을 모은다. 나중 대입이 이긴다.

    Args:
        statements: 클래스 본문 문장.
        attributes: 채울 사전.
    """
    for statement in statements:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    attributes[target.id] = statement.value
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.value:
            attributes[statement.target.id] = statement.value
        elif not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for block in _nested_blocks(statement):
                _collect_attributes(block, attributes)
