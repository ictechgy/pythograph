"""Flask-SQLAlchemy 3 자동 이름 규칙 벡터용 모델."""

from shopapp.extensions import db

order_items = db.Table(
    "order_items",
    db.Column("order_id", db.ForeignKey("customer_order.id"), primary_key=True),
    db.Column("item_id", db.ForeignKey("catalog_item.id"), primary_key=True),
)


class CustomerOrder(db.Model):
    """자동 이름 `customer_order`."""

    id = db.Column(db.Integer, primary_key=True)
    placed_at = db.Column("placed_on", db.DateTime)
    items = db.relationship("CatalogItem", secondary=order_items, backref="orders")


class CatalogItem(db.Model):
    """자동 이름 `catalog_item`."""

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(80))


class HTTPRequestLog(db.Model):
    """대문자 연속: `http_request_log`."""

    id = db.Column(db.Integer, primary_key=True)
    path = db.Column(db.String(200))


class OAuth2Token(db.Model):
    """숫자 경계: 3.x는 `o_auth2_token`."""

    id = db.Column(db.Integer, primary_key=True)


class Legacy(db.Model):
    """명시 이름."""

    __tablename__ = "legacy_records"

    id = db.Column(db.Integer, primary_key=True)


class Employee(db.Model):
    """상속 부모."""

    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(20))
    __mapper_args__ = {"polymorphic_on": type, "polymorphic_identity": "employee"}


class Manager(Employee):
    """자기 기본 키가 없어 단일 테이블 상속(부모 테이블)이다."""

    level = db.Column(db.Integer)
    __mapper_args__ = {"polymorphic_identity": "manager"}


class Engineer(Employee):
    """자기 기본 키가 있어 조인 테이블 상속(`engineer`)이다."""

    id = db.Column(db.Integer, db.ForeignKey("employee.id"), primary_key=True)
    language = db.Column(db.String(20))
    __mapper_args__ = {"polymorphic_identity": "engineer"}


class TimestampMixin:
    """믹스인 컬럼."""

    created = db.Column(db.DateTime)


class Coupon(TimestampMixin, db.Model):
    """믹스인 컬럼을 받는 자동 이름 `coupon`."""

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(12), unique=True)


class Base(db.Model):
    """추상 모델은 테이블이 없다."""

    __abstract__ = True
    id = db.Column(db.Integer, primary_key=True)


class GiftCard(Base):
    """추상 부모의 컬럼을 받는다."""

    balance = db.Column(db.Integer)
