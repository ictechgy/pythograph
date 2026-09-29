"""정적 상수 평가: 문자열·불리언·숫자·그 목록을 실행 없이 구한다.

라우트 문자열·method 목록·설정값처럼 리터럴이거나 리터럴에서 곧장 얻는 값만 구한다. 구하지 못하면
`UNKNOWN`을 돌려주고 호출자가 dynamic·limitation으로 처리한다. 추측하지 않는다.
"""

from __future__ import annotations

import ast
from typing import Final

from pythograph.source.symbols import SymbolTable, ValueSymbol

#: 평가하지 못한 값을 뜻하는 표식이다.
UNKNOWN: Final = object()

#: 이름을 따라 평가할 최대 깊이다(순환 방지).
MAX_EVALUATION_DEPTH = 12


class Evaluator:
    """모듈 수준 이름을 따라가며 상수를 평가한다."""

    def __init__(self, symbols: SymbolTable, overrides: dict[str, object] | None = None) -> None:
        """평가기를 만든다.

        Args:
            symbols: 이름 해석기.
            overrides: `settings.<NAME>`처럼 외부에서 준 값(예: Django 설정 모듈에서 읽은 값).
        """
        self.symbols = symbols
        self.settings = overrides or {}

    def value(self, path: str, node: ast.expr | None, depth: int = 0) -> object:
        """식을 상수로 평가한다.

        Args:
            path: 식이 있는 모듈 경로.
            node: 식.
            depth: 이름을 따라간 깊이.

        Returns:
            `str`·`bool`·`int`·`float`·`None`·그 `list`, 모르면 `UNKNOWN`.
        """
        if node is None or depth > MAX_EVALUATION_DEPTH:
            return UNKNOWN
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, (str, bool, int, float, type(None))) else UNKNOWN
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            items = [self.value(path, item, depth + 1) for item in node.elts]
            return UNKNOWN if any(item is UNKNOWN for item in items) else items
        if isinstance(node, ast.JoinedStr):
            return self._joined(path, node, depth)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return self._concatenate(self.value(path, node.left, depth + 1), self.value(path, node.right, depth + 1))
        if isinstance(node, ast.Name):
            return self._name(path, node.id, depth)
        if isinstance(node, ast.Attribute):
            return self._attribute(path, node, depth)
        return UNKNOWN

    def string(self, path: str, node: ast.expr | None) -> str | None:
        """식을 문자열로 평가한다.

        Args:
            path: 모듈 경로.
            node: 식.

        Returns:
            문자열, 아니면 None.
        """
        result = self.value(path, node)
        return result if isinstance(result, str) else None

    def string_list(self, path: str, node: ast.expr | None) -> list[str] | None:
        """식을 문자열 목록으로 평가한다.

        Args:
            path: 모듈 경로.
            node: 식.

        Returns:
            문자열 목록, 아니면 None.
        """
        result = self.value(path, node)
        if isinstance(result, list) and all(isinstance(item, str) for item in result):
            return [str(item) for item in result]
        return None

    def _joined(self, path: str, node: ast.JoinedStr, depth: int) -> object:
        """f-string을 평가한다. 형식 지정자가 없는 상수 보간만 허용한다.

        Args:
            path: 모듈 경로.
            node: f-string 식.
            depth: 깊이.

        Returns:
            문자열 또는 `UNKNOWN`.
        """
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
                continue
            if not isinstance(value, ast.FormattedValue) or value.format_spec is not None or value.conversion != -1:
                return UNKNOWN
            inner = self.value(path, value.value, depth + 1)
            if not isinstance(inner, str):
                return UNKNOWN
            parts.append(inner)
        return "".join(parts)

    @staticmethod
    def _concatenate(left: object, right: object) -> object:
        """두 값을 `+`로 잇는다(문자열끼리, 목록끼리만).

        Args:
            left: 왼쪽 값.
            right: 오른쪽 값.

        Returns:
            이은 값 또는 `UNKNOWN`.
        """
        if isinstance(left, str) and isinstance(right, str):
            return left + right
        if isinstance(left, list) and isinstance(right, list):
            return left + right
        return UNKNOWN

    def _name(self, path: str, name: str, depth: int) -> object:
        """모듈 수준 이름을 따라 평가한다.

        Args:
            path: 모듈 경로.
            name: 이름.
            depth: 깊이.

        Returns:
            값 또는 `UNKNOWN`.
        """
        symbol = self.symbols.resolve_name(path, name)
        if isinstance(symbol, ValueSymbol):
            return self.value(symbol.path, symbol.node, depth + 1)
        return UNKNOWN

    def _attribute(self, path: str, node: ast.Attribute, depth: int) -> object:
        """`settings.X`나 다른 모듈의 상수를 평가한다.

        Args:
            path: 모듈 경로.
            node: 속성 접근 식.
            depth: 깊이.

        Returns:
            값 또는 `UNKNOWN`.
        """
        base = self.symbols.resolve_expr(path, node.value)
        if base is not None and getattr(base, "dotted", None) == "django.conf.settings":
            return self.settings.get(node.attr, UNKNOWN)
        symbol = self.symbols.resolve_expr(path, node)
        if isinstance(symbol, ValueSymbol):
            return self.value(symbol.path, symbol.node, depth + 1)
        return UNKNOWN
