"""persistence 합성 fixture 전체 출력 회귀(golden)와 fixture별 핵심 사실.

golden은 `project`만 자리표시자로 바꾼 전체 문서다. 의도한 변경이면
`PYTHOGRAPH_UPDATE_GOLDEN=1 uv run pytest tests/test_persistence_fixtures.py`로 갱신한다.
fixture DDL(`experiments/persistence/e2e/*.sql`)은 실제 ORM이 만든 스키마이며, 정적 relation-use가 모두 그 스키마에
있는지(isthmus의 `relation-use-without-decl`·`column-use-without-decl`이 없을 조건)를 오프라인으로 확인한다.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import FIXTURES, REPOSITORY, relation_rows, schema_document

#: (fixture 경로, golden 이름) 목록이다.
TARGETS = (
    (FIXTURES / "persistence" / "django-shop", "persistence-django-shop.json"),
    (FIXTURES / "persistence" / "flask-blog", "persistence-flask-blog.json"),
    (FIXTURES / "persistence-naming" / "django-library", "persistence-django-library.json"),
    (FIXTURES / "persistence-naming" / "sa-catalog", "persistence-sa-catalog.json"),
    (FIXTURES / "persistence-naming" / "fsa-shop", "persistence-fsa-shop.json"),
)

#: golden 문서 디렉터리다.
GOLDEN = Path(__file__).resolve().parent / "golden"

#: 종단 검증 DDL 디렉터리다.
DDL = REPOSITORY / "experiments" / "persistence" / "e2e"


def _golden_text(document: dict[str, Any]) -> str:
    """`project`를 자리표시자로 바꾼 비교용 문자열을 만든다.

    Args:
        document: 문서.

    Returns:
        키 정렬 JSON.
    """
    return json.dumps({**document, "project": "<project>"}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


@pytest.mark.parametrize(("fixture", "name"), TARGETS, ids=[name for _, name in TARGETS])
def test_golden_document(fixture: Path, name: str) -> None:
    """fixture 문서가 golden과 같다."""
    text = _golden_text(schema_document(fixture))
    path = GOLDEN / name
    if os.environ.get("PYTHOGRAPH_UPDATE_GOLDEN") == "1":
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8")


def _ddl_columns(name: str) -> dict[str, set[str]]:
    """기록한 SQLite DDL을 메모리 DB에 적용해 테이블 → 컬럼을 읽는다.

    Args:
        name: fixture 이름.

    Returns:
        테이블 → 컬럼 집합.
    """
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript((DDL / f"{name}.sql").read_text(encoding="utf-8"))
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {table: {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')} for table in tables}
    finally:
        connection.close()


@pytest.mark.parametrize("name", ["django-shop", "flask-blog"])
def test_static_uses_exist_in_real_schema(name: str) -> None:
    """정적 relation-use의 관계·컬럼이 실제 ORM이 만든 스키마에 모두 있다(정밀도 100%)."""
    document = schema_document(FIXTURES / "persistence" / name)
    tables = _ddl_columns(name)
    used = set()
    for channel, column, dynamic, _ in relation_rows(document):
        if dynamic:
            continue
        assert channel in tables, channel
        assert column is None or column in tables[channel], (channel, column)
        used.add(channel)
    project_tables = {table for table in tables if not table.startswith(("auth_", "django_"))} | {"auth_user"} & set(
        tables
    )
    assert project_tables <= used


def test_django_shop_key_facts() -> None:
    """Django fixture: 조회식 조인·역관계·관계 매니저·원시 SQL·dynamic·제외 정책."""
    document = schema_document(FIXTURES / "persistence" / "django-shop")
    rows = relation_rows(document)
    services = "store/services.py#"
    expected = {
        ("store_category", "name", False, services + "product_list"),
        ("store_product_tags", "tag_id", False, services + "search"),
        ("reviews_review", "rating", False, services + "product_detail"),
        ("store_order", "customer_id", False, services + "best_customers"),
        ("store_orderitem", None, False, services + "order_report"),
        ("store_product", None, False, services + "legacy_lookup"),
        ("store_orderitem", "order_id", False, services + "OrderService.place"),
        ("store_orderitem", "order_id", False, "store/models.py#Order.quantity"),
        ("store_category", None, False, "store/models.py#Product.in_category"),
        ("model.objects", None, True, services + "everything"),
        ("SELECT COUNT(*) FROM {}", None, True, services + "count_rows"),
    }
    assert expected <= rows
    channels = {row[0] for row in rows}
    assert "store_legacy_inventory" not in channels
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    assert any(item.startswith("skipped-migration-sources:") for item in limitations)
    assert any(item.startswith("test-sources-excluded:") for item in limitations)
    with_tests = relation_rows(schema_document(FIXTURES / "persistence" / "django-shop", "--include-tests"))
    assert ("store_tag", "name", False, "tests/test_store.py#test_tags") in with_tests


def test_flask_blog_key_facts() -> None:
    """Flask fixture: 자동 이름·Model.query·filter_by·select·조인·연결 테이블·text()."""
    rows = relation_rows(schema_document(FIXTURES / "persistence" / "flask-blog"))
    views = "blog/views.py#"
    expected = {
        ("user", "email", False, views + "user_by_email"),
        ("blog_post", "author_id", False, views + "user_posts"),
        ("blog_post", None, False, views + "post_detail"),
        ("post_comments", "body", False, views + "add_comment"),
        ("post_tags", None, False, views + "tagged"),
        ("tag", "name", False, views + "tagged"),
        ("user", "name", False, views + "authors"),
        ("post_tags", None, False, views + "stats"),
        ("blog_post", None, False, "blog/models.py#BlogPost"),
        ("blog_post", "id", False, None),
    }
    assert expected <= rows
    assert "old_drafts" not in {row[0] for row in rows}
