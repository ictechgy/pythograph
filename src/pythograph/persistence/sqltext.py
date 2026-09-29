"""SQL 텍스트 사실: 명시 SQL 인자(sink)와 대문자 SQL 리터럴.

- 명시 sink(`text()`, `raw()`, `RawSQL`, `exec_driver_sql`, DB 패키지를 import한 모듈의 `execute`·`executemany`·
  `executescript`)의 인자는 SQL로 읽는다(소문자 SQL도 인정). 리터럴·모듈 상수·같은 함수의 상수 대입·f-string·
  `+`·`%`·`.format()`으로 만든 문자열은 알 수 없는 조각을 `{}` 플레이스홀더로 바꿔 읽는다. 관계 자리의
  플레이스홀더는 가족 추출기 규칙대로 dynamic 사실 하나가 된다.
- DB-API 파라미터 표기(`%s`, `%(name)s`)는 값 자리 플레이스홀더라 `?`로 바꾼다. 그대로 두면 `FROM %s`의 `s`가
  관계 이름으로 읽힌다(파이썬 전용 전처리, 공유 벡터 밖).
- 그 밖의 문자열 리터럴은 SQL 동사와 관계 키워드가 대문자일 때만 읽는다(가족 strict 규칙). docstring은 읽지
  않는다. 소문자라 읽지 않은 SQL 모양 리터럴은 `skipped-sql-literals:`로 센다.
"""

from __future__ import annotations

import ast
import re

from pythograph.persistence.location import fact_location
from pythograph.persistence.model import PersistenceExtraction, RelationUse
from pythograph.persistence.scope import ModuleScopes
from pythograph.persistence.sql import looks_like_sql, sql_relations
from pythograph.routes.model import Location
from pythograph.source.evaluate import Evaluator
from pythograph.source.symbols import ValueSymbol

#: DB-API 파라미터 표기다(`%s`, `%(name)s`, `%d` 등). `%%`는 글자 그대로의 `%`다.
_PARAMETER = re.compile(r"%\([^)]*\)[sdifr]|%[sdifr]")

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
        result = sql_relations(_PARAMETER.sub("?", text), strict)
        location = self._location(node)
        symbol = self.scopes.symbol_for(node)
        for relation in result.relations:
            # SQL 추출기가 이미 조각별로 escape한 이름이다.
            self.extraction.uses.append(RelationUse(relation.name, None, False, location, symbol))
        for _ in range(result.unresolved):
            self.extraction.add_dynamic(
                truncate(text), location, symbol, "{count} SQL relation operands are not literal names"
            )

    def text(self, node: ast.expr, depth: int = 0) -> str | None:
        """식을 SQL 텍스트로 바꾼다. 모르는 조각은 `{}`다. 전체를 모르면 None이다.

        Args:
            node: 식.
            depth: 이름을 따라간 깊이.

        Returns:
            텍스트 또는 None.
        """
        if depth > 8:
            return None
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.JoinedStr):
            return "".join(self._joined_part(part, depth) for part in node.values)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            return self._binary(node, depth)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            return self.text(node.func.value, depth + 1)
        if isinstance(node, ast.Name):
            return self._name_text(node, depth)
        if isinstance(node, ast.Attribute):
            return self.evaluator.string(self.scopes.path, node)
        return None

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
            텍스트 또는 None.
        """
        left = self.text(node.left, depth + 1)
        if isinstance(node.op, ast.Mod):
            # 문자열 보간이라 `%s`는 값이 아니라 SQL 조각이다. 알 수 없는 조각으로 바꾼다.
            return None if left is None else _PARAMETER.sub(_HOLE, left)
        right = self.text(node.right, depth + 1)
        if left is None and right is None:
            return None
        return (left if left is not None else _HOLE) + (right if right is not None else _HOLE)

    def _name_text(self, node: ast.Name, depth: int) -> str | None:
        """이름을 같은 함수의 유일한 대입이나 모듈 상수로 푼다.

        같은 모듈 안의 값 식은 대문자 리터럴 스캔에서 빼서(이미 SQL로 읽었다) 같은 SQL을 두 번 내지 않는다.

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
        if isinstance(symbol, ValueSymbol) and symbol.path == self.scopes.path:
            self._consume(symbol.node)
            return self.text(symbol.node, depth + 1)
        return self.evaluator.string(self.scopes.path, node)

    def fallback(self, tree: ast.Module) -> None:
        """sink가 아닌 문자열 리터럴 중 대문자 SQL을 읽는다(docstring 제외).

        Args:
            tree: 모듈 구문 트리.
        """
        docstrings = _docstring_ids(tree)
        for node in ast.walk(tree):
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
            elif looks_like_sql(text) and sql_relations(_PARAMETER.sub("?", text)).relations:
                self.extraction.add_gap(
                    "skipped-sql-literals:", "{count} lowercase SQL-looking string literals were not read"
                )

    def _consume(self, node: ast.AST) -> None:
        """노드와 그 하위 문자열을 대문자 리터럴 스캔에서 뺀다.

        Args:
            node: 노드.
        """
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


def _docstring_ids(tree: ast.Module) -> set[int]:
    """모듈·클래스·함수 docstring 상수 노드의 id를 모은다.

    Args:
        tree: 모듈 구문 트리.

    Returns:
        id 집합.
    """
    found: set[int] = set()
    for node in ast.walk(tree):
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
