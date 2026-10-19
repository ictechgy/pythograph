"""정적 상수 평가: 문자열·불리언·숫자·그 목록을 실행 없이 구한다.

라우트 문자열·method 목록·설정값처럼 리터럴이거나 리터럴에서 곧장 얻는 값만 구한다. 구하지 못하면
`UNKNOWN`을 돌려주고 호출자가 dynamic·limitation으로 처리한다. 추측하지 않는다.

같은 이름을 여러 번 참조하는 f-string·`+`·목록 사슬(`B = f"{A}{A}…"`, `C = f"{B}{B}…"`)은 짧은 소스로도 결과
크기와 평가 횟수가 단계마다 곱으로 늘어난다. 그래서 이어 붙이거나 모아 만든 값에 크기 상한
(`MAX_EVALUATED_LENGTH`)을 두고, 같은 (모듈, 식, 깊이)의 작은 결과는 메모해 다시 펼치지 않는다.
"""

from __future__ import annotations

import ast
from typing import Final

from pythograph.source.symbols import SymbolTable, ValueSymbol

#: 평가하지 못한 값을 뜻하는 표식이다.
UNKNOWN: Final = object()

#: 이름을 따라 평가할 최대 깊이다(순환 방지).
MAX_EVALUATION_DEPTH = 12

#: f-string·`+`·목록 리터럴로 만든 값의 최대 크기다(`value_size`: 문자열은 문자 수, 목록은 원소 수와 원소 크기의
#: 합). 넘으면 `UNKNOWN`이다. 실제 설정값·경로·SQL 문자열(수백~수천 자)보다 충분히 크고, 라우트 템플릿 상한
#: (2,048자)의 32배다. 소스의 문자열 리터럴 하나는 파일 크기 상한(4 MiB) 안이라 그대로 둔다.
MAX_EVALUATED_LENGTH = 64 * 1024

#: 메모에 담는 결과의 최대 크기(`value_size`)다. 메모가 상한 가까운 값을 식마다 붙잡아 소스보다 수천 배 큰
#: 메모리를 쓰지 않게, 큰 결과는 담지 않고 다시 계산한다. 이어 붙이기는 상한을 넘는 즉시 멈추므로(조기 중단),
#: 큰 결과를 다시 계산하는 횟수는 결과 하나에 대략 `MAX_EVALUATED_LENGTH / MAX_MEMO_LENGTH`번을 넘지 않는다.
MAX_MEMO_LENGTH = 1024

#: 크기를 함께 든 평가 결과 (값, `value_size`)다.
Sized = tuple[object, int]


def value_size(value: object, limit: int = MAX_EVALUATED_LENGTH) -> int:
    """평가 결과의 크기를 잰다: 문자열은 문자 수, 목록은 원소 수와 원소 크기의 합, 그 밖은 0이다.

    한 목록이 여러 번 담겼으면 나올 때마다 센다(펼친 크기). 메모가 공유하는 하위 목록도 소비자가 펼치면 그만큼
    일하기 때문이다.

    Args:
        value: 평가 결과.
        limit: 이보다 커지면 더 세지 않는다.

    Returns:
        크기, `limit`을 넘으면 `limit + 1`.
    """
    total = 0
    pending: list[object] = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            total += len(current)
        elif isinstance(current, list):
            total += len(current)
            if total <= limit:
                pending.extend(current)
        if total > limit:
            return limit + 1
    return total


