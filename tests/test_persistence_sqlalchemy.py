"""SQLAlchemy·Flask-SQLAlchemy persistence 규칙 단위 테스트(합성 프로젝트)."""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import pytest

from pythograph.persistence import names
from pythograph.persistence.model import PersistenceExtraction
from pythograph.persistence.scope import ModuleScopes
from pythograph.persistence.sqlalchemy.catalog import SqlAlchemyCatalog
from pythograph.persistence.sqlalchemy.queries import SqlAlchemyQueries
from pythograph.persistence.sqltext import SqlText
from pythograph.source.evaluate import Evaluator
from pythograph.source.project import Project
from pythograph.source.symbols import SymbolTable
from tests.conftest import CountingValues, class_lattice, relation_rows, schema_document

#: 합성 프로젝트를 만드는 함수 타입이다.
MakeProject = Callable[[dict[str, str]], Path]

#: 기본 매핑이다.
MODELS = """
from sqlalchemy import Column, ForeignKey, ForeignKeyConstraint, Integer, MetaData, String, Table, table, column
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column, registry, relationship


class Base(DeclarativeBase):
    metadata = MetaData(schema="core")


legacy = registry().generate_base()

links = Table("links", Base.metadata, Column("a_id", ForeignKey("core.accounts.id")), Column("b", Integer))
lightweight = table("light", column("x"))


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (ForeignKeyConstraint(["owner"], ["core.people.id"]), {"schema": "auth"})

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(50))
    owner = Column(Integer)
    friends = relationship("Account", secondary=links)
    groups = relationship(lambda: Group, secondary="auth.account_groups")
    posts: Mapped[list["Post"]] = relationship(back_populates="author")


class Group(Base):
    __tablename__ = "groups"
    id = Column(Integer, primary_key=True)


class Post(Base):
    __tablename__ = "posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    author_id = mapped_column(ForeignKey("auth.accounts.id"))
    author: Mapped["Account"] = relationship(back_populates="posts")


class Computed(Base):
    @declared_attr
    def __tablename__(cls):
        return cls.__name__.lower()

    id = Column(Integer, primary_key=True)


class Old(legacy):
    __tablename__ = "old"
    id = Column(Integer, primary_key=True)
"""


def _project(make_project: MakeProject, uses: str, models: str = MODELS, extra: dict[str, str] | None = None) -> Path:
    """SQLAlchemy 합성 프로젝트를 만든다.

    Args:
        make_project: 프로젝트 생성 fixture.
        uses: `app/uses.py` 내용.
        models: `app/models.py` 내용.
        extra: 추가 파일.

    Returns:
        프로젝트 루트.
    """
    files = {"app/__init__.py": "", "app/models.py": models, "app/uses.py": uses, **(extra or {})}
    return make_project(files)


def _rows(document: dict[str, object], prefix: str = "app/uses.py") -> set[tuple[object, ...]]:
    """경로 접두사의 사실을 (channel, method, dynamic) 집합으로 돌려준다.

    Args:
        document: 문서.
        prefix: usr 접두사.

    Returns:
        요약 집합.
    """
    return {row[:3] for row in relation_rows(document) if str(row[3] or "").startswith(prefix)}


def test_declarations_schemas_and_foreign_keys(make_project: MakeProject) -> None:
    """스키마(`__table_args__`·MetaData)·외래 키 문자열·연결 테이블·계산 이름을 선언 사실로 낸다."""
    document = schema_document(_project(make_project, "x = 1\n"))
    rows = {row[:3] for row in relation_rows(document)}
    assert {
        ("auth.accounts", None, False),
        ("auth.accounts", "email", False),
        ("core.people", "id", False),
        ("core.links", None, False),
        ("core.links", "a_id", False),
        ("core.accounts", "id", False),
        ("auth.account_groups", None, False),
        ("core.groups", "id", False),
        ("auth.accounts", "id", False),
        ("Computed", None, True),
        ("old", "id", False),
        ("light", "x", False),
    } <= rows


