"""Flask 뷰 함수 옵션 속성(`methods`·`required_methods`) 쓰기를 찾는다.

Flask `App.add_url_rule`은 `methods`를 주지 않으면 `view_func.methods`를, 주든 말든 `view_func.required_methods`를 받는
method에 더한다(Flask 문서 "View Function Options", `flask/sansio/app.py`). 그래서 프로젝트 함수라도 그 속성을 쓰는
코드가 있으면 GET(또는 준 `methods`만)은 증명한 사실이 아니다. 속성 쓰기는 이름으로만 본다(넓게 잡는 쪽이다):

- 모듈 어디서든(함수 본문 포함) `X.methods = …`·`X.required_methods = …`(주석 대입·누적 대입 포함)과
  `setattr(X, "methods", …)`의 `X` 끝 이름(`index.methods`의 `index`, `views.index.methods`의 `index`). `setattr`의 속성
  이름이 리터럴이 아니면 그 대상도 넣는다.
- 장식자 함수 본문이 무엇에든 그 속성을 쓰면(`wrapper.methods = […]`) 그 장식자는 옵션을 단다.

`provide_automatic_options`는 자동 OPTIONS만 바꾸고 pythograph는 자동 OPTIONS를 내지 않으므로 보지 않는다.
"""

from __future__ import annotations

import ast

#: 받는 method를 바꾸는 뷰 함수 속성 이름이다.
OPTION_ATTRIBUTES = frozenset({"methods", "required_methods"})


def option_targets(tree: ast.Module) -> frozenset[str]:
    """모듈 안에서 옵션 속성을 쓰는 대상의 끝 이름을 모은다.

    Args:
        tree: 모듈 구문 트리.

    Returns:
        끝 이름 집합.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        for target in _option_stores(node):
            name = _last_name(target)
            if name is not None:
                found.add(name)
    return frozenset(found)


def stores_options(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """함수 본문(안쪽 함수 포함)이 무엇에든 옵션 속성을 쓰는지 본다(장식자·장식자 팩토리 판정).

    Args:
        function: 함수 정의.

    Returns:
        쓰면 True.
    """
    return any(_option_stores(node) for statement in function.body for node in ast.walk(statement))


def _option_stores(node: ast.AST) -> list[ast.expr]:
    """노드 하나가 옵션 속성을 쓰면 그 대상 식(속성의 앞부분)을 돌려준다.

    Args:
        node: 구문 노드.

    Returns:
        대상 식 목록(쓰지 않으면 빈 목록).
    """
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    elif isinstance(node, ast.Call) and _is_setattr(node):
        attribute = node.args[1]
        if isinstance(attribute, ast.Constant) and isinstance(attribute.value, str):
            return [node.args[0]] if attribute.value in OPTION_ATTRIBUTES else []
        return [node.args[0]]
    else:
        return []
    stores: list[ast.expr] = []
    for target in targets:
        for inner in ast.walk(target):
            if (
                isinstance(inner, ast.Attribute)
                and inner.attr in OPTION_ATTRIBUTES
                and isinstance(inner.ctx, ast.Store)
            ):
                stores.append(inner.value)
    return stores


def _is_setattr(call: ast.Call) -> bool:
    """`setattr(대상, 이름, 값)` 호출인지 본다(앞 두 인자가 펼침이 아닐 때만).

    Args:
        call: 호출 식.

    Returns:
        맞으면 True.
    """
    return (
        isinstance(call.func, ast.Name)
        and call.func.id == "setattr"
        and len(call.args) >= 2
        and not any(isinstance(argument, ast.Starred) for argument in call.args[:2])
    )


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
