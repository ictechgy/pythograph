"""schema 명령 CLI 계약, 문서 조립, SQL 텍스트 수집 테스트."""

from __future__ import annotations

import ast
import json
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
from tests.conftest import FIXTURES, GENERATED_AT, relation_rows, run_cli, schema_document

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
