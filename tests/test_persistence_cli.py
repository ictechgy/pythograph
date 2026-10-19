"""schema 명령 CLI 계약, 문서 조립, SQL 텍스트 수집 테스트."""

from __future__ import annotations

import ast
import json
import random
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pythograph.exchange.document import DocumentHeader, DocumentLimitError
from pythograph.exchange.persistence import build_persistence_document
from pythograph.persistence import sqltext as sqltext_module
from pythograph.persistence.command import is_migration_path
from pythograph.persistence.model import PersistenceExtraction, RelationName, RelationUse
from pythograph.routes.model import Location
from tests.conftest import FIXTURES, GENERATED_AT, REPOSITORY, relation_rows, run_cli, schema_document

#: 합성 프로젝트를 만드는 함수 타입이다.
MakeProject = Callable[[dict[str, str]], Path]

#: 고정 문서 머리다.
HEADER = DocumentHeader("0.0.0", datetime(2026, 1, 1, tzinfo=timezone.utc), "/project", None, False)


@pytest.mark.parametrize(
    "arguments",
    [
        ["schema"],
        ["schema", "--format", "yaml", "--project", "."],
        ["schema", "--project", ".", "extra"],
        ["schema", "--project", ".", "--settings", "not a module"],
        ["schema", "--project", ".", "--generated-at", "yesterday"],
        ["help", "nothing"],
    ],
)
def test_usage_errors_exit_64(arguments: list[str]) -> None:
    """사용법 오류는 64다."""
    assert run_cli(arguments)[0] == 64


def test_help_and_unreadable_project(tmp_path: Path) -> None:
    """도움말은 0, 읽을 수 없는 프로젝트는 2이며 절대 경로를 싣지 않는다."""
    assert run_cli(["help", "schema"])[1].startswith("Usage: pythograph schema")
    assert run_cli(["schema", "--help"])[1].startswith("Usage: pythograph schema")
    code, _, err = run_cli(["schema", "--project", str(tmp_path / "missing")])
    assert code == 2
    assert str(tmp_path) not in err


def test_empty_project_has_null_target(make_project: MakeProject) -> None:
    """사실이 없으면 target은 null이고 문서는 결정적이다."""
    root = make_project({"main.py": "print('hello')\n"})
    document = schema_document(root)
    assert document["target"] is None
    assert document["facts"] == []
    assert document["platform"] == "python"
    arguments = ["schema", "--project", str(root), "--generated-at", GENERATED_AT, "--format", "json"]
    assert run_cli(arguments)[1] == run_cli(arguments)[1]


def test_document_downgrades_invalid_names_and_counts_missing_symbols() -> None:
    """계약이 싣지 못하는 정적 이름은 dynamic으로 내리고, symbol 없는 사실을 센다."""
    extraction = PersistenceExtraction()
    location = Location("a.py", 1, 1)
    extraction.add_relation(RelationName(("",)), location, None)
    extraction.add_column(RelationName(("t",)), "bad name", location, "a.py#f")
    extraction.add_relation(RelationName(("a.b",)), location, "a.py#f")
    extraction.uses.append(RelationUse("\u0000", None, True, location, None))
    document = build_persistence_document(HEADER, extraction)
    facts = document["facts"]
    assert isinstance(facts, list)
    channels = {(fact["channel"], fact["dynamic"]) for fact in facts}
    assert ("a%2Eb", False) in channels
    assert ("<dynamic>", True) in channels
    assert ("t", True) in channels
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    assert any(item.startswith("invalid-relation-names: 2 ") for item in limitations)
    assert any(item.startswith("missing-relation-usrs: 1 ") for item in limitations)


def test_document_fact_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """사실 수 상한을 넘으면 부분 문서 대신 실패한다."""
    import pythograph.exchange.persistence as module

    monkeypatch.setattr(module, "MAX_FACTS", 1)
    extraction = PersistenceExtraction()
    extraction.add_relation(RelationName(("a",)), Location("a.py", 1, 1), None)
    extraction.add_relation(RelationName(("b",)), Location("a.py", 1, 1), None)
    with pytest.raises(DocumentLimitError):
        build_persistence_document(HEADER, extraction)


