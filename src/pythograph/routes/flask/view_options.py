"""Flask 뷰 함수 옵션 속성(`methods`·`required_methods`) 쓰기를 찾는다.

Flask `App.add_url_rule`은 `methods`를 주지 않으면 `view_func.methods`를, 주든 말든 `view_func.required_methods`를 받는
method에 더한다(Flask 문서 "View Function Options", `flask/sansio/app.py`). 그래서 프로젝트 함수라도 그 속성을 쓰는
코드가 있으면 GET(또는 준 `methods`만)은 증명한 사실이 아니다. 속성 쓰기는 이름으로만 보고 프로젝트 전체에서 모은다
(넓게 잡는 쪽이다):

- 대상(`ProjectOptionWrites.targets`): `X.methods = …`·`X.required_methods = …`(주석 대입·누적 대입 포함),
  `setattr(X, "methods", …)`(속성 이름이 리터럴이 아니어도), `X.__dict__[…] = …`·`vars(X)[…] = …`·
  `X.__dict__.update(…)`·`vars(X).update(…)`(열쇠가 옵션 이름이거나 모를 때)의 `X` 끝 이름(`index.methods`의 `index`,
  `views.index.methods`의 `index`). 그 이름의 뷰 함수·클래스 뷰는 method를 모른다.
- 묶이지 않은 쓰기(`ProjectOptionWrites.untied`): 대상 끝 이름이 프로젝트 함수·클래스 이름이 아니고(별칭 `f = view`,
  반복 변수, 매개변수, 호출 결과) 장식자로 쓰는 함수·클래스·람다 안도 아닌 쓰기, 그리고 옵션을 쓴 대상의 `__dict__`를
  옮기는 `functools.wraps(X)`·`update_wrapper(W, X)`. 어느 뷰에 닿는지 모르므로 스코프 없는 한계 하나로 센다.
- 장식자(`stores_options`): 장식자 함수·클래스·람다 본문이 무엇에든 옵션 속성을 쓰면 그 장식자는 옵션을 단다.

`self`·`cls`에 쓰는 것(인스턴스·클래스 속성)은 함수 뷰에 닿지 않으므로 보지 않는다. `provide_automatic_options`는 자동
OPTIONS만 바꾸고 pythograph는 자동 OPTIONS를 내지 않으므로 보지 않는다.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass

#: 받는 method를 바꾸는 뷰 함수 속성 이름이다.
OPTION_ATTRIBUTES = frozenset({"methods", "required_methods"})

#: 인스턴스·클래스 자신을 가리키는 관례 이름이다. 그 속성 쓰기는 함수 뷰의 옵션이 아니다.
_SELF_NAMES = frozenset({"self", "cls"})

#: 장식자로 쓸 수 있는 정의다.
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


@dataclass(frozen=True)
class ProjectOptionWrites:
    """프로젝트 전체의 뷰 함수 옵션 속성 쓰기 요약.

    Attributes:
        targets: 옵션 속성을 쓴 대상의 끝 이름.
        untied: 프로젝트 함수·클래스나 장식자에 묶지 못한 쓰기 수.
    """

    targets: frozenset[str]
    untied: int


def scan_option_writes(trees: Iterable[ast.Module]) -> ProjectOptionWrites:
    """프로젝트 모듈들의 옵션 속성 쓰기를 모은다(한 번의 추출에 한 번 부른다).

    Args:
        trees: 모듈 구문 트리들.

    Returns:
        쓰기 요약.
    """
    modules = list(trees)
    definitions: set[str] = set()
    decorators: set[str] = set()
    local: dict[int, set[str]] = {}
    for tree in modules:
        names = local.setdefault(id(tree), set())
        for node in ast.walk(tree):
            if isinstance(node, _DEFINITIONS):
                names.add(node.name)
                decorators.update(name for item in node.decorator_list if (name := _decorator_name(item)))
        definitions |= names
    writes: list[tuple[str | None, bool]] = []
    copies: list[str] = []
    for tree in modules:
        _collect_writes(tree, False, _Scan(decorators, local[id(tree)], writes, copies))
    targets = frozenset(name for name, _ in writes if name is not None)
    untied = sum(1 for name, inside in writes if not inside and (name is None or name not in definitions))
    untied += sum(1 for name in copies if name in targets)
    return ProjectOptionWrites(targets, untied)


def stores_options(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda) -> bool:
    """장식자 본문(안쪽 함수 포함)이 무엇에든 옵션 속성을 쓰는지 본다(클래스는 본문의 `methods = …` 선언도 본다).

    Args:
        node: 장식자 함수·클래스·람다.

    Returns:
        쓰면 True.
    """
    body: list[ast.AST] = [node.body] if isinstance(node, ast.Lambda) else [*node.body]
    for statement in body:
        if isinstance(node, ast.ClassDef) and _declares_option(statement):
            return True
        if any(_option_targets_of(inner) for inner in ast.walk(statement)):
            return True
    return False


@dataclass(frozen=True)
class _Scan:
    """모듈 하나를 훑는 동안의 재료.

    Attributes:
        decorators: 프로젝트에서 장식자로 쓰는 이름.
        local: 이 모듈에서 정의한 함수·클래스 이름.
        writes: 모을 (대상 끝 이름, 장식자 안인지) 목록.
        copies: 모을 `__dict__` 출처 이름 목록.
    """

    decorators: set[str]
    local: set[str]
    writes: list[tuple[str | None, bool]]
    copies: list[str]


def _collect_writes(node: ast.AST, inside: bool, scan: _Scan) -> None:
    """노드 아래의 옵션 쓰기를 (대상 끝 이름, 장식자 안인지)로, `wraps`·`update_wrapper`가 옮겨 오는 이름을 모은다.

    장식자 이름으로 쓰는 함수·클래스 본문과 장식자 목록 안 식(람다 장식자)은 장식자 안이다. 속성 이름·열쇠가 리터럴이
    아닌 쓰기(`setattr(user, name, value)`)는 대상이 이 모듈에서 정의한 함수·클래스일 때만 옵션 쓰기로 본다(사용자·모델
    객체의 흔한 쓰기를 옵션으로 세지 않는다). 장식자 본문 안의 `wraps(f)`는 장식자 판정이 맡으므로 넣지 않는다.

    Args:
        node: 구문 노드.
        inside: 장식자 안인지.
        scan: 모듈 재료.
    """
    for target, certain in _option_targets_of(node):
        name = _last_name(target)
        if name not in _SELF_NAMES and (certain or name in scan.local):
            scan.writes.append((name, inside))
    copied = _copied_from(node)
    if copied is not None and not inside:
        scan.copies.append(copied)
    if isinstance(node, _DEFINITIONS):
        for decorator in node.decorator_list:
            # 정의에 바로 단 `@functools.wraps(base)`는 그 정의에 `base`의 옵션을 옮긴다(장식자 본문 안의 것과 다르다).
            source = _copied_from(decorator)
            if source is not None and not inside:
                scan.copies.append(source)
            _collect_writes(decorator, True, scan)
        inner = inside or node.name in scan.decorators
        for statement in node.body:
            _collect_writes(statement, inner, scan)
        return
    for child in ast.iter_child_nodes(node):
        _collect_writes(child, inside, scan)


def _option_targets_of(node: ast.AST) -> list[tuple[ast.expr, bool]]:
    """노드 하나가 옵션 속성을 쓰면 (대상 식, 이름이 리터럴 옵션 이름인지)를 돌려준다(속성 대입·`setattr`·`__dict__`·
    `vars` 쓰기).

    Args:
        node: 구문 노드.

    Returns:
        (대상 식, 확실한지) 목록(쓰지 않으면 빈 목록).
    """
    if isinstance(node, ast.Call):
        return _call_targets(node)
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    else:
        return []
    found: list[tuple[ast.expr, bool]] = []
    for target in targets:
        for inner in ast.walk(target):
            if (
                isinstance(inner, ast.Attribute)
                and inner.attr in OPTION_ATTRIBUTES
                and isinstance(inner.ctx, ast.Store)
            ):
                found.append((inner.value, True))
            elif isinstance(inner, ast.Subscript) and isinstance(inner.ctx, ast.Store):
                owner, kind = _namespace_owner(inner.value), _key_kind(inner.slice)
                if owner is not None and kind is not None:
                    found.append((owner, kind))
    return found


def _call_targets(call: ast.Call) -> list[tuple[ast.expr, bool]]:
    """`setattr(X, 이름, …)`·`X.__dict__.update(…)`·`vars(X).update(…)` 호출의 옵션 쓰기 대상이다.

    Args:
        call: 호출 식.

    Returns:
        (대상 식, 확실한지) 목록.
    """
    func = call.func
    if isinstance(func, ast.Name) and func.id == "setattr" and len(call.args) >= 2:
        if any(isinstance(argument, ast.Starred) for argument in call.args[:2]):
            return []
        kind = _key_kind(call.args[1])
        return [] if kind is None else [(call.args[0], kind)]
    if isinstance(func, ast.Attribute) and func.attr == "update":
        owner = _namespace_owner(func.value)
        kind = _update_kind(call)
        if owner is not None and kind is not None:
            return [(owner, kind)]
    return []


def _update_kind(call: ast.Call) -> bool | None:
    """`….update(…)`가 옵션 이름 열쇠를 넣는지 본다.

    Args:
        call: `update` 호출.

    Returns:
        리터럴 옵션 열쇠가 있으면 True, 열쇠를 모르면 False, 옵션 열쇠가 없다고 알면 None.
    """
    kinds = [_key_kind(ast.Constant(keyword.arg) if keyword.arg else None) for keyword in call.keywords]
    for argument in call.args:
        if isinstance(argument, ast.Dict):
            kinds.extend(_key_kind(key) for key in argument.keys)
        else:
            kinds.append(False)
    if True in kinds:
        return True
    return False if False in kinds else None


def _key_kind(node: ast.expr | None) -> bool | None:
    """속성 이름·사전 열쇠 식을 가른다.

    Args:
        node: 이름 식(사전 펼침 `**` 열쇠는 None).

    Returns:
        리터럴 옵션 이름이면 True, 리터럴이 아니면 False, 옵션이 아닌 리터럴이면 None.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True if node.value in OPTION_ATTRIBUTES else None
    return False


