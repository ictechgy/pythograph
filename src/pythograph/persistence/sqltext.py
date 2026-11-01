"""SQL 텍스트 사실: 명시 SQL 인자(sink)와 대문자 SQL 리터럴.

- 명시 sink(`text()`, `raw()`, `RawSQL`, `exec_driver_sql`, DB 패키지를 import한 모듈의 `execute`·`executemany`·
  `executescript`)의 인자는 SQL로 읽는다(소문자 SQL도 인정). 리터럴·모듈 상수·같은 함수의 상수 대입·f-string·
  `+`·`%`·`.format()`으로 만든 문자열은 알 수 없는 조각을 `{}` 플레이스홀더로 바꿔 읽는다. 관계 자리의
  플레이스홀더는 가족 추출기 규칙대로 dynamic 사실 하나가 된다.
- DB-API 파라미터 표기(`%s`, `%(name)s`)는 값 자리 플레이스홀더라 `?`로 바꾼다. 그대로 두면 `FROM %s`의 `s`가
  관계 이름으로 읽힌다(파이썬 전용 전처리, 공유 벡터 밖).
- 그 밖의 문자열 리터럴은 SQL 동사와 관계 키워드가 대문자일 때만 읽는다(가족 strict 규칙). docstring은 읽지
  않는다. 소문자라 읽지 않은 SQL 모양 리터럴은 `skipped-sql-literals:`로 센다.
- 이어 붙인 텍스트(f-string·`+`)가 `MAX_EVALUATED_LENGTH`(64 KiB)를 넘으면 그 식은 모르는 텍스트다. 같은 상수를 여러
  번 참조하는 사슬(`B = f"{A}{A}…"`)이 짧은 소스로도 길이를 단계마다 곱으로 늘리기 때문이다. 같은 (식, 깊이)의
  짧은 결과는 메모해 다시 펼치지 않는다.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

from pythograph.persistence.location import fact_location
from pythograph.persistence.model import PersistenceExtraction, RelationUse
from pythograph.persistence.scope import ModuleScopes
from pythograph.persistence.sql import looks_like_sql, sql_relations
from pythograph.routes.model import Location
from pythograph.source.evaluate import MAX_EVALUATED_LENGTH, MAX_MEMO_LENGTH, Evaluator
from pythograph.source.symbols import ValueSymbol

#: DB-API 파라미터 표기(`%s`, `%(name)s`, `%d` 등)의 서식 문자다. `replace_parameters`가 이 표기를 찾는다.
_CONVERSIONS = frozenset("sdifr")

#: 알 수 없는 조각을 나타내는 플레이스홀더다.
_HOLE = "{}"

#: dynamic 사실 channel에 싣는 원문의 최대 길이다.
MAX_EXPRESSION_LENGTH = 200


class SqlText:
    """모듈 하나의 SQL 텍스트 사실을 모은다."""

    def __init__(self, extraction: PersistenceExtraction, scopes: ModuleScopes, evaluator: Evaluator) -> None:
        """수집기를 만든다.

        Args:
            extraction: 결과를 채울 추출 결과.
            scopes: 모듈 범위 도우미.
            evaluator: 모듈 상수 평가기.
        """
        self.extraction = extraction
        self.scopes = scopes
        self.evaluator = evaluator
        self.consumed: set[int] = set()
        # (식 id, 깊이) → (식, 텍스트). 키의 id는 값에 함께 담은 노드가 살아 있는 동안 다른 노드에 다시 쓰이지 않는다.
        self._texts: dict[tuple[int, int], tuple[ast.expr, str | None]] = {}
        # 상한을 넘은 식의 id다(식마다 한계를 한 번 센다).
        self._too_long_nodes: set[int] = set()

    def sink(self, node: ast.expr, explicit: bool) -> None:
        """SQL 인자 하나를 읽어 관계 사실을 낸다.

        Args:
            node: SQL 인자 식.
            explicit: ORM의 명시 SQL API인지. 명시 API면 읽지 못한 인자를 dynamic 사실로, 아니면(일반
                `execute`) `unresolved-sql-arguments:` 계수로 남긴다.
        """
        if id(node) in self.consumed:
            return
        self._consume(node)
        text = self.text(node)
        if text is None:
            if explicit:
                self.extraction.add_dynamic(
                    expression_text(node), self._location(node), self.scopes.symbol_for(node),
                    "{count} SQL arguments are not string literals",
                )  # fmt: skip
            elif not self._non_string(node):
                self.extraction.add_gap("unresolved-sql-arguments:", "{count} execute() arguments could not be read")
            return
        self.emit(text, node, strict=False)

    def _non_string(self, node: ast.expr) -> bool:
        """인자가 문자열이 아님을 구문으로 아는지 본다(호출·컨테이너, 또는 그런 값을 유일하게 대입한 지역 이름).

        `session.execute(select(...))`·`stmt = select(...); session.execute(stmt)`는 SQL 텍스트가 아니다.

        Args:
            node: 인자.

        Returns:
            문자열이 아니면 True.
        """
        if isinstance(node, (ast.Call, ast.Dict, ast.List, ast.Tuple, ast.Lambda, ast.Await)):
            return True
        if not isinstance(node, ast.Name):
            return False
        function = self.scopes.enclosing_function(node)
        binding = self.scopes.local(function, node.id) if function is not None else None
        return (
            binding is not None
            and binding.kind == "assign"
            and isinstance(binding.value, (ast.Call, ast.Dict, ast.List, ast.Tuple, ast.Await))
        )

    def emit(self, text: str, node: ast.AST, strict: bool) -> None:
        """SQL 텍스트의 관계를 사실로 낸다. 관계 자리의 플레이스홀더는 dynamic 사실이다.

        Args:
            text: SQL 텍스트.
            node: 위치로 쓸 노드.
            strict: 대문자 게이트 여부.
        """
        result = sql_relations(replace_parameters(text, "?"), strict)
        location = self._location(node)
        symbol = self.scopes.symbol_for(node)
        for relation in result.relations:
            # SQL 추출기가 이미 조각별로 escape한 이름이다.
            self.extraction.uses.append(RelationUse(relation.name, None, False, location, symbol))
        if not result.unresolved:
            return
        # 미해석 피연산자마다 같은 원문을 싣는다. 피연산자마다 텍스트 전체를 다시 줄이면 개수 × 길이가 든다.
        channel = truncate(text)
        for _ in range(result.unresolved):
            self.extraction.add_dynamic(
                channel, location, symbol, "{count} SQL relation operands are not literal names"
            )

    def text(self, node: ast.expr, depth: int = 0) -> str | None:
        """식을 SQL 텍스트로 바꾼다. 모르는 조각은 `{}`다. 전체를 모르면 None이다.

        이어 붙인 텍스트가 `MAX_EVALUATED_LENGTH`를 넘어도 None이다. 결과가 None이거나 `MAX_MEMO_LENGTH` 이하면
        (식, 깊이)마다 메모한다. 깊이를 키에 넣어 깊이 상한에서 잘린 결과를 메모 없는 결과와 같게 둔다.

        Args:
            node: 식.
            depth: 이름을 따라간 깊이.

        Returns:
            텍스트 또는 None.
        """
        if depth > 8:
            return None
        key = (id(node), depth)
        cached = self._texts.get(key)
        if cached is not None and cached[0] is node:
            return cached[1]
        result = self._text(node, depth)
        if result is None or len(result) <= MAX_MEMO_LENGTH:
            self._texts[key] = (node, result)
        return result

    def _text(self, node: ast.expr, depth: int) -> str | None:
        """`text`의 계산이다(메모 없이 식 하나를 바꾼다).

        Args:
            node: 식.
            depth: 이름을 따라간 깊이.

        Returns:
            텍스트 또는 None.
        """
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.JoinedStr):
            return self._joined(node, depth)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            return self._binary(node, depth)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            return self.text(node.func.value, depth + 1)
        if isinstance(node, ast.Name):
            return self._name_text(node, depth)
        if isinstance(node, ast.Attribute):
            return self.evaluator.string(self.scopes.path, node)
        return None

    def _joined(self, node: ast.JoinedStr, depth: int) -> str | None:
        """f-string을 텍스트로 바꾼다. 길이가 `MAX_EVALUATED_LENGTH`를 넘는 순간 나머지 조각을 보지 않고 멈춘다.

        Args:
            node: f-string.
            depth: 깊이.

        Returns:
            텍스트 또는 None(상한 초과).
        """
        pieces: list[str] = []
        length = 0
        for part in node.values:
            piece = self._joined_part(part, depth)
            length += len(piece)
            if length > MAX_EVALUATED_LENGTH:
                self._count_too_long(node)
                return None
            pieces.append(piece)
        return "".join(pieces)

    def _count_too_long(self, node: ast.expr) -> None:
        """이은 텍스트가 상한을 넘었음을 식마다 한 번 한계로 센다. 읽지 않은 텍스트가 관계를 가릴 수 있어 조용히
        넘기지 않는다.

        Args:
            node: 상한을 넘은 식(f-string 또는 `+`).
        """
        if id(node) not in self._too_long_nodes:
            self._too_long_nodes.add(id(node))
            self.extraction.add_gap(
                "skipped-sql-literals:", "{count} string expressions longer than 64 KiB were not read as SQL"
            )

    def _joined_part(self, part: ast.expr, depth: int) -> str:
        """f-string 조각 하나를 텍스트로 바꾼다.

        Args:
            part: 조각.
            depth: 깊이.

        Returns:
            텍스트(보간은 상수가 아니면 `{}`).
        """
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            return part.value
        if isinstance(part, ast.FormattedValue):
            inner = self.text(part.value, depth + 1)
            if inner is not None and _HOLE not in inner and part.format_spec is None and part.conversion == -1:
                return inner
        return _HOLE

    def _binary(self, node: ast.BinOp, depth: int) -> str | None:
        """`a + b`·`a % b` 문자열을 텍스트로 바꾼다.

        Args:
            node: 이항 식.
            depth: 깊이.

        Returns:
            텍스트 또는 None(둘 다 모르거나 이은 길이가 `MAX_EVALUATED_LENGTH`를 넘음).
        """
        left = self.text(node.left, depth + 1)
        if isinstance(node.op, ast.Mod):
            # 문자열 보간이라 `%s`는 값이 아니라 SQL 조각이다. 알 수 없는 조각으로 바꾼다.
            return None if left is None else replace_parameters(left, _HOLE)
        right = self.text(node.right, depth + 1)
        if left is None and right is None:
            return None
        left_text = left if left is not None else _HOLE
        right_text = right if right is not None else _HOLE
        if len(left_text) + len(right_text) > MAX_EVALUATED_LENGTH:
            self._count_too_long(node)
            return None
        return left_text + right_text

    def _name_text(self, node: ast.Name, depth: int) -> str | None:
        """이름을 같은 함수의 유일한 대입이나 모듈 상수로 푼다.

        같은 모듈 안의 값 식은 대문자 리터럴 스캔에서 빼서(이미 SQL로 읽었다) 같은 SQL을 두 번 내지 않는다. 정적 값으로
        증명되지 않은 모듈 이름(두 번 이상 묶임·`+=`·조건부 대입 등)은 모르는 텍스트이고, 그 값 식은 다른 리터럴처럼
        리터럴 스캔이 읽는다.

        Args:
            node: 이름.
            depth: 깊이.

        Returns:
            텍스트 또는 None.
        """
        function = self.scopes.enclosing_function(node)
        if function is not None and self.scopes.binds_locally(function, node.id):
            binding = self.scopes.local(function, node.id)
            if binding is None or binding.kind != "assign" or binding.value is None:
                return None
            self._consume(binding.value)
            return self.text(binding.value, depth + 1)
        symbol = self.evaluator.symbols.resolve_name(self.scopes.path, node.id)
        if (
            isinstance(symbol, ValueSymbol)
            and symbol.path == self.scopes.path
            and self.evaluator.symbols.proven(symbol)
        ):
            self._consume(symbol.node)
            return self.text(symbol.node, depth + 1)
        return self.evaluator.string(self.scopes.path, node)

    def fallback(self) -> None:
        """sink가 아닌 문자열 리터럴 중 대문자 SQL을 읽는다(docstring 제외)."""
        docstrings = _docstring_ids(self.scopes.nodes)
        for node in self.scopes.nodes:
            if id(node) in self.consumed or id(node) in docstrings:
                continue
            if isinstance(node, ast.JoinedStr):
                self._consume(node)
                text = self.text(node)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
            else:
                continue
            if text is None:
                continue
            if looks_like_sql(text, strict=True):
                self.emit(text, node, strict=True)
            elif looks_like_sql(text) and sql_relations(replace_parameters(text, "?")).relations:
                self.extraction.add_gap(
                    "skipped-sql-literals:", "{count} lowercase SQL-looking string literals were not read"
                )

    def _consume(self, node: ast.AST) -> None:
        """노드와 그 하위 문자열을 대문자 리터럴 스캔에서 뺀다.

        이미 뺀 노드는 하위 트리도 모두 빠져 있다(`consumed`는 이 메서드만 하위 트리째 채운다). 그래서 같은 상수를
        여러 번 참조해도 그 값 식을 한 번만 돈다.

        Args:
            node: 노드.
        """
        if id(node) in self.consumed:
            return
        for child in ast.walk(node):
            self.consumed.add(id(child))

    def _location(self, node: ast.AST) -> Location:
        """노드 위치를 만든다.

        Args:
            node: 노드.

        Returns:
            위치.
        """
        return fact_location(self.scopes, node)


def _docstring_ids(nodes: Iterable[ast.AST]) -> set[int]:
    """모듈·클래스·함수 docstring 상수 노드의 id를 모은다.

    Args:
        nodes: 모듈 전체 노드(`ModuleScopes.nodes`).

    Returns:
        id 집합.
    """
    found: set[int] = set()
    for node in nodes:
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def expression_text(node: ast.AST) -> str:
    """dynamic 사실 channel에 실을 원문 표현을 만든다(길이 제한).

    Args:
        node: 식.

    Returns:
        원문 표현.
    """
    try:
        text = ast.unparse(node)
    except (ValueError, TypeError, AttributeError, RecursionError):
        text = "<expression>"
    return truncate(text)


def truncate(text: str) -> str:
    """원문을 한 줄로 줄이고 최대 길이로 자른다.

    Args:
        text: 원문.

    Returns:
        줄인 문자열.
    """
    flat = " ".join(text.split())
    return flat if len(flat) <= MAX_EXPRESSION_LENGTH else flat[: MAX_EXPRESSION_LENGTH - 1] + "…"


def replace_parameters(text: str, replacement: str) -> str:
    """DB-API 파라미터 표기(`%s`, `%(name)s` 등)를 `replacement`로 바꾼다.

    정규식 `%\\([^)]*\\)[sdifr]|%[sdifr]`의 `re.sub`와 결과가 같다(`%%s`의 뒤 `%s`도 바꾸고, `%(` 뒤 이름은 줄을
    넘을 수 있다). 그 정규식은 `%(`마다 다음 `)`까지(없으면 끝까지) 다시 훑어, 닫히지 않은 `%(`가 많은 리터럴
    하나에서 길이의 제곱이 들었다. 여기서는 마지막으로 찾은 `)` 위치를 기억해 원문을 한 번만 훑는다.

    Args:
        text: SQL 텍스트.
        replacement: 표기 자리에 넣을 문자열.

    Returns:
        바꾼 텍스트.
    """
    pieces: list[str] = []
    copied = 0
    close = -1
    position = text.find("%")
    while position >= 0:
        end, close = _parameter_end(text, position, close)
        if end < 0:
            position = text.find("%", position + 1)
            continue
        pieces += (text[copied:position], replacement)
        copied = end
        position = text.find("%", end)
    pieces.append(text[copied:])
    return "".join(pieces)


def _parameter_end(text: str, position: int, close: int) -> tuple[int, int]:
    """`position`의 `%`에서 시작하는 파라미터 표기의 끝을 구한다.

    `%(`는 그 뒤 첫 `)` 다음 글자가 서식 문자일 때만 표기다(정규식의 `[^)]*`는 첫 `)`를 넘지 못한다). 첫 `)`는
    `position`이 커져도 뒤로만 가므로, 받은 `close`가 아직 뒤에 있으면 다시 찾지 않는다.

    Args:
        text: SQL 텍스트.
        position: `%` 위치.
        close: 앞서 찾은 `)` 위치(없었으면 텍스트 길이, 아직 찾지 않았으면 -1).

    Returns:
        (표기 끝 위치 또는 표기가 아니면 -1, 갱신한 `close`).
    """
    marker = text[position + 1 : position + 2]
    if marker != "(":
        return (position + 2 if marker in _CONVERSIONS else -1), close
    if close < position + 2:
        found = text.find(")", position + 2)
        close = found if found >= 0 else len(text)
    return (close + 2 if text[close + 1 : close + 2] in _CONVERSIONS else -1), close
