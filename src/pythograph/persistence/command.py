"""`schema` 실행: 모듈 고르기 → Django·SQLAlchemy 목록 → 모듈별 선언·사용·SQL 사실 → 프로젝트 공백.

테스트 소스(`--include-tests`가 없을 때)와 마이그레이션(Django `migrations/`, Alembic·Flask-Migrate
`versions/`)은 읽지 않는다. 마이그레이션은 과거 스키마를 기술하므로 지금 카탈로그와 조인하면 거짓 오류가 된다.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import PurePosixPath

from pythograph.persistence.django.catalog import DjangoCatalog
from pythograph.persistence.django.declarations import DjangoDeclarations
from pythograph.persistence.django.queries import DjangoQueries
from pythograph.persistence.django.settings import read_project_settings
from pythograph.persistence.model import PersistenceExtraction
from pythograph.persistence.scope import ModuleScopes
from pythograph.persistence.sqlalchemy.catalog import SqlAlchemyCatalog
from pythograph.persistence.sqlalchemy.queries import SqlAlchemyQueries
from pythograph.persistence.sqltext import SqlText
from pythograph.routes.versions import declared_majors, normalize_name
from pythograph.source.evaluate import Evaluator
from pythograph.source.project import Project, ReadFailure, SourceModule, is_test_path
from pythograph.source.symbols import SymbolTable

#: `execute` 계열 인자를 SQL로 읽게 하는 DB 패키지(최상위 이름)다.
_DB_PACKAGES = frozenset(
    {
        "sqlite3", "psycopg", "psycopg2", "pymysql", "MySQLdb", "mysql", "cx_Oracle", "oracledb", "asyncpg",
        "aiosqlite", "aiomysql", "pyodbc", "django", "sqlalchemy", "flask_sqlalchemy",
    }
)  # fmt: skip

#: SQL 텍스트를 받는 DB-API 메서드다.
_EXECUTE_METHODS = frozenset({"execute", "executemany", "executescript"})

#: 해석하지 않는 ORM·질의 패키지다.
_UNSUPPORTED_PACKAGES = frozenset(
    {"peewee", "pony", "tortoise", "sqlmodel", "ormar", "piccolo", "databases", "records", "dataset", "sqlobject"}
)

#: 관계형이 아닌 저장소 패키지다.
_NON_RELATIONAL = frozenset({"pymongo", "motor", "mongoengine", "redis", "beanie", "odmantic"})


@dataclass(frozen=True)
class SchemaOptions:
    """schema 명령 옵션.

    Attributes:
        include_tests: 테스트 소스도 읽을지.
        settings_module: Django 설정 모듈(없으면 진입 파일에서 찾는다).
    """

    include_tests: bool
    settings_module: str | None


def extract_persistence(project: Project, options: SchemaOptions) -> PersistenceExtraction:
    """프로젝트의 persistence 사실을 추출한다.

    Args:
        project: 분석 대상 프로젝트.
        options: 명령 옵션.

    Returns:
        추출 결과.
    """
    symbols = SymbolTable(project)
    extraction = PersistenceExtraction()
    modules = _modules(symbols, options, extraction)
    majors = declared_majors(project)
    settings = read_project_settings(symbols, options.settings_module)
    django = DjangoCatalog(symbols, settings, modules)
    sqlalchemy = SqlAlchemyCatalog(symbols, modules, _fsa_majors(majors))
    for module in modules:
        _scan_module(symbols, module, django, sqlalchemy, extraction)
    _catalog_gaps(django, sqlalchemy, settings.backends_known, extraction)
    _version_gaps(majors, django, sqlalchemy, extraction)
    _project_gaps(project, extraction)
    return extraction


def _modules(symbols: SymbolTable, options: SchemaOptions, extraction: PersistenceExtraction) -> list[SourceModule]:
    """읽을 모듈을 고른다. 테스트·마이그레이션은 빼고 센다.

    Args:
        symbols: 이름 해석기.
        options: 옵션.
        extraction: 공백을 기록할 결과.

    Returns:
        파싱한 모듈 목록.
    """
    modules: list[SourceModule] = []
    for path in symbols.project.python_files():
        if is_migration_path(path):
            extraction.add_gap(
                "skipped-migration-sources:", "{count} migration files were not scanned; they describe past schemas"
            )
            continue
        if not options.include_tests and is_test_path(path):
            extraction.add_gap("test-sources-excluded:", "{count} test files were not scanned (pass --include-tests)")
            continue
        index = symbols.index(path)
        if index is not None:
            modules.append(index.module)
    return modules


def is_migration_path(path: str) -> bool:
    """마이그레이션 파일 경로인지 판정한다.

    Django는 앱의 `migrations/` 패키지, Alembic·Flask-Migrate는 `alembic/versions/`·`migrations/versions/`다.

    Args:
        path: 프로젝트 상대 경로.

    Returns:
        마이그레이션이면 True.
    """
    parts = PurePosixPath(path).parts[:-1]
    return "migrations" in parts or any(
        parts[index] == "versions" and index > 0 and parts[index - 1] in ("alembic", "migrations")
        for index in range(len(parts))
    )


def _scan_module(
    symbols: SymbolTable,
    module: SourceModule,
    django: DjangoCatalog,
    sqlalchemy: SqlAlchemyCatalog,
    extraction: PersistenceExtraction,
) -> None:
    """모듈 하나의 선언·ORM 사용·SQL 텍스트 사실을 낸다.

    Args:
        symbols: 이름 해석기.
        module: 모듈.
        django: Django 모델 목록.
        sqlalchemy: SQLAlchemy 매핑 목록.
        extraction: 결과.
    """
    index = symbols.index(module.path)
    if index is None:
        return
    scopes = ModuleScopes(index)
    sql = SqlText(extraction, scopes, Evaluator(symbols))
    DjangoDeclarations(django, scopes, extraction).run()
    DjangoQueries(django, symbols, scopes, extraction, sql).run()
    SqlAlchemyQueries(sqlalchemy, scopes, extraction, sql).run()
    packages = _imported_packages(module.tree)
    if packages & _DB_PACKAGES:
        for node in ast.walk(module.tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _EXECUTE_METHODS
                and node.args
            ):
                sql.sink(node.args[0], explicit=False)
    sql.fallback(module.tree)
    for package in sorted(packages & _UNSUPPORTED_PACKAGES):
        extraction.add_gap("unsupported-db-packages:", f"{{count}} files import {package}, which is not interpreted")
    for package in sorted(packages & _NON_RELATIONAL):
        extraction.add_gap("non-relational-stores:", f"{{count}} files import {package}, a non-relational store")


def _imported_packages(tree: ast.Module) -> set[str]:
    """모듈이 import하는 최상위 패키지 이름을 모은다.

    Args:
        tree: 모듈 구문 트리.

    Returns:
        패키지 이름 집합.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def _fsa_majors(majors: dict[str, set[int | None]]) -> tuple[int, ...]:
    """Flask-SQLAlchemy 자동 이름 후보 메이저를 정한다.

    Args:
        majors: 선언한 메이저.

    Returns:
        후보 메이저(한 메이저로 제한되지 않으면 2와 3).
    """
    found = majors.get(normalize_name("flask-sqlalchemy"))
    if found == {2}:
        return (2,)
    if found == {3}:
        return (3,)
    return (2, 3)


