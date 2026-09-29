"""모듈 색인과 정적 이름 해석, pythograph 심볼 id 규칙.

심볼 id(`symbol.usr`)는 `<프로젝트 상대 POSIX 경로>#<어휘적 점 경로>`다. 점 경로는 바깥 선언부터
함수·클래스 이름을 점으로 잇고, 파이썬 `__qualname__`의 `<locals>`는 넣지 않는다
(`blog/__init__.py#create_app.index`, `catalog/views.py#ItemView.get`). 같은 입력이면 항상 같은 문자열이며,
다음 단계의 호출 그래프(`pythograph graph`)가 같은 규칙으로 정점 id를 만든다.

이름 해석은 import 문과 모듈 수준 정의만 따라간다(정적, 실행 없음). 프로젝트 밖 모듈은
점 경로 문자열(`ExternalSymbol`)로 남겨 알려진 프레임워크 API 표와 맞춘다.

모듈 안에서 찾지 못한 이름은 프로젝트 모듈의 `from x import *`를 뒤에서부터(뒤의 `*`가 앞의 것을 덮는다) 따라간다.
대상 모듈의 리터럴 `__all__`, 없으면 밑줄로 시작하지 않는 이름만 내보낸 것으로 본다(파이썬 `import *` 규칙). 외부
모듈이나 `__all__`을 확정하지 못한 모듈의 `*`를 만나면 그 모듈이 이름을 가릴 수 있어 모른다(None).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from pythograph.source.project import Project, SourceModule

#: 이름 해석에서 따라가는 최대 import 홉 수다(순환 방지).
MAX_RESOLVE_HOPS = 16

#: 함수 정의 노드 타입이다.
FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef

#: `__all__`을 정적으로 확정하지 못했다는 표식이다.
UNKNOWN_EXPORTS = frozenset({"<unknown-exports>"})


def symbol_id(path: str, qualname: str) -> str:
    """pythograph 심볼 id를 만든다.

    Args:
        path: 프로젝트 상대 POSIX 경로.
        qualname: 어휘적 점 경로.

    Returns:
        `<path>#<qualname>`.
    """
    return f"{path}#{qualname}"


@dataclass(frozen=True)
class ImportBinding:
    """import로 묶인 이름. `module`은 절대 모듈 이름, `attribute`는 `from … import` 대상이다."""

    module: str
    attribute: str | None


@dataclass(frozen=True)
class ProjectSymbol:
    """프로젝트 안의 함수·클래스 정의."""

    path: str
    qualname: str
    node: ast.AST

    @property
    def id(self) -> str:
        """심볼 id를 돌려준다.

        Returns:
            `<path>#<qualname>`.
        """
        return symbol_id(self.path, self.qualname)


@dataclass(frozen=True)
class ExternalSymbol:
    """프로젝트 밖의 이름(점 경로). 모듈일 수도 있고 모듈 안의 이름일 수도 있다."""

    dotted: str


@dataclass(frozen=True)
class ModuleSymbol:
    """프로젝트 안의 모듈."""

    path: str
    dotted: str


@dataclass(frozen=True)
class ValueSymbol:
    """모듈 수준 변수. `node`는 마지막 무조건 대입의 값 식이다."""

    path: str
    name: str
    node: ast.expr


@dataclass(frozen=True)
class MemberSymbol:
    """프로젝트 클래스의 속성 접근(`ItemView.as_view`)."""

    owner: ProjectSymbol
    name: str


#: 해석 결과 타입이다.
Symbol = ProjectSymbol | ExternalSymbol | ModuleSymbol | ValueSymbol | MemberSymbol


@dataclass
class ModuleIndex:
    """모듈 하나의 모듈 수준 색인.

    Attributes:
        module: 파싱한 모듈.
        definitions: 모듈 수준 함수·클래스 정의.
        imports: 모듈 수준(조건문 안 포함) import 묶음.
        values: 모듈 수준 단순 이름 대입의 마지막 값 식(조건문 밖).
        parents: 노드 → 부모 노드.
    """

    module: SourceModule
    definitions: dict[str, ast.AST] = field(default_factory=dict)
    imports: dict[str, ImportBinding] = field(default_factory=dict)
    values: dict[str, ast.expr] = field(default_factory=dict)
    parents: dict[ast.AST, ast.AST] = field(default_factory=dict)

    def qualname(self, node: ast.AST) -> str:
        """정의 노드의 어휘적 점 경로를 만든다.

        Args:
            node: 함수·클래스 정의 노드.

        Returns:
            바깥 선언부터 이은 점 경로.
        """
        names: list[str] = []
        current: ast.AST | None = node
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(current.name)
            current = self.parents.get(current)
        return ".".join(reversed(names))


class SymbolTable:
    """프로젝트 전체의 모듈 색인과 이름 해석기."""

    def __init__(self, project: Project) -> None:
        """해석기를 만든다.

        Args:
            project: 분석 대상 프로젝트.
        """
        self.project = project
        self._indexes: dict[str, ModuleIndex | None] = {}
        self._star_cache: dict[tuple[str, str], tuple[Symbol | None, bool]] = {}
        self._exports: dict[str, frozenset[str] | None] = {}

    def index(self, path: str) -> ModuleIndex | None:
        """모듈 색인을 만든다(캐시). 파싱하지 못하면 None이다.

        Args:
            path: 프로젝트 상대 경로.

        Returns:
            색인 또는 None.
        """
        if path not in self._indexes:
            module = self.project.module(path)
            self._indexes[path] = _build_index(module) if isinstance(module, SourceModule) else None
        return self._indexes[path]

    def resolve_name(self, path: str, name: str, hops: int = 0) -> Symbol | None:
        """모듈 수준 이름을 해석한다.

        Args:
            path: 이름을 쓰는 모듈의 경로.
            name: 이름.
            hops: 지금까지 따라간 홉 수.

        Returns:
            해석 결과, 모르면 None.
        """
        index = self.index(path)
        if index is None or hops > MAX_RESOLVE_HOPS:
            return None
        if name in index.definitions:
            node = index.definitions[name]
            return ProjectSymbol(path, index.qualname(node), node)
        if name in index.imports:
            return self.resolve_binding(index.imports[name], hops + 1)
        if name in index.values:
            return ValueSymbol(path, name, index.values[name])
        return self._star_lookup(index, name, hops)[0]

    def star_blocked(self, path: str, name: str) -> bool:
        """모듈의 `*` import가 이름을 가릴 수 있어 모르는지(외부·`__all__` 미확정 모듈의 `*`) 돌려준다.

        Args:
            path: 이름을 쓰는 모듈의 경로.
            name: 이름.

        Returns:
            가릴 수 있으면 True.
        """
        index = self.index(path)
        return index is not None and self._star_lookup(index, name, 0)[1]

    def _star_lookup(self, index: ModuleIndex, name: str, hops: int) -> tuple[Symbol | None, bool]:
        """`from x import *`로 들어온 이름을 푼다(캐시, 순환이면 모른다).

        뒤의 `*`가 앞의 것을 덮으므로 마지막부터 본다. 외부 모듈이나 `__all__`을 확정하지 못한 모듈을 만나면(대상
        모듈 안의 `*`가 그런 경우 포함) 그 모듈이 이름을 가릴 수 있어 멈추고 가림으로 표시한다.

        Args:
            index: 이름을 쓰는 모듈의 색인.
            name: 이름.
            hops: 지금까지 따라간 홉 수.

        Returns:
            (해석 결과 또는 None, 가림 여부).
        """
        key = (index.module.path, name)
        if key in self._star_cache:
            return self._star_cache[key]
        self._star_cache[key] = (None, False)
        result: tuple[Symbol | None, bool] = (None, False)
        for module in reversed(star_import_modules(index)):
            module_path = self.project.resolve_module(module)
            target = self.index(module_path) if module_path is not None else None
            if target is None or module_path is None or self._module_exports(target) is UNKNOWN_EXPORTS:
                result = (None, True)
                break
            if not is_exported(name, self._module_exports(target)):
                continue
            found = self.resolve_name(module_path, name, hops + 1)
            if found is not None or self._star_lookup(target, name, hops + 1)[1]:
                result = (found, found is None)
                break
        self._star_cache[key] = result
        return result

    def _module_exports(self, index: ModuleIndex) -> frozenset[str] | None:
        """`star_exports`를 모듈별로 캐시한다.

        Args:
            index: 모듈 색인.

        Returns:
            `star_exports` 결과.
        """
        path = index.module.path
        if path not in self._exports:
            self._exports[path] = star_exports(index)
        return self._exports[path]

    def resolve_binding(self, binding: ImportBinding, hops: int = 0) -> Symbol | None:
        """import 묶음을 해석한다.

        Args:
            binding: import 묶음.
            hops: 지금까지 따라간 홉 수.

        Returns:
            해석 결과, 모르면 None.
        """
        module_path = self.project.resolve_module(binding.module)
        if binding.attribute is None:
            return ModuleSymbol(module_path, binding.module) if module_path else ExternalSymbol(binding.module)
        if module_path is None:
            return ExternalSymbol(f"{binding.module}.{binding.attribute}")
        found = self.resolve_name(module_path, binding.attribute, hops + 1)
        if found is not None:
            return found
        submodule = f"{binding.module}.{binding.attribute}"
        submodule_path = self.project.resolve_module(submodule)
        return ModuleSymbol(submodule_path, submodule) if submodule_path else None

    def resolve_expr(self, path: str, expr: ast.expr, hops: int = 0) -> Symbol | None:
        """이름·속성 접근 식을 해석한다.

        Args:
            path: 식이 있는 모듈의 경로.
            expr: `Name` 또는 `Attribute` 식.
            hops: 지금까지 따라간 홉 수.

        Returns:
            해석 결과, 모르면 None.
        """
        if hops > MAX_RESOLVE_HOPS:
            return None
        if isinstance(expr, ast.Name):
            return self.resolve_name(path, expr.id, hops)
        if isinstance(expr, ast.Attribute):
            base = self.resolve_expr(path, expr.value, hops + 1)
            return self.member(base, expr.attr, hops + 1)
        return None

    def member(self, base: Symbol | None, name: str, hops: int = 0) -> Symbol | None:
        """해석한 값의 속성을 해석한다.

        Args:
            base: 해석한 값.
            name: 속성 이름.
            hops: 지금까지 따라간 홉 수.

        Returns:
            해석 결과, 모르면 None.
        """
        if isinstance(base, ExternalSymbol):
            return ExternalSymbol(f"{base.dotted}.{name}")
        if isinstance(base, ModuleSymbol):
            found = self.resolve_name(base.path, name, hops + 1)
            if found is not None:
                return found
            submodule = f"{base.dotted}.{name}"
            submodule_path = self.project.resolve_module(submodule)
            return ModuleSymbol(submodule_path, submodule) if submodule_path else None
        if isinstance(base, ProjectSymbol) and isinstance(base.node, ast.ClassDef):
            return MemberSymbol(base, name)
        return None


def _build_index(module: SourceModule) -> ModuleIndex:
    """모듈 수준 정의·import·대입과 부모 관계를 모은다.

    Args:
        module: 파싱한 모듈.

    Returns:
        색인.
    """
    index = ModuleIndex(module=module)
    for parent in ast.walk(module.tree):
        for child in ast.iter_child_nodes(parent):
            index.parents[child] = parent
    _collect_statements(index, module.tree.body, conditional=False)
    return index


def _collect_statements(index: ModuleIndex, statements: list[ast.stmt], conditional: bool) -> None:
    """모듈 수준 문장에서 정의·import·대입을 모은다. 조건문 안의 import도 모은다.

    Args:
        index: 채울 색인.
        statements: 문장 목록.
        conditional: 조건문 안인지(대입 값은 조건문 밖만 신뢰한다).
    """
    for statement in statements:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            index.definitions[statement.name] = statement
        elif isinstance(statement, (ast.Import, ast.ImportFrom)):
            _collect_import(index, statement)
        elif isinstance(statement, ast.Assign) and not conditional:
            _collect_assignment(index, statement)
        elif isinstance(statement, ast.AnnAssign) and not conditional and statement.value is not None:
            if isinstance(statement.target, ast.Name):
                index.values[statement.target.id] = statement.value
        elif isinstance(statement, (ast.If, ast.Try)):
            for block in _blocks(statement):
                _collect_statements(index, block, conditional=True)


def _blocks(statement: ast.If | ast.Try) -> list[list[ast.stmt]]:
    """조건문·try 문의 블록 목록을 돌려준다.

    Args:
        statement: `if` 또는 `try` 문.

    Returns:
        블록 목록.
    """
    if isinstance(statement, ast.If):
        return [statement.body, statement.orelse]
    return [statement.body, *[handler.body for handler in statement.handlers], statement.orelse, statement.finalbody]


def _collect_assignment(index: ModuleIndex, statement: ast.Assign) -> None:
    """단순 이름 대입(`x = …`, `x = y = …`)의 값을 기록한다. 이전 값을 덮어쓴다.

    Args:
        index: 채울 색인.
        statement: 대입문.
    """
    for target in statement.targets:
        if isinstance(target, ast.Name):
            index.values[target.id] = statement.value
            index.definitions.pop(target.id, None)
            index.imports.pop(target.id, None)


def _collect_import(index: ModuleIndex, statement: ast.Import | ast.ImportFrom) -> None:
    """import 문을 묶음으로 기록한다. 상대 import는 모듈 이름 기준 절대 이름으로 푼다.

    Args:
        index: 채울 색인.
        statement: import 문.
    """
    if isinstance(statement, ast.Import):
        for alias in statement.names:
            if alias.asname is not None:
                index.imports[alias.asname] = ImportBinding(alias.name, None)
            else:
                top = alias.name.split(".")[0]
                index.imports[top] = ImportBinding(top, None)
        return
    base = absolute_module(index.module, statement.module, statement.level)
    if base is None:
        return
    for alias in statement.names:
        if alias.name == "*":
            continue
        index.imports[alias.asname or alias.name] = ImportBinding(base, alias.name)


def absolute_module(module: SourceModule, name: str | None, level: int) -> str | None:
    """상대 import의 기준 모듈 이름을 절대 이름으로 푼다.

    Args:
        module: import가 있는 모듈.
        name: `from` 뒤 모듈 이름(없을 수 있음).
        level: 앞 점 개수.

    Returns:
        절대 모듈 이름, 풀 수 없으면 None.
    """
    if level == 0:
        return name
    package_parts = module.name.split(".") if module.name else []
    if not module.is_package:
        package_parts = package_parts[:-1]
    if level - 1 > len(package_parts):
        return None
    base_parts = package_parts[: len(package_parts) - (level - 1)]
    if name:
        base_parts = [*base_parts, *name.split(".")]
    return ".".join(base_parts) if base_parts else None


def external_name(symbol: Symbol | None) -> str | None:
    """외부 이름이면 점 경로를 돌려준다.

    Args:
        symbol: 해석 결과.

    Returns:
        외부 점 경로 또는 None.
    """
    return symbol.dotted if isinstance(symbol, ExternalSymbol) else None


def is_external(symbol: Symbol | None, *candidates: str) -> bool:
    """해석 결과가 후보 외부 이름 중 하나인지 확인한다.

    Args:
        symbol: 해석 결과.
        candidates: 점 경로 후보(재수출 표기 포함).

    Returns:
        일치하면 True.
    """
    return external_name(symbol) in candidates


def star_exports(index: ModuleIndex) -> frozenset[str] | None:
    """`from 모듈 import *`가 내보내는 이름 집합을 정한다.

    모듈 수준 무조건 대입 `__all__ = [...]`(리터럴 문자열 목록·튜플, 주석 대입 포함)이면 그 집합, `__all__`이 없으면
    None(밑줄로 시작하지 않는 이름 모두), 그 밖(리터럴이 아님, `+=`, 조건문 안 대입, 모듈 수준 메서드 호출
    `__all__.extend(...)`)은 `UNKNOWN_EXPORTS`다. 함수·클래스 본문의 같은 이름은 지역 이름이라 보지 않는다.

    Args:
        index: 모듈 색인.

    Returns:
        이름 집합, None, 또는 `UNKNOWN_EXPORTS`.
    """
    exports: frozenset[str] | None = None
    for node in _module_level_nodes(index.module.tree):
        if isinstance(node, ast.Name) and node.id == "__all__" and not isinstance(node.ctx, ast.Load):
            parent = index.parents.get(node)
            if not isinstance(parent, (ast.Assign, ast.AnnAssign)) or parent.value is None:
                return UNKNOWN_EXPORTS
            literal = _literal_names(parent.value)
            if literal is None or index.parents.get(parent) is not index.module.tree:
                return UNKNOWN_EXPORTS
            exports = literal
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "__all__"
        ):
            return UNKNOWN_EXPORTS
    return exports


def _module_level_nodes(tree: ast.Module) -> list[ast.AST]:
    """함수·클래스·람다 본문을 빼고 모듈 수준 노드를 모은다.

    Args:
        tree: 모듈 구문 트리.

    Returns:
        노드 목록.
    """
    nodes: list[ast.AST] = []
    pending: list[ast.AST] = [tree]
    while pending:
        node = pending.pop()
        nodes.append(node)
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                pending.append(child)
    return nodes


def _literal_names(node: ast.expr) -> frozenset[str] | None:
    """리터럴 문자열 목록·튜플을 이름 집합으로 바꾼다.

    Args:
        node: 값 식.

    Returns:
        이름 집합, 리터럴 문자열 목록이 아니면 None.
    """
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    names = [element.value for element in node.elts if isinstance(element, ast.Constant)]
    if len(names) != len(node.elts) or not all(isinstance(name, str) for name in names):
        return None
    return frozenset(str(name) for name in names)


def is_exported(name: str, exports: frozenset[str] | None) -> bool:
    """`import *`가 이름을 내보내는지 본다.

    Args:
        name: 이름.
        exports: `star_exports` 결과(`UNKNOWN_EXPORTS` 제외).

    Returns:
        내보내면 True.
    """
    return not name.startswith("_") if exports is None else name in exports


def star_import_modules(index: ModuleIndex) -> list[str]:
    """모듈 수준 `from x import *`의 절대 모듈 이름을 순서대로 돌려준다.

    Args:
        index: 모듈 색인.

    Returns:
        모듈 이름 목록.
    """
    names: list[str] = []
    for statement in index.module.tree.body:
        if isinstance(statement, ast.ImportFrom) and any(alias.name == "*" for alias in statement.names):
            base = absolute_module(index.module, statement.module, statement.level)
            if base is not None:
                names.append(base)
    return names
