"""Flask 클래스 뷰 본문에서 정적으로 읽을 수 없는 이름 묶음과 `__init_subclass__` 훅의 `super()` 호출을 판정한다.

`analyze_class`·`own_assignment`는 본문 맨 위의 이름 대입(`x = …`, 값 있는 `x: T = …`)과 함수 정의만 읽는다. 같은
이름을 블록(`if`·`try`·`for`·`with`·`match`) 안에서 묶거나, 누적 대입·`del`·풀어 쓰는 대입·바다코끼리·import·
`global`으로 묶으면 클래스 사전의 값은 실행 경로에 따라 달라지므로 Flask `methods` 판정(`view_methods`)은 그 이름을
모른다고 본다. 안쪽 함수·클래스·람다·내포 식의 본문은 클래스 사전에 이름을 묶지 않으므로(메서드의 지역 변수
`methods`) 보지 않는다. Django·DRF 판정은 이 모듈을 쓰지 않는다.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator

#: 본문이 따로 실행되는 정의다(그 안의 `return`·`raise`는 훅을 끝내지 않는다).
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

#: 자기 범위를 만드는 식이다. 그 안의 이름 묶음은 클래스 사전에 닿지 않는다(클래스 본문의 내포 식 안 바다코끼리는
#: 문법 오류다).
_SCOPED_EXPRESSIONS = (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def irregular_names(node: ast.ClassDef) -> frozenset[str]:
    """본문 맨 위의 이름 대입·함수 정의가 아닌 방법으로 클래스 사전에 묶이거나 지워질 수 있는 이름을 모은다.

    맨 위 함수 정의 `methods`는 `own_assignment`가 읽지 않으므로 여기에 넣는다.

    Args:
        node: 클래스 정의.

    Returns:
        이름 집합.
    """
    found: set[str] = set()
    for statement in node.body:
        regular = _plain_targets(statement)
        for name in _bindings(statement):
            if name in regular:
                regular.remove(name)
            else:
                found.add(name)
    return frozenset(found)


def _plain_targets(statement: ast.stmt) -> list[str]:
    """문장이 `analyze_class`가 읽는 방식으로 묶는 이름을 돌려준다(같은 이름이 두 번 나오면 두 번).

    Args:
        statement: 클래스 본문 맨 위 문장.

    Returns:
        이름 목록.
    """
    if isinstance(statement, ast.Assign) and all(isinstance(target, ast.Name) for target in statement.targets):
        return [target.id for target in statement.targets if isinstance(target, ast.Name)]
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.value is not None:
        return [statement.target.id]
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) and statement.name != "methods":
        return [statement.name]
    return []


def _bindings(node: ast.AST) -> Iterator[str]:
    """노드가 둘러싼 범위(클래스 본문)에 묶거나 지우는 이름을 차례로 낸다. 안쪽 범위의 본문은 걷지 않는다.

    Args:
        node: 구문 노드.

    Returns:
        이름 반복자.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        yield node.name
        outer: list[ast.AST] = [*node.decorator_list]
        if isinstance(node, ast.ClassDef):
            outer += [*node.bases, *node.keywords]
        else:
            outer += [*node.args.defaults, *(default for default in node.args.kw_defaults if default is not None)]
        for child in outer:
            yield from _bindings(child)
        return
    if isinstance(node, _SCOPED_EXPRESSIONS):
        return
    if isinstance(node, ast.AnnAssign) and node.value is None:
        return
    yield from _own_bindings(node)
    for child in ast.iter_child_nodes(node):
        yield from _bindings(child)


def _own_bindings(node: ast.AST) -> Iterator[str]:
    """노드 자신이 묶는 이름(대입·`del` 대상 이름, 예외 이름, import, `global`·`nonlocal`, `match` 캡처)을 낸다.

    Args:
        node: 구문 노드.

    Returns:
        이름 반복자.
    """
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        yield node.id
    elif isinstance(node, ast.ExceptHandler) and node.name:
        yield node.name
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        yield from (alias.asname or alias.name.split(".")[0] for alias in node.names)
    elif isinstance(node, (ast.Global, ast.Nonlocal)):
        yield from node.names
    elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
        yield node.name
    elif isinstance(node, ast.MatchMapping) and node.rest:
        yield node.rest


def calls_super_hook(hook: ast.FunctionDef | ast.AsyncFunctionDef, owner: str) -> bool:
    """`__init_subclass__` 정의가 반드시 다음 클래스의 훅(`super().__init_subclass__(…)`)을 부르는지 본다.

    본문 맨 위 문장의 호출만 받는다. 그 앞 문장이 `return`·`raise`를 담으면(조건 안 포함, 안쪽 정의 제외) 부르기 전에
    끝날 수 있다. 인자 없는 `super()`나 `super(<훅을 정의한 클래스>, <첫 매개변수>)`만 받는다(`super(MethodView, cls)`는
    `MethodView`의 훅을 건너뛴다). `async def`나 `yield`를 담은 훅은 부르면 본문을 실행하지 않는다.

    Args:
        hook: `__init_subclass__` 정의.
        owner: 훅을 정의한 클래스 이름.

    Returns:
        반드시 부르면 True.
    """
    generator = any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in _walk(hook))
    if isinstance(hook, ast.AsyncFunctionDef) or generator:
        return False
    first = hook.args.posonlyargs[0] if hook.args.posonlyargs else hook.args.args[0] if hook.args.args else None
    for statement in hook.body:
        if isinstance(statement, (ast.Expr, ast.Return)) and _is_super_hook_call(statement.value, owner, first):
            return True
        if isinstance(statement, _DEFINITIONS):
            continue
        if any(isinstance(node, (ast.Return, ast.Raise)) for node in _walk(statement)):
            return False
    return False


def _is_super_hook_call(node: ast.expr | None, owner: str, first: ast.arg | None) -> bool:
    """식이 `super().__init_subclass__(…)`나 `super(<owner>, <첫 매개변수>).__init_subclass__(…)` 호출인지 본다.

    Args:
        node: 식.
        owner: 훅을 정의한 클래스 이름.
        first: 훅의 첫 매개변수(없으면 None).

    Returns:
        맞으면 True.
    """
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "__init_subclass__"
        and isinstance(node.func.value, ast.Call)
        and isinstance(node.func.value.func, ast.Name)
        and node.func.value.func.id == "super"
    ):
        return False
    arguments = node.func.value.args
    if not arguments and not node.func.value.keywords:
        return True
    return (
        first is not None
        and len(arguments) == 2
        and not node.func.value.keywords
        and isinstance(arguments[0], ast.Name)
        and arguments[0].id == owner
        and isinstance(arguments[1], ast.Name)
        and arguments[1].id == first.arg
    )


def _walk(node: ast.AST) -> Iterator[ast.AST]:
    """노드 아래를 걷되 안쪽 함수·클래스·람다의 본문으로는 들어가지 않는다(노드 자신이 정의면 그 본문은 걷는다).

    Args:
        node: 시작 노드.

    Returns:
        노드 반복자.
    """
    pending: list[ast.AST] = [node]
    while pending:
        current = pending.pop()
        yield current
        for child in ast.iter_child_nodes(current):
            if not isinstance(child, (*_DEFINITIONS, ast.Lambda)):
                pending.append(child)
