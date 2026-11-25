"""Flask 클래스 뷰 본문에서 정적으로 읽을 수 없는 이름 묶음과 `__init_subclass__` 훅의 `super()` 호출을 판정한다.

`analyze_class`·`own_assignment`는 본문 맨 위의 이름 대입(`x = …`, 값 있는 `x: T = …`)과 함수 정의만 읽는다. 같은
이름을 블록(`if`·`try`·`for`·`with`·`match`) 안에서 묶거나, 누적 대입·`del`·풀어 쓰는 대입·바다코끼리·import·
`global`으로 묶으면 클래스 사전의 값은 실행 경로에 따라 달라지므로 Flask `methods` 판정(`view_methods`)은 그 이름을
모른다고 본다. 본문이 인자 없는 `vars()`·`locals()`나 인자 하나인 `exec(…)`를 부르면 클래스 사전의 어떤 이름이든 바꿀 수
있으므로 모든 이름을 모른다(`NAMESPACE_WRITES`). 안쪽 함수·클래스·람다·내포 식의 본문은 클래스 사전에 이름을 묶지
않으므로(메서드의 지역 변수 `methods`) 보지 않는다. 클래스 장식자가 받은 클래스를 그대로 돌려주는지도 여기서 본다
(`returns_its_argument`). Django·DRF 판정은 이 모듈을 쓰지 않는다.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator

#: 클래스 이름공간 전체를 바꿀 수 있는 호출(인자 없는 `vars()`·`locals()`, 인자 하나인 `exec(…)`)이 있다는 표식이다.
#: 식별자가 될 수 없는 문자열이라 실제 이름과 겹치지 않는다.
NAMESPACE_WRITES = "*"

#: 인자 없이 부르면 지금 이름공간(클래스 본문이면 클래스 사전)을 돌려주는 내장 함수다.
_NAMESPACE_FUNCTIONS = frozenset({"vars", "locals"})

#: 본문이 따로 실행되는 정의다(그 안의 `return`·`raise`는 훅을 끝내지 않는다).
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

#: 자기 범위를 만드는 식이다. 그 안의 이름 묶음은 클래스 사전에 닿지 않는다(클래스 본문의 내포 식 안 바다코끼리는
#: 문법 오류다).
_SCOPED_EXPRESSIONS = (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def irregular_names(node: ast.ClassDef) -> frozenset[str]:
    """본문 맨 위의 이름 대입·함수 정의가 아닌 방법으로 클래스 사전에 묶이거나 지워질 수 있는 이름을 모은다.

    맨 위 함수 정의 `methods`는 `own_assignment`가 읽지 않으므로 여기에 넣는다. 이름공간 전체를 바꿀 수 있는 호출이
    있으면 `NAMESPACE_WRITES`를 넣는다(어떤 이름이든 모른다).

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

    이름공간 전체를 바꿀 수 있는 호출이면 `NAMESPACE_WRITES`를 낸다.

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
    elif _writes_namespace(node):
        yield NAMESPACE_WRITES


def _writes_namespace(node: ast.AST) -> bool:
    """노드가 지금 이름공간을 돌려주거나 그 안에서 코드를 실행하는 호출인지 본다.

    인자 없는 `vars()`·`locals()`, 전역·지역 사전을 주지 않은 `exec(code)`다.

    Args:
        node: 구문 노드.

    Returns:
        맞으면 True.
    """
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.keywords:
        return False
    if node.func.id in _NAMESPACE_FUNCTIONS:
        return not node.args
    return node.func.id == "exec" and len(node.args) == 1


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
    first = _first_parameter(hook)
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


def returns_its_argument(function: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> bool:
    """클래스 장식자 함수가 받은 클래스를 그대로 돌려주는지 본다.

    본문의 모든 `return`(안쪽 정의 제외)이 `return <첫 매개변수>`이고 마지막 맨 위 문장도 그것이며, 첫 매개변수를 다시
    묶지 않을 때만이다. 다른 클래스나 하위 클래스를 돌려주는 장식자(`return Other`, 안에서 만든 `Wrapped(cls)`), 끝까지
    가서 None을 돌려줄 수 있는 장식자, `yield`·`async def` 장식자, 클래스 장식자(인스턴스를 돌려준다)는 `methods`를
    바꾼 클래스를 돌려줄 수 있다.

    Args:
        function: 장식자 정의.

    Returns:
        그대로 돌려주면 True.
    """
    if not isinstance(function, ast.FunctionDef) or not function.body:
        return False
    first = _first_parameter(function)
    if first is None or not _returns_name(function.body[-1], first.arg):
        return False
    nodes = list(_walk(function))
    if any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in nodes):
        return False
    rebound = any(first.arg in _bindings(statement) for statement in function.body)
    returns = [node for node in nodes if isinstance(node, ast.Return)]
    return not rebound and all(_returns_name(node, first.arg) for node in returns)


def returns_class_decorator(factory: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> bool:
    """장식자 팩토리(`@factory(...)`)가 돌려주는 장식자가 받은 클래스를 그대로 돌려주는지 본다.

    팩토리의 모든 `return`이 팩토리 본문 맨 위에서 한 번만 정의한 안쪽 함수 이름 하나를 돌려주고, 마지막 맨 위 문장도
    그것이며, 그 안쪽 함수가 `returns_its_argument`일 때만이다.

    Args:
        factory: 팩토리 정의.

    Returns:
        그대로 돌려주는 장식자를 돌려주면 True.
    """
    if not isinstance(factory, ast.FunctionDef) or not factory.body:
        return False
    last = factory.body[-1]
    if not isinstance(last, ast.Return) or not isinstance(last.value, ast.Name):
        return False
    name = last.value.id
    inner = [
        statement for statement in factory.body if isinstance(statement, ast.FunctionDef) and statement.name == name
    ]
    bound = [bound for statement in factory.body for bound in _bindings(statement) if bound == name]
    nodes = list(_walk(factory))
    if len(inner) != 1 or len(bound) != 1 or any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in nodes):
        return False
    returns = [node for node in nodes if isinstance(node, ast.Return)]
    return all(_returns_name(node, name) for node in returns) and returns_its_argument(inner[0])


def _first_parameter(function: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.arg | None:
    """함수의 첫 위치 매개변수를 돌려준다.

    Args:
        function: 함수 정의.

    Returns:
        매개변수, 없으면 None.
    """
    arguments = function.args
    return arguments.posonlyargs[0] if arguments.posonlyargs else arguments.args[0] if arguments.args else None


def _returns_name(node: ast.AST, name: str) -> bool:
    """노드가 `return <name>` 문장인지 본다.

    Args:
        node: 구문 노드.
        name: 이름.

    Returns:
        맞으면 True.
    """
    return isinstance(node, ast.Return) and isinstance(node.value, ast.Name) and node.value.id == name


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