def test_migration_paths() -> None:
    """Django·Alembic·Flask-Migrate 마이그레이션 경로를 가린다."""
    assert is_migration_path("app/migrations/0001_initial.py")
    assert is_migration_path("alembic/versions/abc.py")
    assert is_migration_path("migrations/versions/abc.py")
    assert not is_migration_path("app/versions/abc.py")
    assert not is_migration_path("app/models.py")


def test_sql_text_sinks_and_uppercase_literals(make_project: MakeProject) -> None:
    """SQL 인자의 f-string·연결·`%`·format·상수를 읽고, 대문자 리터럴만 읽으며 docstring은 건너뛴다."""
    root = make_project(
        {
            "db.py": '''
"""SELECT * FROM docstring_table"""
import sqlite3

TABLE_SQL = "SELECT * FROM constant_table"
PREFIX = "orders"


def run(connection, name, table):
    """UPDATE docstring_two SET x = 1"""
    cursor = connection.cursor()
    cursor.execute(TABLE_SQL)
    cursor.execute(f"SELECT * FROM {PREFIX}_archive JOIN {table} ON 1 = 1")
    cursor.execute("SELECT * FROM " + table)
    cursor.execute("DELETE FROM items WHERE id = %s" % name)
    cursor.execute("INSERT INTO {} VALUES (1)".format(table))
    local = "select * from lower_table"
    cursor.execute(local)
    cursor.executemany(name, [])
    cursor.execute(connection.statement())
    query = "SELECT 1 FROM upper_literal"
    note = "select something from somewhere"
    return query, note
''',
        }
    )
    document = schema_document(root)
    rows = {row[:3] for row in relation_rows(document)}
    assert {
        ("constant_table", None, False),
        ("orders_archive", None, False),
        ("items", None, False),
        ("lower_table", None, False),
        ("upper_literal", None, False),
    } <= rows
    dynamic = {row[0] for row in rows if row[2]}
    assert any(text.startswith("SELECT * FROM orders_archive JOIN {}") for text in dynamic)
    assert "SELECT * FROM {}" in dynamic
    assert "INSERT INTO {} VALUES (1)" in dynamic
    channels = {row[0] for row in rows}
    assert "docstring_table" not in channels
    assert "docstring_two" not in channels
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    assert any(item.startswith("skipped-sql-literals: 1 ") for item in limitations)
    assert any(item.startswith("unresolved-sql-arguments: 1 ") for item in limitations)


def test_sql_text_rebound_constants_are_not_read(make_project: MakeProject) -> None:
    """두 번 이상 묶이거나(`+=` 포함) 조건부로 다시 묶은 모듈 상수는 SQL 인자·조각으로 읽지 않는다.

    마지막 무조건 대입만 SQL로 읽어 `QUERY += " JOIN audit …"`·조건부 테이블 이름을 한계 없이 첫 값으로 냈다.
    읽지 못한 인자는 `unresolved-sql-arguments:`, 읽지 못한 조각은 `{}`이고, 대문자 SQL 리터럴은 리터럴 스캔이 읽는다.
    """
    root = make_project(
        {
            "db.py": """
import os
import sqlite3

QUERY = "SELECT * FROM orders"
QUERY += " JOIN audit ON audit.id = orders.id"
TABLE = "accounts"
if os.environ.get("ARCHIVE"):
    TABLE = "archived_accounts"
STABLE = "SELECT * FROM stable_table"


def run(connection):
    cursor = connection.cursor()
    cursor.execute(QUERY)
    cursor.execute(f"select * from {TABLE}")
    cursor.execute(STABLE)
""",
        }
    )
    document = schema_document(root)
    rows = {row[:3] for row in relation_rows(document)}
    assert {("stable_table", None, False), ("orders", None, False), ("select * from {}", None, True)} <= rows
    assert ("accounts", None, False) not in rows
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    assert any(item.startswith("unresolved-sql-arguments: 1 ") for item in limitations)


