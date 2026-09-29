"""SQLAlchemy 이름 규칙 벡터용 매핑."""

from typing import List, Optional

from sqlalchemy import Column, ForeignKey, Integer, String, Table
from sqlalchemy.orm import Mapped, mapped_column, relationship

from catalog.base import Base, LegacyBase

product_tags = Table(
    "product_tags",
    Base.metadata,
    Column("product_id", ForeignKey("products.id"), primary_key=True),
    Column("tag_label", String, ForeignKey("tags.label"), primary_key=True),
)

audit_log = Table("audit_log", Base.metadata, Column("id", Integer, primary_key=True), schema="audit")


class AuditMixin:
    """믹스인 컬럼은 각 매핑 클래스에 복사된다."""

    created_by: Mapped[Optional[str]] = mapped_column("created_by_user")


class Product(AuditMixin, Base):
    """명시 이름·이름 인자·주석 컬럼."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column("stock_keeping_unit", String(20))
    price = Column("unit_price", Integer)
    weight: Mapped[int] = mapped_column(name="weight_grams")
    note: Mapped[Optional[str]]
    tags: Mapped[List["Tag"]] = relationship(secondary=product_tags)


class Tag(Base):
    """태그."""

    __tablename__ = "tags"

    label: Mapped[str] = mapped_column(primary_key=True)


class NamedMixin:
    """믹스인이 이름을 준다."""

    __tablename__ = "named_things"


class NamedThing(NamedMixin, Base):
    """믹스인 이름을 쓴다."""

    id: Mapped[int] = mapped_column(primary_key=True)


class Vehicle(Base):
    """단일 테이블 상속 부모."""

    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str]
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "vehicle"}


class Truck(Vehicle):
    """단일 테이블 상속 자식. 컬럼은 부모 테이블에 붙는다."""

    payload: Mapped[Optional[int]] = mapped_column(nullable=True)
    __mapper_args__ = {"polymorphic_identity": "truck"}


class Car(Vehicle):
    """조인 테이블 상속 자식."""

    __tablename__ = "cars"

    id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"), primary_key=True)
    seats: Mapped[int]
    __mapper_args__ = {"polymorphic_identity": "car"}


class Invoice(Base):
    """`__table_args__` 스키마."""

    __tablename__ = "invoices"
    __table_args__ = {"schema": "billing"}

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))


class Report(LegacyBase):
    """기반 MetaData 스키마."""

    __tablename__ = "reports"

    id = Column(Integer, primary_key=True)
    title = Column(String)


class AuditEntry(Base):
    """Core 테이블을 `__table__`로 매핑한다."""

    __table__ = audit_log
