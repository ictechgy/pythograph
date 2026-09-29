"""사실 위치 계산: 1부터 시작하는 줄과 UTF-8 바이트 열.

`ast`의 열은 UTF-8 바이트 오프셋이다(routes `node_location`과 같은 규칙, BOM이면 첫 줄에 3바이트를 더한다).
속성 접근(`Article.objects`, `User.name`)은 식의 시작이 아니라 속성 이름을 가리키도록 끝 위치에서 이름의 바이트
길이를 뺀다.
"""

from __future__ import annotations

import ast

from pythograph.persistence.scope import ModuleScopes
from pythograph.routes.django.urlconf import node_location
from pythograph.routes.model import Location


def fact_location(scopes: ModuleScopes, node: ast.AST) -> Location:
    """노드의 사실 위치를 만든다.

    Args:
        scopes: 모듈 범위 도우미.
        node: 노드.

    Returns:
        위치.
    """
    has_bom = scopes.index.module.has_bom
    if isinstance(node, ast.Attribute) and node.end_lineno is not None and node.end_col_offset is not None:
        column = node.end_col_offset - len(node.attr.encode()) + 1
        if has_bom and node.end_lineno == 1:
            column += 3
        return Location(scopes.path, node.end_lineno, column)
    return node_location(scopes.path, node, has_bom)