def test_sql_text_expands_repeated_constants_once(make_project: MakeProject, monkeypatch: pytest.MonkeyPatch) -> None:
    """같은 상수를 여러 번 참조하는 f-string 사슬은 (식, 깊이)마다 한 번만 펼친다.

    대문자 SQL 리터럴 스캔은 모듈의 모든 f-string을 펼치므로, 메모가 없으면 수백 바이트 모듈 하나가 펼침 횟수와
    텍스트 길이를 참조 수의 거듭제곱으로 늘렸다.
    """
    source = 'Q0 = "SELECT * FROM t "\n' + "".join(
        f'Q{level} = f"' + ("{" + f"Q{level - 1}" + "}") * 4 + '"\n' for level in range(1, 5)
    )
    root = make_project({"queries.py": source})
    calls: list[int] = []
    joined_part = sqltext_module.SqlText._joined_part

    def counting(self: sqltext_module.SqlText, part: ast.expr, depth: int) -> str:
        calls.append(depth)
        return joined_part(self, part, depth)

    monkeypatch.setattr(sqltext_module.SqlText, "_joined_part", counting)
    document = schema_document(root)
    assert ("t", None, False) in {row[:3] for row in relation_rows(document)}
    # f-string 네 개를 스캔하며 Qk는 깊이 0·2·…마다 한 번씩, 모두 열 번 펼치고 한 번에 조각 4개를 본다
    # (메모 없이 펼치면 Q4 하나만 4 + 4·20 + … = 340번).
    assert len(calls) == 10 * 4
    assert not any("64 KiB" in item for item in document["limitations"])  # type: ignore[union-attr]


def test_sql_text_longer_than_the_cap_is_a_limitation(make_project: MakeProject) -> None:
    """이어 붙인 SQL 텍스트가 64 KiB를 넘으면 읽지 않고 식마다 한계로 센다. 상한 안의 텍스트는 그대로 읽는다."""
    pad = "x" * 40_000
    root = make_project(
        {
            "db.py": f"""
import sqlite3

PAD = "{pad}"
NEAR = f"SELECT * FROM near_table {{PAD}}"
HIDDEN = f"SELECT * FROM hidden_table {{PAD}}{{PAD}}"


def run(connection):
    connection.cursor().execute(f"SELECT * FROM sink_table {{PAD}}{{PAD}}")
    connection.cursor().execute("SELECT * FROM plain_table " + PAD + PAD)
""",
        }
    )
    document = schema_document(root)
    channels = {row[0] for row in relation_rows(document)}
    assert "near_table" in channels
    assert not channels & {"hidden_table", "sink_table", "plain_table"}
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    assert "skipped-sql-literals: 3 string expressions longer than 64 KiB were not read as SQL" in limitations
    assert any(item.startswith("unresolved-sql-arguments: 2 ") for item in limitations)


