"""모듈 상수 증명: URL 조각에 치환해도 되는 모듈 수준 이름인지 판정한다.

계약(HTTP-WRAPPERS `compose` 5): Python은 모듈 최상위에서 한 번만 대입되고 다시 대입되지 않는 이름만 상수로 치환한다.
대문자 이름 관례는 증명이 아니다. 다음이 하나라도 있으면 그 이름은 상수가 아니다:

- 모듈 수준(조건문·반복문·`try`·`with` 안 포함, 함수·클래스 본문 제외)에서 두 번 이상 묶임 — 대입·주석 대입·누적
  대입·반복 변수·`with … as`·import·함수·클래스 정의·`del`·예외 이름·바다코끼리·match 캡처를 모두 센다.
- 그 모듈의 어느 함수·클래스든 `global 이름`을 선언함(실행 중 다시 대입할 수 있다).
- 프로젝트 어디서든 모듈 속성으로 대입함(`constants.API_ROOT = …`, 리터럴 `setattr(constants, "API_ROOT", …)`).
"""

from __future__ import annotations

import ast
from collections import Counter

from pythograph.source.symbols import SymbolTable

#: 함수·클래스 본문처럼 모듈 범위가 아닌 노드다.
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


class ModuleConstants:
    """모듈별 이름 묶음 수를 세어 상수를 증명한다."""

    def __init__(self, symbols: SymbolTable, module_writes: set[tuple[str, str]]) -> None:
        """판정기를 만든다.

        Args:
            symbols: 모듈 색인.
            module_writes: 모듈 속성으로 대입된 (모듈 경로, 이름).
        """
        self.symbols = symbols
        self.module_writes = module_writes
        self._counts: dict[str, tuple[Counter[str], set[str]]] = {}

    def proven(self, path: str, name: str) -> bool:
        """모듈의 이름이 한 번만 묶인 상수인지 본다.

        Args:
            path: 모듈 경로.
            name: 이름.

        Returns:
            상수면 True.
        """
        if (path, name) in self.module_writes:
            return False
        counts, globals_declared = self._module(path)
        return counts[name] == 1 and name not in globals_declared

    def _module(self, path: str) -> tuple[Counter[str], set[str]]:
        """모듈의 묶음 수와 `global` 선언 이름을 모은다(캐시).

        Args:
            path: 모듈 경로.

        Returns:
            (이름 → 모듈 수준 묶음 수, `global`로 선언된 이름).
        """
        if path not in self._counts:
            index = self.symbols.index(path)
            counts: Counter[str] = Counter()
            declared: set[str] = set()
            if index is not None:
                _count_module(index.module.tree, counts, declared)
            self._counts[path] = (counts, declared)
        return self._counts[path]


def _count_module(tree: ast.Module, counts: Counter[str], declared: set[str]) -> None:
    """모듈 수준 묶음과 모든 범위의 `global` 선언을 센다.

    Args:
        tree: 모듈 구문 트리.
        counts: 채울 묶음 수.
        declared: 채울 `global` 이름.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            declared.update(node.names)
    pending: list[ast.AST] = list(tree.body)
    while pending:
        node = pending.pop()
        for name in _bound_names(node):
            counts[name] += 1
        if isinstance(node, _NESTED_SCOPES):
            continue
        pending.extend(child for child in ast.iter_child_nodes(node) if not _is_comprehension(child))


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
