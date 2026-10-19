"""SQL 관계 추출기 테스트.

기대값은 tsograph `sql-relations.test.ts`(cartograph `SqlRelationsTests`·dartograph `sql_relations_test`와 같은
공유 벡터)를 그대로 옮겼다. 가족 생산자가 같은 SQL을 같게 읽어야 isthmus 조인 결과가 생산자 언어와 무관해진다.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from pythograph.persistence import sql as sql_module
from pythograph.persistence.sql import (
    SqlToken,
    escape_name,
    escape_qualified,
    lex_sql,
    looks_like_sql,
    sql_relations,
)


def relations(sql: str, strict: bool = False) -> list[str]:
    """관계 이름만 뽑는다.

    Args:
        sql: SQL 텍스트.
        strict: 대문자 게이트 여부.

    Returns:
        관계 이름 목록.
    """
    return [relation.name for relation in sql_relations(sql, strict).relations]


def test_reads_basic_relation_keywords() -> None:
    """기본 관계 키워드의 피연산자를 읽는다."""
    assert relations("SELECT * FROM users") == ["users"]
    assert relations("SELECT * FROM users JOIN orders ON true") == ["users", "orders"]
    assert relations("INSERT INTO users (id) VALUES (1)") == ["users"]
    assert relations("UPDATE users SET name = 'x'") == ["users"]
    assert relations("DELETE FROM users") == ["users"]
    assert relations("SELECT * FROM s.t") == ["s.t"]
    assert relations("SELECT * FROM `a.b`") == ["a%2Eb"]


def test_reads_comma_lists_and_aliases() -> None:
    """쉼표 목록과 별칭을 읽는다."""
    assert relations("SELECT * FROM a, b") == ["a", "b"]
    assert relations("SELECT * FROM a x, b y") == ["a", "b"]
    assert relations("SELECT * FROM a AS x, b AS y") == ["a", "b"]


def test_fires_only_at_statement_boundaries() -> None:
    """문장 경계에서만 발화한다."""
    assert relations("UPDATE a SET x = 1; UPDATE b SET y = 2") == ["a", "b"]
    assert relations("please update the config") == []
    assert relations("UPDATE t SET x = 1") == ["t"]
    assert relations("GRANT SELECT ON TABLE metrics TO app") == ["metrics"]
    assert relations("GRANT SELECT ON metrics TO app") == ["metrics"]
    assert relations("REVOKE SELECT ON FUNCTION f FROM r") == []
    assert relations("grant select on the report to auditors") == []
    assert relations("grant access on staging to intern") == []
    assert relations("markAdult:\nUPDATE users SET adult = 1") == ["users"]
    assert relations("clearAll:\nTRUNCATE users") == ["users"]
    assert relations("SELECT x::int FROM t") == ["t"]


def test_prose_makes_no_relations() -> None:
    """산문은 관계를 만들지 않는다."""
    assert relations("the report into the folder") == []
    assert relations("merged the branch into main") == []
    assert relations("drop the table at noon") == []
    assert relations("turn the table over") == []


def test_follows_subqueries_and_parentheses() -> None:
    """하위 질의와 괄호를 따라간다."""
    assert relations("SELECT * FROM (SELECT * FROM a) x JOIN b ON true") == ["a", "b"]
    assert relations("INSERT INTO a SELECT * FROM ignored_c") == ["a", "ignored_c"]


def test_counts_unresolved_operands() -> None:
    """미해석 피연산자는 개수로 센다."""
    deleted = sql_relations("DELETE FROM {} WHERE id = ?")
    assert deleted.relations == ()
    assert deleted.unresolved == 1
    assert sql_relations("SELECT * FROM users").unresolved == 0
    assert sql_relations("SELECT 1 FROM").unresolved == 1
    assert sql_relations("SELECT * FROM {} JOIN ?").unresolved == 2


def test_sql_shape_gate_filters_prose() -> None:
    """SQL 형태 게이트가 산문을 걸러낸다."""
    assert looks_like_sql("SELECT * FROM t")
    assert looks_like_sql("update t set x = 1")
    assert not looks_like_sql("please update the config")
    assert not looks_like_sql("a plain sentence")
    assert not looks_like_sql("")


def test_strict_mode_rejects_prose_and_lowercase() -> None:
    """strict 모드는 산문과 소문자 키워드를 거부한다."""
    assert not looks_like_sql("Select an option from the menu", True)
    assert looks_like_sql("SELECT an option FROM the menu", True)
    assert relations("Select an option from the menu", True) == []
    assert relations("select * from users", True) == []
    assert relations("SELECT a FROM the") == []
    assert relations("select * from users") == ["users"]


def test_keyword_offset_is_string_index() -> None:
    """키워드 위치는 파이썬 문자열 인덱스다."""
    result = sql_relations("-- 한글 주석\nSELECT * FROM users")
    assert result.relations[0].keyword == len("-- 한글 주석\nSELECT * ")


def test_escape_distinguishes_qualifier_and_segment() -> None:
    """이름 escape는 한정자와 한 세그먼트를 구분한다."""
    assert escape_qualified("main.users") == "main.users"
    assert escape_qualified("100%.t") == "100%25.t"
    assert escape_name("a.b") == "a%2Eb"


def test_lexes_comments_strings_and_quoted_identifiers() -> None:
    """주석·문자열·인용 식별자를 어휘로 올바르게 나눈다(포트 분기 회귀)."""
    tokens = lex_sql("/* c */ SELECT 'it''s \\' x' FROM [dbo].\"T\" -- tail")
    assert [token.text for token in tokens] == ["SELECT", "FROM", "dbo", ".", "T"]
    assert relations("SELECT * FROM [dbo].[Order Items]") == ["dbo.Order Items"]
    assert relations('SELECT * FROM "100%"') == ["100%25"]
    assert relations("SELECT 'unterminated FROM x") == []


def test_reads_ddl_and_index_trigger_targets() -> None:
    """DDL과 인덱스·트리거의 ON 대상을 읽는다(포트 분기 회귀)."""
    assert relations("CREATE TABLE IF NOT EXISTS t (id int)") == ["t"]
    assert relations("ALTER TABLE ONLY public.t ADD COLUMN x int") == ["public.t"]
    assert relations("TRUNCATE TABLE a, b") == ["a", "b"]
    assert relations("CREATE INDEX i ON t (x)") == ["t"]
    assert relations("CREATE RULE r AS ON INSERT TO t DO NOTHING") == []
    assert relations("SELECT * FROM a JOIN b ON a.id = b.id") == ["a", "b"]
    assert relations("CREATE TABLE") == []
    assert sql_relations("CREATE TABLE").unresolved == 1
    assert relations("DROP TABLE t", True) == ["t"]
    assert relations("drop TABLE t", True) == []


def test_follows_grant_object_kinds() -> None:
    """GRANT 객체 종류와 형태 검사를 따른다(포트 분기 회귀)."""
    assert relations("GRANT SELECT ON TABLES a, b TO r") == ["a", "b"]
    assert relations("GRANT SELECT ON a, b") == ["a", "b"]
    assert relations("GRANT SELECT ON a WITH GRANT OPTION") == []
    assert relations("GRANT EXECUTE ON FUNCTION f(int) TO r") == []
    assert sql_relations("GRANT USAGE ON SEQUENCE {s} TO r").unresolved == 1
    assert sql_relations("GRANT EXECUTE ON FUNCTION f(int TO r").unresolved == 1
    assert relations('GRANT SELECT ON "quoted" TO r') == ["quoted"]
    assert relations("GRANT SELECT ON TABLE") == []
    assert relations("SELECT * FROM a UNION SELECT * FROM b") == ["a", "b"]


def test_handles_unclosed_parentheses_and_duplicates() -> None:
    """닫히지 않은 괄호와 중복 피연산자를 처리한다(포트 분기 회귀)."""
    assert sql_relations("SELECT * FROM (SELECT 1").unresolved == 1
    assert relations("SELECT * FROM a, a") == ["a"]
    assert relations('SELECT * FROM a."b.c"') == ["a.b%2Ec"]
    assert relations("SELECT * FROM a.where") == ["a"]
    assert relations('SELECT * FROM ""') == []
    assert relations('SELECT * FROM "a" AS') == ["a"]


def test_table_valued_functions_are_not_table_names() -> None:
    """테이블 값 함수는 관계로 추측하지 않고 미해석으로 센다."""
    assert relations("SELECT * FROM pragma_table_info('t')") == []
    assert relations("SELECT * FROM users JOIN main.pragma_table_info('t') p ON true") == ["users"]
    assert relations("SELECT * FROM pragma_table_info('t') AS p, users") == ["users"]
    assert sql_relations("SELECT * FROM pragma_table_info('t')").unresolved == 1
    assert relations("INSERT INTO users (id) VALUES (1)") == ["users"]


def test_semicolon_tokens_bound_the_keyword_context() -> None:
    """INTO·UPDATE·ON 판정이 보는 문장 범위는 `;` 토큰에서 끝난다. 인용 식별자 `";"`·`[;]`도 범위를 끊는다
    (포트 원본 그대로, 문장 머리 표시와는 다르다)."""
    assert relations("SELECT 1 INTO t") == ["t"]
    assert relations("SELECT 1; INTO t") == []
    assert relations('SELECT 1 ";" INTO t') == []
    assert relations("SELECT 1 [;] INTO t") == []
    assert relations('INSERT "x" INTO t') == ["t"]
    assert relations("UPDATE t SET x = 1") == ["t"]
    assert relations("UPDATE t; SET x = 1") == []
    assert relations('UPDATE t ";" SET x = 1') == []
    assert relations('UPDATE t "SET" x = 1') == []
    assert relations("UPDATE t set x = 1", True) == []
    assert relations("UPDATE a SET x = (SELECT y FROM b); UPDATE c SET z = 1") == ["a", "b", "c"]
    assert relations('CREATE INDEX i ";" ON t (x)') == []
    assert relations("CREATE index i ON t (x)", True) == []
    assert relations('GRANT SELECT ";" ON t TO r') == []
    assert relations("GRANT select ON t TO r", True) == []
    assert relations('SELECT * FROM (SELECT * FROM a ")" ) x, b') == ["b", "a"]


class _CountingTokens(list[SqlToken]):
    """읽은 토큰 수를 세는 토큰 목록. 위치 읽기, 슬라이스(복사한 길이), 순회를 모두 센다."""

    def __init__(self, tokens: list[SqlToken]) -> None:
        """토큰을 담고 계수를 0으로 둔다.

        Args:
            tokens: 어휘 목록.
        """
        super().__init__(tokens)
        self.reads = 0

    def __getitem__(self, key: Any) -> Any:
        """읽은 토큰 수를 더하고 목록과 같게 돌려준다."""
        result = super().__getitem__(key)
        self.reads += len(result) if isinstance(key, slice) else 1
        return result

    def __iter__(self) -> Iterator[SqlToken]:
        """순회한 토큰 수를 더한다."""
        for token in super().__iter__():
            self.reads += 1
            yield token


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT " + "INTO " * 2_000,
        "CREATE " + "ON " * 2_000,
        "GRANT SELECT " + "ON " * 2_000,
        "UPDATE t SET x = 1; " * 1_000,
        "SELECT " + "FROM ( " * 2_000,
        "SELECT * FROM (" * 1_000 + "SELECT 1" + ")" * 1_000,
        "GRANT ALL " + "ON FUNCTION f ( " * 1_000,
    ],
    ids=["into", "create-on", "grant-on", "update-set", "unclosed-parens", "nested-subqueries", "grant-object-parens"],
)
def test_scan_reads_each_token_a_bounded_number_of_times(sql: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """관계 스캔은 키워드마다 문장·짝 괄호를 다시 훑지 않는다(읽는 토큰 수가 토큰 수에 비례).

    세미콜론 없는 긴 문장에서 INTO·ON·UPDATE마다 문장 앞뒤를, `(` 피연산자마다 짝 괄호까지 다시 훑으면 대문자 SQL
    리터럴 하나로 schema가 입력 길이의 제곱 시간을 썼다(INTO 1만 개 6초, 4 MiB면 십여 시간).
    """
    counted: list[_CountingTokens] = []
    lex = sql_module.lex_sql

    def counting_lex(text: str) -> list[SqlToken]:
        tokens = _CountingTokens(lex(text))
        counted.append(tokens)
        return tokens

    monkeypatch.setattr(sql_module, "lex_sql", counting_lex)
    sql_relations(sql, True)
    (tokens,) = counted
    # 지금은 토큰마다 6~15번 읽는다. 수정 전에는 이 크기에서 토큰마다 250~1,500번이었다.
    assert tokens.reads <= 64 * len(tokens)