def _namespace_owner(node: ast.expr) -> ast.expr | None:
    """`X.__dict__`·`vars(X)` 식이면 `X`를 돌려준다.

    Args:
        node: 식.

    Returns:
        이름공간의 주인 식, 아니면 None.
    """
    if isinstance(node, ast.Attribute) and node.attr == "__dict__":
        return node.value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "vars"
        and len(node.args) == 1
    ):
        return node.args[0]
    return None


def _copied_from(node: ast.AST) -> str | None:
    """`wraps(X)`·`update_wrapper(W, X)` 호출이면 `__dict__`를 옮겨 오는 `X`의 끝 이름을 돌려준다.

    Args:
        node: 구문 노드.

    Returns:
        끝 이름, 아니면 None.
    """
    if not isinstance(node, ast.Call):
        return None
    name = _last_name(node.func)
    position = 0 if name == "wraps" else 1 if name == "update_wrapper" else None
    if position is None or len(node.args) <= position:
        return None
    return _last_name(node.args[position])


def _declares_option(statement: ast.AST) -> bool:
    """클래스 본문 문장이 `methods = …`·`required_methods = …` 선언인지 본다(클래스 장식자 인스턴스의 속성).

    Args:
        statement: 클래스 본문 문장.

    Returns:
        선언이면 True.
    """
    targets = (
        statement.targets
        if isinstance(statement, ast.Assign)
        else [statement.target]
        if isinstance(statement, (ast.AnnAssign, ast.AugAssign))
        else []
    )
    return any(isinstance(target, ast.Name) and target.id in OPTION_ATTRIBUTES for target in targets)


def _decorator_name(node: ast.expr) -> str | None:
    """장식자 식(팩토리 호출이면 그 함수)의 끝 이름이다.

    Args:
        node: 장식자 식.

    Returns:
        끝 이름 또는 None.
    """
    return _last_name(node.func if isinstance(node, ast.Call) else node)


def _last_name(node: ast.expr) -> str | None:
    """`a`·`a.b`·`a.b.c` 식의 끝 이름이다.

    Args:
        node: 식.

    Returns:
        끝 이름, 이름·속성 식이 아니면 None.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None