def test_rebound_table_name_constants_are_dynamic(make_project: MakeProject) -> None:
    """조건부·누적으로 다시 묶은 모듈 상수로 만든 `__tablename__`·Core `Table` 이름은 정적 이름으로 내지 않는다.

    마지막 무조건 대입을 상수로 써서 `PREFIX = "shop_"; if …: PREFIX = "tenant_"`의 테이블을 `shop_orders`로 한계
    없이 냈다. 한 번만 대입한 상수로 만든 이름은 그대로 정적이다.
    """
    models = """
import os

from sqlalchemy import Column, Integer, Table
from sqlalchemy.orm import DeclarativeBase

PREFIX = "shop_"
if os.environ.get("TENANT"):
    PREFIX = "tenant_"
LOG = "log"
LOG += "_v2"
STABLE = "stable_"


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = PREFIX + "orders"
    id = Column(Integer, primary_key=True)


class Item(Base):
    __tablename__ = STABLE + "items"
    id = Column(Integer, primary_key=True)


events = Table(LOG, Base.metadata, Column("id", Integer))
"""
    document = schema_document(_project(make_project, "x = 1\n", models))
    rows = {row[:3] for row in relation_rows(document)}
    assert ("stable_items", "id", False) in rows
    assert not {row[0] for row in rows if not row[2]} & {"shop_orders", "tenant_orders", "log", "log_v2"}
    assert ("Order", None, True) in rows


#: 다시 묶이는 이름이 가리키는 Core 테이블 객체를 쓰는 매핑이다(조건부·반복문·두 번째 무조건 대입, 대조군 `stable`).
REBOUND_TABLES = """
import os

from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table
from sqlalchemy.orm import DeclarativeBase, relationship

LEGACY = os.environ.get("LEGACY")
metadata = MetaData()


class Base(DeclarativeBase):
    pass


users = Table("users", metadata, Column("id", Integer, primary_key=True))
if LEGACY:
    users = Table("legacy_users", metadata, Column("id", Integer, primary_key=True))
stable = Table("stable", metadata, Column("id", Integer, primary_key=True))
accounts = Table("accounts", metadata, Column("id", Integer, primary_key=True))
for _ in range(1):
    accounts = Table("old_accounts", metadata, Column("id", Integer, primary_key=True))
links = Table("links", Base.metadata, Column("a", ForeignKey("a.id")), Column("b", ForeignKey("b.id")))
links = Table("links_v2", Base.metadata, Column("a", ForeignKey("a.id")), Column("b", ForeignKey("b.id")))


class Account(Base):
    __table__ = accounts


class A(Base):
    __tablename__ = "a"
    id = Column(Integer, primary_key=True)
    bs = relationship("B", secondary=links)


class B(Base):
    __tablename__ = "b"
    id = Column(Integer, primary_key=True)
"""

#: 위 객체를 다른 모듈에서 쓰는 질의다.
REBOUND_TABLE_USES = """
from sqlalchemy import select

from app.models import A, Account, stable, users


def list_users(session):
    return session.execute(select(users))


def list_stable(session):
    return session.execute(select(stable))


def list_accounts(session):
    return session.execute(select(Account))


def list_links(session):
    return session.execute(select(A).join(A.bs))
"""


def test_rebound_core_table_objects_are_not_resolved(make_project: MakeProject) -> None:
    """다시 묶이는 이름이 가리키는 Core `Table` 객체(`select(users)`, `__table__ = accounts`, `secondary=links`)는
    정적 테이블로 풀지 않고 dynamic 사실과 `dynamic-relation-names:` 한계로 낸다.

    묶음은 이름을 값으로 평가할 때만 보고 테이블 객체로 풀 때는 보지 않아, `users = Table("users", …)` 뒤
    `if …: users = Table("legacy_users", …)`의 `select(users)`를 한계 없이 정적 `users`로 냈다. 한 번만 묶인
    `stable`은 그대로 정적이다.
    """
    document = schema_document(_project(make_project, REBOUND_TABLE_USES, REBOUND_TABLES))
    rows = _rows(document)
    rebound = {"users", "legacy_users", "accounts", "old_accounts", "links", "links_v2"}
    assert ("stable", None, False) in rows
    assert ("b", None, False) in rows
    assert not {row[0] for row in rows if not row[2]} & rebound
    assert {row[0] for row in rows if row[2]} >= {"users", "Account"}
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    reasons = [line for line in limitations if line.startswith("dynamic-relation-names:")]
    for phrase in ("statement entities could not be resolved", "computed table names", "secondary tables"):
        assert any(phrase in line for line in reasons), phrase