def test_unresolved_operands_flatten_the_sql_text_once(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    """미해석 피연산자가 여럿이어도 dynamic 사실 channel에 실을 한 줄 원문은 SQL 텍스트마다 한 번만 만든다.

    피연산자마다 텍스트 전체를 다시 split·join하면 미해석 n개짜리 리터럴 하나가 n × 길이를 써서, `JOIN ?` 1만 개
    리터럴 하나로 schema가 2.7초를 썼다(대부분 이 반복).
    """
    sql = "SELECT * FROM t" + " JOIN ?" * 50
    root = make_project({"queries.py": f'QUERY = "{sql}"\n'})
    calls: list[str] = []
    truncate = sqltext_module.truncate

    def counting(text: str) -> str:
        calls.append(text)
        return truncate(text)

    monkeypatch.setattr(sqltext_module, "truncate", counting)
    document = schema_document(root)
    assert calls == [sql]
    assert {("t", None, False), (truncate(sql), None, True)} <= {row[:3] for row in relation_rows(document)}
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    assert "dynamic-relation-names: 50 SQL relation operands are not literal names" in limitations


#: 수정 전 DB-API 파라미터 표기 정규식이다. `replace_parameters`가 이 정규식의 `re.sub`와 같은 결과를 내는지 본다.
_PARAMETER_PATTERN = re.compile(r"%\([^)]*\)[sdifr]|%[sdifr]")


@pytest.mark.parametrize(
    "text",
    [
        "", "%", "%s", "%%s", "%x", "a % b", "%(name)s", "%(name)d %(other)r %i %f", "%(name)x", "%(name", "%()s",
        "%(\n)s", "%(a%s)x", "%(a)s%(b)s", "%(a(b)s", "%(%(%(x)s", "%(x)", "%(x))s", "%(x)s)s", "%(x)s%",
        "SELECT * FROM t WHERE a = %s AND b = %(b)s",
        "SELECT * FROM t WHERE name LIKE '%(%'\nAND id IN (SELECT id FROM u WHERE v = %(v)s)",
    ],
)  # fmt: skip
def test_parameter_markers_follow_the_dbapi_pattern(text: str) -> None:
    """DB-API 파라미터 표기 치환은 수정 전 정규식의 `re.sub`와 결과가 같다(줄을 넘는 `%(…)`, `%%s` 포함)."""
    for replacement in ("?", "{}"):
        assert sqltext_module.replace_parameters(text, replacement) == _PARAMETER_PATTERN.sub(replacement, text)


def test_parameter_markers_follow_the_dbapi_pattern_on_random_text() -> None:
    """고정 시드의 무작위 짧은 텍스트에서도 치환 결과가 수정 전 정규식과 같다."""
    generator = random.Random(20261019)
    for _ in range(20_000):
        text = "".join(generator.choice("%()sdx\n") for _ in range(generator.randrange(16)))
        assert sqltext_module.replace_parameters(text, "?") == _PARAMETER_PATTERN.sub("?", text)


def test_long_parameter_marker_runs_stay_linear(tmp_path: Path) -> None:
    """닫히지 않은 `%(`가 많이 든 대문자 SQL 리터럴도 선형 시간에 읽는다.

    수정 전 정규식은 `%(`마다 다음 `)`까지(없으면 끝까지) 다시 훑어 `%(` 4만 개에 1.5초가 들었고 길이의 제곱으로
    늘었다(4 MiB 파일이면 한 시간 넘게). 별도 프로세스의 제한 시간은 멈춤을 실패로 바꾸는 안전장치일 뿐 성능 측정이
    아니다(지금은 몇 초 안에 끝난다).
    """
    root = tmp_path / "project"
    root.mkdir()
    literal = "SELECT * FROM marker_table WHERE a = " + "%(" * 400_000
    (root / "queries.py").write_text(f"QUERY = {literal!r}\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pythograph", "schema", "--project", str(root), "--generated-at", GENERATED_AT],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPOSITORY,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert ("marker_table", None, False) in {row[:3] for row in relation_rows(json.loads(result.stdout))}


def test_unsupported_and_non_relational_packages(make_project: MakeProject) -> None:
    """해석하지 않는 ORM과 비관계형 저장소 import를 센다."""
    root = make_project({"a.py": "import peewee\nimport redis\n", "b.py": "from pymongo import MongoClient\n"})
    limitations = schema_document(root)["limitations"]
    assert isinstance(limitations, list)
    assert "unsupported-db-packages: 1 files import peewee, which is not interpreted" in limitations
    assert any(item.startswith("non-relational-stores: 1 files import pymongo") for item in limitations)


def test_fixture_documents_are_valid_json_contract() -> None:
    """fixture 문서는 계약 모양이다(relation-use만, 정렬, persistence target)."""
    document = schema_document(FIXTURES / "persistence" / "django-shop")
    assert document["format"] == "bridge-facts"
    assert document["version"] == 1
    assert document["target"] == "persistence"
    facts = document["facts"]
    assert isinstance(facts, list)
    assert all(fact["kind"] == "relation-use" for fact in facts)
    assert json.dumps(facts, sort_keys=True) == json.dumps(sorted(facts, key=_sort_key), sort_keys=True)


def _sort_key(fact: dict[str, object]) -> tuple[object, ...]:
    """문서 정렬 키를 재현한다.

    Args:
        fact: 사실.

    Returns:
        정렬 키.
    """
    location = fact["location"]
    assert isinstance(location, dict)
    return (
        str(fact["channel"]),
        str(fact.get("method", "")),
        bool(fact["dynamic"]),
        str(location["path"]),
        int(location["line"]),
        int(location["column"]),
        json.dumps(fact, sort_keys=True, ensure_ascii=False),
    )