class Evaluator:
    """모듈 수준 이름을 따라가며 상수를 평가한다.

    결과는 (모듈 경로, 식, 깊이)마다 메모한다(크기가 `MAX_MEMO_LENGTH` 이하인 결과만). 깊이를 키에 넣는 것은 깊이
    상한에서 잘린 결과를 메모 없이 평가한 결과와 같게 두기 위해서다. 메모한 목록은 호출자 사이에 공유되므로 고치지
    않는다.
    """

    def __init__(self, symbols: SymbolTable, overrides: dict[str, object] | None = None) -> None:
        """평가기를 만든다.

        Args:
            symbols: 이름 해석기.
            overrides: `settings.<NAME>`처럼 외부에서 준 값(예: Django 설정 모듈에서 읽은 값). 평가기를 만든 뒤
                바꾸지 않는다(메모가 그 값을 담는다).
        """
        self.symbols = symbols
        self.settings = overrides or {}
        # 키의 id는 값에 함께 담은 노드가 살아 있는 동안 다른 노드에 다시 쓰이지 않는다.
        self._memo: dict[tuple[str, int, int], tuple[ast.expr, object, int]] = {}
        self._setting_sizes: dict[str, int] = {}

    def value(self, path: str, node: ast.expr | None, depth: int = 0) -> object:
        """식을 상수로 평가한다.

        Args:
            path: 식이 있는 모듈 경로.
            node: 식.
            depth: 이름을 따라간 깊이.

        Returns:
            `str`·`bool`·`int`·`float`·`None`·그 `list`, 모르면 `UNKNOWN`. f-string·`+`·목록 리터럴로 만든 값이
            `MAX_EVALUATED_LENGTH`를 넘어도 `UNKNOWN`이다.
        """
        return self._sized(path, node, depth)[0]

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

    def _sized(self, path: str, node: ast.expr | None, depth: int) -> Sized:
        """`value`와 같되 결과 크기(`value_size`)도 돌려주고, 작은 결과를 메모한다.

        Args:
            path: 식이 있는 모듈 경로.
            node: 식.
            depth: 이름을 따라간 깊이.

        Returns:
            (값 또는 `UNKNOWN`, 크기).
        """
        if node is None or depth > MAX_EVALUATION_DEPTH:
            return UNKNOWN, 0
        key = (path, id(node), depth)
        cached = self._memo.get(key)
        if cached is not None and cached[0] is node:
            return cached[1], cached[2]
        result, size = self._evaluate(path, node, depth)
        if size <= MAX_MEMO_LENGTH:
            self._memo[key] = (node, result, size)
        return result, size

    def _evaluate(self, path: str, node: ast.expr, depth: int) -> Sized:
        """식 하나를 메모 없이 평가한다.

        Args:
            path: 식이 있는 모듈 경로.
            node: 식.
            depth: 이름을 따라간 깊이.

        Returns:
            (값 또는 `UNKNOWN`, 크기).
        """
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                return node.value, len(node.value)
            return (node.value, 0) if isinstance(node.value, (bool, int, float, type(None))) else (UNKNOWN, 0)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return self._items(path, node.elts, depth)
        if isinstance(node, ast.JoinedStr):
            return self._joined(path, node, depth)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return _concatenate(self._sized(path, node.left, depth + 1), self._sized(path, node.right, depth + 1))
        if isinstance(node, ast.Name):
            return self._name(path, node.id, depth)
        if isinstance(node, ast.Attribute):
            return self._attribute(path, node, depth)
        return UNKNOWN, 0

    def _items(self, path: str, elements: list[ast.expr], depth: int) -> Sized:
        """목록·튜플·집합 리터럴의 원소를 평가한다.

        원소 하나라도 모르거나 크기가 `MAX_EVALUATED_LENGTH`를 넘는 순간 나머지 원소를 평가하지 않고 멈춘다.

        Args:
            path: 모듈 경로.
            elements: 원소 식.
            depth: 깊이.

        Returns:
            (원소 목록 또는 `UNKNOWN`, 크기).
        """
        items: list[object] = []
        size = len(elements)
        for element in elements:
            item, item_size = self._sized(path, element, depth + 1)
            size += item_size
            if item is UNKNOWN or size > MAX_EVALUATED_LENGTH:
                return UNKNOWN, 0
            items.append(item)
        return items, size

    def _joined(self, path: str, node: ast.JoinedStr, depth: int) -> Sized:
        """f-string을 평가한다. 형식 지정자가 없는 상수 보간만 허용한다.

        길이가 `MAX_EVALUATED_LENGTH`를 넘는 순간 나머지 조각을 평가하지 않고 멈춘다.

        Args:
            path: 모듈 경로.
            node: f-string 식.
            depth: 깊이.

        Returns:
            (문자열 또는 `UNKNOWN`, 크기).
        """
        parts: list[str] = []
        length = 0
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                text = value.value
            elif not isinstance(value, ast.FormattedValue) or value.format_spec is not None or value.conversion != -1:
                return UNKNOWN, 0
            else:
                inner = self._sized(path, value.value, depth + 1)[0]
                if not isinstance(inner, str):
                    return UNKNOWN, 0
                text = inner
            length += len(text)
            if length > MAX_EVALUATED_LENGTH:
                return UNKNOWN, 0
            parts.append(text)
        return "".join(parts), length

    def _name(self, path: str, name: str, depth: int) -> Sized:
        """모듈 수준 이름을 따라 평가한다.

        Args:
            path: 모듈 경로.
            name: 이름.
            depth: 깊이.

        Returns:
            (값 또는 `UNKNOWN`, 크기).
        """
        symbol = self.symbols.resolve_name(path, name)
        if isinstance(symbol, ValueSymbol):
            return self._sized(symbol.path, symbol.node, depth + 1)
        return UNKNOWN, 0

    def _attribute(self, path: str, node: ast.Attribute, depth: int) -> Sized:
        """`settings.X`나 다른 모듈의 상수를 평가한다.

        Args:
            path: 모듈 경로.
            node: 속성 접근 식.
            depth: 깊이.

        Returns:
            (값 또는 `UNKNOWN`, 크기).
        """
        base = self.symbols.resolve_expr(path, node.value)
        if base is not None and getattr(base, "dotted", None) == "django.conf.settings":
            return self._setting(node.attr)
        symbol = self.symbols.resolve_expr(path, node)
        if isinstance(symbol, ValueSymbol):
            return self._sized(symbol.path, symbol.node, depth + 1)
        return UNKNOWN, 0

    def _setting(self, name: str) -> Sized:
        """외부에서 준 설정값과 그 크기를 돌려준다(크기는 이름마다 한 번 잰다).

        Args:
            name: 설정 이름.

        Returns:
            (값 또는 `UNKNOWN`, 크기).
        """
        value = self.settings.get(name, UNKNOWN)
        if name not in self._setting_sizes:
            self._setting_sizes[name] = value_size(value)
        return value, self._setting_sizes[name]


def _concatenate(left: Sized, right: Sized) -> Sized:
    """두 값을 `+`로 잇는다(문자열끼리, 목록끼리만).

    Args:
        left: 왼쪽 (값, 크기).
        right: 오른쪽 (값, 크기).

    Returns:
        (이은 값, 크기). 종류가 맞지 않거나 크기가 `MAX_EVALUATED_LENGTH`를 넘으면 (`UNKNOWN`, 0).
    """
    (left_value, left_size), (right_value, right_size) = left, right
    size = left_size + right_size
    if size > MAX_EVALUATED_LENGTH:
        return UNKNOWN, 0
    if isinstance(left_value, str) and isinstance(right_value, str):
        return left_value + right_value, size
    if isinstance(left_value, list) and isinstance(right_value, list):
        return left_value + right_value, size
    return UNKNOWN, 0