def test_unparseable_string_annotations_are_not_columns(make_project: MakeProject) -> None:
    """파싱할 수 없거나 너무 깊은 문자열 주석(NUL·5만 항)은 컬럼으로 보지 않고 schema는 성공한다.

    문자열 주석 파싱이 SyntaxError만 잡아 3.10은 NUL 주석의 ValueError로, 모든 버전은 5만 항 주석의
    RecursionError로 schema 전체가 내부 오류(종료 2)였다.
    """
    wide = " + ".join(["str"] * 50_000)
    models = (
        "from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column\n\n\n"
        "class Base(DeclarativeBase):\n    pass\n\n\n"
        "class Item(Base):\n"
        '    __tablename__ = "items"\n'
        "    id: Mapped[int] = mapped_column(primary_key=True)\n"
        '    note: "Mapped[str]\\x00"\n'
        f'    wide: "Mapped[{wide}]"\n'
        '    title: "Mapped[str]"\n'
    )
    rows = {row[:3] for row in relation_rows(schema_document(_project(make_project, "x = 1\n", models=models)))}
    assert ("items", "title", False) in rows
    assert not {("items", "note", False), ("items", "wide", False)} & rows


def test_statement_entities_columns_and_joins(make_project: MakeProject) -> None:
    """엔터티 자리·컬럼 속성·관계 조인·filter_by·생성자·Core 테이블 사용을 읽는다."""
    document = schema_document(
        _project(
            make_project,
            """
from sqlalchemy import delete, exists, insert, select, text, update
from sqlalchemy.orm import aliased

from app.models import Account, Group, Post, links


def run(session, model, account_id):
    session.execute(select(Account.email, Post).join(Account.posts).where(Post.id == account_id))
    session.query(Account).join(Group).filter_by(id=1).all()
    session.query(Post).filter_by(author_id=account_id)
    session.get(Account, account_id)
    alias = aliased(Account)
    select(alias).where(alias.email == "x")
    session.execute(insert(Group), [{"id": 1}])
    session.execute(update(Post).values(author_id=1))
    session.execute(delete(links))
    session.execute(links.select())
    links.c.b
    session.add(Account(email="e", groups=[]))
    select(model)
    exists(model)
    session.execute(text("SELECT * FROM core.accounts"))
    session.connection().exec_driver_sql("DELETE FROM core.links")
    stmt = select(Post)
    stmt2 = stmt.filter_by(id=3)
    session.execute(stmt2)
""",
        )
    )
    rows = _rows(document)
    assert {
        ("auth.accounts", None, False),
        ("auth.accounts", "email", False),
        ("core.posts", None, False),
        ("core.posts", "id", False),
        ("core.groups", None, False),
        ("core.groups", "id", False),
        ("core.posts", "author_id", False),
        ("core.links", None, False),
        ("core.links", "b", False),
        ("auth.account_groups", None, False),
        ("model", None, True),
        ("core.accounts", None, False),
    } <= rows


def test_flask_sqlalchemy_autonaming_and_queries(make_project: MakeProject) -> None:
    """Flask-SQLAlchemy 자동 이름·단일/조인 테이블 상속·Model.query·db.get_or_404를 읽는다."""
    models = """
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Person(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(10))


class Staff(Person):
    badge = db.Column(db.String(10))


class Admin(Person):
    id = db.Column(db.Integer, db.ForeignKey("person.id"), primary_key=True)
    level = db.Column(db.Integer)


class APIKey(db.Model):
    id = db.Column(db.Integer, primary_key=True)
"""
    document = schema_document(
        _project(
            make_project,
            """
from app.models import APIKey, Admin, Staff, db


def run(key):
    Staff.query.filter_by(badge="b").all()
    Admin.query.get(1)
    db.get_or_404(APIKey, key)
    db.session.execute(db.select(Admin).where(Admin.level > 1))
""",
            models=models,
            extra={"requirements.txt": "Flask-SQLAlchemy==3.1.1\nSQLAlchemy==2.0.54\n"},
        )
    )
    rows = _rows(document)
    assert {
        ("person", None, False),
        ("person", "badge", False),
        ("admin", None, False),
        ("admin", "level", False),
        ("api_key", None, False),
    } <= rows
    assert ("staff", None, False) not in rows


