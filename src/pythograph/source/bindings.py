"""모듈 수준 이름 묶음 셈: 모듈 이름이 정적 값 하나로 증명되는지 판정하는 공용 재료.

routes·schema의 상수 평가(`source.symbols.ModuleIndex.rebound` → `source.evaluate.Evaluator`)와 client 모듈 상수
증명(`routes.client.constants.ModuleConstants`)이 같은 셈을 쓴다. graph 해석기(`graph.scope`)는 함수 범위까지 다루는
자기 묶음 수집기를 쓰고, 모듈 이름 판정 결과가 같은지는 `tests/test_source.py`가 고정한다.

- 묶음 수: 모듈 수준(조건문·반복문·`try`·`with`·`match` 안 포함, 함수·클래스 본문 제외)의 대입·주석 대입(값 없는
  선언 포함)·누적 대입·반복 변수·`with … as`·import·함수·클래스 정의·`del`·예외 이름·바다코끼리·match 캡처를 모두
  센다. 함수·클래스 정의는 정의 시점에 모듈 범위에서 평가되는 식(장식자·기본값·주석·클래스 기반)만 본다.
- 블록 안 묶음: 조건·반복·예외·`with`·`match` 블록 안에서 묶인 이름이다. 실행 경로에 따라 묶이지 않거나 값이 달라진다.
- `global` 선언: 어느 범위든 `global 이름`을 선언한 이름이다. 함수가 실행 중 다시 묶을 수 있다.
- 제자리 변경: 모듈 어디서든(함수 본문 포함) 이름에 직접 부른 내장 목록·집합·사전 변경 메서드(`NAME.append(…)`)와
  첨자 쓰기·지우기(`NAME[k] = …`, `del NAME[k]`)다. 묶음 수는 그대로여도 값이 바뀐다. 같은 이름의 지역 변수도 세므로
  넓게 잡는 쪽이다. 함수에 넘겨 바꾸는 경우(`add_apps(NAME)`)는 보지 않는다.
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from dataclasses import dataclass

#: 함수·클래스 본문처럼 모듈 범위가 아닌 노드다.
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)

if sys.version_info >= (3, 11):  # pragma: no cover - 실행 중인 파이썬 버전에 따라 한쪽만 탄다.
    _TRY_STATEMENTS: tuple[type[ast.stmt], ...] = (ast.Try, ast.TryStar)
else:  # pragma: no cover
    _TRY_STATEMENTS = (ast.Try,)

#: 안의 묶음이 실행 경로에 따라 생기지 않거나 달라지는 블록 문장이다(`try … except*` 포함).
BLOCK_STATEMENTS: tuple[type[ast.stmt], ...] = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Match,
    *_TRY_STATEMENTS,
)

#: 제자리에서 값을 바꾸는 내장 목록·집합·사전 메서드다. 상수 평가기가 만드는 값(문자열·수·불리언·None·목록)을 바꿀 수
#: 있는 것은 이 메서드뿐이다(문자열·수·튜플 메서드는 새 값을 돌려준다).
MUTATING_METHODS = frozenset(
    {
        "append",
        "extend",
        "insert",
        "remove",
        "pop",
        "clear",
        "sort",
        "reverse",
        "add",
        "discard",
        "update",
        "difference_update",
        "intersection_update",
        "symmetric_difference_update",
        "setdefault",
        "popitem",
        "__setitem__",
        "__delitem__",
        "__iadd__",
        "__imul__",
        "__ior__",
        "__iand__",
        "__isub__",
        "__ixor__",
    }
)


@dataclass(frozen=True)
class ModuleBindings:
    """모듈 하나의 모듈 수준 묶음 요약. 모듈 색인이 처음 쓸 때 만들어 소비자가 함께 쓰므로 고치지 않는다.

    Attributes:
        counts: 이름 → 모듈 수준 묶음 수.
        declared_globals: 어느 범위든 `global`로 선언한 이름.
        nested: 조건·반복·예외·`with`·`match` 블록 안에서 묶인 이름.
        mutated: 이름에 직접 부른 변경 메서드나 첨자 쓰기·지우기로 값이 바뀌는 이름.
        package_imports: 이름 → 별칭 없는 `import 이름`·`import 이름.하위` 묶음 수(`counts`에 포함). 모두 같은 최상위
            패키지 객체를 묶으므로 값 판정은 한 묶음으로 본다.
    """

    counts: Counter[str]
    declared_globals: frozenset[str]
    nested: frozenset[str]
    mutated: frozenset[str]
    package_imports: Counter[str]


def module_bindings(tree: ast.Module) -> ModuleBindings:
    """모듈의 모듈 수준 묶음을 센다.

    Args:
        tree: 모듈 구문 트리.

    Returns:
        묶음 요약.
    """
    declared, mutated = _whole_module_names(tree)
    counts: Counter[str] = Counter()
    packages: Counter[str] = Counter()
    nested: set[str] = set()
    pending: list[tuple[ast.AST, bool]] = [(statement, False) for statement in tree.body]
    while pending:
        node, inside = pending.pop()
        for name in _bound_names(node):
            counts[name] += 1
            if inside:
                nested.add(name)
        if isinstance(node, ast.Import):
            packages.update(alias.name.split(".")[0] for alias in node.names if alias.asname is None)
        block = inside or isinstance(node, BLOCK_STATEMENTS)
        pending.extend((child, block) for child in _module_scope_children(node))
    return ModuleBindings(counts, frozenset(declared), frozenset(nested), frozenset(mutated), packages)


def _whole_module_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """모듈 전체(함수·클래스 본문 포함)에서 `global` 선언 이름과 제자리 변경 대상 이름을 모은다.

    Args:
        tree: 모듈 구문 트리.

    Returns:
        (`global` 선언 이름, 제자리 변경 대상 이름).
    """
    declared: set[str] = set()
    mutated: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            declared.update(node.names)
            continue
        target = _mutated_name(node)
        if target is not None:
            mutated.add(target)
    return declared, mutated


def _mutated_name(node: ast.AST) -> str | None:
    """노드가 제자리에서 바꾸는 이름을 돌려준다: `NAME.append(…)`, `NAME[k] = …`, `del NAME[k]`(첨자 사슬 포함).

    Args:
        node: 구문 노드.

    Returns:
        이름, 아니면 None.
    """
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in MUTATING_METHODS:
        return _subscript_root(node.func.value)
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return _subscript_root(node.value)
    return None


def _subscript_root(node: ast.expr) -> str | None:
    """첨자 사슬(`NAME[a][b]`)의 맨 앞 이름을 돌려준다.

    Args:
        node: 식.

    Returns:
        이름, 이름이 아니면 None.
    """
    while isinstance(node, ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _module_scope_children(node: ast.AST) -> list[ast.AST]:
    """모듈 범위에서 평가되는 자식 노드를 돌려준다. 정의는 정의 시점 식만, 바다코끼리 없는 컴프리헨션은 뺀다.

    Args:
        node: 구문 노드.

    Returns:
        자식 노드 목록.
    """
    if isinstance(node, _NESTED_SCOPES):
        return _definition_time_nodes(node)
    return [child for child in ast.iter_child_nodes(node) if not _is_comprehension(child)]


def _definition_time_nodes(node: ast.AST) -> list[ast.AST]:
    """정의 시점에 바깥(모듈) 범위에서 평가되는 식을 돌려준다: 장식자, 기본값, 주석, 클래스 기반·키워드.

    이 식 안의 바다코끼리 대입은 모듈 이름을 다시 묶는다.

    Args:
        node: 함수·클래스·람다 정의.

    Returns:
        식 목록.
    """
    if isinstance(node, ast.Lambda):
        return [*node.args.defaults, *(item for item in node.args.kw_defaults if item is not None)]
    found: list[ast.AST] = list(getattr(node, "decorator_list", []))
    if isinstance(node, ast.ClassDef):
        found.extend(node.bases)
        found.extend(keyword.value for keyword in node.keywords)
        return found
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        arguments = node.args
        found.extend(arguments.defaults)
        found.extend(item for item in arguments.kw_defaults if item is not None)
        every = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs, arguments.vararg, arguments.kwarg]
        found.extend(item.annotation for item in every if item is not None and item.annotation is not None)
        if node.returns is not None:
            found.append(node.returns)
    return found


def _is_comprehension(node: ast.AST) -> bool:
    """컴프리헨션(자기 범위)인지 본다. 안의 바다코끼리 대입만 바깥에 묶이지만 모르는 값이라 센다.

    Args:
        node: 구문 노드.

    Returns:
        컴프리헨션이면 True.
    """
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        return not any(isinstance(child, ast.NamedExpr) for child in ast.walk(node))
    return False


def _bound_names(node: ast.AST) -> list[str]:
    """노드 하나가 모듈 범위에 묶는 이름을 돌려준다.

    Args:
        node: 구문 노드.

    Returns:
        이름 목록.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return [node.id]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [alias.asname or alias.name.split(".")[0] for alias in node.names if alias.name != "*"]
    if isinstance(node, ast.ExceptHandler) and node.name:
        return [node.name]
    if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
        return [node.name]
    if isinstance(node, ast.MatchMapping) and node.rest:
        return [node.rest]
    return []
