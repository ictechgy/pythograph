"""명명 벡터 테스트: pythograph의 이름 계산이 실제 ORM이 만든 이름(`fixtures/persistence-naming/vectors.json`)과
100% 같은지 오프라인으로 확인한다.

벡터는 `experiments/persistence/run_naming.py`가 스크래치 가상 환경에서 Django(백엔드 4개)·SQLAlchemy·
Flask-SQLAlchemy로 fixture 모델을 import해 기록한다. Oracle은 식별자를 대문자로 바꾸지만 isthmus는 소문자로
접어 조인하므로 대소문자를 접어 비교한다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pythograph.persistence.django.catalog import DjangoCatalog
from pythograph.persistence.django.settings import read_project_settings
from pythograph.persistence.names import MYSQL, ORACLE, POSTGRESQL, SQLITE, DjangoBackend
from pythograph.persistence.sqlalchemy.catalog import SqlAlchemyCatalog
from pythograph.source.project import Project, SourceModule
from pythograph.source.symbols import SymbolTable
from tests.conftest import FIXTURES

#: 명명 fixture 루트다.
NAMING = FIXTURES / "persistence-naming"

#: 기록한 벡터다.
VECTORS = json.loads((NAMING / "vectors.json").read_text(encoding="utf-8"))

#: 벡터의 백엔드 이름 → pythograph 백엔드다.
BACKENDS: dict[str, DjangoBackend] = {"sqlite3": SQLITE, "postgresql": POSTGRESQL, "mysql": MYSQL, "oracle": ORACLE}


def _modules(root: Path) -> tuple[SymbolTable, list[SourceModule]]:
    """프로젝트의 모든 모듈을 파싱한다.

    Args:
        root: 프로젝트 루트.

    Returns:
        (이름 해석기, 모듈 목록).
    """
    symbols = SymbolTable(Project.open(root.resolve()))
    modules = [index.module for path in symbols.project.python_files() if (index := symbols.index(path))]
    return symbols, modules


def _django_names(catalog: DjangoCatalog) -> dict[str, dict[str, object]]:
    """pythograph가 계산한 Django 이름을 벡터와 같은 모양으로 만든다.

    Args:
        catalog: 백엔드를 고정한 모델 목록.

    Returns:
        라벨 → {table, columns}.
    """
    names: dict[str, dict[str, object]] = {}
    for model in catalog.models.values():
        if model.abstract or model.label is None:
            continue
        relation = catalog.relation(model.key)
        columns = {
            field.name: catalog.column(field.column)
            for field in model.fields.values()
            if field.owner == model.key and field.kind != "m2m" and not model.proxy
        }
        names[f"{model.label[0]}.{model.model_name}"] = {
            "table": list(relation.segments) if relation else None,
            "columns": columns,
        }
        for field in model.fields.values():
            if field.kind == "m2m" and field.owner == model.key and field.m2m and field.m2m.through is None:
                through = catalog.m2m_relation(field.m2m)
                target = catalog.models[field.target] if field.target else None
                source_name = f"from_{model.model_name}" if target is model else model.model_name
                target_name = f"to_{target.model_name}" if target is model else target.model_name if target else "?"
                names[f"{model.label[0]}.{model.model_name}_{field.name.lower()}"] = {
                    "table": list(through.segments) if through else None,
                    "columns": {
                        "id": "id",
                        source_name: catalog.column(field.m2m.source_column),
                        target_name: catalog.column(field.m2m.target_column),
                    },
                }
    return names


def _fold(value: object) -> object:
    """비교용으로 문자열을 소문자로 접는다(중첩 구조 포함).

    Args:
        value: 값.

    Returns:
        접은 값.
    """
    if isinstance(value, str):
        return value.lower()
    if isinstance(value, list):
        return [_fold(item) for item in value]
    if isinstance(value, dict):
        return {str(_fold(key)): _fold(item) for key, item in value.items()}
    return value


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_django_names_match_real_django(backend: str) -> None:
    """Django 기본·명시 이름, 절단, M2M, 상속 이름이 백엔드마다 실제 Django와 같다."""
    symbols, modules = _modules(NAMING / "django-library")
    catalog = DjangoCatalog(symbols, read_project_settings(symbols, None), modules)
    catalog.backends = (BACKENDS[backend],)
    ours = _django_names(catalog)
    expected = VECTORS["django"][backend]["models"]
    compared = 0
    for label, vector in sorted(expected.items()):
        assert label in ours, label
        mine = ours[label]
        assert _fold(mine["table"]) == _fold(vector["table"]), label
        assert _fold(mine["columns"]) == _fold(vector["columns"]), label
        compared += 1
    assert compared == len(expected) >= 30


def test_django_unknown_backend_keeps_only_agreeing_names() -> None:
    """백엔드를 모르면 모든 백엔드가 같은 이름만 정적이고 나머지는 None(dynamic)이다."""
    symbols, modules = _modules(NAMING / "django-library")
    catalog = DjangoCatalog(symbols, read_project_settings(symbols, None), modules)
    catalog.backends = tuple(BACKENDS.values())
    ours = _django_names(catalog)
    for label in VECTORS["django"]["sqlite3"]["models"]:
        tables = {json.dumps(_fold(VECTORS["django"][backend]["models"][label]["table"])) for backend in BACKENDS}
        mine = ours[label]["table"]
        if len(tables) == 1:
            assert json.dumps(_fold(mine)) in tables, label
        else:
            assert mine is None, label


def _module_name(path: str) -> str:
    """fixture 상대 경로를 모듈 이름으로 바꾼다.

    Args:
        path: 경로.

    Returns:
        점 경로.
    """
    return path.removesuffix(".py").replace("/", ".")


@pytest.mark.parametrize("fixture", sorted(VECTORS["sqlalchemy"]))
def test_sqlalchemy_names_match_real_mappers(fixture: str) -> None:
    """SQLAlchemy·Flask-SQLAlchemy 테이블·컬럼 이름이 실제 매퍼와 같다."""
    symbols, modules = _modules(NAMING / fixture)
    catalog = SqlAlchemyCatalog(symbols, modules, (3,))
    expected = VECTORS["sqlalchemy"][fixture]
    ours: dict[str, dict[str, object]] = {}
    tables: dict[str, set[str]] = {}
    for model in catalog.models.values():
        if model.abstract:
            continue
        relation = catalog.relation(model.key)
        columns: dict[str, object] = {}
        for key, column in model.columns.items():
            owner = catalog.relation(column.owner)
            columns[key] = [*owner.segments, column.column] if owner else None
            if owner is not None and column.column is not None:
                tables.setdefault(owner.channel, set()).add(column.column)
        ours[f"{_module_name(model.path)}.{model.name}"] = {
            "table": list(relation.segments) if relation else None,
            "columns": columns,
        }
    for table in catalog.tables.values():
        assert table.name is not None
        tables.setdefault(table.name.channel, set()).update(table.columns)
    assert ours == expected["classes"]
    assert {name: sorted(columns) for name, columns in tables.items()} == expected["tables"]


def test_vectors_cover_all_backends_and_orms() -> None:
    """벡터가 네 백엔드와 두 SQLAlchemy fixture를 담는다(재기록 누락 방지)."""
    assert set(VECTORS["django"]) == set(BACKENDS)
    assert set(VECTORS["sqlalchemy"]) == {"fsa-shop", "sa-catalog"}
    assert VECTORS["oracle"] == {"django": "5.2.17", "flask-sqlalchemy": "3.1.1", "sqlalchemy": "2.0.54"}
