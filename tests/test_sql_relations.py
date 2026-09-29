"""SQL 관계 추출기 테스트.

기대값은 tsograph `sql-relations.test.ts`(cartograph `SqlRelationsTests`·dartograph `sql_relations_test`와 같은
공유 벡터)를 그대로 옮겼다. 가족 생산자가 같은 SQL을 같게 읽어야 isthmus 조인 결과가 생산자 언어와 무관해진다.
"""

from __future__ import annotations

from pythograph.persistence.sql import escape_name, escape_qualified, lex_sql, looks_like_sql, sql_relations


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
