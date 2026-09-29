"""SQLAlchemy·Flask-SQLAlchemy persistence 규칙 단위 테스트(합성 프로젝트)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tests.conftest import relation_rows, schema_document

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
