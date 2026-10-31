"""SQL 관계 추출기 테스트.

기대값은 tsograph `sql-relations.test.ts`(cartograph `SqlRelationsTests`·dartograph `sql_relations_test`와 같은
공유 벡터)를 그대로 옮겼다. 가족 생산자가 같은 SQL을 같게 읽어야 isthmus 조인 결과가 생산자 언어와 무관해진다.
CTE 이름·함수 인자 안의 FROM·`IS [NOT] DISTINCT FROM` 공통 규칙은 `test_conformance.py`가 벤더링한 정본으로 검증한다.
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


#: 공통 규칙 문구가 정하지 않은 경계의 해석이다(계열 gartograph·tsograph 판독과 같다). (SQL, 관계 집합, 미해석 수).
_BOUNDARY_CASES = [
    # R1의 함수 이름은 절 단어가 아닌 비인용 이름이고 R2가 알아본 CTE 본문 괄호는 함수 인자가 아니다. `materialized`는
    # 절 단어가 아니라서 이 제외가 없으면 본문 DML의 `t`까지 사라진다.
    pytest.param("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", ["t"], 0, id="cte-body-dml"),
    pytest.param(
        "WITH d AS MATERIALIZED (DELETE FROM t RETURNING *) SELECT * FROM d", ["t"], 0, id="materialized-body-dml"
    ),
    pytest.param(
        "WITH d AS NOT MATERIALIZED (DELETE FROM t RETURNING *) SELECT * FROM d",
        ["t"],
        0,
        id="not-materialized-body-dml",
    ),
    # R1은 FROM을 감싼 가장 안쪽 괄호만 본다. 함수 인자 안의 하위 질의는 그대로 관계를 낸다.
    pytest.param(
        "SELECT EXTRACT(YEAR FROM (SELECT max(d) FROM t)) FROM orders", ["t", "orders"], 0, id="nested-extract"
    ),
    # 절 단어(`JOIN`·`IN`·`AS` 등) 뒤 괄호는 함수 인자 괄호가 아니어서 그 안의 FROM은 지금처럼 관계를 연다.
    pytest.param("SELECT * FROM a JOIN (b FROM c) ON true", ["a", "c"], 0, id="clause-word-before-paren"),
    # 관계 자리가 아니므로 그 자리의 플레이스홀더도 미해석으로 세지 않는다(`$1`은 어휘상 이름이라 관계로 새던 자리다).
    pytest.param("SELECT EXTRACT(EPOCH FROM ?) FROM events", ["events"], 0, id="function-from-placeholder"),
    pytest.param("SELECT * FROM events WHERE kind IS DISTINCT FROM ?", ["events"], 0, id="distinct-from-placeholder"),
    pytest.param("SELECT * FROM events WHERE kind IS DISTINCT FROM $1", ["events"], 0, id="distinct-from-dollar"),
    # 함수 괄호 문맥은 비인용 `;`에서 끝난다. 닫히지 않은 함수 괄호가 다음 문장의 FROM을 가리지 않는다.
    pytest.param("SELECT f(x; SELECT * FROM t", ["t"], 0, id="function-group-ends-at-semicolon"),
    # 규칙은 FROM만 정한다. 함수 괄호 안의 JOIN은 지금처럼 관계를 연다.
    pytest.param("SELECT f(a JOIN b ON true) FROM t", ["b", "t"], 0, id="join-inside-function"),
    # R1 문구대로 절 단어가 아닌 이름 뒤 괄호 속 DML의 FROM도 함수 인자로 읽는다(계열 공통의 알려진 손실).
    pytest.param("COPY (DELETE FROM t RETURNING *) TO STDOUT", [], 0, id="copy-dml-reads-as-function"),
    # GRANT·REVOKE의 FROM은 세 규칙 이전에 권한 주체 자리로 걸러진다.
    pytest.param("REVOKE SELECT ON t FROM r", ["t"], 0, id="revoke-from-grantee"),
    # WITH 뒤가 이름이 아니면(SQL Server 테이블 힌트) CTE 정의가 아니다.
    pytest.param("SELECT * FROM t WITH (NOLOCK)", ["t"], 0, id="table-hint-is-not-cte"),
    # R2 이름 비교: 비인용끼리는 대소문자를 무시하고 인용 이름은 쓴 그대로다(PostgreSQL 식별자 접기).
    # 한정 이름은 CTE가 아니고, 한정자 자리의 CTE 이름도 관계를 가리지 않는다.
    pytest.param("WITH Recent AS (SELECT 1) SELECT * FROM recent", [], 0, id="cte-unquoted-case"),
    pytest.param('WITH "Recent" AS (SELECT 1) SELECT * FROM recent', ["recent"], 0, id="cte-quoted-case"),
    pytest.param('WITH "recent" AS (SELECT 1) SELECT * FROM recent', [], 0, id="cte-quoted-lowercase"),
    pytest.param('WITH Recent AS (SELECT 1) SELECT * FROM "Recent"', ["Recent"], 0, id="cte-operand-quoted"),
    pytest.param("WITH recent AS (SELECT 1) SELECT * FROM s.recent", ["s.recent"], 0, id="cte-qualified"),
    pytest.param("WITH s AS (SELECT 1) SELECT * FROM s.t", ["s.t"], 0, id="cte-name-as-qualifier"),
    # 문장 경계는 비인용 `;`다(문장 머리 표시와 같다). 인용 식별자 `";"`는 CTE 범위를 끊지 않는다.
    pytest.param('WITH recent AS (SELECT 1) SELECT ";" FROM recent', [], 0, id="cte-quoted-semicolon"),
    # 규칙이 문장 단위라, 같은 이름의 실제 테이블을 자기 본문에서 읽는 CTE는 그 사용도, 같은 문장의 INSERT 대상도
    # 내지 않는다(계열 공통의 알려진 손실).
    pytest.param(
        "WITH users AS (SELECT * FROM users WHERE active) SELECT * FROM users", [], 0, id="cte-shadows-same-table"
    ),
    pytest.param(
        "WITH orders AS (SELECT 1) INSERT INTO orders SELECT * FROM orders", [], 0, id="cte-shadows-insert-target"
    ),
]


@pytest.mark.parametrize("strict", [False, True], ids=["plain", "strict"])
@pytest.mark.parametrize(("sql", "expected", "unresolved"), _BOUNDARY_CASES)
def test_family_rules_boundary_readings(sql: str, expected: list[str], unresolved: int, strict: bool) -> None:
    """세 규칙의 해석 경계(CTE 본문 괄호, 가장 안쪽 괄호, 문장 경계, 이름 비교)를 계열 판독과 같게 고정한다."""
    result = sql_relations(sql, strict)
    assert {relation.name for relation in result.relations} == set(expected)
    assert result.unresolved == unresolved


def test_family_rule_words_ignore_case_in_strict_mode() -> None:
    """세 규칙이 알아보는 단어(WITH·RECURSIVE·AS·NOT·MATERIALIZED·IS·DISTINCT·SELECT)는 strict 모드에서도
    대소문자를 가리지 않는다.

    strict 게이트는 산문이 관계 자리를 여는 것을 막는 장치이고 세 규칙은 관계를 빼기만 한다. 그래서 대문자 FROM이 연
    자리의 판정은 인식 단어의 대소문자와 무관해야 한다(하위 질의 머리 `select`가 소문자여도 그 FROM은 관계다).
    """
    assert relations("with recent as (SELECT * FROM orders) SELECT * FROM recent", True) == ["orders"]
    assert relations("WITH recursive tree AS (SELECT * FROM nodes) SELECT * FROM tree", True) == ["nodes"]
    assert relations("WITH c as not materialized (SELECT * FROM orders) SELECT * FROM c", True) == ["orders"]
    assert relations("SELECT * FROM events WHERE a is not distinct FROM b", True) == ["events"]
    assert relations("SELECT * FROM t WHERE x = ANY (select y FROM u)", True) == ["t", "u"]


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
        "SELECT f(" + "a FROM b " * 2_000 + ")",
        "SELECT " + "f(a FROM " * 2_000,
        "WITH a AS (SELECT 1) SELECT * FROM " + "a, " * 2_000 + "b",
        "WITH " + "a AS (SELECT 1), " * 1_000 + "z AS (SELECT 1) SELECT * FROM a",
        "WITH x AS (SELECT 1)" + ", a (b" * 2_000 + " SELECT * FROM x",
        "SELECT * FROM t WHERE " + "a IS NOT DISTINCT FROM b AND " * 1_000,
    ],
    ids=[
        "into",
        "create-on",
        "grant-on",
        "update-set",
        "unclosed-parens",
        "nested-subqueries",
        "grant-object-parens",
        "function-from",
        "nested-function-from",
        "cte-operands",
        "cte-definitions",
        "cte-unclosed-columns",
        "distinct-from",
    ],
)
def test_scan_reads_each_token_a_bounded_number_of_times(sql: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """관계 스캔은 키워드마다 문장·짝 괄호를 다시 훑지 않는다(읽는 토큰 수가 토큰 수에 비례).

    세미콜론 없는 긴 문장에서 INTO·ON·UPDATE마다 문장 앞뒤를, `(` 피연산자마다 짝 괄호까지 다시 훑으면 대문자 SQL
    리터럴 하나로 schema가 입력 길이의 제곱 시간을 썼다(INTO 1만 개 6초, 4 MiB면 십여 시간). 계열 공통 규칙의
    판정(FROM을 감싼 괄호, 피연산자 문장의 CTE 이름, CTE 열 목록 건너뛰기)도 같은 이유로 색인 한 번으로 답한다.
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
    # 지금은 토큰마다 5~15번 읽고 입력을 두 배·네 배로 늘려도 같다. 이차 비용 수정 전에는 이 크기에서 토큰마다
    # 250~1,500번이었다.
    assert tokens.reads <= 64 * len(tokens)