def test_flask_sqlalchemy_unknown_version_keeps_agreeing_names(make_project: MakeProject) -> None:
    """버전을 모르면 2.x·3.x 규칙이 같은 이름만 정적이다."""
    models = """
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class OrderLine(db.Model):
    id = db.Column(db.Integer, primary_key=True)


class User2FA(db.Model):
    id = db.Column(db.Integer, primary_key=True)
"""
    document = schema_document(_project(make_project, "x = 1\n", models=models))
    rows = {row[:3] for row in relation_rows(document)}
    assert ("order_line", None, False) in rows
    assert ("User2FA", None, True) in rows
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    assert any(item.startswith("flask-sqlalchemy-naming-unverified: 1 ") for item in limitations)


#: Flask-SQLAlchemy 2.x 규칙을 적용하지 않은 긴 클래스 이름 하나의 한계 문장이다.
LONG_CLASS_NAMES = (
    "flask-sqlalchemy-naming-unverified: 1 Flask-SQLAlchemy class names are longer than 1,000 characters, so their "
    "major version 2 automatic table names were not computed; set __tablename__ on these classes"
)


def _recording_v2(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Flask-SQLAlchemy 2.x 자동 이름 함수가 받은 이름 길이를 호출 순서대로 기록하게 바꾼다.

    1,000자를 넘는 이름은 실제 정규식을 부르지 않고 빈 이름을 돌려준다. 규칙을 건너뛰지 않는 코드에서도 테스트가 정규식
    역추적(이름 길이의 제곱)으로 오래 멈추지 않고 바로 실패하게 하려는 것이다.

    Args:
        monkeypatch: pytest monkeypatch.

    Returns:
        받은 이름 길이 목록(호출할 때마다 늘어난다).
    """
    lengths: list[int] = []
    original = names.FSA_NAMING[2]

    def recording(name: str) -> str:
        """길이를 기록하고 1,000자 이하만 실제 2.x 규칙으로 바꾼다."""
        lengths.append(len(name))
        return original(name) if len(name) <= 1_000 else ""

    monkeypatch.setitem(names.FSA_NAMING, 2, recording)
    return lengths


def _flask_models(*class_names: str) -> str:
    """이름 없는 Flask-SQLAlchemy 매핑 클래스만 담은 모델 모듈을 만든다(자동 이름을 받는다).

    Args:
        class_names: 클래스 이름.

    Returns:
        `app/models.py` 내용.
    """
    lines = ["from flask_sqlalchemy import SQLAlchemy", "", "db = SQLAlchemy()", ""]
    for name in class_names:
        lines += ["", f"class {name}(db.Model):", "    id = db.Column(db.Integer, primary_key=True)", ""]
    return "\n".join(lines)


@pytest.mark.parametrize("requirements", [None, "flask-sqlalchemy==2.5.1\n"], ids=["unknown-version", "major-2"])
def test_flask_sqlalchemy_2_rule_skips_class_names_over_1000_characters(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch, requirements: str | None
) -> None:
    """2.x가 후보면 1,000자를 넘는 클래스 이름에는 2.x 자동 이름 규칙을 적용하지 않고 dynamic과 한계로 남긴다.

    2.x 정규식 `([A-Z]+)(?=[a-z0-9])`는 소문자·숫자가 뒤따르지 않는 대문자 연속에서 이름 길이의 제곱 시간이 든다
    (`"A"`×32,000이 5초, 4 MiB 이름 하나면 약 하루). 정규식은 Flask-SQLAlchemy와 같아야 해서 바꾸지 않고, 1,000자
    이하 이름은 지금처럼 이름을 짓는다. 두 이름은 2.x·3.x 규칙이 같은 꼴이라 길이만 결과를 가른다. 2.x 이름을 모르면
    두 규칙이 같은지도 모르므로 버전을 모를 때도 3.x 이름을 정적으로 내지 않는다.
    """
    at_limit, over_limit = "A" + "b" * 999, "A" + "b" * 1_000
    lengths = _recording_v2(monkeypatch)
    extra = {"requirements.txt": requirements} if requirements else None
    models = _flask_models(at_limit, over_limit)
    document = schema_document(_project(make_project, "x = 1\n", models=models, extra=extra))
    rows = {row[:3] for row in relation_rows(document)}
    assert lengths == [1_000]
    assert {(at_limit.lower(), None, False), (at_limit.lower(), "id", False), (over_limit, None, True)} <= rows
    assert not {row for row in rows if row[0] == over_limit.lower()}
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    assert LONG_CLASS_NAMES in limitations
    assert "dynamic-relation-names: 1 SQLAlchemy mapped classes have computed table names" in limitations
    assert not any("differ between major versions" in item for item in limitations)


def test_flask_sqlalchemy_3_rule_names_class_names_of_any_length(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3.x만 후보면 2.x 규칙을 부르지 않으므로 긴 클래스 이름도 3.x 규칙으로 이름을 짓고 한계를 남기지 않는다.

    3.x 정규식은 한 글자 대안과 고정 폭 둘러보기라 이름 길이에 선형이다(`"A"`×32,000이 1 ms 안팎).
    """
    long_name = "A" * 40_000
    lengths = _recording_v2(monkeypatch)
    extra = {"requirements.txt": "flask-sqlalchemy==3.1.1\n"}
    document = schema_document(_project(make_project, "x = 1\n", models=_flask_models(long_name), extra=extra))
    rows = {row[:3] for row in relation_rows(document)}
    assert lengths == []
    assert {(long_name.lower(), None, False), (long_name.lower(), "id", False)} <= rows
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    assert not any(item.startswith("flask-sqlalchemy-naming-unverified:") for item in limitations)


def test_flask_sqlalchemy_model_class_options(make_project: MakeProject) -> None:
    """disable_autonaming·model_class(declarative_base 결과)·알 수 없는 설정을 구분한다."""
    models = """
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase, declarative_base

plain = SQLAlchemy(disable_autonaming=True)
made = SQLAlchemy(model_class=declarative_base())
unknown = SQLAlchemy(disable_autonaming=flag())


class Base(DeclarativeBase):
    pass


typed = SQLAlchemy(model_class=Base)


class A(plain.Model):
    __tablename__ = "a_table"
    id = plain.Column(plain.Integer, primary_key=True)


class B(made.Model):
    id = made.Column(made.Integer, primary_key=True)


class C(unknown.Model):
    id = unknown.Column(unknown.Integer, primary_key=True)


class DItem(typed.Model):
    id = typed.Column(typed.Integer, primary_key=True)
"""
    document = schema_document(
        _project(make_project, "x = 1\n", models=models, extra={"requirements.txt": "flask-sqlalchemy==3.1.1\n"})
    )
    rows = {row[:3] for row in relation_rows(document)}
    assert {("a_table", None, False), ("B", None, True), ("C", None, True), ("d_item", None, False)} <= rows


def test_mixins_abstract_and_table_mapping(make_project: MakeProject) -> None:
    """믹스인 이름·스키마, 추상 클래스, `__table__` 매핑, sqlmodel 미지원 한계를 처리한다."""
    models = """
from sqlalchemy import Column, Integer, Table
from sqlalchemy.orm import DeclarativeBase
from sqlmodel import SQLModel


class Base(DeclarativeBase):
    pass


class Named:
    __tablename__ = "mixed"
    __table_args__ = {"schema": "mx"}
    created = Column("created_at", Integer)


class Deep(Named):
    pass


class Thing(Deep, Base):
    id = Column(Integer, primary_key=True)


class Abstract(Base):
    __abstract__ = True
    __tablename__ = "shared"
    ident = Column(Integer, primary_key=True)


class Concrete(Abstract):
    extra = Column(Integer)


mapped = Table("mapped_table", Base.metadata, Column("z", Integer, primary_key=True))


class Mapped(Base):
    __table__ = mapped


class Hero(SQLModel, table=True):
    id: int
"""
    document = schema_document(_project(make_project, "x = 1\n", models=models))
    rows = {row[:3] for row in relation_rows(document)}
    assert {
        ("mx.mixed", None, False),
        ("mx.mixed", "created_at", False),
        ("shared", "ident", False),
        ("shared", "extra", False),
        ("mapped_table", "z", False),
    } <= rows
    limitations = document["limitations"]
    assert isinstance(limitations, list)
    assert any(item.startswith("unsupported-db-packages:") for item in limitations)


def test_statement_values_and_explicit_none_tablename(make_project: MakeProject) -> None:
    """`update(M).values(k=)` 키는 컬럼 사실이고, `__tablename__ = None`은 부모 테이블(단일 테이블 상속)이다."""
    models = """
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Employee(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column("name", db.String(20))


class Contractor(Employee):
    __tablename__ = None
    rate = db.Column(db.Integer)
"""
    document = schema_document(
        _project(
            make_project,
            """
from sqlalchemy import insert, update

from app.models import Contractor, Employee


def run(session):
    session.execute(update(Employee).where(Employee.id == 1).values(full_name="x"))
    session.execute(insert(Employee).values(id=2))
    session.query(Contractor).filter_by(rate=3)
""",
            models=models,
            extra={"requirements.txt": "flask-sqlalchemy==3.1.1\n"},
        )
    )
    rows = _rows(document)
    assert {("employee", "name", False), ("employee", "id", False), ("employee", "rate", False)} <= rows
    assert not any(row[0] == "contractor" for row in rows)


def _mapping_module(number: int) -> str:
    """매핑 클래스·추상 매핑·Core 테이블을 하나씩 둔 모듈 소스를 만든다(뒤 모듈의 클래스를 상속하는 클래스 포함).

    Args:
        number: 모듈 번호.

    Returns:
        모듈 소스.
    """
    lines = ["from sqlalchemy import Column, Integer, Table", "from app.base import Base"]
    if number < 3:
        lines += [f"from app.models{number + 1} import Item{number + 1}", "", ""]
        lines += [f"class Special{number}(Item{number + 1}):", f'    __tablename__ = "special{number}"']
    lines += ["", "", f"class Item{number}(Base):", f'    __tablename__ = "item{number}"']
    lines += ["    id = Column(Integer, primary_key=True)", "", "", f"class Abstract{number}(Base):"]
    lines += ["    __abstract__ = True", "", "", f'links{number} = Table("links{number}", Base.metadata)']
    return "\n".join(lines) + "\n"


def test_module_scopes_walk_the_tree_once_in_ast_walk_order(make_project: MakeProject) -> None:
    """판독기들이 함께 쓰는 노드 목록은 `ast.walk`와 같은 노드·순서이고(출력 바이트 보존), 도우미마다 한 번만 만든다."""
    root = make_project(
        {"app/models.py": MODELS, "app/queries.py": 'def rows(session):\n    return session.execute("SELECT 1")\n'}
    )
    symbols = SymbolTable(Project.open(root))
    for path in ("app/models.py", "app/queries.py"):
        index = symbols.index(path)
        assert index is not None
        scopes = ModuleScopes(index)
        nodes = scopes.nodes
        assert isinstance(nodes, tuple)
        assert len(nodes) == sum(1 for _ in ast.walk(index.module.tree))
        assert all(left is right for left, right in zip(nodes, ast.walk(index.module.tree), strict=True))
        assert scopes.nodes is nodes
        # 다른 도우미는 모듈마다 새로 만들어 실행 동안 노드 목록이 쌓이지 않는다.
        assert ModuleScopes(index).nodes is not nodes


def test_queries_read_only_their_module_mappings(make_project: MakeProject) -> None:
    """매핑 선언 사실은 모듈마다 그 모듈의 매핑 클래스·Core 테이블만 본다. 경로별 색인은 카탈로그 순서 그대로다.

    모듈마다 카탈로그 전체 매핑과 테이블을 돌며 경로로 거르면 모듈 수 × (매핑 + 테이블) 수가 든다. 다른 모듈의
    클래스를 상속하면 부모가 먼저 해석되어 카탈로그 순서가 파일 순서와 달라지므로 그 순서도 함께 본다.
    """
    files = {
        "app/__init__.py": "",
        "app/base.py": "from sqlalchemy.orm import DeclarativeBase\n\n\nclass Base(DeclarativeBase):\n    pass\n",
    }
    for number in range(4):
        files |= {f"app/models{number}.py": _mapping_module(number), f"app/util{number}.py": "X = 1\n"}
    root = make_project(files)
    symbols = SymbolTable(Project.open(root.resolve()))
    modules = [index.module for path in symbols.project.python_files() if (index := symbols.index(path))]
    catalog = SqlAlchemyCatalog(symbols, modules, (3,))
    filtered_models: dict[str, list[str]] = {}
    for model in catalog.models.values():
        filtered_models.setdefault(model.path, []).append(model.key)
    filtered_tables: dict[str, list[int]] = {}
    for table in catalog.tables.values():
        filtered_tables.setdefault(table.path, []).append(id(table))
    assert {path: [model.key for model in models] for path, models in catalog.models_by_path.items()} == filtered_models
    assert {path: [id(table) for table in tables] for path, tables in catalog.tables_by_path.items()} == filtered_tables
    assert [model.name for model in catalog.models_by_path["app/models1.py"]] == ["Item1", "Special1", "Abstract1"]
    models, tables = CountingValues(catalog.models), CountingValues(catalog.tables)
    catalog.models, catalog.tables = models, tables
    extraction = PersistenceExtraction()
    for module in modules:
        index = symbols.index(module.path)
        assert index is not None
        scopes = ModuleScopes(index)
        SqlAlchemyQueries(catalog, scopes, extraction, SqlText(extraction, scopes, Evaluator(symbols))).run()
    assert models.visits <= len(models)
    assert tables.visits <= len(tables)
    channels = {use.channel for use in extraction.uses if use.column is None and not use.dynamic}
    assert {f"item{number}" for number in range(4)} | {f"links{number}" for number in range(4)} <= channels
    assert {f"special{number}" for number in range(3)} <= channels


def _catalog(root: Path) -> SqlAlchemyCatalog:
    """합성 프로젝트의 모든 모듈로 매핑 목록을 만든다.

    Args:
        root: 프로젝트 루트.

    Returns:
        매핑 목록.
    """
    symbols = SymbolTable(Project.open(root.resolve()))
    modules = [index.module for path in symbols.project.python_files() if (index := symbols.index(path))]
    return SqlAlchemyCatalog(symbols, modules, (3,))


def test_non_mapping_class_lattice_resolves_each_class_once(
    make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    """매핑이 아닌 클래스의 다이아몬드 상속 격자도 클래스마다 한 번만 푼다(아니라는 결과를 기억한다).

    매핑 클래스만 기억해, 모델과 무관한 깊이 16 격자(클래스 34개)에서 해석이 52만 번 되풀이됐다. 깊이 20인 1 KB 모듈
    하나로 schema가 18초, 깊이 32면 수십 시간이었다.
    """
    source = class_lattice(12)
    root = make_project({"app/__init__.py": "", "app/shapes.py": source})
    calls = 0
    original = SqlAlchemyCatalog._resolve

    def counting(self: SqlAlchemyCatalog, key: str, depth: int) -> object:
        nonlocal calls
        calls += 1
        return original(self, key, depth)

    monkeypatch.setattr(SqlAlchemyCatalog, "_resolve", counting)
    catalog = _catalog(root)
    assert catalog.models == {}
    classes = [node for node in ast.parse(source).body if isinstance(node, ast.ClassDef)]
    assert calls <= len(classes) + sum(len(node.bases) for node in classes)


def test_depth_cut_resolutions_are_retried(make_project: MakeProject) -> None:
    """상속 깊이 상한에 걸려 매핑이 아니게 나온 결과는 그 깊이에서만, 매핑 목록이 그대로인 동안만 다시 쓴다.

    거꾸로 적은 41단 사슬은 앞 클래스부터 풀면 깊이 상한에서 끊긴다. `P1`·`P2`는 처음 풀 때 사슬 끝이 끊겨 매핑이
    아니다(`P2`는 `P1`이 남긴 끊긴 결과를 다시 쓴다). 사슬 아래쪽이 매핑으로 풀린 뒤 `Z`가 `P2`를 다시 찾으면 `P2`와
    사슬 위쪽도 매핑이 된다. 다시 찾는 클래스가 없는 `P1`은 수정 전처럼 매핑이 아니다. 끊긴 결과를 어느 깊이에서나 같은
    결과로 기억하면 `P2`·`Z`와 사슬 위쪽 여덟 단계를 잃는다.
    """
    lines = ["from sqlalchemy import Column, Integer", "from sqlalchemy.orm import DeclarativeBase", ""]
    for name, parent in [("P1", "M40"), ("P2", "M40")] + [
        (f"M{n}", f"M{n - 1}" if n else "Base") for n in range(40, -1, -1)
    ]:
        lines += [f"class {name}({parent}):", f"    __tablename__ = '{name.lower()}'"]
        lines += ["    id = Column(Integer, primary_key=True)", ""]
    lines += ["class Base(DeclarativeBase):", "    pass", ""]
    lines += ["class Z(P2):", "    __tablename__ = 'z'", "    id = Column(Integer, primary_key=True)", ""]
    root = make_project({"app/__init__.py": "", "app/models.py": "\n".join(lines)})
    catalog = _catalog(root)
    assert sorted(model.name for model in catalog.models.values()) == sorted([f"M{n}" for n in range(41)] + ["P2", "Z"])