def _catalog_gaps(
    django: DjangoCatalog, sqlalchemy: SqlAlchemyCatalog, backends_known: bool, extraction: PersistenceExtraction
) -> None:
    """모델 목록 수준의 공백을 더한다.

    Args:
        django: Django 모델 목록.
        sqlalchemy: SQLAlchemy 매핑 목록.
        backends_known: Django `DATABASES` ENGINE을 모두 확인했는지.
        extraction: 결과.
    """
    project_models = [model for model in django.models.values() if model.path is not None and not model.abstract]
    if django.unresolved_labels:
        extraction.add_gap(
            "django-app-label-unresolved:",
            f"{django.unresolved_labels} Django models have no app label resolvable from INSTALLED_APPS",
        )
    if project_models and not backends_known:
        extraction.add_gap(
            "django-database-backend-unknown:",
            "the DATABASES ENGINE could not be read, so Django names are emitted only when every built-in backend "
            "agrees on them",
        )
    if any(not model.complete for model in project_models):
        extraction.add_gap(
            "unmodeled-orm-mappings:",
            f"{sum(not model.complete for model in project_models)} Django models inherit from unknown base classes",
        )
    if sqlalchemy.naming_disagreements:
        extraction.add_gap(
            "flask-sqlalchemy-naming-unverified:",
            f"{sqlalchemy.naming_disagreements} Flask-SQLAlchemy table names differ between major versions 2 and 3",
        )
    for package in sorted(sqlalchemy.unsupported):
        extraction.add_gap("unsupported-db-packages:", f"mapped classes built on {package} are not interpreted")


def _version_gaps(
    majors: dict[str, set[int | None]],
    django: DjangoCatalog,
    sqlalchemy: SqlAlchemyCatalog,
    extraction: PersistenceExtraction,
) -> None:
    """이름 규칙을 확인한 메이저로 선언하지 않은 ORM을 알린다.

    Args:
        majors: 선언한 메이저.
        django: Django 모델 목록.
        sqlalchemy: SQLAlchemy 매핑 목록.
        extraction: 결과.
    """
    checks = []
    if any(model.path is not None for model in django.models.values()):
        checks.append(("django", 5, "Django"))
    if sqlalchemy.models or sqlalchemy.tables:
        checks.append(("sqlalchemy", 2, "SQLAlchemy"))
    for package, expected, label in checks:
        if majors.get(normalize_name(package)) != {expected}:
            extraction.add_gap(
                "persistence-framework-version-unknown:",
                f"the project does not declare {label} limited to major version {expected}, the version whose naming "
                "rules pythograph verified",
            )


def _project_gaps(project: Project, extraction: PersistenceExtraction) -> None:
    """순회 상한·링크·읽기 실패 같은 프로젝트 공백을 더한다.

    Args:
        project: 프로젝트.
        extraction: 결과.
    """
    if project.scan_capped:
        extraction.add_gap("scan-truncated:", "the project scan stopped at its entry or depth limit")
    if project.unencodable_names:
        extraction.add_gap(
            "unreadable-sources:", f"{project.unencodable_names} Python files have names that are not valid UTF-8"
        )
    if project.skipped_links:
        extraction.add_gap("skipped-symlinks:", f"{project.skipped_links} symbolic links were not followed")
    for module in project.parsed_modules().values():
        if isinstance(module, ReadFailure):
            extraction.add_gap("unreadable-sources:", "{count} Python files could not be analyzed: " + module.reason)
